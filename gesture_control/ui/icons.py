"""Small line icons, drawn here so the app ships no image files or icon fonts.

Each icon is drawn large and scaled down for smooth edges, once per color pair.
"""

from __future__ import annotations

import customtkinter as ctk
from PIL import Image, ImageDraw

from . import theme

S = 96      # drawing grid
W = 7       # stroke width on that grid


def _line(d, color, *pts):
    d.line(pts, fill=color, width=W, joint="curve")
    r = W / 2
    for x, y in (pts[0], pts[-1]):          # round caps
        d.ellipse((x - r, y - r, x + r, y + r), fill=color)


def _grid(d, c):
    for x in (14, 54):
        for y in (14, 54):
            d.rounded_rectangle((x, y, x + 28, y + 28), radius=7, outline=c, width=W)


def _list(d, c):
    for y in (26, 48, 70):
        d.ellipse((12, y - 6, 24, y + 6), fill=c)
        _line(d, c, (40, y), (84, y))


def _sliders(d, c):
    for y, knob in ((24, 62), (48, 34), (72, 56)):
        _line(d, c, (12, y), (84, y))
        d.ellipse((knob - 10, y - 10, knob + 10, y + 10), fill=c)


def _search(d, c):
    d.ellipse((14, 14, 62, 62), outline=c, width=W)
    _line(d, c, (58, 58), (82, 82))


def _chevrons(d, c):
    _line(d, c, (32, 40), (48, 24), (64, 40))
    _line(d, c, (32, 56), (48, 72), (64, 56))


def _camera(d, c):
    d.rounded_rectangle((10, 28, 86, 80), radius=10, outline=c, width=W)
    d.ellipse((35, 41, 61, 67), outline=c, width=W)
    _line(d, c, (34, 18), (62, 18))


def _globe(d, c):
    d.ellipse((12, 12, 84, 84), outline=c, width=W)
    d.ellipse((32, 12, 64, 84), outline=c, width=W)
    _line(d, c, (14, 48), (82, 48))


def _monitor(d, c):
    d.rounded_rectangle((10, 16, 86, 66), radius=8, outline=c, width=W)
    _line(d, c, (48, 68), (48, 80))
    _line(d, c, (30, 82), (66, 82))


def _code(d, c):
    _line(d, c, (36, 28), (16, 48), (36, 68))
    _line(d, c, (60, 28), (80, 48), (60, 68))


def _hand(d, c):
    d.rounded_rectangle((26, 40, 72, 86), radius=14, outline=c, width=W)
    for x, top in ((33, 22), (46, 14), (59, 20)):
        _line(d, c, (x, 42), (x, top))
    _line(d, c, (72, 58), (82, 44))


def _play(d, c):
    d.polygon(((30, 18), (78, 48), (30, 78)), fill=c)


def _pause(d, c):
    d.rounded_rectangle((24, 18, 40, 78), radius=5, fill=c)
    d.rounded_rectangle((56, 18, 72, 78), radius=5, fill=c)


def _power(d, c):
    d.arc((16, 20, 80, 84), start=-60, end=240, fill=c, width=W)
    _line(d, c, (48, 12), (48, 46))


def _logo(d, c):
    d.rounded_rectangle((0, 0, S, S), radius=24, fill=c)
    white = "#FFFFFF"
    d.rounded_rectangle((30, 44, 68, 80), radius=12, fill=white)
    for x, top in ((36, 26), (48, 18), (60, 24)):
        d.rounded_rectangle((x - 5, top, x + 5, 56), radius=5, fill=white)


DRAW = {"grid": _grid, "list": _list, "sliders": _sliders, "search": _search,
        "chevrons": _chevrons, "camera": _camera, "globe": _globe, "monitor": _monitor,
        "code": _code, "hand": _hand, "play": _play, "pause": _pause, "power": _power,
        "logo": _logo}

_cache: dict = {}


def _render(name: str, color: str) -> Image.Image:
    image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    DRAW[name](ImageDraw.Draw(image), color)
    return image


def icon(name: str, size: int = 16, color=theme.TEXT) -> ctk.CTkImage:
    key = (name, size, color)
    if key not in _cache:
        _cache[key] = ctk.CTkImage(light_image=_render(name, color), size=(size, size))
    return _cache[key]


# icon and tint per action type, varied like the card icons in the reference
ACTION_ICONS = {"Chrome": ("globe", theme.ACCENT_TEXT), "System": ("monitor", theme.PURPLE),
                "Script": ("code", theme.TEAL)}
