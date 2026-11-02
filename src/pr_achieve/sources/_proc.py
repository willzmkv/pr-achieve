from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

PROC_ROOT = Path("/proc")


def process_running(names: Iterable[str], proc_root: Path = PROC_ROOT) -> bool:
    """True if any process whose /proc/<pid>/comm matches one of ``names`` exists.

    Used to avoid polling remote APIs while the relevant client is not running.
    If /proc cannot be read (non-Linux, sandbox), returns True so polling is
    never blocked by a failed check.
    """
    wanted = {n.strip().lower()[:15] for n in names}  # comm is truncated to 15 chars
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip().lower()
        except OSError:
            continue
        if comm in wanted:
            return True
    return False
