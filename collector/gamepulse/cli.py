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


def _handle_sigterm(sig: int, frame: Any) -> None:
    global _SHUTDOWN
    _SHUTDOWN = True


def _timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _print_debug(tick: int, docs: list[tuple[str, dict[str, Any]]]) -> None:
    parts = []
    for _index, doc in docs:
        if "cpu" in doc:
            parts.append(f"CPU:{doc['cpu'].get('total_utilisation_pct', '?')}%")
        if "gpu" in doc:
            g = doc["gpu"]
            parts.append(
                f"GPU:{g.get('utilisation_pct', '?')}%/{g.get('temperature_c', '?')}°C"
            )
        if "memory" in doc:
            parts.append(f"MEM:{doc['memory'].get('system_used_mb', '?')}MB")
        if "fps" in doc:
            f = doc["fps"]
            parts.append(f"FPS:{f.get('current', '?')} (1%:{f.get('low_1pct', '?')})")
        if "storage" in doc:
            s = doc["storage"]
            parts.append(f"IO:R{s.get('read_mbps', '?')}/W{s.get('write_mbps', '?')}MB/s")
        if "network" in doc:
            n = doc["network"]
            parts.append(f"NET:{n.get('rx_mbps', '?')}/{n.get('tx_mbps', '?')}MB/s")
        if "power" in doc:
            p = doc["power"]
            if "battery_pct" in p:
                parts.append(f"BAT:{p['battery_pct']}%")
            if "tdp_current_w" in p:
                parts.append(f"TDP:{p['tdp_current_w']}W")

    game_doc = next((d for _, d in docs if "game" in d), None)
    game_str = f" [{game_doc['game']['name']}]" if game_doc else ""
    print(f"[{tick:05d}] {' '.join(parts)}{game_str}")


def run(cfg: config_mod.Config, debug: bool, once: bool) -> None:
    global _SHUTDOWN

    # Session identity
    session = Session(id=str(uuid.uuid4()))

    # Static host snapshot
    log.info("Collecting host environment snapshot…")
    enricher = HostEnricher()

    # Collectors
    cpu = CpuCollector()
    mem = MemoryCollector()
    storage = StorageCollector()
    gpu = make_gpu_collector() if cfg.collection.gpu else None
    network = NetworkCollector() if cfg.collection.network else None
    power = PowerCollector()   # always attempt; returns None if no battery/power data
    audio = AudioCollector()   # always attempt; at minimum records backend name
    frame = MangoHudCollector() if cfg.collection.frame_timing else None
    detector = GameDetector() if cfg.collection.game_detection else None

    # Shipper
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
        # Ship the session document immediately
        session_doc: dict[str, Any] = {
            "@timestamp": _timestamp(),
            **session.base_doc(__version__, cfg.privacy.opt_in_public),
            **enricher.snapshot,
        }
        shipper.queue("metrics-gamepulse.session-default", session_doc)
        shipper.flush()
        log.info("Session %s started", session.id)

    interval = cfg.collection.interval_ms / 1000.0
    tick = 0
    prev_game_pid: int | None = None

    try:
        while not _SHUTDOWN:
            tick_start = time.monotonic()
            tick += 1
            ts = _timestamp()

            # Game detection
            if detector:
                game = detector.detect()
                if game and (prev_game_pid is None or game.pid != prev_game_pid):
                    session.game = GameInfo(
                        name=game.name,
                        steam_app_id=game.steam_app_id,
                        pid=game.pid,
                        graphics_api=game.graphics_api,
                        uses_proton=game.uses_proton,
                    )
                    mem.set_game_pid(game.pid)
                    prev_game_pid = game.pid
                    log.info("Detected game: %s (pid %d)", game.name, game.pid)

                    # Ship updated session doc with game info
                    if shipper:
                        updated = {
                            "@timestamp": ts,
                            **session.base_doc(__version__, cfg.privacy.opt_in_public),
                            **enricher.snapshot,
                            "compatibility": {
                                k: v for k, v in {
                                    "proton_version": game.proton_version,
                                    "dxvk_version": game.dxvk_version,
                                    "vkd3d_proton_version": game.vkd3d_version,
                                }.items() if v is not None
                            },
                        }
                        shipper.queue("metrics-gamepulse.session-default", updated)

                elif game is None and prev_game_pid is not None:
                    log.info("Game exited")
                    session.game = None
                    mem.set_game_pid(None)
                    prev_game_pid = None

            # Collect metrics
            base = {
                "@timestamp": ts,
                **session.base_doc(__version__, cfg.privacy.opt_in_public),
            }

            docs: list[tuple[str, dict[str, Any]]] = []

            if cfg.collection.cpu:
                if r := cpu.collect():
                    docs.append((cpu.data_stream, {**base, **r}))

            if cfg.collection.memory:
                if r := mem.collect():
                    docs.append((mem.data_stream, {**base, **r}))

            if cfg.collection.storage:
                if r := storage.collect():
                    docs.append((storage.data_stream, {**base, **r}))

            if gpu and cfg.collection.gpu:
                if r := gpu.collect():
                    docs.append((gpu.data_stream, {**base, **r}))

            if frame and cfg.collection.frame_timing:
                if r := frame.collect():
                    docs.append((frame.data_stream, {**base, **r}))

            if network and cfg.collection.network:
                if r := network.collect():
                    docs.append((network.data_stream, {**base, **r}))

            if power:
                if r := power.collect():
                    docs.append((power.data_stream, {**base, **r}))

            if r := audio.collect():
                docs.append((audio.data_stream, {**base, **r}))

            # Ship or print
            if debug:
                _print_debug(tick, docs)
            elif shipper:
                for index, doc in docs:
                    shipper.queue(index, doc)
                shipper.flush_if_due()

            if once:
                break

            # Sleep remainder of interval
            elapsed = time.monotonic() - tick_start
            sleep_time = max(0.0, interval - elapsed)
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        pass
    finally:
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

    # CLI overrides
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
