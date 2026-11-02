import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp
import pytest

from pr_achieve.sources import Achievement
from pr_achieve.sources.steam import SteamSource, SteamWebApi

STEAM_ID = "76561197960287930"


@pytest.fixture
def steam_stats_dir(tmp_path: Path) -> Path:
    stats_dir = tmp_path / "userdata" / "123456789" / "stats"
    stats_dir.mkdir(parents=True)
    return stats_dir


def write_achievements(stats_dir: Path, app_id: int, achievements: list[dict]) -> Path:
    path = stats_dir / f"{app_id}_achievements.json"
    path.write_text(json.dumps({"achievements": achievements}))
    return path


def drain(queue: asyncio.Queue) -> list[Achievement]:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


async def test_steam_detects_new_unlock(steam_stats_dir: Path):
    achievements = [
        {"id": "ACH_WIN_ONE_GAME", "name": "First Win", "description": "Win your first game",
         "unlocked": True, "icon_url": None, "game_name": "Portal 2", "app_id": 620},
        {"id": "ACH_WIN_TEN_GAMES", "name": "Win 10 games", "description": "",
         "unlocked": False, "icon_url": None, "game_name": "Portal 2", "app_id": 620},
    ]
    write_achievements(steam_stats_dir, 620, achievements)

    source = SteamSource(userdata_dir=steam_stats_dir.parent.parent)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    await source.poll_once(queue)

    assert queue.qsize() == 1
    ach = queue.get_nowait()
    assert ach.achievement_name == "First Win"
    assert ach.source == "steam"
    assert ach.game_name == "Portal 2"


async def test_steam_no_duplicate_notifications(steam_stats_dir: Path):
    achievements = [
        {"id": "ACH_WIN_ONE_GAME", "name": "First Win", "description": "",
         "unlocked": True, "icon_url": None, "game_name": "Portal 2", "app_id": 620},
    ]
    write_achievements(steam_stats_dir, 620, achievements)

    source = SteamSource(userdata_dir=steam_stats_dir.parent.parent)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    await source.poll_once(queue)
    await source.poll_once(queue)

    assert queue.qsize() == 1  # second poll must not re-emit


async def test_steam_multiple_game_files(steam_stats_dir: Path):
    write_achievements(steam_stats_dir, 620, [
        {"id": "A1", "name": "Portal Win", "description": "",
         "unlocked": True, "icon_url": None, "game_name": "Portal 2", "app_id": 620},
    ])
    write_achievements(steam_stats_dir, 730, [
        {"id": "B1", "name": "CS Kill", "description": "",
         "unlocked": True, "icon_url": None, "game_name": "CS:GO", "app_id": 730},
    ])

    source = SteamSource(userdata_dir=steam_stats_dir.parent.parent)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    await source.poll_once(queue)

    assert queue.qsize() == 2
    assert {a.achievement_name for a in drain(queue)} == {"Portal Win", "CS Kill"}


async def test_steam_corrupt_file_is_skipped(steam_stats_dir: Path):
    (steam_stats_dir / "999_achievements.json").write_text("{not json")
    write_achievements(steam_stats_dir, 620, [
        {"id": "A1", "name": "Portal Win", "unlocked": True, "app_id": 620},
    ])
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    await SteamSource(userdata_dir=steam_stats_dir.parent.parent).poll_once(queue)
    assert [a.achievement_name for a in drain(queue)] == ["Portal Win"]


async def test_steam_run_baselines_existing_unlocks(steam_stats_dir: Path):
    write_achievements(steam_stats_dir, 620, [
        {"id": "OLD", "name": "Old Unlock", "unlocked": True, "app_id": 620},
    ])
    source = SteamSource(userdata_dir=steam_stats_dir.parent.parent, poll_interval=0.01)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    task = asyncio.create_task(source.run(queue))
    await asyncio.sleep(0.05)
    assert queue.empty()  # pre-existing unlocks are not replayed at startup

    now = int(time.time())
    write_achievements(steam_stats_dir, 620, [
        {"id": "OLD", "name": "Old Unlock", "unlocked": True, "app_id": 620},
        {"id": "NEW", "name": "New Unlock", "unlocked": True, "app_id": 620, "unlock_time": now + 5},
    ])
    # A file for another game appearing later: an unlock older than daemon start
    # is history (e.g. first Web API sync of that game) and must stay silent.
    write_achievements(steam_stats_dir, 730, [
        {"id": "ANCIENT", "name": "Ancient", "unlocked": True, "app_id": 730,
         "unlock_time": now - 86400},
    ])
    ach = await asyncio.wait_for(queue.get(), timeout=2)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ach.achievement_name == "New Unlock"
    assert queue.empty()


# --- Steam Web API producer -------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self.payload


