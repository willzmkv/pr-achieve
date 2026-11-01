# pr-achieve

> Project-R achievement overlay — displays achievements from RetroAchievements, Steam, Heroic, and native hooks.

**Language:** Python 3.11+ · **Status:** Pre-alpha · [Implementation Plan](https://github.com/willzmkv/project-r-os/blob/main/ROADMAP.md#pr-achieve--achievement-overlay)

## What it does

Monitors multiple achievement sources concurrently and displays a frameless, always-on-top PyQt6 overlay with slide-in animation. Inspired by Steam Achievement Notifier.

## Sources

| Source | Method |
|---|---|
| RetroAchievements | Polls RetroArch HTTP API at localhost:55355 |
| Steam | Watches userdata `*_achievements.json` files |
| Native hooks | inotify on `/run/project-r/achieve-hook/` |
| Heroic/GOG/Epic | Polls Heroic achievement JSON files |

## Overlay

- Frameless, transparent, always-on-top PyQt6 window
- QPropertyAnimation slide-in/hold/slide-out
- Project-R palette: `#1a1a2e` bg, `#4ecdc4` accent

## Architecture

- `achievement.py` — Achievement dataclass + Config
- `sources/` — RetroArchSource, SteamSource, NativeHookSource, HeroicSource
- `overlay_queue.py` — sequential asyncio display queue
- `overlay_window.py` — PyQt6 frameless animated window
- `daemon.py` — asyncio main loop

## License

GPL-3.0
