import asyncio
from unittest.mock import AsyncMock

import pytest

from pr_achieve.config import Config
from pr_achieve.overlay import OverlayQueue
from pr_achieve.sources import Achievement


def make_achievement(name: str = "Test Achievement") -> Achievement:
    return Achievement(
        game_id="test_game",
        game_name="Test Game",
        achievement_name=name,
        points=5,
        description="A test achievement",
        source="native_hook",
    )


async def test_queue_calls_display_for_each_achievement():
    cfg = Config()
    display_mock = AsyncMock()
    oq = OverlayQueue(config=cfg, display_fn=display_mock)

    a1 = make_achievement("First")
    a2 = make_achievement("Second")
    await oq.enqueue(a1)
    await oq.enqueue(a2)

    await oq.drain()

    assert display_mock.call_count == 2
    first_call_arg = display_mock.call_args_list[0][0][0]
    assert first_call_arg.achievement_name == "First"


async def test_queue_respects_sequential_order():
    cfg = Config()
    display_order = []

    async def record_display(ach: Achievement):
        display_order.append(ach.achievement_name)

    oq = OverlayQueue(config=cfg, display_fn=record_display)
    for name in ["A", "B", "C"]:
        await oq.enqueue(make_achievement(name))

    await oq.drain()

    assert display_order == ["A", "B", "C"]


async def test_queue_empty_drain_returns_immediately():
    cfg = Config()
    display_mock = AsyncMock()
    oq = OverlayQueue(config=cfg, display_fn=display_mock)
    await oq.drain()
    display_mock.assert_not_called()


async def test_queue_size():
    cfg = Config()
    oq = OverlayQueue(config=cfg, display_fn=AsyncMock())
    assert oq.pending == 0
    await oq.enqueue(make_achievement())
    await oq.enqueue(make_achievement())
    assert oq.pending == 2


async def test_drain_continues_after_display_error():
    shown = []

    async def flaky(ach: Achievement):
        if ach.achievement_name == "Boom":
            raise RuntimeError("render failed")
        shown.append(ach.achievement_name)

    oq = OverlayQueue(config=Config(), display_fn=flaky)
    for name in ["Boom", "After"]:
        await oq.enqueue(make_achievement(name))
    await oq.drain()
    assert shown == ["After"]
    assert oq.pending == 0


async def test_run_displays_one_at_a_time_from_source_queue():
    started: list[str] = []
    release = asyncio.Event()

    async def display(ach: Achievement):
        started.append(ach.achievement_name)
        if ach.achievement_name == "A":
            await release.wait()  # A is still on screen
        elif ach.achievement_name == "Boom":
            raise RuntimeError("render failed")

    source_queue: asyncio.Queue[Achievement] = asyncio.Queue()
    oq = OverlayQueue(config=Config(), display_fn=display)
    task = asyncio.create_task(oq.run(source_queue))
    for name in ["A", "Boom", "B"]:
        await source_queue.put(make_achievement(name))

    await asyncio.sleep(0.05)
    assert started == ["A"]  # B must wait for A's full show cycle
    release.set()
    await asyncio.sleep(0.05)
    assert started == ["A", "Boom", "B"]  # an error does not kill the consumer

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
