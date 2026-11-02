import asyncio
import json
import time
from pathlib import Path

import pytest

from pr_achieve.sources import Achievement
from pr_achieve.sources.heroic import HeroicSource


@pytest.fixture
def heroic_dir(tmp_path: Path) -> Path:
    d = tmp_path / "heroic"
    (d / "gog_store" / "achievements").mkdir(parents=True)
    (d / "store" / "achievements").mkdir(parents=True)
    return d


def write_gog_achievements(heroic_dir: Path, game_id: str, achievements: list[dict]) -> Path:
    path = heroic_dir / "gog_store" / "achievements" / f"{game_id}.json"
    path.write_text(json.dumps({"achievements": achievements, "game_name": "The Witcher 3"}))
    return path


async def test_heroic_gog_detects_unlock(heroic_dir: Path):
    write_gog_achievements(heroic_dir, "1207664643", [
        {"id": "COMPLETE_PROLOGUE", "name": "Prologue Complete",
         "description": "Finish the prologue", "Achieved": True, "icon": None},
        {"id": "WIN_GWENT", "name": "Gwent Master",
         "description": "Win 100 games of Gwent", "Achieved": False, "icon": None},
    ])

    source = HeroicSource(heroic_config_dir=heroic_dir)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    await source.poll_once(queue)

    assert queue.qsize() == 1
    ach = queue.get_nowait()
    assert ach.achievement_name == "Prologue Complete"
    assert ach.source == "heroic"
    assert ach.game_name == "The Witcher 3"


async def test_heroic_no_duplicates(heroic_dir: Path):
    write_gog_achievements(heroic_dir, "1207664643", [
        {"id": "COMPLETE_PROLOGUE", "name": "Prologue Complete",
         "description": "", "Achieved": True, "icon": None},
    ])

    source = HeroicSource(heroic_config_dir=heroic_dir)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    await source.poll_once(queue)
    await source.poll_once(queue)

    assert queue.qsize() == 1


async def test_heroic_missing_dir_does_not_raise(tmp_path: Path):
    source = HeroicSource(heroic_config_dir=tmp_path / "nonexistent")
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    await source.poll_once(queue)
    assert queue.qsize() == 0


async def test_heroic_epic_dir_and_malformed_entries(heroic_dir: Path):
    epic = heroic_dir / "store" / "achievements" / "Fortnite.json"
    epic.write_text(json.dumps({"game_name": "Some Epic Game", "achievements": [
        {"name": "No id", "Achieved": True},
        "not-a-dict",
        {"id": "E1", "name": "Epic One", "Achieved": True, "points": 25,
         "icon": "https://cdn.example/e1.png"},
    ]}))
    (heroic_dir / "gog_store" / "achievements" / "broken.json").write_text("{nope")
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    await HeroicSource(heroic_config_dir=heroic_dir).poll_once(queue)
    assert queue.qsize() == 1
    ach = queue.get_nowait()
    assert (ach.game_id, ach.achievement_name, ach.points, ach.icon_url) == (
        "Fortnite", "Epic One", 25, "https://cdn.example/e1.png")


async def test_heroic_run_baselines_existing_unlocks(heroic_dir: Path):
    write_gog_achievements(heroic_dir, "1207664643", [
        {"id": "OLD", "name": "Old", "Achieved": True},
    ])
    source = HeroicSource(heroic_config_dir=heroic_dir, poll_interval=0.01)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    task = asyncio.create_task(source.run(queue))
    await asyncio.sleep(0.05)
    assert queue.empty()

    write_gog_achievements(heroic_dir, "1207664643", [
        {"id": "OLD", "name": "Old", "Achieved": True},
        {"id": "NEW", "name": "New", "Achieved": True, "unlock_time": int(time.time()) + 5},
        {"id": "HIST", "name": "Historic", "Achieved": True, "unlock_time": 1000},
    ])
    ach = await asyncio.wait_for(queue.get(), timeout=2)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ach.achievement_name == "New"
    assert queue.empty()
