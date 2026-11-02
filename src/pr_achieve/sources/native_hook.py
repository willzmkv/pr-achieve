from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from pr_achieve.sources import Achievement

log = logging.getLogger(__name__)

DEFAULT_HOOK_DIR = "/run/project-r/achieve-hook"
MAX_HOOK_FILE_BYTES = 64 * 1024


class NativeHookSource:
    """
    Watches /run/project-r/achieve-hook/ for JSON files written by native ports.

    Protocol (full contract in README): a native port writes
        {"game_id":"sm64","achievement":"Name","points":10,"description":"..."}
    to a temporary name (not ending in .json) and renames it to
    achieve-hook/<game_id>_<timestamp_ns>.json.

    This source processes each file and deletes it after reading.
    """

    def __init__(self, hook_dir: Path | None = None, poll_interval: float = 2.0) -> None:
        self._hook_dir = hook_dir or Path(DEFAULT_HOOK_DIR)
        self._poll_interval = poll_interval

    async def process_file(self, path: Path, queue: asyncio.Queue[Achievement]) -> None:
        """Parse one hook JSON file, emit Achievement, then delete the file."""
        try:
            if path.stat().st_size > MAX_HOOK_FILE_BYTES:
                raise ValueError(f"larger than {MAX_HOOK_FILE_BYTES} bytes")
            data = json.loads(path.read_text(encoding="utf-8"))
            await queue.put(_parse_payload(data))
        except FileNotFoundError:
            return  # already consumed (startup scan and inotify both saw it)
        except Exception as exc:
            log.warning("Could not parse hook file %s: %s", path, exc)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    async def scan_existing(self, queue: asyncio.Queue[Achievement]) -> None:
        """Process any JSON files already present in hook_dir at startup."""
        for f in sorted(self._hook_dir.glob("*.json")):
            if _is_hook_file(f.name):
                await self.process_file(f, queue)

    async def run(self, queue: asyncio.Queue[Achievement]) -> None:
        """
        Watch hook_dir with inotify (IN_CLOSE_WRITE | IN_MOVED_TO). Falls back to
        polling if inotify is unavailable. The watch is added before the startup
        scan so no file can slip in between.
        """
        try:
            self._hook_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Cannot create hook dir %s: %s", self._hook_dir, exc)

        try:
            import inotify_simple  # type: ignore[import-not-found]

            inotify = inotify_simple.INotify()
        except (ImportError, OSError) as exc:
            log.warning("inotify unavailable (%s); polling %s", exc, self._hook_dir)
            await self._poll_loop(queue)
            return

        flags = inotify_simple.flags
        try:
            inotify.add_watch(str(self._hook_dir), flags.CLOSE_WRITE | flags.MOVED_TO)
        except OSError as exc:
            inotify.close()
            log.warning("Cannot watch %s (%s); polling instead", self._hook_dir, exc)
            await self._poll_loop(queue)
            return

        # Non-blocking: wake on fd readability instead of parking a thread in
        # inotify.read(), which would block executor shutdown on SIGTERM.
        loop = asyncio.get_running_loop()
        readable = asyncio.Event()
        fd = inotify.fileno()
        loop.add_reader(fd, readable.set)
        try:
            await self.scan_existing(queue)
            while True:
                await readable.wait()
                readable.clear()
                for event in inotify.read(timeout=0):
                    if event.mask & flags.Q_OVERFLOW:
                        await self.scan_existing(queue)
                    elif _is_hook_file(event.name):
                        await self.process_file(self._hook_dir / event.name, queue)
        finally:
            loop.remove_reader(fd)
            inotify.close()

    async def _poll_loop(self, queue: asyncio.Queue[Achievement]) -> None:
        while True:
            try:
                await self.scan_existing(queue)
            except OSError as exc:
                log.debug("Hook dir scan failed: %s", exc)
            await asyncio.sleep(self._poll_interval)


def _is_hook_file(name: str) -> bool:
    return name.endswith(".json") and not name.startswith(".")


def _parse_payload(data: Any) -> Achievement:
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    game_id = data.get("game_id")
    name = data.get("achievement")
    if not isinstance(game_id, (str, int)) or not str(game_id).strip():
        raise ValueError("'game_id' must be a non-empty string")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("'achievement' must be a non-empty string")
    try:
        points = int(data.get("points") or 0)
    except (TypeError, ValueError):
        points = 0
    icon_url = data.get("icon_url")
    return Achievement(
        game_id=str(game_id),
        game_name=str(data.get("game_name") or game_id),
        achievement_name=name,
        points=points,
        description=str(data.get("description") or ""),
        source="native_hook",
        icon_url=icon_url if isinstance(icon_url, str) and icon_url else None,
    )
