from __future__ import annotations

import dataclasses
import logging
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Credentials are never committed: they come from the config file or from
# these environment variables (which win over the file).
ENV_OVERRIDES: dict[str, str] = {
    "PR_ACHIEVE_RA_USERNAME": "ra_username",
    "PR_ACHIEVE_RA_API_KEY": "ra_api_key",
    "PR_ACHIEVE_STEAM_API_KEY": "steam_api_key",
    "PR_ACHIEVE_STEAM_ID": "steam_id",
}


class OverlayPosition(str, Enum):
    TOP_RIGHT = "top_right"
    TOP_LEFT = "top_left"
    BOTTOM_RIGHT = "bottom_right"
    BOTTOM_LEFT = "bottom_left"


@dataclass
class Theme:
    bg_color: str = "#1a1a2e"
    accent_color: str = "#4ecdc4"
    text_color: str = "#ffffff"
    subtext_color: str = "#a0a0b0"
    font_family: str = "JetBrains Mono, monospace"
    icon_size: int = 56  # px


@dataclass
class Config:
    # Overlay
    position: OverlayPosition = OverlayPosition.TOP_RIGHT
    hold_seconds: float = 4.0
    slide_duration_ms: int = 350
    theme: Theme = field(default_factory=Theme)
    icon_cache_dir: str = "~/.cache/pr-achieve/icons"

    # RetroAchievements (unlocks made in RetroArch, polled from the RA Web API)
    ra_username: str = ""
    ra_api_key: str = field(default="", repr=False)
    ra_api_url: str = "https://retroachievements.org/API"
    ra_media_url: str = "https://media.retroachievements.org"
    ra_poll_seconds: float = 10.0

    # Steam (Web API -> sidecar JSON files -> SteamSource)
    steam_api_key: str = field(default="", repr=False)
    steam_id: str = ""  # SteamID64
    steam_app_ids: list[int] = field(default_factory=list)  # always polled
    steam_sidecar_dir: str = "~/.local/state/pr-achieve/steam"

    # Heroic (GOG/Epic achievement JSON files)
    heroic_config_dir: str = "~/.config/heroic"

    # Native ports drop JSON files here
    hook_dir: str = "/run/project-r/achieve-hook"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Config":
        data = dict(data)  # never mutate the caller's mapping
        theme_data = data.pop("theme", None) or {}
        position_raw = data.pop("position", OverlayPosition.TOP_RIGHT)
        return cls(
            position=OverlayPosition(position_raw),
            theme=Theme(**_known_fields(Theme, theme_data, "theme")),
            **_known_fields(cls, data, "config"),
        )

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "Config":
        """Load config.toml (missing file = defaults), then apply env overrides.

        An unreadable or invalid file is logged and replaced by defaults so the
        overlay keeps working on an appliance with a broken config.
        """
        environ = os.environ if environ is None else environ
        if path is None:
            path = default_config_path(environ)
        cfg = cls()
        if path.exists():
            try:
                with open(path, "rb") as f:
                    cfg = cls.from_dict(tomllib.load(f))
            except (OSError, tomllib.TOMLDecodeError, ValueError, TypeError) as exc:
                log.error("Invalid config %s (%s); using defaults", path, exc)
        return cfg.with_env(environ)

    def with_env(self, environ: Mapping[str, str]) -> "Config":
        overrides = {
            attr: environ[var] for var, attr in ENV_OVERRIDES.items() if environ.get(var)
        }
        return dataclasses.replace(self, **overrides) if overrides else self


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """$PR_ACHIEVE_CONFIG, else $XDG_CONFIG_HOME/pr-achieve/config.toml
    (XDG_CONFIG_HOME defaults to ~/.config)."""
    environ = os.environ if environ is None else environ
    if environ.get("PR_ACHIEVE_CONFIG"):
        return Path(environ["PR_ACHIEVE_CONFIG"]).expanduser()
    base = environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "pr-achieve" / "config.toml"


def _known_fields(cls: type, data: Mapping[str, Any], where: str) -> dict[str, Any]:
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - names)
    if unknown:
        log.warning("Ignoring unknown %s keys: %s", where, ", ".join(unknown))
    return {k: v for k, v in data.items() if k in names}
