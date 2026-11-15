"""The main screen: a two-column grid of cards. The camera is the first card,
and each gesture is a card of the same shape."""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageOps, ImageTk

from ..contract import PreviewFrame, action_type, describe_action
from . import theme
from .icons import ACTION_ICONS
from .theme import BORDER, ERROR_TEXT, GAP, INSET, MUTED, PAD, SOFT
from .widgets import (Badge, Scroll, bullet, button, card, card_header, clear, inset, label,
                      separator)

PREVIEW = (176, 132)   # a small status preview, 4:3


class MainView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._photo = None
        self._camera_ok = True
        self._filter = ""
        self._gesture_widgets: list = []
        self._texts: dict = {}      # what each status line shows now

        top = clear(self)
        top.pack(fill="x", pady=(0, PAD))
        label(top, "Gestures", "title").pack(side="left")
        button(top, "+  New command", self.app.new_command, kind="primary", width=140,
               height=34).pack(side="right")

        self.grid_box = Scroll(self)
        self.grid_box.pack(fill="both", expand=True)
        self.grid_box.grid_columnconfigure(0, weight=1, uniform="cards")
        self.grid_box.grid_columnconfigure(1, weight=1, uniform="cards")
        self._place(self._build_camera(), 0)

    def _place(self, widget, index: int) -> None:
        column = index % 2
        widget.grid(row=index // 2, column=column, sticky="nsew", pady=(0, PAD),
                    padx=(0, GAP) if column == 0 else (GAP, 4))

    # camera card -------------------------------------------------------

    def _build_camera(self):
        box = card(self.grid_box)
        header = card_header(box, "camera", "Camera")
        self.camera_badge = Badge(header, "Starting", "neutral")
        self.camera_badge.pack(side="right")

        footer = clear(box)
        footer.pack(side="bottom", fill="x", padx=PAD, pady=(12, PAD))
        button(footer, "Camera settings", lambda: self.app.open_settings("camera"),
               kind="quiet", width=124).pack(side="left")
        self.arm_button = button(footer, "Pause  →", self.app.toggle_armed, kind="primary",
                                 width=96)
        self.arm_button.pack(side="right")

        block = inset(box)
        block.pack(fill="both", expand=True, padx=8)
        self.canvas = tk.Canvas(block, highlightthickness=0, bd=0, bg=INSET,
                                width=PREVIEW[0], height=PREVIEW[1])
        self.canvas.pack(side="left", padx=(10, 4), pady=10, anchor="n")
        self._image_item = self.canvas.create_image(0, 0, anchor="nw")
        self._text_item = self.canvas.create_text(0, 0, anchor="center", text="", fill=MUTED,
                                                  font=theme.fonts().small)
        lines = clear(block)
        lines.pack(side="left", fill="both", expand=True, pady=4, padx=(0, 8))
        self.hand_line = bullet(lines, "Hand: –")
        self.hand_line.pack(fill="x", padx=12, pady=(8, 8))
        separator(lines).pack(fill="x", padx=12)
        self.fps_line = bullet(lines, "– fps")
        self.fps_line.pack(fill="x", padx=12, pady=8)
        separator(lines).pack(fill="x", padx=12)
        self.speed_line = bullet(lines, "Speed: –")     # the debug readout of file 02
        self.speed_line.pack(fill="x", padx=12, pady=8)
        separator(lines).pack(fill="x", padx=12)
        self.motion_line = bullet(lines, "Motion: –")
        self.motion_line.pack(fill="x", padx=12, pady=8)
        separator(lines).pack(fill="x", padx=12)
        self.last_line = bullet(lines, "Last: –")
        self.last_line.configure(wraplength=180)
        self.last_line.pack(fill="x", padx=12, pady=8)
        return box

    def render_preview(self, frame: PreviewFrame) -> None:
        if not self._camera_ok:
            return
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w < 16 or h < 16:
            return
        settings = self.app.settings
        image, points = frame.image, frame.points
        if settings.mirror:
            image = ImageOps.mirror(image)
            points = tuple((1 - x, y) for x, y in points)
        scale = min(w / image.width, h / image.height)
        size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        image = image.resize(size, Image.BILINEAR)
        if settings.show_points and points:
            draw = ImageDraw.Draw(image)
            for x, y in points:
                px, py = x * size[0], y * size[1]
                draw.ellipse((px - 2, py - 2, px + 2, py + 2), fill=theme.ACCENT)
        self._photo = ImageTk.PhotoImage(image)
        self.canvas.itemconfigure(self._image_item, image=self._photo)
        self.canvas.coords(self._image_item, (w - size[0]) // 2, (h - size[1]) // 2)
        self.canvas.itemconfigure(self._text_item, text="")

    def _say(self, line, text: str, **kw) -> None:
        """Change a status line only if it differs: every configure makes
        customtkinter redraw the label, four times a second per line."""
        if self._texts.get(line) != (text, kw):
            self._texts[line] = (text, kw)
            line.configure(text=text, **kw)

    def set_motion(self, speed, moving, segment, tracked: int) -> None:
        """Live speed and the last motion's boundaries, for tuning by eye."""
        if moving is None:              # a back end without the segmenter
            return
        self._say(self.speed_line,
                  f"•   Speed {speed:.2f} · {'moving' if moving else 'still'} · {tracked} parts")
        if segment:
            self._say(self.motion_line, f"•   Motion: {segment['ms']} ms, {segment['reason']} "
                                        f"(at {segment['t']:.1f} s)")

    def set_tracker(self, hand_seen: bool, fps, camera_ok: bool, loading: bool = False) -> None:
        self._camera_ok = camera_ok
        if camera_ok:
            self._say(self.hand_line, "•   Hand " + ("seen" if hand_seen else "not seen"))
            self._say(self.fps_line, f"•   {fps:.0f} fps")
            badge = ("Loading models", "neutral") if loading else \
                ("Hand seen", "accent") if hand_seen else ("No hand", "warn")
        else:
            self._say(self.hand_line, "•   Camera error, retrying")
            self._say(self.fps_line, "•   – fps")
            badge = ("Camera error", "error")
        if self._texts.get(self.camera_badge) != badge:
            self._texts[self.camera_badge] = badge
            self.camera_badge.set(*badge)
        if not camera_ok:
            self._photo = None
            self.canvas.itemconfigure(self._image_item, image="")
            self.canvas.itemconfigure(self._text_item, text="Camera error")
            self.canvas.coords(self._text_item, self.canvas.winfo_width() // 2,
                               self.canvas.winfo_height() // 2)

    def set_last(self, text: str, error: bool = False) -> None:
        self._say(self.last_line, "•   " + text, text_color=ERROR_TEXT if error else SOFT)

    def set_armed(self, armed: bool) -> None:
        self.arm_button.configure(text="Pause  →" if armed else "Arm  →")

    # gesture cards -----------------------------------------------------

    def set_filter(self, text: str) -> None:
        text = text.lower()
        if text != self._filter:
            self._filter = text
            self.set_gestures(self.app.gestures)

    def set_gestures(self, gestures: list[dict]) -> None:
        for widget in self._gesture_widgets:
            widget.destroy()
        shown = [g for g in gestures if self._filter in g["name"].lower()]
        self._gesture_widgets = [self._card(g) for g in shown]
        if not shown:
            message = "No gestures match your search." if gestures else \
                "No gestures yet. Add one with New command."
            self._gesture_widgets = [label(self.grid_box, message, "muted", anchor="nw")]
        for i, widget in enumerate(self._gesture_widgets):
            self._place(widget, i + 1)

    def _card(self, gesture: dict):
        kind = action_type(gesture)
        icon_name, tint = ACTION_ICONS[kind]
        box = card(self.grid_box)
        header = card_header(box, icon_name, gesture["name"], tint)
        if gesture["id"] in self.app.new_ids:
            Badge(header, "New", "warn").pack(side="right")
        else:
            Badge(header, kind, "neutral").pack(side="right")

        footer = clear(box)
        footer.pack(side="bottom", fill="x", padx=PAD, pady=(12, PAD))
        gid = gesture["id"]
        button(footer, "Re-train", lambda: self.app.new_command(gesture), kind="quiet",
               width=76).pack(side="left")
        button(footer, "Delete", lambda: self.app.confirm_delete(gesture), kind="danger",
               width=68).pack(side="left", padx=GAP)
        button(footer, "Test  →", lambda: self.app.test_action(gid), kind="primary",
               width=84).pack(side="right")

        block = inset(box)
        block.pack(fill="both", expand=True, padx=8)
        first, second = describe_action(gesture)
        bullet(block, first).pack(fill="x", padx=12, pady=(12, 8))
        separator(block).pack(fill="x", padx=12)
        bullet(block, second).pack(fill="x", padx=12, pady=8)
        return box
