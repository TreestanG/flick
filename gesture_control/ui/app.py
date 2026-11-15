"""The window: icon rail, top bar, pages, and the pump that reads events from the back end."""

from __future__ import annotations

import queue
import traceback

import customtkinter as ctk

from .. import keychain, store
from ..contract import Bus
from ..parts import group
from ..tray import Tray
from . import theme
from .first_run import FirstRunView
from .icons import icon
from .main_view import MainView
from .new_command import NewCommandDialog
from .settings_view import SettingsView
from .theme import ACCENT_TEXT, BORDER, HOVER, INSET, MUTED, RADIUS, SURFACE, TEXT
from .widgets import Chip, ConfirmDialog, PopupMenu, clear, label, separator

PUMP_MS = 33   # events are read every tick, the preview is drawn every second tick (~15 fps)

PAGES = (   # key, name, icon
    ("main", "Gestures", "grid"),
    ("first_run", "Setup", "list"),
    ("settings", "Settings", "sliders"),
)
APP_MENU = (("armed", "Armed", "play"), ("paused", "Paused", "pause"), ("quit", "Quit", "power"))


class App(ctk.CTk):
    def __init__(self, bus: Bus, settings: store.Settings, start_hidden: bool = False,
                 use_tray: bool = True):
        theme.apply()
        super().__init__(fg_color=theme.BG)
        self.bus = bus
        self.settings = settings
        self.title("Gesture Control")
        theme.set_icon(self)
        self.geometry("1000x620")
        self.minsize(860, 540)

        self.armed = True
        self.hand_seen = False
        self.camera_ok = True
        self.chrome_connected = False
        self.has_key = keychain.has_key()
        self.gestures: list[dict] = []
        self.new_ids: set[str] = set()      # gestures added since the app started
        self._known_ids: set[str] | None = None
        self.idle_videos: list[dict] = []
        self.cameras: list[str] = []
        self.dialog: NewCommandDialog | None = None
        self.menu: PopupMenu | None = None
        self._menu_anchor = None
        self.page = ""
        self._pending_match: str | None = None
        self._tick = 0
        self._ui_queue: queue.Queue = queue.Queue()

        self._build_rail()
        separator(self, vertical=True).pack(side="left", fill="y")
        right = clear(self)
        right.pack(side="left", fill="both", expand=True)
        self._build_top_bar(right)
        separator(right).pack(fill="x")
        self.content = clear(right)
        self.content.pack(fill="both", expand=True, padx=24, pady=(20, 0))
        self.pages: dict[str, ctk.CTkFrame] = {}
        self.pages["main"] = self.main = MainView(self.content, self)
        self.pages["first_run"] = self.first_run = FirstRunView(self.content, self)
        self.pages["settings"] = self.settings_view = SettingsView(self.content, self)
        self.show("main" if settings.first_run_done else "first_run")

        self.bind_all("<Button-1>", self._click_away, add="+")
        self.bind("<Configure>", lambda e: e.widget is self and self._close_menu(), add="+")
        self.bind("<Unmap>", lambda e: e.widget is self and self._close_menu(), add="+")
        self.bind("<Control-f>", lambda _e: self.search.focus_set())

        self.tray = Tray(self._ui_queue.put, self.armed)
        if use_tray:
            self.tray.start()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if start_hidden and self.tray.available:
            self.withdraw()
        self.after(PUMP_MS, self._pump)

    # rail --------------------------------------------------------------

    def _build_rail(self) -> None:
        rail = ctk.CTkFrame(self, fg_color=SURFACE, width=58, corner_radius=0)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        ctk.CTkLabel(rail, text="", image=icon("logo", 32, theme.ACCENT)).pack(pady=(13, 13))
        separator(rail).pack(fill="x", padx=12, pady=(0, 10))
        self.nav = {}
        for key, name, icon_name in PAGES:
            if key == "settings":
                separator(rail).pack(fill="x", padx=12, pady=10)
            self.nav[key] = ctk.CTkButton(
                rail, text="", image=icon(icon_name, 16, MUTED), width=36, height=36,
                corner_radius=RADIUS, fg_color="transparent", hover_color=HOVER,
                border_color=BORDER, border_width=0, command=lambda k=key: self.show(k))
            self.nav[key].pack(pady=2)

    # top bar -----------------------------------------------------------

    def _pill(self, bar, text: str, command, outlined: bool) -> ctk.CTkButton:
        tint = ACCENT_TEXT if outlined else MUTED
        return ctk.CTkButton(
            bar, text=text, image=icon("chevrons", 12, tint), compound="left", height=34,
            width=0, corner_radius=RADIUS, font=theme.fonts().body, command=command,
            fg_color=SURFACE if outlined else INSET, hover_color=HOVER,
            border_width=1 if outlined else 0, border_color=theme.ACCENT,
            text_color=ACCENT_TEXT if outlined else TEXT)

    def _build_top_bar(self, parent) -> None:
        bar = clear(parent)
        bar.pack(fill="x", padx=24, pady=12)

        self.app_pill = self._pill(bar, "Gesture Control", self.toggle_app_menu, outlined=False)
        self.app_pill.pack(side="left")
        label(bar, "»", "muted").pack(side="left", padx=10)
        self.page_pill = self._pill(bar, "Gestures", self.toggle_page_menu, outlined=True)
        self.page_pill.pack(side="left")

        self.arm_chip = Chip(bar, "Armed", theme.OK, command=self.toggle_armed)
        self.arm_chip.pack(side="right")

        box = ctk.CTkFrame(bar, fg_color=INSET, corner_radius=RADIUS, height=34, width=300)
        box.pack(side="left", expand=True)
        box.pack_propagate(False)
        ctk.CTkLabel(box, text="", image=icon("search", 14, MUTED), width=14).pack(
            side="left", padx=(12, 4))
        hint = ctk.CTkFrame(box, fg_color=HOVER, corner_radius=4)
        hint.pack(side="right", padx=8, pady=7)
        ctk.CTkLabel(hint, text="Ctrl F", font=theme.fonts().small, text_color=MUTED,
                     height=14).pack(padx=6, pady=1)
        self.search = ctk.CTkEntry(box, placeholder_text="Search", fg_color=INSET,
                                   border_width=0, text_color=TEXT, height=28,
                                   placeholder_text_color=MUTED, font=theme.fonts().body)
        self.search.pack(side="left", fill="x", expand=True, pady=3)
        self.search.bind("<KeyRelease>", self._on_search)
        self.search.bind("<Escape>", self._clear_search)

    def _on_search(self, _event=None) -> None:
        text = self.search.get().strip()
        if text and self.page != "main":
            self.show("main")
        self.main.set_filter(text)

    def _clear_search(self, _event=None) -> None:
        self.search.delete(0, "end")
        self.main.set_filter("")
        self.focus_set()

    # menus -------------------------------------------------------------

    def _toggle_menu(self, anchor, items, selected: str, on_pick) -> None:
        same = self.menu is not None and self._menu_anchor is anchor
        self._close_menu()
        if same:
            return
        self._menu_anchor = anchor
        self.menu = PopupMenu(anchor, items, selected, on_pick,
                              on_close=lambda: setattr(self, "menu", None))

    def toggle_page_menu(self) -> None:
        self._toggle_menu(self.page_pill, PAGES, self.page, self.show)

    def toggle_app_menu(self) -> None:
        self._toggle_menu(self.app_pill, APP_MENU, "armed" if self.armed else "paused",
                          self._app_menu_pick)

    def _app_menu_pick(self, name: str) -> None:
        if name == "quit":
            self.quit_app()
        else:
            self.set_armed(name == "armed")

    def _close_menu(self) -> None:
        if self.menu is not None:
            self.menu.close()

    def _click_away(self, event) -> None:
        if self.menu is None or self.menu.contains(event.widget):
            return
        if str(event.widget).startswith(str(self._menu_anchor)):
            return                     # the pill's own command closes the menu
        self._close_menu()

    # pages -------------------------------------------------------------

    def show(self, name: str) -> None:
        if name == self.page:
            return
        for page in self.pages.values():
            page.pack_forget()
        if name == "first_run":
            self.first_run.refresh()
        self.pages[name].pack(fill="both", expand=True)
        self.page = name
        for key, title, icon_name in PAGES:
            active = key == name
            self.nav[key].configure(
                image=icon(icon_name, 16, ACCENT_TEXT if active else MUTED),
                border_width=1 if active else 0)
            if active:
                self.page_pill.configure(text=title)

    def open_settings(self, section: str | None = None) -> None:
        self.show("settings")
        if section:
            self.after(60, self.settings_view.show_section, section)

    def finish_first_run(self) -> None:
        self.settings.first_run_done = True
        self.save_settings()
        self.show("main")

    def checklist(self) -> dict[str, bool]:
        return {"camera": self.camera_ok and self._tick > 0, "idle": bool(self.idle_videos),
                "key": self.has_key, "chrome": self.chrome_connected}

    def state_changed(self) -> None:
        if getattr(self, "first_run", None) is not None:
            self.first_run.refresh()

    def save_settings(self) -> None:
        store.save_settings(self.settings)
        self.bus.send("settings_changed", settings=self.settings.to_dict())

    # commands ----------------------------------------------------------

    def set_armed(self, armed: bool) -> None:
        if armed == self.armed:
            return
        self.armed = armed
        self.bus.send("set_armed", armed=armed)
        self.tray.set_armed(armed)
        self.arm_chip.set("Armed" if armed else "Paused", theme.OK if armed else MUTED)
        self.main.set_armed(armed)

    def toggle_armed(self) -> None:
        self.set_armed(not self.armed)

    def new_command(self, gesture: dict | None = None) -> None:
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.focus_force()
            return
        self.dialog = NewCommandDialog(self, self, gesture)

    def dialog_closed(self, dialog) -> None:
        if self.dialog is dialog:
            self.dialog = None

    def test_action(self, gesture_id: str) -> None:
        self._pending_match = None
        self.bus.send("test_action", gesture_id=gesture_id)

    def confirm_delete(self, gesture: dict) -> None:
        ConfirmDialog(
            self, "Delete gesture", f'Delete "{gesture["name"]}"? This cannot be undone.',
            "Delete", danger=True,
            on_confirm=lambda: self.bus.send("delete_gesture", gesture_id=gesture["id"]))

    def confirm_delete_clip(self, clip: dict) -> None:
        ConfirmDialog(
            self, "Delete idle video", f'Delete "{clip["name"]}"? This cannot be undone.',
            "Delete", danger=True,
            on_confirm=lambda: self.bus.send("delete_idle_video", clip_id=clip["id"]))

    # events ------------------------------------------------------------

    def _pump(self) -> None:
        self._tick += 1
        for _ in range(200):
            try:
                msg = self.bus.events.get_nowait()
            except queue.Empty:
                break
            try:
                getattr(self, "_on_" + msg.kind)(**msg.data)
            except Exception:
                traceback.print_exc()
        while True:
            try:
                name = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            {"toggle": self.toggle_armed, "open": self.open_window, "quit": self.quit_app}[name]()
            if name == "quit":
                return
        if self._tick % 2 == 0:
            frame = self.bus.preview.take()
            if frame is not None and self.page == "main" and self.winfo_viewable():
                self.main.render_preview(frame)
        self.after(PUMP_MS, self._pump)

    def _on_tracker_status(self, fps=0, visible=(), speed=0.0, camera_ok=True, moving=None,
                           segment=None, loading=False, **_extra) -> None:
        self.camera_ok = camera_ok
        self.hand_seen = any(group(p) != "head" for p in visible)
        self.main.set_tracker(self.hand_seen, fps, camera_ok, loading)
        self.main.set_motion(speed, moving, segment, len(visible))
        self.state_changed()

    def _on_match(self, gesture: str, ms: int) -> None:
        self._pending_match = gesture
        self.main.set_last(f"Last: {gesture} ({ms:.0f} ms)")

    def _on_action_result(self, ok: bool, message: str = "", gesture: str = "") -> None:
        matched, self._pending_match = self._pending_match == gesture, None
        if not ok:
            self.main.set_last(f"{gesture}: {message}" if gesture else message, error=True)
        elif not matched:
            self.main.set_last(f"Last: {gesture}, {message or 'ok'}")

    def _on_training_prompt(self, **data) -> None:
        if self.dialog is not None:
            self.dialog.on_prompt(**data)

    def _on_training_progress(self, **data) -> None:
        if self.dialog is not None:
            self.dialog.on_progress(**data)

    def _on_training_result(self, **data) -> None:
        if self.dialog is not None:
            self.dialog.on_result(**data)

    def _on_gestures_changed(self, gestures: list[dict]) -> None:
        ids = {g["id"] for g in gestures}
        if self._known_ids is not None:
            self.new_ids = (self.new_ids | (ids - self._known_ids)) & ids
        self._known_ids = ids
        self.gestures = gestures
        self.main.set_gestures(gestures)

    def _on_chrome_status(self, connected: bool, message: str = "") -> None:
        self.chrome_connected = connected
        self.settings_view.set_chrome(connected, message)
        self.state_changed()

    def _on_idle_videos(self, clips: list[dict]) -> None:
        self.idle_videos = clips
        self.settings_view.set_idle_videos(clips)
        self.state_changed()

    def _on_cameras(self, devices: list[str]) -> None:
        self.cameras = devices
        self.settings_view.set_cameras(devices)

    # window and tray ---------------------------------------------------

    def open_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def on_close(self) -> None:
        self._close_menu()
        if self.settings.run_in_background and self.tray.available:
            self.withdraw()
        else:
            self.quit_app()

    def quit_app(self) -> None:
        self._close_menu()
        self.tray.stop()
        self.destroy()
