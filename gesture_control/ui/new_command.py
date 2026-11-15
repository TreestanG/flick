"""The New command dialog: a short form that becomes the training view."""

from __future__ import annotations

import customtkinter as ctk

from . import theme
from .theme import BORDER, GAP, PAD
from .widgets import CodeDialog, Dialog, button, clear, entry, label

FAILED_TEXT = ("Couldn't tell this gesture apart from your other movements. "
               "Try a more distinctive motion.")


class NewCommandDialog(Dialog):
    """`gesture` is set when re-training an existing gesture."""

    def __init__(self, master, app, gesture: dict | None = None):
        super().__init__(master, "Re-train" if gesture else "New command")
        self.app = app
        self.gesture = gesture
        self.values = {"name": gesture["name"] if gesture else "", "action": "", "motion": ""}
        self.training = False
        self.round = 1
        self.code = ""
        self.view = None
        self.show_form()

    # shared ------------------------------------------------------------

    def _swap(self) -> ctk.CTkFrame:
        if self.view is not None:
            self.view.destroy()
        self.view = clear(self.body)
        self.view.pack(fill="both", expand=True)
        return self.view

    def _buttons(self, parent, *specs) -> None:
        """specs: (text, command, kind), listed right to left."""
        row = clear(parent)
        row.pack(fill="x", pady=(PAD, 0))
        for i, (text, command, kind) in enumerate(specs):
            button(row, text, command, kind=kind).pack(side="right", padx=(0, GAP if i else 0))

    def close(self) -> None:
        if self.training:
            self.app.bus.send("cancel_training")   # nothing is saved on cancel
            self.training = False
        self.app.dialog_closed(self)
        super().close()

    # form --------------------------------------------------------------

    def show_form(self) -> None:
        view = self._swap()
        self.fields = {}
        for key, title, hint in (
            ("name", "Name", "close tab"),
            ("action", "What should happen?", "Close the current tab"),
            ("motion", "Briefly describe the motion", "Swipe my right index finger to the left"),
        ):
            label(view, title, "small").pack(fill="x", pady=(0, 4))
            field = entry(view, hint)
            field.pack(fill="x", pady=(0, 12))
            if self.values[key]:
                field.insert(0, self.values[key])
            field.bind("<Return>", lambda _e: self.start())
            self.fields[key] = field
        self.form_error = label(view, "", "error")
        self.form_error.pack(fill="x")
        self._buttons(view, ("Start", self.start, "primary"), ("Cancel", self.close, "secondary"))
        self.after(120, lambda: self.fields["name"].winfo_exists() and self.fields["name"].focus_set())

    def start(self) -> None:
        self.values = {k: f.get().strip() for k, f in self.fields.items()}
        if not all(self.values.values()):
            self.form_error.configure(text="Fill in all three fields.")
            return
        own_id = self.gesture["id"] if self.gesture else None
        taken = {g["name"].lower() for g in self.app.gestures if g["id"] != own_id}
        if self.values["name"].lower() in taken:
            self.form_error.configure(text="Another gesture already has this name.")
            return
        self.training = True
        self.app.bus.send("start_training", name=self.values["name"],
                          action_text=self.values["action"], motion_text=self.values["motion"],
                          gesture_id=own_id)
        self.show_training()

    # training ----------------------------------------------------------

    def show_training(self) -> None:
        view = self._swap()
        self.phase_label = label(view, "Collecting", "small")
        self.phase_label.pack(fill="x")
        self.prompt = label(view, "Perform the motion", "title")
        self.prompt.pack(fill="x", pady=(GAP, 0))
        self.count = label(view, "Getting ready…", "muted")
        self.count.pack(fill="x", pady=(4, PAD))
        self.bar = ctk.CTkProgressBar(view, height=4, corner_radius=2, fg_color=BORDER,
                                      progress_color=theme.ACCENT, border_width=0)
        self.bar.set(0)
        self.bar.pack(fill="x")
        self.asked = label(view, "Samples asked: 0 of 40", "small")
        self.asked.pack(fill="x", pady=(GAP, 0))
        self._buttons(view, ("Cancel", self.close, "secondary"))

    def on_progress(self, round: int, phase: str) -> None:
        if not self.training:
            return
        self.round = round
        self._set_phase(phase)
        if phase == "revising":
            self.prompt.configure(text="One moment", text_color=theme.TEXT)
            self.count.configure(text="Writing the gesture function…")

    def on_prompt(self, phase: str, count: int, index: int, asked: int, budget: int,
                  ready: int = 0) -> None:
        if not self.training:
            return
        self._set_phase(phase)
        sample = f"Sample {min(index + 1, count)} of {count}"
        if ready:                           # the countdown: 1, 2, then go
            self.prompt.configure(text=f"Get ready…  {ready}", text_color=theme.MUTED)
            self.count.configure(text=f"{sample}. Keep still until it says go.")
        else:
            self.prompt.configure(text="Go: perform the motion", text_color=theme.TEXT)
            self.count.configure(text=sample)
        self.bar.set(index / count if count else 0)
        self.asked.configure(text=f"Samples asked: {asked} of {budget}")

    def _set_phase(self, phase: str) -> None:
        self.phase_label.configure(text=f"{phase.capitalize()}  ·  round {self.round}")

    # outcomes ----------------------------------------------------------

    def on_result(self, passed: bool, summary: str = "", message: str = "",
                  parts=(), code: str = "") -> None:
        if not self.training:
            return
        self.training = False
        view = self._swap()
        if passed:
            self.code = code
            label(view, "Gesture ready", "title").pack(fill="x", pady=(0, GAP))
            label(view, "Action", "small").pack(fill="x")
            label(view, summary, wraplength=self.WIDTH).pack(fill="x", pady=(0, GAP))
            label(view, "Uses", "small").pack(fill="x")
            label(view, ", ".join(p.replace("_", " ") for p in parts) or "–",
                  wraplength=self.WIDTH).pack(fill="x", pady=(0, GAP))
            if message:                     # the agent's note, e.g. the motion differs
                label(view, "Note", "small").pack(fill="x")     # from its description
                label(view, message, wraplength=self.WIDTH).pack(fill="x", pady=(0, GAP))
            button(view, "View code", self.view_code, kind="link").pack(anchor="w")
            self._buttons(view, ("Save", self.save, "primary"), ("Cancel", self.close, "secondary"))
        else:
            label(view, "Not passed", "title").pack(fill="x", pady=(0, GAP))
            label(view, message or FAILED_TEXT, wraplength=self.WIDTH).pack(fill="x")
            self._buttons(view, ("Try again", self.show_form, "primary"),
                          ("Close", self.close, "secondary"))

    def view_code(self) -> None:
        CodeDialog(self, f"{self.values['name']}: detect", self.code)

    def save(self) -> None:
        self.app.bus.send("save_gesture")
        self.close()
