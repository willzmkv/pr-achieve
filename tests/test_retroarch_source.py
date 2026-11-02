import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from pr_achieve.sources import Achievement
from pr_achieve.sources._proc import process_running
from pr_achieve.sources.retroarch import RetroArchSource


def test_achievement_fields():
    a = Achievement(
        game_id="sm64",
        game_name="Super Mario 64",
        achievement_name="Big Bob-omb on the Summit",
        points=10,
        description="Defeat Big Bob-omb",
        icon_url=None,
        source="native_hook",
    )
    assert a.game_id == "sm64"
    assert a.points == 10
    assert a.source == "native_hook"


# Shape of API_GetUserRecentAchievements.php as documented at
# https://api-docs.retroachievements.org/v1/get-user-recent-achievements.html
# (newest first).
def ra_item(ach_id: int, title: str, date: str, hardcore: int = 1, points: int = 10) -> dict:
    return {
        "Date": date,
        "HardcoreMode": hardcore,
        "AchievementID": ach_id,
        "Title": title,
        "Description": f"{title} description",
        "BadgeName": f"{ach_id}0",
        "Points": points,
        "TrueRatio": 25,
        "Type": None,
        "Author": "jos",
        "AuthorULID": "00003EMFWR7XB8SDPEHB3K56ZQ",
        "GameTitle": "Super Mario 64",
        "GameIcon": "/Images/028399.png",
        "GameID": 7346,
        "ConsoleName": "Nintendo 64",
        "BadgeURL": f"/Badge/{ach_id}0.png",
        "GameURL": "/game/7346",
    }


MOCK_RECENT = [ra_item(1001, "Big Bob-omb on the Summit", "2026-11-01 20:00:10")]


def mock_response(payload, status: int = 200) -> AsyncMock:
    mock_resp = AsyncMock()
    mock_resp.json = AsyncMock(return_value=payload)
    mock_resp.status = status
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


def make_source(**kwargs) -> RetroArchSource:
    kwargs.setdefault("username", "player1")
    kwargs.setdefault("api_key", "TESTKEY")
    return RetroArchSource(**kwargs)


async def test_retroarch_detects_new_unlock():
    source = make_source()
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.return_value = mock_response(MOCK_RECENT)
        assert await source.poll_once(queue) is True

    assert queue.qsize() == 1
    ach = queue.get_nowait()
    assert ach.achievement_name == "Big Bob-omb on the Summit"
    assert ach.points == 10
    assert ach.source == "retroarch"
    assert ach.game_name == "Super Mario 64"
    assert ach.game_id == "7346"
    assert ach.description == "Big Bob-omb on the Summit description"
    assert ach.icon_url == "https://media.retroachievements.org/Badge/10010.png"

    url = mock_get.call_args.args[0]
    params = mock_get.call_args.kwargs["params"]
    assert url == "https://retroachievements.org/API/API_GetUserRecentAchievements.php"
    assert params == {"u": "player1", "y": "TESTKEY", "m": "10"}


async def test_retroarch_no_duplicate_notifications():
    source = make_source()
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.return_value = mock_response(MOCK_RECENT)
        await source.poll_once(queue)
        await source.poll_once(queue)  # second poll — same data

    assert queue.qsize() == 1  # only one notification, not two


async def test_retroarch_connection_error_does_not_raise():
    source = make_source()
    queue: asyncio.Queue[Achievement] = asyncio.Queue()

    with patch("aiohttp.ClientSession.get", side_effect=aiohttp.ClientConnectionError):
        assert await source.poll_once(queue) is False  # must not raise

    assert queue.qsize() == 0


async def test_retroarch_error_logs_never_contain_api_key(caplog):
    source = make_source(api_key="SUPERSECRET")
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    err = aiohttp.ClientConnectionError("GET https://retroachievements.org/API/x?y=SUPERSECRET")
    with caplog.at_level(logging.DEBUG), patch("aiohttp.ClientSession.get", side_effect=err):
        await source.poll_once(queue)
    assert "SUPERSECRET" not in caplog.text


async def test_retroarch_http_error_status_is_ignored():
    source = make_source()
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.return_value = mock_response({"message": "Unauthenticated."}, status=401)
        assert await source.poll_once(queue) is False
    assert queue.qsize() == 0


async def test_retroarch_unconfigured_makes_no_request():
    source = RetroArchSource()
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    with patch("aiohttp.ClientSession.get") as mock_get:
        assert await source.poll_once(queue) is False
    mock_get.assert_not_called()
    assert not source.configured


async def test_retroarch_emits_oldest_first():
    newest_first = [
        ra_item(2, "Second", "2026-11-01 20:05:00"),
        ra_item(1, "First", "2026-11-01 20:01:00"),
    ]
    source = make_source()
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.return_value = mock_response(newest_first)
        await source.poll_once(queue)
    assert [queue.get_nowait().achievement_name for _ in range(2)] == ["First", "Second"]


async def test_retroarch_baseline_poll_does_not_emit():
    source = make_source()
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.return_value = mock_response(MOCK_RECENT)
        await source.poll_once(queue, emit=False)
        await source.poll_once(queue)
        assert queue.qsize() == 0
        mock_get.return_value = mock_response(
            [ra_item(1002, "Footrace with Koopa the Quick", "2026-11-01 20:09:00")] + MOCK_RECENT
        )
        await source.poll_once(queue)
    assert queue.qsize() == 1
    assert queue.get_nowait().achievement_name == "Footrace with Koopa the Quick"


async def test_retroarch_hardcore_unlock_after_softcore_is_new():
    source = make_source()
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    payload = [
        ra_item(1001, "Big Bob-omb on the Summit", "2026-11-01 20:03:00", hardcore=1),
        ra_item(1001, "Big Bob-omb on the Summit", "2026-11-01 20:00:00", hardcore=0),
    ]
    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.return_value = mock_response(payload)
        await source.poll_once(queue)
    assert queue.qsize() == 2


async def test_retroarch_run_baselines_then_polls_only_while_retroarch_runs(tmp_path: Path):
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    comm = proc / "4242" / "comm"
    comm.write_text("bash\n")

    source = make_source(poll_interval=0.01, proc_root=proc)
    queue: asyncio.Queue[Achievement] = asyncio.Queue()
    later = [ra_item(1002, "Footrace with Koopa the Quick", "2026-11-01 20:09:00")] + MOCK_RECENT

    with patch("aiohttp.ClientSession.get") as mock_get:
        mock_get.return_value = mock_response(MOCK_RECENT)
        task = asyncio.create_task(source.run(queue))
        await asyncio.sleep(0.1)
        assert mock_get.call_count == 1  # only the startup baseline: no retroarch process
        mock_get.return_value = mock_response(later)
        comm.write_text("retroarch\n")
        ach = await asyncio.wait_for(queue.get(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert ach.achievement_name == "Footrace with Koopa the Quick"
    assert queue.empty()  # the baseline unlock was never replayed


async def test_retroarch_run_without_credentials_returns():
    await asyncio.wait_for(RetroArchSource().run(asyncio.Queue()), timeout=1)


def test_process_running(tmp_path: Path):
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "comm").write_text("systemd\n")
    (tmp_path / "77").mkdir()
    (tmp_path / "77" / "comm").write_text("retroarch\n")
    (tmp_path / "self").mkdir()
    assert process_running(["retroarch"], proc_root=tmp_path)
    assert not process_running(["steam"], proc_root=tmp_path)
    # No /proc (non-Linux): cannot tell, so do not block polling.
    assert process_running(["steam"], proc_root=tmp_path / "missing")
