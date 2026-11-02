from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable

from pr_achieve.config import Config, OverlayPosition
from pr_achieve.sources import Achievement

log = logging.getLogger(__name__)

DisplayFn = Callable[[Achievement], Awaitable[None]]


class OverlayQueue:
    """
    Sequential achievement display queue.

    Accepts Achievement items via enqueue(); drain() processes them
    one-at-a-time in order, calling display_fn for each and awaiting it.
    display_fn is responsible for the full show cycle (slide-in, hold, slide-out).
    """

    def __init__(self, config: Config, display_fn: DisplayFn) -> None:
        self._config = config
        self._display_fn = display_fn
        self._queue: asyncio.Queue[Achievement] = asyncio.Queue()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def enqueue(self, achievement: Achievement) -> None:
        await self._queue.put(achievement)

    async def drain(self) -> None:
        """Process all queued achievements sequentially."""
        while not self._queue.empty():
            await self._show(self._queue.get_nowait())

    async def run(self, source_queue: asyncio.Queue[Achievement]) -> None:
        """
        Long-running loop: consume from source_queue and display sequentially.
        This is the coroutine started by daemon.py.
        """
        while True:
            await self._show(await source_queue.get())

    async def _show(self, ach: Achievement) -> None:
        try:
            await self._display_fn(ach)
        except Exception:
            log.exception("display_fn raised for %s", ach.achievement_name)


# ---------------------------------------------------------------------------
# PyQt6 overlay window — PyQt6 is imported lazily so the queue logic and the
# sources can be used (and tested) without it.
# ---------------------------------------------------------------------------

def build_stylesheet(config: Config) -> str:
    t = config.theme
    return f"""
        QWidget#overlay {{
            background-color: {t.bg_color};
            border-radius: 12px;
            border: 1.5px solid {t.accent_color};
        }}
        QLabel#game_name {{
            color: {t.subtext_color};
            font-family: {t.font_family};
            font-size: 11px;
            font-weight: 400;
        }}
        QLabel#achievement_name {{
            color: {t.text_color};
            font-family: {t.font_family};
            font-size: 14px;
            font-weight: 700;
        }}
        QLabel#points {{
            color: {t.accent_color};
            font-family: {t.font_family};
            font-size: 12px;
            font-weight: 600;
        }}
        QLabel#description {{
            color: {t.subtext_color};
            font-family: {t.font_family};
            font-size: 11px;
            font-weight: 400;
        }}
    """


def _make_overlay_window(config: Config):
    """
    Construct and return an OverlayWindow (PyQt6), creating the QApplication
    if none exists yet. Must be called on the GUI (main) thread.
    """
    import sys

    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = _overlay_window_class()(config)
    window._qapp = app  # keep the application alive as long as the window
    return window


