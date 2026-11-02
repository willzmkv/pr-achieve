from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pr_achieve.sources import Achievement

log = logging.getLogger(__name__)

_POLL_INTERVAL = 15.0


class HeroicSource:
    """
    Polls Heroic Games Launcher achievement JSON files for new unlocks.

    GOG:  ~/.config/heroic/gog_store/achievements/{game_id}.json
    Epic: ~/.config/heroic/store/achievements/{game_id}.json

    NOTE: current Heroic releases do not write these files (GOG achievements go
    through Comet, Epic's through the EOS overlay). The layout is a pr-achieve
    file contract (see README) for a producer to fill.
    """

    def __init__(
        self, heroic_config_dir: Path | None = None, poll_interval: float = _POLL_INTERVAL
    ) -> None:
        if heroic_config_dir is None:
            heroic_config_dir = Path("~/.config/heroic").expanduser()
        self._heroic_dir = heroic_config_dir
        self._poll_interval = poll_interval
        self._seen_keys: set[str] = set()  # "{game_id}:{achievement_id}"
        self._not_before: float | None = None  # set by run(): daemon start time

    def _iter_achievement_files(self) -> Iterator[Path]:
        for sub in ("gog_store/achievements", "store/achievements"):
            d = self._heroic_dir / sub
            if d.is_dir():
                yield from sorted(d.glob("*.json"))

    async def poll_once(self, queue: asyncio.Queue[Achievement], *, emit: bool = True) -> None:
        for ach_file in self._iter_achievement_files():
            try:
                data = json.loads(ach_file.read_text(encoding="utf-8"))
            except Exception as exc:
                log.debug("Could not read %s: %s", ach_file, exc)
                continue
            if not isinstance(data, dict):
                continue

            game_id = ach_file.stem
            game_name = data.get("game_name") or game_id

            for item in data.get("achievements", []):
                if not isinstance(item, dict) or not item.get("Achieved") or "id" not in item:
                    continue
                key = f"{game_id}:{item['id']}"
                if key in self._seen_keys:
                    continue
                self._seen_keys.add(key)
                if not emit or self._unlocked_before_start(item):
                    continue
                await queue.put(Achievement(
                    game_id=game_id,
                    game_name=game_name,
                    achievement_name=item.get("name") or str(item["id"]),
                    points=_as_int(item.get("points")),
                    description=item.get("description") or "",
                    source="heroic",
                    icon_url=item.get("icon"),
                ))

    def _unlocked_before_start(self, item: dict[str, Any]) -> bool:
        unlock_time = item.get("unlock_time")
        return (
            self._not_before is not None
            and isinstance(unlock_time, (int, float))
            and 0 < unlock_time < self._not_before
        )

    async def run(self, queue: asyncio.Queue[Achievement]) -> None:
        self._not_before = time.time()
        await self.poll_once(queue, emit=False)  # baseline: no replay at startup
        while True:
            await asyncio.sleep(self._poll_interval)
            await self.poll_once(queue)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
