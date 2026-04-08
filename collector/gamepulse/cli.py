"""
gamepulse-collector — main entry point.

Usage:
  gamepulse-collector [--config PATH] [--debug] [--once]
                      [--es-endpoint URL] [--es-api-key KEY]
                      [--interval-ms MS] [--no-game-detection]
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from gamepulse import __version__
from gamepulse import config as config_mod
from gamepulse.collectors.audio import AudioCollector
from gamepulse.collectors.cpu import CpuCollector
from gamepulse.collectors.frame.mangohud import MangoHudCollector
from gamepulse.collectors.gpu.detect import make_gpu_collector
from gamepulse.collectors.memory import MemoryCollector
from gamepulse.collectors.network import NetworkCollector
from gamepulse.collectors.power import PowerCollector
from gamepulse.collectors.storage import StorageCollector
from gamepulse.detector.game import GameDetector
from gamepulse.enricher.host import HostEnricher
from gamepulse.session import GameInfo, Session
from gamepulse.shipper.elasticsearch import ElasticsearchShipper

log = logging.getLogger("gamepulse")

_SHUTDOWN = False


def _session_json_path() -> Path:
    """Canonical path for the session handoff file read by the eBPF daemon."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(xdg) if xdg else Path("/tmp")
    return base / "gamepulse" / "session.json"


def _write_session_json(session_id: str, game_pid: int, game_name: str,
                        steam_app_id: int | None) -> None:
    path = _session_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"session_id": session_id, "game_pid": game_pid, "game_name": game_name}
    if steam_app_id is not None:
        doc["steam_app_id"] = steam_app_id
    path.write_text(json.dumps(doc))
    log.debug("wrote session.json: %s", path)


def _remove_session_json() -> None:
    path = _session_json_path()
    try:
        path.unlink()
        log.debug("removed session.json")
    except FileNotFoundError:
        pass


def _handle_sigterm(sig: int, frame: Any) -> None:
    global _SHUTDOWN
    _SHUTDOWN = True


def _timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _merge_docs(*docs: dict[str, Any]) -> dict[str, Any]:
    """Recursively deep-merge multiple dicts.

    Later dicts win on scalar conflicts; nested dicts are merged rather than
    replaced. This is needed because all sources now contribute to the same
    top-level 'gamepulse' key and naive ** unpacking would clobber earlier values.
    """
    result: dict[str, Any] = {}
    for doc in docs:
        for k, v in doc.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = _merge_docs(result[k], v)
            else:
                result[k] = v
    return result


def _gp(doc: dict[str, Any]) -> dict[str, Any]:
    """Return the gamepulse sub-dict from a document (empty dict if absent)."""
    return doc.get("gamepulse", {})


def _print_debug(tick: int, docs: list[tuple[str, dict[str, Any]]]) -> None:
    parts = []
    for _index, doc in docs:
        gp = _gp(doc)
        if cpu := gp.get("cpu"):
            parts.append(f"CPU:{cpu.get('total_utilisation_pct', '?')}%")
        if g := gp.get("gpu"):
            parts.append(
                f"GPU:{g.get('utilisation_pct', '?')}%/{g.get('temperature_c', '?')}°C"
            )
        if mem := gp.get("memory"):
            parts.append(f"MEM:{mem.get('system_used_mb', '?')}MB")
        if fps := gp.get("fps"):
            parts.append(f"FPS:{fps.get('current', '?')} (1%:{fps.get('low_1pct', '?')})")
        if s := gp.get("storage"):
            parts.append(f"IO:R{s.get('read_mbps', '?')}/W{s.get('write_mbps', '?')}MB/s")
        if n := gp.get("network"):
            parts.append(
                f"NET:{n.get('rx_mbps', '?')}/{n.get('tx_mbps', '?')}MB/s"
            )
        if p := gp.get("power"):
            if "battery_pct" in p:
                parts.append(f"BAT:{p['battery_pct']}%")
            if "tdp_current_w" in p:
                parts.append(f"TDP:{p['tdp_current_w']}W")

    game = _gp(next((d for _, d in docs), {})).get("game", {})
    game_str = f" [{game.get('name', '')}]" if game else ""
    print(f"[{tick:05d}] {' '.join(parts)}{game_str}")