class FakeSession:
    """Stands in for aiohttp.ClientSession; routes by URL suffix."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                if isinstance(payload, BaseException):
                    raise payload
                return FakeResponse(payload)
        return FakeResponse({}, status=404)


SUMMARIES = {"response": {"players": [{
    "steamid": STEAM_ID, "communityvisibilitystate": 3, "personaname": "player1",
    "gameid": "620", "gameextrainfo": "Portal 2",
}]}}
PLAYER_ACHIEVEMENTS = {"playerstats": {
    "steamID": STEAM_ID, "gameName": "Portal 2", "success": True,
    "achievements": [
        {"apiname": "ACH_WIN_ONE_GAME", "achieved": 1, "unlocktime": 1762030000,
         "name": "First Win", "description": "Win your first game"},
        {"apiname": "ACH_WIN_TEN_GAMES", "achieved": 0, "unlocktime": 0,
         "name": "Win 10 games", "description": ""},
    ],
}}
SCHEMA = {"game": {"gameName": "Portal 2", "gameVersion": "1", "availableGameStats": {
    "achievements": [
        {"name": "ACH_WIN_ONE_GAME", "defaultvalue": 0, "displayName": "First Win",
         "hidden": 0, "description": "Win your first game",
         "icon": "https://cdn.example/620/first.jpg", "icongray": "https://cdn.example/620/g.jpg"},
    ],
}}}


def routes(**overrides) -> dict:
    r = {
        "ISteamUser/GetPlayerSummaries/v2/": SUMMARIES,
        "ISteamUserStats/GetPlayerAchievements/v1/": PLAYER_ACHIEVEMENTS,
        "ISteamUserStats/GetSchemaForGame/v2/": SCHEMA,
    }
    r.update({k.replace("__", "/"): v for k, v in overrides.items()})
    return r


def make_api(session: FakeSession, **kwargs) -> SteamWebApi:
    return SteamWebApi(api_key="STEAMKEY", steam_id=STEAM_ID,
                       session_factory=lambda: session, **kwargs)


async def test_web_api_sync_writes_sidecar_read_by_source(tmp_path: Path):
    session = FakeSession(routes())
    written = await make_api(session).sync_once(tmp_path)

    path = tmp_path / STEAM_ID / "stats" / "620_achievements.json"
    assert written == [path]
    doc = json.loads(path.read_text())
    first = doc["achievements"][0]
    assert first == {
        "id": "ACH_WIN_ONE_GAME", "name": "First Win", "description": "Win your first game",
        "unlocked": True, "unlock_time": 1762030000,
        "icon_url": "https://cdn.example/620/first.jpg", "game_name": "Portal 2", "app_id": 620,
    }
    assert doc["achievements"][1]["unlocked"] is False

    for url, params in session.calls:
        assert url.startswith("https://api.steampowered.com/")
        assert params["key"] == "STEAMKEY"

    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    await SteamSource(userdata_dir=tmp_path).poll_once(queue)
    [ach] = drain(queue)
    assert (ach.achievement_name, ach.game_id, ach.icon_url) == (
        "First Win", "620", "https://cdn.example/620/first.jpg")


async def test_web_api_private_profile_writes_nothing(tmp_path: Path):
    session = FakeSession(routes(**{"ISteamUserStats__GetPlayerAchievements/v1/": {
        "playerstats": {"error": "Profile is not public", "success": False}}}))
    assert await make_api(session).sync_once(tmp_path) == []
    assert not any(tmp_path.rglob("*.json"))


async def test_web_api_network_error_does_not_raise_or_leak_key(tmp_path: Path, caplog):
    err = aiohttp.ClientConnectionError("https://api.steampowered.com/x?key=STEAMKEY")
    session = FakeSession({"ISteamUser/GetPlayerSummaries/v2/": err})
    with caplog.at_level(logging.DEBUG):
        assert await make_api(session).sync_once(tmp_path) == []
    assert "STEAMKEY" not in caplog.text


async def test_web_api_configured_app_ids_and_shortcut_gameids(tmp_path: Path):
    summaries = {"response": {"players": [{"steamid": STEAM_ID,
                                           "gameid": "13832560934289752064"}]}}
    session = FakeSession(routes(**{"ISteamUser__GetPlayerSummaries/v2/": summaries}))
    await make_api(session, app_ids=[730]).sync_once(tmp_path)
    appids = {p["appid"] for url, p in session.calls if "GetPlayerAchievements" in url}
    assert appids == {730}  # non-Steam shortcut gameid (> uint32) is ignored


async def test_web_api_caches_schema_and_skips_unchanged_writes(tmp_path: Path):
    session = FakeSession(routes())
    api = make_api(session)
    assert len(await api.sync_once(tmp_path)) == 1
    assert await api.sync_once(tmp_path) == []  # same content: no rewrite
    schema_calls = [u for u, _ in session.calls if "GetSchemaForGame" in u]
    assert len(schema_calls) == 1


async def test_web_api_unconfigured_does_nothing(tmp_path: Path):
    session = FakeSession(routes())
    api = SteamWebApi(session_factory=lambda: session)
    assert not api.configured
    assert await api.sync_once(tmp_path) == []
    assert session.calls == []
