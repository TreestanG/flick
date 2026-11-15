"""First run: a short checklist. Each item opens its setting."""

from __future__ import annotations

import customtkinter as ctk

from . import theme
from .theme import MUTED, PAD
from .widgets import button, card, clear, label, separator

ITEMS = (
    # key, text, settings section, optional
    ("camera", "Allow camera access", "camera", False),
    ("idle", "Record an idle video (needed for training)", "idle", False),
    ("key", "Add a Fireworks API key", "model", False),
    ("chrome", "Connect Chrome", "chrome", True),
)


class FirstRunView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        label(self, "Setup", "title").pack(fill="x")
        label(self, "A few things to do before your first gesture.", "muted").pack(
            fill="x", pady=(0, PAD))
        box = card(self)
        box.pack(fill="x")
        self.marks = {}
        for i, (key, text, section, optional) in enumerate(ITEMS):
            if i:
                separator(box).pack(fill="x", padx=PAD)
            row = clear(box)
            row.pack(fill="x", padx=PAD, pady=12)
            mark = ctk.CTkFrame(row, width=10, height=10, corner_radius=5, border_width=1)
            mark.pack(side="left", padx=(0, 12))
            self.marks[key] = mark
            label(row, f"{i + 1}. {text}").pack(side="left")
            if optional:
                label(row, "optional", "small").pack(side="left", padx=8)
            button(row, "Open", lambda s=section: app.open_settings(s), kind="quiet", width=64).pack(side="right")
        self.finish = button(self, "Continue", app.finish_first_run, kind="primary", width=130, height=34)
        self.finish.pack(anchor="e", pady=PAD)
        self.refresh()

    def refresh(self) -> None:
        done = self.app.checklist()
        for key, mark in self.marks.items():
            if done[key]:
                mark.configure(fg_color=theme.OK, border_color=theme.OK)
            else:
                mark.configure(fg_color="transparent", border_color=MUTED)
        required = all(done[key] for key, _t, _s, optional in ITEMS if not optional)
        self.finish.configure(text="Continue  →" if required else "Skip for now  →")
