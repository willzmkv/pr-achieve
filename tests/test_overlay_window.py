"""Headless tests for the PyQt6 overlay (run with QT_QPA_PLATFORM=offscreen)."""
import os
import threading
import time
from pathlib import Path

import pytest

from pr_achieve.config import Config, OverlayPosition, Theme
from pr_achieve.overlay import build_stylesheet
from pr_achieve.sources import Achievement

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QCoreApplication, QEventLoop, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from pr_achieve.overlay import _make_overlay_window  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(["pr-achieve-tests"])
    yield app


def fast_config(**kwargs) -> Config:
    kwargs.setdefault("hold_seconds", 0.05)
    kwargs.setdefault("slide_duration_ms", 30)
    return Config(**kwargs)


def make_achievement(**kwargs) -> Achievement:
    data = dict(game_id="sm64", game_name="Super Mario 64",
                achievement_name="Big Bob-omb on the Summit", points=10,
                description="Defeat Big Bob-omb", source="native_hook")
    data.update(kwargs)
    return Achievement(**data)


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(0.002)
    return predicate()


def test_build_stylesheet_uses_theme_colours():
    css = build_stylesheet(Config(theme=Theme(bg_color="#101010", accent_color="#f5a623")))
    assert "background-color: #101010" in css
    assert "#f5a623" in css
    assert "QLabel#achievement_name" in css


def test_window_flags_and_attributes(qapp):
    w = _make_overlay_window(fast_config())
    flags = w.windowFlags()
    for flag in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
                 Qt.WindowType.WindowTransparentForInput,
                 Qt.WindowType.WindowDoesNotAcceptFocus,
                 Qt.WindowType.X11BypassWindowManagerHint):
        assert flags & flag, flag
    for attr in (Qt.WidgetAttribute.WA_TranslucentBackground,
                 Qt.WidgetAttribute.WA_StyledBackground,
                 Qt.WidgetAttribute.WA_ShowWithoutActivating):
        assert w.testAttribute(attr), attr
    assert (w.width(), w.height()) == (w.WINDOW_WIDTH, w.WINDOW_HEIGHT)


@pytest.mark.parametrize("position", list(OverlayPosition))
def test_positions_slide_from_outside_the_screen(qapp, position):
    w = _make_overlay_window(fast_config(position=position))
    geo = QApplication.primaryScreen().availableGeometry()
    on, off = w._position_for()
    assert geo.contains(on) and geo.contains(on.x() + w.WINDOW_WIDTH - 1, on.y())
    assert on.y() == off.y()
    if position.value.endswith("right"):
        assert off.x() > geo.right() and on.x() + w.WINDOW_WIDTH < geo.right()
    else:
        assert off.x() + w.WINDOW_WIDTH < geo.left() and on.x() > geo.left()
    if position.value.startswith("top"):
        assert on.y() < geo.center().y()
    else:
        assert on.y() > geo.center().y()


def test_full_show_cycle_calls_done_once_and_hides(qapp):
    w = _make_overlay_window(fast_config())
    done: list[float] = []
    w.show_achievement(make_achievement(), done_callback=lambda: done.append(time.monotonic()))

    assert w.isVisible()
    assert w._game_label.text() == "SUPER MARIO 64"
    assert w._ach_label.text() == "Big Bob-omb on the Summit"
    assert w._pts_label.text() == "+10 pts"
    assert wait_until(lambda: w.pos() == w._position_for()[0])  # slid in
    assert wait_until(lambda: bool(done))
    assert not w.isVisible()
    assert w.pos() == w._position_for()[1]  # slid back out
    wait_until(lambda: False, timeout=0.2)
    assert len(done) == 1


def test_background_is_actually_painted(qapp):
    w = _make_overlay_window(fast_config(hold_seconds=5))
    w.show_achievement(make_achievement())
    assert wait_until(lambda: w.pos() == w._position_for()[0])
    img = w.grab().toImage()
    pixel = img.pixelColor(6, w.WINDOW_HEIGHT // 2)  # inside the card margin, no text
    assert pixel.alpha() == 255
    assert pixel.name() == Config().theme.bg_color
    corner = img.pixelColor(0, 0)  # outside the rounded corner stays transparent
    assert corner.alpha() < 255
    w.hide()


def test_request_show_is_thread_safe(qapp):
    w = _make_overlay_window(fast_config())
    done = threading.Event()
    gui_thread = threading.get_ident()
    called_on: list[int] = []

    def on_done():
        called_on.append(threading.get_ident())
        done.set()

    t = threading.Thread(target=w.request_show, args=(make_achievement(), on_done))
    t.start()
    t.join()
    assert wait_until(done.is_set)
    assert called_on == [gui_thread]  # Qt work happened on the GUI thread


def test_new_show_releases_interrupted_cycle(qapp):
    w = _make_overlay_window(fast_config(hold_seconds=10))
    first, second = [], []
    w.show_achievement(make_achievement(achievement_name="One"), lambda: first.append(1))
    w.show_achievement(make_achievement(achievement_name="Two"), lambda: second.append(1))
    assert first == [1]  # the waiter of the interrupted show is not left hanging
    assert w._ach_label.text() == "Two"
    w.hide()


def test_icon_from_local_path_and_hidden_without_icon(qapp, tmp_path: Path):
    icon = tmp_path / "icon.png"
    img = QImage(64, 64, QImage.Format.Format_ARGB32)
    img.fill(QColor("#e94560"))
    assert img.save(str(icon))

    w = _make_overlay_window(fast_config(hold_seconds=5))
    w.show_achievement(make_achievement(), icon_path=str(icon))
    assert not w._icon_label.isHidden()
    assert not w._icon_label.pixmap().isNull()

    w.show_achievement(make_achievement(), icon_path=str(tmp_path / "missing.png"))
    assert w._icon_label.isHidden()
    w.hide()


def test_long_text_is_elided_not_clipped(qapp):
    w = _make_overlay_window(fast_config(hold_seconds=5))
    w.show_achievement(make_achievement(description="x" * 400, achievement_name="y" * 400))
    assert w._desc_label.text().endswith("…")
    assert w._ach_label.text().endswith("…")
    w.hide()
