"""Settings: one column of groups."""

from __future__ import annotations

import time

import customtkinter as ctk

from .. import autostart, keychain
from ..store import DETECTION_DEFAULTS
from . import theme
from .theme import BORDER, GAP, HOVER, INSET, MUTED, PAD, SURFACE, TEXT
from .widgets import button, card, card_header, clear, entry, label, separator, switch

DETECTION_LABELS = {
    "start_speed": "Start speed (sizes per second)",
    "stop_speed": "Stop speed (sizes per second)",
    "quiet_ms": "Quiet time to end a motion (ms)",
    "min_dur_ms": "Shortest motion (ms)",
    "max_dur_ms": "Longest motion (ms)",
    "cooldown_ms": "Cooldown after an action (ms)",
    "negatives_per_positive": "Negatives per positive sample",
}


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


class SettingsView(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.sections: dict[str, ctk.CTkFrame] = {}
        self._recording_since: float | None = None

        label(self, "Settings", "title").pack(fill="x", pady=(0, PAD))

        self.scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent", corner_radius=0, scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=MUTED)
        self.scroll.pack(fill="both", expand=True)

        self._camera()
        self._model()
        self._chrome()
        self._idle()
        self._startup()
        self._advanced()

    # layout helpers ----------------------------------------------------

    def _section(self, key: str, title: str, icon_name: str) -> ctk.CTkFrame:
        box = card(self.scroll)
        box.pack(fill="x", pady=(0, PAD), padx=(0, GAP))
        card_header(box, icon_name, title).pack_configure(pady=(PAD, 4))
        self.sections[key] = box
        return box

    def _row(self, box, title: str, hint: str = "") -> ctk.CTkFrame:
        row = clear(box)
        row.pack(fill="x", padx=PAD, pady=(4, 4))
        text = clear(row)
        text.pack(side="left", fill="x", expand=True)
        label(text, title).pack(fill="x")
        if hint:
            label(text, hint, "small", height=16).pack(fill="x")
        return row

    @staticmethod
    def _end(box) -> ctk.CTkFrame:
        spacer = ctk.CTkFrame(box, fg_color="transparent", height=6, width=8)
        spacer.pack(pady=(0, 2))
        return spacer

    @staticmethod
    def _note(note, text: str | None) -> None:
        """Show an error line under a group, or hide it when there is nothing to say."""
        note.configure(text=text or "")
        if text:
            note.pack(fill="x", padx=PAD, before=note.spacer)
        else:
            note.pack_forget()

    def _changed(self) -> None:
        self.app.save_settings()

    def show_section(self, key: str) -> None:
        self.update_idletasks()
        box = self.sections.get(key)
        total = self.scroll.winfo_height()
        if box is not None and total > 0:
            self.scroll._parent_canvas.yview_moveto(max(0, box.winfo_y() - GAP) / total)

    # camera ------------------------------------------------------------

    def _camera(self) -> None:
        s = self.app.settings
        box = self._section("camera", "Camera", "camera")
        row = self._row(box, "Device")
        self.camera_menu = ctk.CTkOptionMenu(
            row, values=["Default camera"], command=self._set_camera, width=180, height=32,
            corner_radius=theme.RADIUS, fg_color=INSET, button_color=INSET,
            button_hover_color=HOVER, text_color=TEXT, font=theme.fonts().body,
            dropdown_fg_color=SURFACE, dropdown_text_color=TEXT, dropdown_hover_color=HOVER,
            dropdown_font=theme.fonts().body)
        self.camera_menu.pack(side="right")
        self.mirror = switch(self._row(box, "Mirror the preview"), self._set_toggles)
        self.mirror.pack(side="right")
        self.points = switch(self._row(box, "Show tracked points"), self._set_toggles)
        self.points.pack(side="right")
        if s.mirror:
            self.mirror.select()
        if s.show_points:
            self.points.select()
        self._end(box)
        self.set_cameras(self.app.cameras)

    def set_cameras(self, devices: list[str]) -> None:
        devices = devices or ["Default camera"]
        self.camera_menu.configure(values=devices)
        current = self.app.settings.camera_device
        self.camera_menu.set(current if current in devices else devices[0])

    def _set_camera(self, name: str) -> None:
        self.app.settings.camera_device = name
        self._changed()

    def _set_toggles(self) -> None:
        self.app.settings.mirror = bool(self.mirror.get())
        self.app.settings.show_points = bool(self.points.get())
        self._changed()

    # model -------------------------------------------------------------

    def _model(self) -> None:
        box = self._section("model", "Model", "code")
        self.key_row = self._row(box, "Fireworks API key",
                                 f"Read from {keychain.ENV_VAR} if set, otherwise stored in the OS keychain.")
        self.key_controls = clear(self.key_row)
        self.key_controls.pack(side="right")
        row = self._row(box, "Model name")
        self.model_name = entry(row, "accounts/fireworks/models/…", width=260)
        self.model_name.pack(side="right")
        if self.app.settings.model_name:
            self.model_name.insert(0, self.app.settings.model_name)
        for event in ("<FocusOut>", "<Return>"):
            self.model_name.bind(event, lambda _e: self._set_model())
        self.key_error = label(box, "", "error", height=16)
        self.key_error.spacer = self._end(box)
        self._refresh_key()

    def _refresh_key(self) -> None:
        for child in self.key_controls.winfo_children():
            child.destroy()
        saved = keychain.get_key()
        if saved and keychain.from_env():
            label(self.key_controls, f"{keychain.masked(saved)}  from {keychain.ENV_VAR}",
                  "muted").pack(side="left")
        elif saved:
            label(self.key_controls, keychain.masked(saved), "muted").pack(side="left", padx=GAP)
            button(self.key_controls, "Remove", self._remove_key, width=72).pack(side="left")
        else:
            self.key_entry = entry(self.key_controls, "Paste key", width=180, show="•")
            self.key_entry.pack(side="left", padx=(0, GAP))
            self.key_entry.bind("<Return>", lambda _e: self._save_key())
            button(self.key_controls, "Save", self._save_key, width=72).pack(side="left")
        self.app.has_key = bool(saved)
        self.app.state_changed()

    def _save_key(self) -> None:
        value = self.key_entry.get().strip()
        if not value:
            return
        self._note(self.key_error, keychain.set_key(value))
        self._refresh_key()

    def _remove_key(self) -> None:
        self._note(self.key_error, keychain.delete_key())
        self._refresh_key()

    def _set_model(self) -> None:
        value = self.model_name.get().strip()
        if value != self.app.settings.model_name:
            self.app.settings.model_name = value
            self._changed()

    # chrome ------------------------------------------------------------

    def _chrome(self) -> None:
        box = self._section("chrome", "Chrome", "globe")
        row = self._row(box, "Connection")
        self.chrome_button = button(row, "Connect", lambda: self.app.bus.send("connect_chrome"),
                                    kind="quiet", width=80)
        self.chrome_button.pack(side="right")
        self.chrome_status = label(row, "Not connected", "muted", anchor="e")
        self.chrome_status.pack(side="right", padx=PAD)
        self._end(box)

    def set_chrome(self, connected: bool, message: str) -> None:
        self.chrome_status.configure(text=message or ("Connected" if connected else "Not connected"))
        self.chrome_button.configure(state="disabled" if connected else "normal")

    # idle videos -------------------------------------------------------

    def _idle(self) -> None:
        box = self._section("idle", "Idle videos", "hand")
        row = self._row(box, "Record yourself doing nothing",
                        "Used as negatives in training. About 5 minutes is a good start.")
        self.record_button = button(row, "Record", self._toggle_record, kind="quiet", width=80)
        self.record_button.pack(side="right")
        self.record_time = label(row, "", "muted", anchor="e")
        self.record_time.pack(side="right", padx=PAD)
        self.clips = clear(box)
        self.clips.pack(fill="x", padx=PAD)
        self._end(box)
        self.set_idle_videos(self.app.idle_videos)

    def _toggle_record(self) -> None:
        recording = self._recording_since is None
        self._recording_since = time.monotonic() if recording else None
        self.app.bus.send("record_idle", on=recording)
        self.record_button.configure(text="Stop" if recording else "Record")
        self._tick_record()

    def _tick_record(self) -> None:
        if self._recording_since is None:
            self.record_time.configure(text="")
            return
        self.record_time.configure(
            text=f"Recording  {_clock(time.monotonic() - self._recording_since)}")
        self.after(500, self._tick_record)

    def set_idle_videos(self, clips: list[dict]) -> None:
        for child in self.clips.winfo_children():
            child.destroy()
        for clip in clips:
            separator(self.clips).pack(fill="x")
            row = clear(self.clips)
            row.pack(fill="x", pady=4)
            label(row, clip["name"]).pack(side="left")
            button(row, "Delete", kind="danger", width=64, height=28,
                   command=lambda c=clip: self.app.confirm_delete_clip(c)).pack(side="right")
            label(row, _clock(clip["seconds"]), "muted", anchor="e").pack(side="right", padx=PAD)

    # startup -----------------------------------------------------------

    def _startup(self) -> None:
        s = self.app.settings
        box = self._section("startup", "Startup", "monitor")
        self.login = switch(self._row(box, "Start on login"), self._set_login)
        self.login.pack(side="right")
        self.background = switch(
            self._row(box, "Run in background", "Closing the window keeps the app in the tray."),
            self._set_background)
        self.background.pack(side="right")
        self.startup_error = label(box, "", "error", height=16)
        if s.start_on_login:
            self.login.select()
        if s.run_in_background:
            self.background.select()
        self.startup_error.spacer = self._end(box)

    def _set_login(self) -> None:
        wanted = bool(self.login.get())
        error = autostart.set_enabled(wanted)
        self._note(self.startup_error, error)
        if error:
            (self.login.deselect if wanted else self.login.select)()
            return
        self.app.settings.start_on_login = wanted
        self._changed()

    def _set_background(self) -> None:
        self.app.settings.run_in_background = bool(self.background.get())
        self._changed()

    # advanced ----------------------------------------------------------

    def _advanced(self) -> None:
        box = self._section("advanced", "Advanced", "sliders")
        self.advanced_toggle = button(box, "Show detection settings", self._toggle_advanced,
                                      kind="link")
        self.advanced_toggle.pack(anchor="w", padx=PAD, pady=(4, 0))
        self.advanced_body = clear(box)
        self.detection_fields = {}
        for key, title in DETECTION_LABELS.items():
            row = self._row(self.advanced_body, title)
            field = entry(row, width=80, justify="right")
            field.insert(0, str(self.app.settings.detection[key]))
            field.pack(side="right")
            for event in ("<FocusOut>", "<Return>"):
                field.bind(event, lambda _e, k=key: self._set_detection(k))
            self.detection_fields[key] = field
        button(self.advanced_body, "Reset to defaults", self._reset_detection, width=130).pack(
            anchor="e", padx=PAD, pady=(GAP, 0))
        self.advanced_end = self._end(box)

    def _toggle_advanced(self) -> None:
        if self.advanced_body.winfo_manager():
            self.advanced_body.pack_forget()
            self.advanced_toggle.configure(text="Show detection settings")
        else:
            self.advanced_body.pack(fill="x", before=self.advanced_end)
            self.advanced_toggle.configure(text="Hide detection settings")

    def _set_detection(self, key: str) -> None:
        field = self.detection_fields[key]
        default = DETECTION_DEFAULTS[key]
        current = self.app.settings.detection[key]
        try:
            value = type(default)(float(field.get()))
            if value <= 0:
                raise ValueError
        except ValueError:
            value = current
        field.delete(0, "end")
        field.insert(0, str(value))
        if value != current:
            self.app.settings.detection[key] = value
            self._changed()

    def _reset_detection(self) -> None:
        self.app.settings.detection = dict(DETECTION_DEFAULTS)
        for key, field in self.detection_fields.items():
            field.delete(0, "end")
            field.insert(0, str(DETECTION_DEFAULTS[key]))
        self._changed()
