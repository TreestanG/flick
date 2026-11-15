"""Small building blocks shared by the screens."""

from __future__ import annotations

import sys
import tkinter as tk

import customtkinter as ctk

from . import theme
from .icons import icon
from .theme import (ACCENT_TEXT, BORDER, CARD_RADIUS, ERROR_TEXT, HOVER, INSET, MUTED, PAD,
                    RADIUS, SOFT, SURFACE, TEXT)


def label(master, text: str = "", kind: str = "body", **kw):
    f = theme.fonts()
    font, color = {
        "title": (f.title, TEXT),
        "heading": (f.heading, TEXT),
        "body": (f.body, TEXT),
        "bold": (f.bold, TEXT),
        "soft": (f.body, SOFT),
        "muted": (f.body, MUTED),
        "small": (f.small, MUTED),
        "error": (f.small, ERROR_TEXT),
    }[kind]
    kw.setdefault("anchor", "w")
    kw.setdefault("justify", "left")
    return ctk.CTkLabel(master, text=text, font=font, text_color=color, **kw)


def button(master, text: str, command=None, kind: str = "secondary", **kw):
    """primary: accent fill. secondary: outlined. quiet: outlined with accent text.
    danger: outlined with error text. link: underlined text."""
    f = theme.fonts()
    style = dict(corner_radius=RADIUS, height=32, font=f.body, text_color_disabled=MUTED,
                 fg_color=SURFACE, hover_color=HOVER, text_color=TEXT,
                 border_width=1, border_color=BORDER)
    if kind == "primary":
        style.update(fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                     text_color=theme.ON_ACCENT, border_width=0, font=f.bold)
    elif kind == "quiet":
        style.update(text_color=ACCENT_TEXT)
    elif kind == "danger":
        style.update(text_color=ERROR_TEXT)
    elif kind == "link":
        style.update(fg_color="transparent", border_width=0, font=f.link, height=24,
                     width=0, corner_radius=0)
    style.update(kw)
    style.setdefault("width", max(72, 20 + 8 * len(text)))
    return ctk.CTkButton(master, text=text, command=command, **style)


def entry(master, placeholder: str = "", **kw):
    style = dict(fg_color=INSET, border_color=BORDER, border_width=1, corner_radius=RADIUS,
                 text_color=TEXT, placeholder_text_color=MUTED, font=theme.fonts().body,
                 height=34, placeholder_text=placeholder)
    style.update(kw)
    return ctk.CTkEntry(master, **style)


def switch(master, command=None, **kw):
    return ctk.CTkSwitch(master, text="", command=command, width=40, switch_width=36,
                         switch_height=18, fg_color=theme.TRACK, progress_color=theme.ACCENT,
                         button_color=theme.KNOB, button_hover_color=theme.KNOB, **kw)


def card(master, **kw):
    """A white card with a hairline border."""
    return ctk.CTkFrame(master, fg_color=SURFACE, border_width=1, border_color=BORDER,
                        corner_radius=CARD_RADIUS, **kw)


def inset(master, **kw):
    """The gray block inside a card."""
    return ctk.CTkFrame(master, fg_color=INSET, corner_radius=RADIUS, border_width=0, **kw)


def clear(master) -> ctk.CTkFrame:
    return ctk.CTkFrame(master, fg_color="transparent")


def separator(master, color=BORDER, vertical: bool = False):
    """A one-pixel hairline (a plain tk frame: the toolkit's own does not draw at one pixel)."""
    size = dict(width=1) if vertical else dict(height=1)
    return tk.Frame(master, bg=color, bd=0, highlightthickness=0, **size)


def bullet(master, text: str, kind: str = "soft"):
    """One line of a card's gray block."""
    return label(master, "\u2022   " + text, kind, height=20)


def icon_circle(master, name: str, color=ACCENT_TEXT):
    """The round icon at the start of a card header."""
    circle = ctk.CTkFrame(master, width=36, height=36, corner_radius=18, fg_color="#FAFAFA",
                          border_width=1, border_color=BORDER)
    circle.pack_propagate(False)
    ctk.CTkLabel(circle, text="", image=icon(name, 18, color), fg_color="transparent",
                 width=18, height=18).place(relx=0.5, rely=0.5, anchor="center")
    return circle


def card_header(box, icon_name: str, title: str, tint=ACCENT_TEXT):
    """Icon circle and heading. Returns the row so a badge can be packed on the right."""
    row = clear(box)
    row.pack(fill="x", padx=PAD, pady=(PAD, 12))
    icon_circle(row, icon_name, tint).pack(side="left")
    label(row, title, "heading").pack(side="left", padx=12)
    return row


class Badge(ctk.CTkFrame):
    """A small outlined tag, like the reference design's "New"."""

    KINDS = {"neutral": (BORDER, MUTED), "accent": (theme.ACCENT, ACCENT_TEXT),
             "warn": (theme.WARN, theme.WARN_TEXT), "error": (theme.ERROR, ERROR_TEXT)}

    def __init__(self, master, text: str = "", kind: str = "neutral"):
        super().__init__(master, fg_color="transparent", border_width=1, corner_radius=6)
        self.text = ctk.CTkLabel(self, text=text, font=theme.fonts().small, height=16)
        self.text.pack(padx=8, pady=3)
        self.set(text, kind)

    def set(self, text: str, kind: str = "neutral") -> None:
        border, color = self.KINDS[kind]
        self.configure(border_color=border)
        self.text.configure(text=text, text_color=color)


