"""Session state — tracks the current game session identity and game info."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _machine_id() -> str:
    """Anonymous stable identifier derived from /etc/machine-id."""
    try:
        raw = Path("/etc/machine-id").read_text().strip()
    except OSError:
        raw = "unknown"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass
class GameInfo:
    name: str
    steam_app_id: int | None
    pid: int
    graphics_api: str | None
    uses_proton: bool = False


@dataclass
class Session:
    id: str
    user_id: str = field(default_factory=_machine_id)
    game: GameInfo | None = None

    def base_doc(self, agent_version: str, opt_in_public: bool) -> dict[str, Any]:
        """Fields included in every document shipped to ES."""
        doc: dict[str, Any] = {
            "session_id": self.id,
            "agent_version": agent_version,
            "user_id": self.user_id,
            "opt_in_public": opt_in_public,
        }
        if self.game:
            game_doc: dict[str, Any] = {"name": self.game.name}
            if self.game.steam_app_id is not None:
                game_doc["steam_app_id"] = self.game.steam_app_id
            if self.game.graphics_api:
                game_doc["graphics_api"] = self.game.graphics_api
            game_doc["uses_proton"] = self.game.uses_proton
            doc["game"] = game_doc
        return doc
