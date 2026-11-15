"""Colors, type and spacing. One light theme, following the reference design:
white page and cards, light gray blocks inside cards, hairline borders,
near-black text, and light blue where the reference uses green.
"""

from __future__ import annotations

import sys
import tkinter.font as tkfont
from pathlib import Path

import customtkinter as ctk

BG = "#FFFFFF"            # page
SURFACE = "#FFFFFF"       # cards, rail, dialogs, menus
INSET = "#F5F5F5"         # blocks inside cards, pills, search, text fields
HOVER = "#EDEDED"
BORDER = "#E4E4E4"
TEXT = "#111111"
SOFT = "#3A3A3A"          # body text inside card blocks
MUTED = "#666666"
ACCENT = "#6CB8F0"        # light blue fills and outlines
ACCENT_HOVER = "#58A9E6"
ON_ACCENT = "#0A2A43"     # text on an accent fill
ACCENT_TEXT = "#1B6CA8"   # the accent deep enough to read as text
OK = "#3E9B5A"            # dots only
WARN = "#F08A24"          # badge outlines
WARN_TEXT = "#B4560A"
ERROR = "#C95A4A"         # dots and borders
ERROR_TEXT = "#A63F30"
KNOB = "#3A3A3A"          # switch knob
TRACK = "#CFCFCF"         # switch track when off
PURPLE = "#7446D8"        # icon tints, as in the reference's card icons
TEAL = "#127A5F"

PAD = 16
GAP = 8
RADIUS = 8        # buttons, fields, inner blocks
CARD_RADIUS = 12

ICON = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"


def apply() -> None:
    ctk.set_appearance_mode("light")


class Fonts:
    """System font: page title, card heading, body, small."""

    def __init__(self):
        family = tkfont.nametofont("TkDefaultFont").actual("family")
        self.title = ctk.CTkFont(family=family, size=22, weight="bold")
        self.heading = ctk.CTkFont(family=family, size=15, weight="bold")
        self.body = ctk.CTkFont(family=family, size=13)
        self.bold = ctk.CTkFont(family=family, size=13, weight="bold")
        self.small = ctk.CTkFont(family=family, size=11)
        self.link = ctk.CTkFont(family=family, size=13, underline=True)
        self.mono = ctk.CTkFont(family=tkfont.nametofont("TkFixedFont").actual("family"), size=12)


_fonts: Fonts | None = None


def fonts() -> Fonts:
    global _fonts
    if _fonts is None:
        _fonts = Fonts()
    return _fonts


def set_icon(window) -> None:
    """Use the app icon instead of the toolkit's default (Windows .ico only)."""
    if sys.platform == "win32" and ICON.exists():
        try:
            window.iconbitmap(str(ICON))
        except Exception:
            pass


def _luminance(hex_color: str) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two #RRGGBB colors."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)