class Chip(ctk.CTkFrame):
    """A gray pill with a colored dot and a word. Clickable when given a command."""

    def __init__(self, master, text: str = "", color=MUTED, command=None):
        super().__init__(master, fg_color=INSET, border_width=0, corner_radius=RADIUS, height=34)
        self._command = command
        self.dot = ctk.CTkFrame(self, width=8, height=8, corner_radius=4, fg_color=color)
        self.dot.pack(side="left", padx=(12, 8), pady=13)
        self.text = label(self, text)
        self.text.pack(side="left", padx=(0, 12), pady=3)
        if command:
            for widget in (self, self.dot, self.text):
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", lambda _e: self._command())
                widget.bind("<Enter>", lambda _e: self.configure(fg_color=HOVER))
                widget.bind("<Leave>", lambda _e: self.configure(fg_color=INSET))

    def set(self, text: str, color) -> None:
        self.text.configure(text=text)
        self.dot.configure(fg_color=color)


class PopupMenu(tk.Toplevel):
    """A drop-down list under a widget. Items are (key, text, icon name); the
    current one is outlined, as in the reference design."""

    KEY = "#FEFEFE"   # stands in for "transparent" around the rounded box

    def __init__(self, anchor, items, selected: str, on_pick, on_close=None):
        super().__init__(anchor.winfo_toplevel())
        self._on_pick, self._on_close = on_pick, on_close
        self.overrideredirect(True)
        key = self.KEY
        self.configure(bg=key)
        if sys.platform == "win32":
            try:
                self.attributes("-transparentcolor", key)
            except tk.TclError:
                pass
        box = ctk.CTkFrame(self, fg_color=SURFACE, bg_color=key, border_width=1,
                           border_color=BORDER, corner_radius=CARD_RADIUS)
        box.pack()
        for i, (name, text, icon_name) in enumerate(items):
            current = name == selected
            ctk.CTkButton(
                box, text="  " + text, anchor="w", width=176, height=32, corner_radius=RADIUS,
                image=icon(icon_name, 14, ACCENT_TEXT if current else TEXT), compound="left",
                font=theme.fonts().body, fg_color="transparent", hover_color=HOVER,
                text_color=ACCENT_TEXT if current else TEXT,
                border_width=1 if current else 0, border_color=BORDER,
                command=lambda n=name: self._pick(n),
            ).pack(padx=8, pady=(8 if i == 0 else 2, 8 if i == len(items) - 1 else 0))
        self.update_idletasks()
        self.geometry(f"+{anchor.winfo_rootx() - 8}+{anchor.winfo_rooty() + anchor.winfo_height() + 4}")
        self.bind("<Escape>", lambda _e: self.close())
        self.lift()

    def contains(self, widget) -> bool:
        return str(widget).startswith(str(self))

    def _pick(self, name: str) -> None:
        self.close()
        self._on_pick(name)

    def close(self) -> None:
        if self.winfo_exists():
            self.destroy()
        if self._on_close:
            self._on_close()


class Dialog(ctk.CTkToplevel):
    """A modal dialog with a short fade in."""

    WIDTH = 440

    def __init__(self, master, title: str):
        super().__init__(master, fg_color=SURFACE)
        self.title(title)
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        theme.set_icon(self)
        self.attributes("-alpha", 0.0)
        self.body = clear(self)
        self.body.pack(fill="both", expand=True, padx=20, pady=20)
        ctk.CTkFrame(self.body, width=self.WIDTH, height=1, fg_color="transparent").pack()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self.after(20, self._show)

    def _show(self) -> None:
        if not self.winfo_exists():
            return
        self.update_idletasks()
        top = self.master.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_reqwidth()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except Exception:
            pass
        self._fade(0)

    def _fade(self, step: int) -> None:
        if not self.winfo_exists():
            return
        self.attributes("-alpha", min(1.0, step / 6))
        if step < 6:
            self.after(20, self._fade, step + 1)

    def close(self) -> None:
        if not self.winfo_exists():
            return
        parent = self.master
        self.grab_release()
        self.destroy()
        if isinstance(parent, Dialog) and parent.winfo_exists():
            parent.grab_set()      # a dialog opened from a dialog hands the grab back


class ConfirmDialog(Dialog):
    WIDTH = 340

    def __init__(self, master, title: str, message: str, confirm: str, on_confirm, danger=False):
        super().__init__(master, title)
        label(self.body, message, wraplength=self.WIDTH).pack(fill="x", pady=(0, PAD))
        row = clear(self.body)
        row.pack(fill="x")

        def go():
            self.close()
            on_confirm()
        button(row, confirm, go, kind="danger" if danger else "primary").pack(side="right")
        button(row, "Cancel", self.close).pack(side="right", padx=(0, 8))


class CodeDialog(Dialog):
    WIDTH = 520

    def __init__(self, master, title: str, code: str):
        super().__init__(master, title)
        box = ctk.CTkTextbox(self.body, width=self.WIDTH, height=280, fg_color=INSET,
                             text_color=TEXT, border_width=1, border_color=BORDER,
                             corner_radius=RADIUS, font=theme.fonts().mono, wrap="none")
        box.insert("1.0", code)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True, pady=(0, PAD))
        button(self.body, "Close", self.close).pack(anchor="e")
