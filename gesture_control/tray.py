"""Tray icon with a menu: Arm or Pause, Open window, Quit.

The engine owns the tray (file 01), because it is the only process that is
always running. pystray calls back on its own thread, so the callbacks given
here must be safe to call from any thread.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

from PIL import Image, ImageDraw

from . import store


def icon_image(armed: bool) -> Image.Image:
    """Filled blue dot when armed, hollow gray ring when paused."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if armed:
        draw.ellipse((8, 8, size - 8, size - 8), fill="#6CB8F0")
    else:
        draw.ellipse((8, 8, size - 8, size - 8), outline="#A39A91", width=8)
    return image


def ui_path() -> Path | None:
    """Where the Tauri app is. It writes `ui.json` every time it starts."""
    try:
        exe = Path(json.loads((store.runtime_dir() / "ui.json").read_text("utf-8"))["exe"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return exe if exe.exists() else None


def open_window() -> bool:
    """Start the UI. If it is already running, it brings its window forward."""
    exe = ui_path()
    if exe is None:
        print("Open window: the UI app has not been run yet, so its path is unknown.",
              file=sys.stderr)
        return False
    options: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, cwd=str(exe.parent))
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        subprocess.Popen([str(exe)], **options)
        return True
    except OSError as error:
        print(f"Open window: {error}", file=sys.stderr)
        return False


class Tray:
    def __init__(self, on_toggle, on_quit, on_open=open_window, armed: bool = True,
                 enabled: bool = True):
        self._on_toggle, self._on_quit, self._on_open = on_toggle, on_quit, on_open
        self._armed = armed
        self._enabled = enabled
        self._icon = None

    @property
    def available(self) -> bool:
        return self._icon is not None

    def _make(self) -> bool:
        if not self._enabled:
            return False
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem(lambda _i: "Pause" if self._armed else "Arm",
                                 lambda: self._on_toggle()),
                pystray.MenuItem("Open window", lambda: self._on_open(), default=True),
                pystray.MenuItem("Quit", lambda: self._on_quit()),
            )
            self._icon = pystray.Icon("gesture_control", icon_image(self._armed),
                                      self._title(), menu)
            return True
        except Exception:
            self._icon = None
            return False

    def run(self, stop: threading.Event) -> None:
        """Show the icon and block until `stop` is set. Call from the main
        thread: on macOS the tray needs it. Without a tray (none available,
        or turned off) this just waits."""
        if self._make() and sys.platform == "darwin":
            threading.Thread(target=lambda: (stop.wait(), self.stop()), name="tray-stop",
                             daemon=True).start()
            self._icon.run()
            return
        if self._icon is not None:
            try:
                self._icon.run_detached()
            except Exception:
                self._icon = None
        while not stop.wait(0.5):               # short waits, so Ctrl+C gets through
            pass

    def _title(self) -> str:
        return "Gesture Control: " + ("armed" if self._armed else "paused")

    def set_armed(self, armed: bool) -> None:
        self._armed = armed
        if self._icon is None:
            return
        try:
            self._icon.icon = icon_image(armed)
            self._icon.title = self._title()
            self._icon.update_menu()
        except Exception:
            pass

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
