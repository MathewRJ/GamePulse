#!/usr/bin/env python3
"""Build curated GamePulse dashboard saved objects from verified panels.

The generated dashboards intentionally reuse panels from the current verified
GamePulse suite. That keeps the saved-object Lens state close to known-good
exports while letting us reshape the user experience around higher-level
dashboard concepts.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "kibana" / "dashboard"
NORMALIZED_DIR = ROOT / "kibana" / "dashboard"
IMPORT_DIR = ROOT / "dashboards"

NAV = (
    "**GamePulse** &nbsp;.&nbsp; "
    "[Command Center](/app/dashboards#/view/gamepulse-gp-command-center) &nbsp;|&nbsp; "
    "[Regression Lab](/app/dashboards#/view/gamepulse-gp-regression-lab) &nbsp;|&nbsp; "
    "[Player Overview](/app/dashboards#/view/gamepulse-gp-home) &nbsp;|&nbsp; "
    "[Game Performance](/app/dashboards#/view/gamepulse-gp-game-perf) &nbsp;|&nbsp; "
    "[Engine](/app/dashboards#/view/gamepulse-gp-engine) &nbsp;|&nbsp; "
    "[Hardware](/app/dashboards#/view/gamepulse-gp-hardware) &nbsp;|&nbsp; "
    "[Software](/app/dashboards#/view/gamepulse-gp-software) &nbsp;|&nbsp; "
    "[Compare](/app/dashboards#/view/828db140-b330-4d26-8045-40a7895bfc41)"
)


def load(name: str) -> dict:
    with (SOURCE_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


SOURCES = {
    "home": load("gamepulse-gp-home.json"),
    "game": load("gamepulse-gp-game-perf.json"),
    "hardware": load("gamepulse-gp-hardware.json"),
    "software": load("gamepulse-gp-software.json"),
    "engine": load("gamepulse-gp-engine.json"),
}


def source_panel(source: str, panel_index: str, *, x: int, y: int, w: int, h: int) -> dict:
    panel = next(
        p for p in SOURCES[source]["attributes"]["panelsJSON"] if p["panelIndex"] == panel_index
    )
    out = copy.deepcopy(panel)
    out["gridData"].update({"x": x, "y": y, "w": w, "h": h, "i": out["panelIndex"]})
    return out


def nav_panel() -> dict:
    return {
        "type": "markdown",
        "panelIndex": "nav-bar",
        "gridData": {"x": 0, "y": 0, "w": 48, "h": 3, "i": "nav-bar"},
        "embeddableConfig": {
            "content": NAV,
            "settings": {"open_links_in_new_tab": True},
        },
    }


def refs_for(source: str, panel_indexes: set[str]) -> list[dict]:
    refs: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in SOURCES[source].get("references", []):
        name = ref.get("name", "")
        if ":" not in name:
            continue
        panel_index = name.split(":", 1)[0]
        if panel_index not in panel_indexes:
            continue
        key = (ref["type"], ref["id"], ref["name"])
        if key not in seen:
            refs.append(copy.deepcopy(ref))
            seen.add(key)
    return refs


def base_attributes(title: str, description: str, panels: list[dict]) -> dict:
    options = copy.deepcopy(SOURCES["home"]["attributes"]["optionsJSON"])
    options.update(
        {
            "hidePanelTitles": False,
            "hidePanelBorders": False,
            "useMargins": True,
            "syncColors": True,
            "syncCursor": True,
            "syncTooltips": True,
            "autoApplyFilters": True,
        }
    )
    return {
        "title": title,
        "description": description,
        "timeFrom": "now-90d",
        "timeTo": "now",
        "timeRestore": True,
        "optionsJSON": options,
        "kibanaSavedObjectMeta": {"searchSourceJSON": {}},
        "panelsJSON": panels,
    }


def normalized_dashboard(
    dashboard_id: str,
    title: str,
    description: str,
    panels: list[dict],
    source_panel_map: dict[str, set[str]],
) -> dict:
    refs: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for source, panel_indexes in source_panel_map.items():
        for ref in refs_for(source, panel_indexes):
            key = (ref["type"], ref["id"], ref["name"])
            if key not in seen:
                refs.append(ref)
                seen.add(key)
    return {
        "type": "dashboard",
        "id": dashboard_id,
        "attributes": base_attributes(title, description, panels),
        "references": refs,
    }


def importable(dashboard: dict) -> dict:
    out = copy.deepcopy(dashboard)
    attrs = out["attributes"]
    attrs["panelsJSON"] = json.dumps(attrs["panelsJSON"], separators=(",", ":"))
    attrs["optionsJSON"] = json.dumps(attrs["optionsJSON"], separators=(",", ":"))
    meta = attrs.setdefault("kibanaSavedObjectMeta", {})
    meta["searchSourceJSON"] = json.dumps(meta.get("searchSourceJSON", {}), separators=(",", ":"))
    out["coreMigrationVersion"] = "8.8.0"
    out["typeMigrationVersion"] = "10.3.0"
    out["managed"] = False
    return out


def write_dashboard(dashboard: dict, slug: str) -> None:
    normalized_path = NORMALIZED_DIR / f"{dashboard['id']}.json"
    import_path = IMPORT_DIR / f"{slug}.ndjson"
    normalized_path.write_text(json.dumps(dashboard, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    import_path.write_text(json.dumps(importable(dashboard), separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {normalized_path.relative_to(ROOT)}")
    print(f"wrote {import_path.relative_to(ROOT)}")


def build_command_center() -> dict:
    panel_specs = [
        ("home", "ctrl-game", 0, 3, 16, 4),
        ("home", "ctrl-driver", 16, 3, 16, 4),
        ("home", "ctrl-kernel", 32, 3, 16, 4),
        ("home", "kpi-sessions", 0, 7, 12, 6),
        ("home", "kpi-games", 12, 7, 12, 6),
        ("home", "kpi-hours", 24, 7, 12, 6),
        ("home", "kpi-avg-fps", 36, 7, 12, 6),
        ("home", "chart-fps-time", 0, 13, 48, 16),
        ("software", "table-fps-driver", 0, 29, 24, 10),
        ("software", "table-fps-kernel", 24, 29, 24, 10),
        ("home", "table-hours-game", 0, 39, 24, 10),
        ("home", "table-fps-game", 24, 39, 24, 10),
        ("home", "table-sessions", 0, 49, 48, 14),
        ("home", "table-hw", 0, 63, 48, 10),
    ]
    panels = [nav_panel()]
    source_map: dict[str, set[str]] = {}
    for source, panel_index, x, y, w, h in panel_specs:
        panels.append(source_panel(source, panel_index, x=x, y=y, w=w, h=h))
        source_map.setdefault(source, set()).add(panel_index)
    return normalized_dashboard(
        "gamepulse-gp-command-center",
        "GamePulse - Performance Command Center",
        "Curated 90-day gamer overview: session health, game history, FPS trend, and likely regression causes.",
        panels,
        source_map,
    )


def build_regression_lab() -> dict:
    panel_specs = [
        ("software", "ctrl-game", 0, 3, 12, 4),
        ("software", "ctrl-driver", 12, 3, 12, 4),
        ("software", "ctrl-proton", 24, 3, 12, 4),
        ("software", "ctrl-kernel", 36, 3, 12, 4),
        ("software", "kpi-drivers", 0, 7, 8, 6),
        ("software", "kpi-protons", 8, 7, 8, 6),
        ("software", "kpi-kernels", 16, 7, 8, 6),
        ("software", "kpi-best-fps", 24, 7, 8, 6),
        ("software", "kpi-sessions", 32, 7, 8, 6),
        ("software", "kpi-audio", 40, 7, 8, 6),
        ("software", "chart-fps-by-driver", 0, 13, 48, 14),
        ("software", "table-fps-driver", 0, 27, 24, 10),
        ("software", "table-fps-proton", 24, 27, 24, 10),
        ("software", "table-fps-kernel", 0, 37, 24, 10),
        ("software", "table-compat", 24, 37, 24, 10),
        ("hardware", "chart-gpu-temp", 0, 47, 24, 10),
        ("hardware", "chart-gpu-power", 24, 47, 24, 10),
        ("hardware", "chart-cpu-util", 0, 57, 24, 10),
        ("hardware", "chart-memory", 24, 57, 24, 10),
        ("engine", "chart-gpu-pipeline", 0, 67, 48, 12),
        ("engine", "chart-stutter-score", 0, 79, 48, 12),
        ("engine", "chart-cpu-sched", 0, 91, 24, 12),
        ("engine", "chart-io-lat", 24, 91, 24, 12),
    ]
    panels = [nav_panel()]
    source_map: dict[str, set[str]] = {}
    for source, panel_index, x, y, w, h in panel_specs:
        panels.append(source_panel(source, panel_index, x=x, y=y, w=w, h=h))
        source_map.setdefault(source, set()).add(panel_index)
    return normalized_dashboard(
        "gamepulse-gp-regression-lab",
        "GamePulse - Regression Lab",
        "Curated 90-day regression dashboard for driver, Proton, kernel, hardware, and low-level causality checks.",
        panels,
        source_map,
    )


def main() -> None:
    write_dashboard(build_command_center(), "command-center-dashboard")
    write_dashboard(build_regression_lab(), "regression-lab-dashboard")


if __name__ == "__main__":
    main()
