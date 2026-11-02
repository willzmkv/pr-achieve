import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from pr_achieve.sources import Achievement
from pr_achieve.sources.native_hook import NativeHookSource

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify is Linux-only")


@pytest.fixture
def hook_dir(tmp_path: Path) -> Path:
    d = tmp_path / "achieve-hook"
    d.mkdir()
    return d


def drop(hook_dir: Path, name: str, payload) -> Path:
    """Write a hook file the way the README contract says: temp file + rename."""
    tmp = hook_dir / f".{name}.tmp"
    tmp.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    final = hook_dir / name
    tmp.rename(final)
    return final


async def test_native_hook_reads_json_file(hook_dir: Path):
    payload = {
        "game_id": "sm64",
        "achievement": "Big Bob-omb on the Summit",
        "points": 10,
        "description": "Defeat Big Bob-omb",
    }
    hook_file = hook_dir / "sm64_1234567890.json"
    hook_file.write_text(json.dumps(payload))

    source = NativeHookSource(hook_dir=hook_dir)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    await source.process_file(hook_file, queue)

    assert queue.qsize() == 1
    ach = queue.get_nowait()
    assert ach.achievement_name == "Big Bob-omb on the Summit"
    assert ach.game_id == "sm64"
    assert ach.points == 10
    assert ach.source == "native_hook"


async def test_native_hook_deletes_file_after_processing(hook_dir: Path):
    payload = {"game_id": "oot", "achievement": "Dungeon Complete", "points": 5, "description": ""}
    hook_file = hook_dir / "oot_9999.json"
    hook_file.write_text(json.dumps(payload))

    source = NativeHookSource(hook_dir=hook_dir)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    await source.process_file(hook_file, queue)

    assert not hook_file.exists()


async def test_native_hook_invalid_json_does_not_raise(hook_dir: Path):
    bad_file = hook_dir / "bad_123.json"
    bad_file.write_text("NOT JSON {{")

    source = NativeHookSource(hook_dir=hook_dir)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    await source.process_file(bad_file, queue)

    assert queue.qsize() == 0
    assert not bad_file.exists()


async def test_native_hook_scan_existing_on_startup(hook_dir: Path):
    """Files present in hook_dir at startup must be processed immediately."""
    for i in range(3):
        payload = {"game_id": "sm64", "achievement": f"Star {i}", "points": i, "description": ""}
        (hook_dir / f"sm64_{i}.json").write_text(json.dumps(payload))

    source = NativeHookSource(hook_dir=hook_dir)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    await source.scan_existing(queue)

    assert queue.qsize() == 3


async def test_native_hook_optional_fields_and_coercion(hook_dir: Path):
    f = drop(hook_dir, "sm64_1.json", {
        "game_id": "sm64", "game_name": "Super Mario 64", "achievement": "Star",
        "points": "15", "icon_url": "/usr/share/project-r/icons/sm64.png",
    })
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    await NativeHookSource(hook_dir=hook_dir).process_file(f, queue)
    ach = queue.get_nowait()
    assert (ach.game_name, ach.points, ach.description, ach.icon_url) == (
        "Super Mario 64", 15, "", "/usr/share/project-r/icons/sm64.png")


@pytest.mark.parametrize("payload", [
    {"game_id": "sm64"},                       # missing achievement
    {"achievement": "Star"},                   # missing game_id
    {"game_id": "sm64", "achievement": ""},    # empty name
    ["not", "an", "object"],
])
async def test_native_hook_rejects_invalid_payloads(hook_dir: Path, payload):
    f = drop(hook_dir, "bad.json", payload)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    await NativeHookSource(hook_dir=hook_dir).process_file(f, queue)
    assert queue.empty()
    assert not f.exists()


async def test_native_hook_rejects_oversized_file(hook_dir: Path):
    f = drop(hook_dir, "big.json", {"game_id": "x", "achievement": "y", "description": "z" * 70000})
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    await NativeHookSource(hook_dir=hook_dir).process_file(f, queue)
    assert queue.empty()
    assert not f.exists()


async def test_native_hook_scan_ignores_temp_and_hidden_files(hook_dir: Path):
    payload = json.dumps({"game_id": "sm64", "achievement": "Star"})
    (hook_dir / "sm64_1.json.tmp").write_text(payload)
    (hook_dir / ".sm64_2.json").write_text(payload)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    await NativeHookSource(hook_dir=hook_dir).scan_existing(queue)
    assert queue.empty()
    assert (hook_dir / "sm64_1.json.tmp").exists()


async def test_native_hook_missing_file_is_silent(hook_dir: Path):
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    await NativeHookSource(hook_dir=hook_dir).process_file(hook_dir / "gone.json", queue)
    assert queue.empty()


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)  # must not hang on cancel


@linux_only
async def test_native_hook_run_uses_inotify(hook_dir: Path):
    pytest.importorskip("inotify_simple")
    (hook_dir / "sm64_0.json").write_text(json.dumps({"game_id": "sm64", "achievement": "Boot"}))
    # Polling interval far beyond the timeouts: only inotify can deliver in time.
    source = NativeHookSource(hook_dir=hook_dir, poll_interval=3600)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    task = asyncio.create_task(source.run(queue))

    first = await asyncio.wait_for(queue.get(), timeout=2)  # present before start
    drop(hook_dir, "sm64_1.json", {"game_id": "sm64", "achievement": "Renamed"})  # IN_MOVED_TO
    second = await asyncio.wait_for(queue.get(), timeout=2)
    (hook_dir / "sm64_2.json").write_text(  # IN_CLOSE_WRITE
        json.dumps({"game_id": "sm64", "achievement": "Direct"}))
    third = await asyncio.wait_for(queue.get(), timeout=2)
    await _stop(task)

    assert [a.achievement_name for a in (first, second, third)] == ["Boot", "Renamed", "Direct"]
    assert list(hook_dir.iterdir()) == []


async def test_native_hook_run_falls_back_to_polling(hook_dir: Path):
    source = NativeHookSource(hook_dir=hook_dir, poll_interval=0.01)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    with patch.dict(sys.modules, {"inotify_simple": None}):  # import fails
        task = asyncio.create_task(source.run(queue))
        await asyncio.sleep(0.05)
        drop(hook_dir, "oot_1.json", {"game_id": "oot", "achievement": "Polled"})
        ach = await asyncio.wait_for(queue.get(), timeout=2)
        await _stop(task)
    assert ach.achievement_name == "Polled"


async def test_native_hook_run_creates_missing_dir(tmp_path: Path):
    hook_dir = tmp_path / "run" / "project-r" / "achieve-hook"
    source = NativeHookSource(hook_dir=hook_dir, poll_interval=0.01)
    task = asyncio.create_task(source.run(asyncio.Queue()))
    await asyncio.sleep(0.1)
    await _stop(task)
    assert hook_dir.is_dir()
