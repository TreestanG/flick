"""Tray icon with a menu: Arm or Pause, Open window, Quit.

pystray runs its own thread, so its callbacks only post to the UI queue.
"""

from __future__ import annotations

import sys

from PIL import Image, ImageDraw


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


class Tray:
    def __init__(self, post, armed: bool = True):
        """`post(name)` is called from the tray thread with toggle, open or quit."""
        self._post = post
        self._armed = armed
        self._icon = None

    @property
    def available(self) -> bool:
        return self._icon is not None

    def start(self) -> bool:
        # On macOS the tray must own the main thread, which tkinter already has.
        if sys.platform == "darwin":
            return False
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem(lambda _i: "Pause" if self._armed else "Arm",
                                 lambda: self._post("toggle")),
                pystray.MenuItem("Open window", lambda: self._post("open"), default=True),
                pystray.MenuItem("Quit", lambda: self._post("quit")),
            )
            self._icon = pystray.Icon("gesture_control", icon_image(self._armed),
                                      self._title(), menu)
            self._icon.run_detached()
            return True
        except Exception:
            self._icon = None
            return False

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
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
