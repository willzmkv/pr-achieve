from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiohttp

from pr_achieve.sources import Achievement
from pr_achieve.sources._proc import PROC_ROOT, process_running

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://retroachievements.org/API"
DEFAULT_MEDIA_URL = "https://media.retroachievements.org"
_POLL_INTERVAL = 10.0  # seconds
_LOOKBACK_MINUTES = 10  # window requested from the API; tolerates missed polls


class RetroArchSource:
    """RetroAchievements unlocks earned in RetroArch.

    RetroArch's port 55355 is a UDP command interface that cannot list
    achievements, so this source polls the RetroAchievements Web API
    (``API_GetUserRecentAchievements.php``), which RetroArch reports each unlock
    to as it happens. The first successful fetch is a silent baseline (no replay
    of recent unlocks on daemon start); afterwards the API is only polled while a
    ``retroarch`` process is running.
    """

    def __init__(
        self,
        username: str = "",
        api_key: str = "",
        api_url: str = DEFAULT_API_URL,
        media_url: str = DEFAULT_MEDIA_URL,
        poll_interval: float = _POLL_INTERVAL,
        lookback_minutes: int = _LOOKBACK_MINUTES,
        process_names: Iterable[str] = ("retroarch",),
        proc_root: Path = PROC_ROOT,
    ) -> None:
        self._username = username
        self._api_key = api_key
        self._api_url = api_url.rstrip("/")
        self._media_url = media_url.rstrip("/")
        self._poll_interval = poll_interval
        self._lookback_minutes = lookback_minutes
        self._process_names = tuple(process_names)
        self._proc_root = proc_root
        self._seen_keys: set[str] = set()  # "{achievement_id}:{hardcore}"

    @property
    def configured(self) -> bool:
        return bool(self._username and self._api_key)

    async def poll_once(self, queue: asyncio.Queue[Achievement], *, emit: bool = True) -> bool:
        """Fetch recent unlocks and enqueue the unseen ones.

        Returns True if the API answered. With ``emit=False`` unseen unlocks are
        only recorded (baseline). Never raises on network/API errors.
        """
        if not self.configured:
            return False
        url = f"{self._api_url}/API_GetUserRecentAchievements.php"
        params = {"u": self._username, "y": self._api_key, "m": str(self._lookback_minutes)}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        log.debug("RetroAchievements API returned HTTP %s", resp.status)
                        return False
                    data: Any = await resp.json(content_type=None)
        except Exception as exc:
            # Exception text may contain the request URL (and thus the key): log type only.
            log.debug("RetroAchievements API unreachable: %s", type(exc).__name__)
            return False

        if not isinstance(data, list):
            log.debug("Unexpected RetroAchievements payload: %s", type(data).__name__)
            return False

        for item in sorted(
            (i for i in data if isinstance(i, dict)), key=lambda i: str(i.get("Date", ""))
        ):
            try:
                key = f"{int(item['AchievementID'])}:{int(item.get('HardcoreMode') or 0)}"
            except (KeyError, TypeError, ValueError):
                continue
            if key in self._seen_keys:
                continue
            self._seen_keys.add(key)
            if not emit:
                continue
            await queue.put(Achievement(
                game_id=str(item.get("GameID", "unknown")),
                game_name=item.get("GameTitle") or "Unknown Game",
                achievement_name=item.get("Title") or "Achievement unlocked",
                points=_as_int(item.get("Points")),
                description=item.get("Description") or "",
                source="retroarch",
                icon_url=self._badge_url(item),
            ))
        return True

    def _badge_url(self, item: dict[str, Any]) -> str | None:
        badge = item.get("BadgeURL")
        if not badge and item.get("BadgeName"):
            badge = f"/Badge/{item['BadgeName']}.png"
        if not badge:
            return None
        if badge.startswith(("http://", "https://")):
            return badge
        return f"{self._media_url}/{badge.lstrip('/')}"

    async def run(self, queue: asyncio.Queue[Achievement]) -> None:
        """Long-running poll loop. Runs until task is cancelled."""
        if not self.configured:
            log.info("ra_username/ra_api_key not set; RetroAchievements source disabled")
            return
        primed = await self.poll_once(queue, emit=False)
        while True:
            await asyncio.sleep(self._poll_interval)
            if not primed:
                primed = await self.poll_once(queue, emit=False)
            elif process_running(self._process_names, self._proc_root):
                await self.poll_once(queue)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
