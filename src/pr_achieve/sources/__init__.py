from dataclasses import dataclass
from typing import Optional


@dataclass
class Achievement:
    game_id: str
    game_name: str
    achievement_name: str
    points: int
    description: str
    source: str  # "retroarch" | "steam" | "native_hook" | "heroic"
    icon_url: Optional[str] = None
