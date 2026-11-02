from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import aiohttp

from pr_achieve.sources import Achievement
from pr_achieve.sources._proc import PROC_ROOT, process_running

log = logging.getLogger(__name__)

_POLL_INTERVAL = 10.0
DEFAULT_SIDECAR_DIR = "~/.local/state/pr-achieve/steam"
STEAM_API_URL = "https://api.steampowered.com"


class SteamSource:
    """Watches ``<root>/<id>/stats/*_achievements.json`` sidecars for new unlocks.

    Steam's own stats cache is binary, so pr-achieve consumes a JSON sidecar per
    app (format in README). The sidecars are produced by :class:`SteamWebApi`
    when Steam Web API credentials are configured; any other tool may write them.
    """

    def __init__(
        self,
        userdata_dir: Path | None = None,
        web_api: SteamWebApi | None = None,
        poll_interval: float = _POLL_INTERVAL,
        process_names: Iterable[str] = ("steam",),
        proc_root: Path = PROC_ROOT,
    ) -> None:
        if userdata_dir is None:
            userdata_dir = Path(DEFAULT_SIDECAR_DIR).expanduser()
        self._userdata_dir = userdata_dir
        self._web_api = web_api
        self._poll_interval = poll_interval
        self._process_names = tuple(process_names)
        self._proc_root = proc_root
        self._seen_keys: set[str] = set()  # "{app_id}:{achievement_id}"
        self._not_before: float | None = None  # set by run(): daemon start time

    def _iter_achievement_files(self) -> Iterator[Path]:
        """Yield all *_achievements.json files under any userdata sub-directory."""
        for stats_dir in sorted(self._userdata_dir.glob("*/stats")):
            yield from sorted(stats_dir.glob("*_achievements.json"))

    async def poll_once(self, queue: asyncio.Queue[Achievement], *, emit: bool = True) -> None:
        for ach_file in self._iter_achievement_files():
            try:
                data = json.loads(ach_file.read_text(encoding="utf-8"))
            except Exception as exc:
                log.debug("Could not read %s: %s", ach_file, exc)
                continue
            if not isinstance(data, dict):
                continue

            for item in data.get("achievements", []):
                if not isinstance(item, dict) or not item.get("unlocked") or "id" not in item:
                    continue
                app_id = item.get("app_id", ach_file.stem.split("_")[0])
                key = f"{app_id}:{item['id']}"
                if key in self._seen_keys:
                    continue
                self._seen_keys.add(key)
                if not emit or self._unlocked_before_start(item):
                    continue
                await queue.put(Achievement(
                    game_id=str(app_id),
                    game_name=item.get("game_name") or data.get("game_name") or "Unknown Game",
                    achievement_name=item.get("name") or str(item["id"]),
                    points=_as_int(item.get("points")),
                    description=item.get("description") or "",
                    source="steam",
                    icon_url=item.get("icon_url"),
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
        api = self._web_api if self._web_api and self._web_api.configured else None
        if api is None:
            log.info("steam_api_key/steam_id not set; Steam source only reads sidecar files")
        while True:
            if api is not None and process_running(self._process_names, self._proc_root):
                await api.sync_once(self._userdata_dir)
            await self.poll_once(queue)
            await asyncio.sleep(self._poll_interval)


class SteamWebApi:
    """Writes ``<root>/<steamid64>/stats/<appid>_achievements.json`` sidecars.

    Uses the Steam Web API: ISteamUser/GetPlayerSummaries/v2 (current ``gameid``;
    needs public "game details"), ISteamUserStats/GetPlayerAchievements/v1 and
    ISteamUserStats/GetSchemaForGame/v2 (icons; cached per app). Apps listed in
    ``app_ids`` are always synced.
    """

    def __init__(
        self,
        api_key: str = "",
        steam_id: str = "",
        app_ids: Iterable[int] = (),
        language: str = "english",
        api_url: str = STEAM_API_URL,
        session_factory: Callable[[], Any] = aiohttp.ClientSession,
    ) -> None:
        self._api_key = api_key
        self._steam_id = str(steam_id)
        self._app_ids = {int(a) for a in app_ids}
        self._language = language
        self._api_url = api_url.rstrip("/")
        self._session_factory = session_factory
        self._schema_cache: dict[int, dict[str, dict[str, Any]]] = {}

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._steam_id)

    async def sync_once(self, out_root: Path) -> list[Path]:
        """Refresh sidecars for the running game (+ configured apps).

        Returns the sidecar paths whose content changed. Never raises on
        network/API errors.
        """
        if not self.configured:
            return []
        written: list[Path] = []
        async with self._session_factory() as session:
            apps = await self._current_apps(session)
            for app_id in sorted(apps):
                doc = await self._fetch_app(session, app_id, apps[app_id])
                if doc is None:
                    continue
                path = out_root / self._steam_id / "stats" / f"{app_id}_achievements.json"
                if _write_json_if_changed(path, doc):
                    written.append(path)
        return written

    async def _get_json(self, session: Any, endpoint: str, params: dict[str, Any]) -> Any:
        url = f"{self._api_url}/{endpoint}"
        try:
            async with session.get(
                url, params={"key": self._api_key, **params},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.debug("Steam %s returned HTTP %s", endpoint, resp.status)
                    return None
                return await resp.json(content_type=None)
        except Exception as exc:
            # Exception text may include the URL with the key: log type only.
            log.debug("Steam %s failed: %s", endpoint, type(exc).__name__)
            return None

    async def _current_apps(self, session: Any) -> dict[int, str | None]:
        apps: dict[int, str | None] = {a: None for a in self._app_ids}
        data = await self._get_json(
            session, "ISteamUser/GetPlayerSummaries/v2/", {"steamids": self._steam_id}
        )
        players = ((data or {}).get("response") or {}).get("players") or []
        for player in players:
            game_id = str(player.get("gameid") or "")
            # Non-Steam shortcuts report 64-bit ids that have no achievements.
            if game_id.isdigit() and int(game_id) < 2**32:
                apps[int(game_id)] = player.get("gameextrainfo")
        return apps

    async def _schema(self, session: Any, app_id: int) -> dict[str, dict[str, Any]]:
        if app_id in self._schema_cache:
            return self._schema_cache[app_id]
        data = await self._get_json(
            session, "ISteamUserStats/GetSchemaForGame/v2/",
            {"appid": app_id, "l": self._language},
        )
        if data is None:
            return {}
        stats = ((data.get("game") or {}).get("availableGameStats") or {})
        schema = {
            a["name"]: a for a in stats.get("achievements") or []
            if isinstance(a, dict) and "name" in a
        }
        self._schema_cache[app_id] = schema
        return schema

    async def _fetch_app(
        self, session: Any, app_id: int, name_hint: str | None
    ) -> dict[str, Any] | None:
        data = await self._get_json(
            session, "ISteamUserStats/GetPlayerAchievements/v1/",
            {"steamid": self._steam_id, "appid": app_id, "l": self._language},
        )
        stats = (data or {}).get("playerstats") or {}
        if not stats.get("success"):
            if stats.get("error"):
                log.info("Steam achievements for app %s unavailable: %s", app_id, stats["error"])
            return None
        schema = await self._schema(session, app_id)
        game_name = stats.get("gameName") or name_hint or str(app_id)
        achievements = []
        for entry in stats.get("achievements") or []:
            api_name = entry.get("apiname") if isinstance(entry, dict) else None
            if not api_name:
                continue
            meta = schema.get(api_name, {})
            achievements.append({
                "id": api_name,
                "name": entry.get("name") or meta.get("displayName") or api_name,
                "description": entry.get("description") or meta.get("description") or "",
                "unlocked": bool(entry.get("achieved")),
                "unlock_time": _as_int(entry.get("unlocktime")) or None,
                "icon_url": meta.get("icon"),
                "game_name": game_name,
                "app_id": app_id,
            })
        return {"app_id": app_id, "game_name": game_name, "achievements": achievements}


def _write_json_if_changed(path: Path, doc: dict[str, Any]) -> bool:
    text = json.dumps(doc, indent=1, sort_keys=True)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace so SteamSource never reads a half-written file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