@functools.cache
def _overlay_window_class():
    from PyQt6.QtCore import (
        QEasingCurve, QPoint, QPropertyAnimation, QSize, Qt, QTimer, pyqtSignal,
    )
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget

    class OverlayWindow(QWidget):
        WINDOW_WIDTH = 380
        WINDOW_HEIGHT = 88
        MARGIN = 16

        # (achievement, done_callback, icon_path); may be emitted from any thread
        show_requested = pyqtSignal(object, object, object)

        def __init__(self, cfg: Config) -> None:
            super().__init__()
            self._cfg = cfg
            self._done_callback: Callable[[], None] | None = None
            self._setup_window()
            self._setup_layout()
            self._setup_animations()
            self.show_requested.connect(
                self.show_achievement, Qt.ConnectionType.QueuedConnection
            )

        def request_show(self, ach: Achievement, done_callback=None, icon_path=None) -> None:
            """Thread-safe entry point: the show runs on the GUI thread."""
            self.show_requested.emit(ach, done_callback, icon_path)

        def _screen_geometry(self):
            screen = QApplication.primaryScreen()
            return screen.availableGeometry() if screen else None

        def _setup_window(self) -> None:
            self.setObjectName("overlay")
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowTransparentForInput
                | Qt.WindowType.WindowDoesNotAcceptFocus  # never steal game focus
                | Qt.WindowType.X11BypassWindowManagerHint  # stay above fullscreen on X11
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            # A plain QWidget only paints its QSS background with this set;
            # without it the card is invisible on a translucent window.
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setFixedSize(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)
            self.setStyleSheet(build_stylesheet(self._cfg))

        def _setup_layout(self) -> None:
            root = QHBoxLayout(self)
            root.setContentsMargins(12, 12, 12, 12)
            root.setSpacing(12)

            self._icon_label = QLabel()
            self._icon_label.setFixedSize(
                QSize(self._cfg.theme.icon_size, self._cfg.theme.icon_size)
            )
            self._icon_label.setScaledContents(True)
            root.addWidget(self._icon_label)

            text_col = QVBoxLayout()
            text_col.setSpacing(2)

            self._game_label = QLabel()
            self._game_label.setObjectName("game_name")
            text_col.addWidget(self._game_label)

            self._ach_label = QLabel()
            self._ach_label.setObjectName("achievement_name")
            text_col.addWidget(self._ach_label)

            self._desc_label = QLabel()
            self._desc_label.setObjectName("description")
            text_col.addWidget(self._desc_label)

            self._pts_label = QLabel()
            self._pts_label.setObjectName("points")
            text_col.addWidget(self._pts_label)

            root.addLayout(text_col, 1)

        def _setup_animations(self) -> None:
            self._anim_in = QPropertyAnimation(self, b"pos", self)
            self._anim_in.setDuration(self._cfg.slide_duration_ms)
            self._anim_in.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._anim_in.finished.connect(self._on_slide_in_done)

            self._anim_out = QPropertyAnimation(self, b"pos", self)
            self._anim_out.setDuration(self._cfg.slide_duration_ms)
            self._anim_out.setEasingCurve(QEasingCurve.Type.InCubic)
            self._anim_out.finished.connect(self._on_slide_out_done)

            self._hold_timer = QTimer(self)
            self._hold_timer.setSingleShot(True)
            self._hold_timer.timeout.connect(self._start_slide_out)

        def _position_for(self) -> tuple[QPoint, QPoint]:
            """Return (on_screen_pos, off_screen_pos) based on config position."""
            geo = self._screen_geometry()
            if geo is None:
                return QPoint(0, 0), QPoint(-self.WINDOW_WIDTH, 0)

            margin = self.MARGIN
            pos = self._cfg.position
            top = geo.top() + margin
            bottom = geo.bottom() - self.WINDOW_HEIGHT - margin
            right_on = geo.right() - self.WINDOW_WIDTH - margin
            left_on = geo.left() + margin

            if pos == OverlayPosition.TOP_RIGHT:
                return QPoint(right_on, top), QPoint(geo.right() + 10, top)
            if pos == OverlayPosition.TOP_LEFT:
                return QPoint(left_on, top), QPoint(geo.left() - self.WINDOW_WIDTH - 10, top)
            if pos == OverlayPosition.BOTTOM_RIGHT:
                return QPoint(right_on, bottom), QPoint(geo.right() + 10, bottom)
            return QPoint(left_on, bottom), QPoint(geo.left() - self.WINDOW_WIDTH - 10, bottom)

        def show_achievement(self, ach: Achievement, done_callback=None, icon_path=None) -> None:
            """Populate labels and start slide-in animation (GUI thread only)."""
            if self._done_callback is not None:
                self._finish()  # interrupted cycle: release its waiter first
            self._anim_in.stop()
            self._anim_out.stop()
            self._hold_timer.stop()
            self._done_callback = done_callback

            self._set_icon(icon_path)
            self._game_label.setText(ach.game_name.upper())
            self._set_elided(self._ach_label, ach.achievement_name)
            self._set_elided(self._desc_label, ach.description)
            self._pts_label.setText(f"+{ach.points} pts" if ach.points else "")

            on_pos, off_pos = self._position_for()
            self.move(off_pos)
            self.show()
            self.raise_()

            self._anim_in.setStartValue(off_pos)
            self._anim_in.setEndValue(on_pos)
            self._anim_in.start()

        def _set_icon(self, icon_path) -> None:
            pixmap = QPixmap(str(icon_path)) if icon_path else None
            if pixmap is not None and not pixmap.isNull():
                self._icon_label.setPixmap(pixmap)
                self._icon_label.show()
            else:
                self._icon_label.clear()
                self._icon_label.hide()

        def _set_elided(self, label, text: str) -> None:
            # Fixed-height card: elide instead of wrapping into clipped lines.
            available = self.WINDOW_WIDTH - 24
            if not self._icon_label.isHidden():
                available -= self._cfg.theme.icon_size + 12
            label.ensurePolished()
            label.setText(label.fontMetrics().elidedText(
                text, Qt.TextElideMode.ElideRight, available
            ))

        def _on_slide_in_done(self) -> None:
            self._hold_timer.start(int(self._cfg.hold_seconds * 1000))

        def _start_slide_out(self) -> None:
            _, off_pos = self._position_for()
            self._anim_out.setStartValue(self.pos())
            self._anim_out.setEndValue(off_pos)
            self._anim_out.start()

        def _on_slide_out_done(self) -> None:
            self._finish()

        def _finish(self) -> None:
            self.hide()
            callback, self._done_callback = self._done_callback, None
            if callback is not None:
                try:
                    callback()
                except Exception:
                    log.exception("overlay done_callback raised")

    return OverlayWindow
