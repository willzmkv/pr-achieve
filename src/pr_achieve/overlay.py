from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pr_achieve.config import Config
from pr_achieve.sources import Achievement

log = logging.getLogger(__name__)

DisplayFn = Callable[[Achievement], Awaitable[None]]


class OverlayQueue:
    """
    Sequential achievement display queue.

    Accepts Achievement items via enqueue(); drain() processes them
    one-at-a-time in order, calling display_fn for each and awaiting it.
    display_fn is responsible for the full show cycle (slide-in, hold, slide-out).
    """

    def __init__(self, config: Config, display_fn: DisplayFn) -> None:
        self._config = config
        self._display_fn = display_fn
        self._queue: asyncio.Queue[Achievement] = asyncio.Queue()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def enqueue(self, achievement: Achievement) -> None:
        await self._queue.put(achievement)

    async def drain(self) -> None:
        """Process all queued achievements sequentially."""
        while not self._queue.empty():
            await self._show(self._queue.get_nowait())

    async def run(self, source_queue: asyncio.Queue[Achievement]) -> None:
        """
        Long-running loop: consume from source_queue and display sequentially.
        This is the coroutine started by daemon.py.
        """
        while True:
            await self._show(await source_queue.get())

    async def _show(self, ach: Achievement) -> None:
        try:
            await self._display_fn(ach)
        except Exception:
            log.exception("display_fn raised for %s", ach.achievement_name)