def run(cfg: config_mod.Config, debug: bool, once: bool) -> None:
    global _SHUTDOWN

    session = Session(id=str(uuid.uuid4()))

    log.info("Collecting host environment snapshot…")
    enricher = HostEnricher()

    cpu = CpuCollector()
    mem = MemoryCollector()
    storage = StorageCollector()
    gpu = make_gpu_collector() if cfg.collection.gpu else None
    network = NetworkCollector() if cfg.collection.network else None
    power = PowerCollector()
    audio = AudioCollector()
    frame = MangoHudCollector() if cfg.collection.frame_timing else None
    detector = GameDetector() if cfg.collection.game_detection else None

    shipper: ElasticsearchShipper | None = None
    if not debug:
        es = cfg.elasticsearch
        shipper = ElasticsearchShipper(
            endpoint=es.endpoint,
            api_key=es.api_key,
            username=es.username,
            password=es.password,
            batch_size=es.batch_size,
            flush_interval_secs=es.flush_interval_secs,
        )
        session_doc = _merge_docs(
            {"@timestamp": _timestamp()},
            session.base_doc(__version__, cfg.privacy.opt_in_public),
            enricher.snapshot,
        )
        shipper.queue("metrics-gamepulse.session-default", session_doc)
        shipper.flush()
        log.info("Session %s started", session.id)

    interval = cfg.collection.interval_ms / 1000.0
    tick = 0
    prev_game_pid: int | None = None

    session_start = time.monotonic()
    fps_samples: list[float] = []
    frametime_samples: list[float] = []
    stutter_total: int = 0
    peak_gpu_temp: float | None = None
    peak_cpu_temp: float | None = None
    peak_gpu_power: float | None = None
    bottleneck_counts: dict[str, int] = {"gpu": 0, "cpu": 0, "balanced": 0}

    try:
        while not _SHUTDOWN:
            tick_start = time.monotonic()
            tick += 1
            ts = _timestamp()

            if detector:
                game = detector.detect()
                if game and (prev_game_pid is None or game.pid != prev_game_pid):
                    session.game = GameInfo(
                        name=game.name,
                        steam_app_id=game.steam_app_id,
                        pid=game.pid,
                        graphics_api=game.graphics_api,
                        uses_proton=game.uses_proton,
                        proton_version=getattr(game, "proton_version", None),
                        dxvk_version=getattr(game, "dxvk_version", None),
                        vkd3d_version=getattr(game, "vkd3d_version", None),
                    )
                    mem.set_game_pid(game.pid)
                    prev_game_pid = game.pid
                    log.info("Detected game: %s (pid %d)", game.name, game.pid)
                    _write_session_json(
                        session.id, game.pid, game.name, game.steam_app_id
                    )

                    if shipper:
                        compat: dict[str, Any] = {
                            k: v for k, v in {
                                "proton_version": getattr(game, "proton_version", None),
                                "dxvk_version": getattr(game, "dxvk_version", None),
                                "vkd3d_proton_version": getattr(game, "vkd3d_version", None),
                            }.items() if v is not None
                        }
                        updated = _merge_docs(
                            {"@timestamp": ts},
                            session.base_doc(__version__, cfg.privacy.opt_in_public),
                            enricher.snapshot,
                            {"gamepulse": {"compatibility": compat}} if compat else {},
                        )
                        shipper.queue("metrics-gamepulse.session-default", updated)

                elif game is None and prev_game_pid is not None:
                    log.info("Game exited")
                    session.game = None
                    mem.set_game_pid(None)
                    prev_game_pid = None
                    _remove_session_json()

            # Build the base fields included in every per-tick document
            base = _merge_docs(
                {"@timestamp": ts},
                session.base_doc(__version__, cfg.privacy.opt_in_public),
            )

            docs: list[tuple[str, dict[str, Any]]] = []

            if cfg.collection.cpu:
                if r := cpu.collect():
                    docs.append((cpu.data_stream, _merge_docs(base, r)))

            if cfg.collection.memory:
                if r := mem.collect():
                    docs.append((mem.data_stream, _merge_docs(base, r)))

            if cfg.collection.storage:
                if r := storage.collect():
                    docs.append((storage.data_stream, _merge_docs(base, r)))

            if gpu and cfg.collection.gpu:
                if r := gpu.collect():
                    docs.append((gpu.data_stream, _merge_docs(base, r)))

            if frame and cfg.collection.frame_timing:
                if r := frame.collect():
                    docs.append((frame.data_stream, _merge_docs(base, r)))

            if network and cfg.collection.network:
                if r := network.collect():
                    docs.append((network.data_stream, _merge_docs(base, r)))

            if power:
                if r := power.collect():
                    docs.append((power.data_stream, _merge_docs(base, r)))

            if r := audio.collect():
                docs.append((audio.data_stream, _merge_docs(base, r)))

            # Update session accumulators and inject per-tick bottleneck
            tick_gpu_util: float | None = None
            tick_cpu_util: float | None = None
            for ds, d in docs:
                gp = _gp(d)
                if gpu_d := gp.get("gpu"):
                    tick_gpu_util = gpu_d.get("utilisation_pct")
                    t = gpu_d.get("temperature_c")
                    if t is not None:
                        peak_gpu_temp = t if peak_gpu_temp is None else max(peak_gpu_temp, t)
                    p = gpu_d.get("power_w")
                    if p is not None:
                        peak_gpu_power = p if peak_gpu_power is None else max(peak_gpu_power, p)
                if cpu_d := gp.get("cpu"):
                    tick_cpu_util = cpu_d.get("total_utilisation_pct")
                    t = cpu_d.get("temperature_c")
                    if t is not None:
                        peak_cpu_temp = t if peak_cpu_temp is None else max(peak_cpu_temp, t)
                if fps_d := gp.get("fps"):
                    fps_samples.append(fps_d["avg_1s"])
                    stutter_total += fps_d.get("stutter_count", 0)
                    if ft := fps_d.get("frametime_ms"):
                        frametime_samples.append(ft)

            if tick_gpu_util is not None and tick_cpu_util is not None:
                tick_bn = (
                    "gpu" if tick_gpu_util > 90
                    else "cpu" if tick_cpu_util > 90
                    else "balanced"
                )
                bottleneck_counts[tick_bn] += 1
                # Inject per-tick bottleneck into the frame document
                if frame:
                    for i, (ds, d) in enumerate(docs):
                        if ds == frame.data_stream:
                            docs[i] = (ds, _merge_docs(
                                d, {"gamepulse": {"performance": {"bottleneck": tick_bn}}}
                            ))
                            break

            if debug:
                _print_debug(tick, docs)
            elif shipper:
                for index, doc in docs:
                    shipper.queue(index, doc)
                shipper.flush_if_due()

            if once:
                break

            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.0, interval - elapsed))

    except KeyboardInterrupt:
        pass
    finally:
        if shipper and tick > 0:
            duration_s = round(time.monotonic() - session_start)
            summary: dict[str, Any] = {
                "ended": True,
                "duration_s": duration_s,
                "stutter_count": stutter_total,
            }
            if fps_samples:
                _sorted = sorted(fps_samples)
                summary["avg_fps"] = round(sum(fps_samples) / len(fps_samples), 1)
                summary["low_1pct_fps"] = int(
                    _sorted[max(0, int(len(_sorted) * 0.01) - 1)]
                )
                summary["total_frames"] = round(sum(fps_samples) * interval)
            if frametime_samples:
                _ft_sorted = sorted(frametime_samples)
                summary["p99_frametime_ms"] = round(
                    _ft_sorted[max(0, int(len(_ft_sorted) * 0.99) - 1)], 2
                )
            if peak_gpu_temp is not None:
                summary["peak_gpu_temp_c"] = peak_gpu_temp
            if peak_cpu_temp is not None:
                summary["peak_cpu_temp_c"] = peak_cpu_temp
            if peak_gpu_power is not None:
                summary["peak_gpu_power_w"] = peak_gpu_power
            if any(bottleneck_counts.values()):
                summary["bottleneck_dominant"] = max(
                    bottleneck_counts, key=lambda k: bottleneck_counts[k]
                )
            close_doc = _merge_docs(
                {"@timestamp": _timestamp()},
                session.base_doc(__version__, cfg.privacy.opt_in_public),
                enricher.snapshot,
                {"gamepulse": {"summary": summary}},
            )
            shipper.queue("metrics-gamepulse.session-default", close_doc)
            shipper.flush()
            log.info("Session %s ended after %ds", session.id, duration_s)
        _remove_session_json()
        if shipper:
            shipper.close()
        log.info("Collector stopped after %d ticks", tick)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gamepulse-collector",
        description="GamePulse Linux telemetry collector",
    )
    parser.add_argument("--config", type=Path, help="Config file path")
    parser.add_argument("--es-endpoint", help="Elasticsearch endpoint URL")
    parser.add_argument("--es-api-key", help="Elasticsearch API key")
    parser.add_argument("--interval-ms", type=int, help="Collection interval in ms")
    parser.add_argument("--debug", action="store_true", help="Print to stdout, skip ES")
    parser.add_argument("--once", action="store_true", help="Collect one sample and exit")
    parser.add_argument(
        "--no-game-detection", action="store_true", help="Disable Steam game detection"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    cfg = config_mod.load(args.config)

    if args.es_endpoint:
        cfg.elasticsearch.endpoint = args.es_endpoint
    if args.es_api_key:
        cfg.elasticsearch.api_key = args.es_api_key
    if args.interval_ms:
        cfg.collection.interval_ms = args.interval_ms
    if args.no_game_detection:
        cfg.collection.game_detection = False

    signal.signal(signal.SIGTERM, _handle_sigterm)

    run(cfg, debug=args.debug, once=args.once)


if __name__ == "__main__":
    main()
