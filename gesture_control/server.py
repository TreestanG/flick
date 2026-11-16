"""The local WebSocket server the UI windows connect to (file 01).

Text messages are JSON: commands from a window, a reply to each, and events
to every window. Binary messages are preview frames, one JPEG each, sent only
to windows that asked for them.

The engine never waits for a window. Events go out to whoever is connected
and are dropped if nobody is; a window that cannot keep up loses preview
frames first and is disconnected if it falls far behind.

Everything that touches the list of windows runs on the server's own asyncio
thread. Other threads hand work over with `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import collections
import hmac
import json
import queue
import secrets
import threading
import time
import traceback

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from . import actions, autostart, keychain, preview, script_runner, store
from .contract import Bus
from .dataset import Dataset

VERSION = 1
HELLO_S = 2.0           # the first message must arrive within this
GRACE_S = 3.0           # how long the last window may be gone before "not in background" quits
BACKLOG = 500           # undelivered text messages before a window is dropped

# Commands passed straight to the back end: name -> (required keys, optional keys).
# Checked here, because the back end runs them on the tracking thread.
FORWARDED = {
    "cancel_training": ((), ()),
    "submit_samples": ((), ()),
    "rerecord_sample": ((), ("index",)),
    "restart_sample": ((), ()),
    "add_sample": ((), ()),
    "save_gesture": ((), ("action",)),
    "set_action": (("gesture_id", "action"), ()),
    "run_script": (("action",), ("gesture_id",)),
    "set_testing": (("on",), ()),
    "delete_gesture": (("gesture_id",), ()),
    "test_action": (("gesture_id",), ()),
    "connect_chrome": ((), ()),
    "record_idle": (("on",), ()),
    "delete_idle_video": (("clip_id",), ()),
    "start_training": (("name", "action_text", "motion_text"), ("gesture_id",)),
    "update_gesture": (("gesture_id",), ("motion", "notes", "remove", "count", "complements")),
}


class CommandError(Exception):
    """A command that cannot be done; the text goes back to the window."""


def api_key_info() -> dict:
    """What a window may know about the Claude Code token: never the token itself."""
    value = keychain.get_key()
    return {"present": bool(value), "masked": keychain.masked(value),
            "from_env": keychain.from_env(), "env_var": keychain.ENV_VAR}


class _Client:
    """One connected window."""

    def __init__(self, ws):
        self.ws = ws
        self.preview = False
        self.texts: collections.deque = collections.deque()
        self.frame: bytes | None = None     # only the newest preview frame waits
        self.wake = asyncio.Event()

    def text(self, message: str) -> None:
        self.texts.append(message)
        self.wake.set()

    def image(self, data: bytes) -> None:
        self.frame = data
        self.wake.set()

    async def writer(self) -> None:
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.texts:
                await self.ws.send(self.texts.popleft())
            frame, self.frame = self.frame, None
            if frame is not None:
                await self.ws.send(frame)


class Server:
    def __init__(self, bus: Bus, settings: store.Settings, backend=None, port: int = 0,
                 on_quit=None, on_armed=None):
        """`backend` only needs a `preview` attribute to switch frame making
        on and off. `on_quit()` and `on_armed(armed)` are called from the
        server thread."""
        self.bus, self.settings, self.backend = bus, settings, backend
        self.token = secrets.token_urlsafe(32)
        self.port = port
        self.armed = True
        self._on_quit = on_quit or (lambda: None)
        self._on_armed = on_armed or (lambda armed: None)
        self._clients: set[_Client] = set()
        self._owner: _Client | None = None      # the window whose training is running
        self._tester: _Client | None = None     # the window in test mode
        self._cache = {"gestures": [], "chrome_status": {"connected": False, "message": ""},
                       "idle_videos": [], "cameras": []}
        self._seen_window = False
        self._want_preview = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping: asyncio.Event | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._threads = [
            threading.Thread(target=self._serve, name="ui-server", daemon=True),
            threading.Thread(target=self._pump_events, name="ui-events", daemon=True),
            threading.Thread(target=self._pump_preview, name="ui-preview", daemon=True),
        ]
        if backend is not None:
            backend.preview = False             # nothing is encoded until a window asks

    # lifecycle ---------------------------------------------------------

    def start(self) -> int:
        """Start serving on 127.0.0.1 and return the port."""
        self._threads[0].start()
        self._ready.wait(10)
        if self._error or not self._ready.is_set():
            raise RuntimeError(f"The UI server did not start: {self._error}")
        for thread in self._threads[1:]:
            thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        if self._loop is not None and self._stopping is not None:
            try:
                self._loop.call_soon_threadsafe(self._stopping.set)
            except RuntimeError:
                pass
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=2)

    def _serve(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as error:          # noqa: BLE001 - reported by start()
            self._error = error
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        async with serve(self._handle, "127.0.0.1", self.port) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stopping.wait()

    def _call(self, fn, *args) -> None:
        """Run `fn` on the server thread, from any thread."""
        loop = self._loop
        if loop is not None and not self._stop.is_set():
            try:
                loop.call_soon_threadsafe(fn, *args)
            except RuntimeError:
                pass                            # the loop has closed: we are quitting

    # from other threads --------------------------------------------------

    def set_armed(self, armed: bool) -> None:
        """Arm or pause, from the tray."""
        self._call(self._set_armed, bool(armed))

    def toggle_armed(self) -> None:
        self._call(lambda: self._set_armed(not self.armed))

    def _pump_events(self) -> None:
        while not self._stop.is_set():
            try:
                message = self.bus.events.get(timeout=0.2)
            except queue.Empty:
                continue
            self._call(self._event, message.kind, message.data)

    def _pump_preview(self) -> None:
        period, due = 1 / preview.FPS, time.monotonic()
        while not self._stop.wait(max(0.0, due - time.monotonic())):
            due += period                       # 15 a second on average, whatever the
            if due < time.monotonic() - period: # timer's step is; no burst after a stall
                due = time.monotonic()
            if not self._want_preview:
                continue
            frame = self.bus.preview.take()
            if frame is None:
                continue
            try:
                data = preview.encode(frame, self.settings.show_points)
            except Exception:
                traceback.print_exc()
                continue
            self._call(self._frame, data)

    # sending -------------------------------------------------------------

    def _broadcast(self, name: str, data: dict) -> None:
        text = json.dumps({"type": "event", "name": name, "data": data}, default=str)
        for client in list(self._clients):
            client.text(text)
            if len(client.texts) > BACKLOG:     # it stopped reading; let it reconnect
                self._clients.discard(client)
                asyncio.ensure_future(client.ws.close(1013, "too slow"))

    def _frame(self, data: bytes) -> None:
        for client in self._clients:
            if client.preview:
                client.image(data)

    def _event(self, name: str, data: dict) -> None:
        if name == "gestures_changed":
            self._cache["gestures"] = data.get("gestures", [])
        elif name == "chrome_status":
            self._cache["chrome_status"] = data
        elif name == "idle_videos":
            self._cache["idle_videos"] = data.get("clips", [])
        elif name == "cameras":
            self._cache["cameras"] = data.get("devices", [])
        elif name == "training_result" and not data.get("passed"):
            self._owner = None                  # nothing is waiting to be saved
        self._broadcast(name, data)

    def _snapshot(self) -> dict:
        return {"version": VERSION, "armed": self.armed, "settings": self.settings.to_dict(),
                "api_key": api_key_info(),
                "training": {"active": True} if self._owner else None, **self._cache}

    # one window ----------------------------------------------------------

    async def _handle(self, ws) -> None:
        if ws.request.path != "/ui":
            await ws.close(1008, "")
            return
        hello_id = None
        try:
            message = json.loads(await asyncio.wait_for(ws.recv(), HELLO_S))
            data = message.get("data") or {}
            hello_id = message.get("id")
            ok = (message.get("type") == "command" and message.get("name") == "hello"
                  and isinstance(data.get("token"), str)
                  and hmac.compare_digest(data["token"].encode(), self.token.encode()))
        except Exception:
            ok = False
        if not ok:
            await ws.close(1008, "")            # no detail for a wrong token
            return
        if data.get("version") != VERSION:
            await ws.send(json.dumps({
                "type": "reply", "id": hello_id, "ok": False,
                "error": f"This window speaks version {data.get('version')} and the engine "
                         f"speaks version {VERSION}. Update Gesture Control."}))
            await ws.close(1008, "version")
            return

        client = _Client(ws)
        self._clients.add(client)
        self._seen_window = True
        writer = None
        try:
            await ws.send(json.dumps({"type": "reply", "id": hello_id, "ok": True,
                                      "data": self._snapshot()}, default=str))
            writer = asyncio.ensure_future(client.writer())
            async for raw in ws:
                if isinstance(raw, str):
                    self._command(client, raw)
        except ConnectionClosed:
            pass
        finally:
            if writer is not None:
                writer.cancel()
            self._left(client)

    def _left(self, client: _Client) -> None:
        self._clients.discard(client)
        if self._tester is client:              # its test mode ends: actions fire again
            self._tester = None
            self.bus.send("set_testing", on=False)
        if self._owner is client:               # closing the window is Cancel (file 03)
            self._owner = None
            self.bus.send("cancel_training")
        self._update_preview()
        if not self._clients and not self.settings.run_in_background and self._loop:
            self._loop.call_later(GRACE_S, self._quit_if_alone)

    def _quit_if_alone(self) -> None:
        if not self._clients and not self.settings.run_in_background and self._seen_window:
            self._on_quit()

    def _update_preview(self) -> None:
        self._want_preview = any(c.preview for c in self._clients)
        if self.backend is not None:
            self.backend.preview = self._want_preview

    # commands ------------------------------------------------------------

    def _command(self, client: _Client, raw: str) -> None:
        reply: dict = {"type": "reply", "id": None}
        try:
            message = json.loads(raw)
            reply["id"] = message.get("id")
            name, data = message.get("name"), message.get("data") or {}
            if message.get("type") != "command" or not isinstance(name, str) \
                    or not isinstance(data, dict):
                raise CommandError("Not a command.")
            reply.update(ok=True, data=self._run(client, name, data) or {})
        except CommandError as error:
            reply.update(ok=False, error=str(error))
        except ValueError:
            reply.update(ok=False, error="Not valid JSON.")
        except Exception:
            traceback.print_exc()
            reply.update(ok=False, error="The engine hit an internal error.")
        client.text(json.dumps(reply, default=str))

    def _run(self, client: _Client, name: str, data: dict) -> dict | None:
        if name in FORWARDED:
            return self._forward(client, name, data)
        handler = getattr(self, "_cmd_" + name, None)
        if handler is None:
            raise CommandError(f"Unknown command: {name}")
        return handler(client, data)

    def _forward(self, client: _Client, name: str, data: dict) -> None:
        required, optional = FORWARDED[name]
        missing = [key for key in required if key not in data]
        extra = [key for key in data if key not in required + optional]
        if missing or extra:
            raise CommandError(f"{name}: wrong fields.")
        if name == "start_training":
            if self._owner not in (None, client):
                raise CommandError("Another window is training a gesture.")
            self._owner = client
        elif name == "set_testing":
            self._tester = client if data["on"] else None
        elif name in ("submit_samples", "rerecord_sample", "restart_sample", "add_sample"):
            if self._owner is not client:
                raise CommandError("This window is not training a gesture.")
        elif name in ("cancel_training", "save_gesture"):
            if self._owner not in (None, client):
                raise CommandError("Another window is training a gesture.")
            self._owner = None
        self.bus.send(name, **data)

    def _set_armed(self, armed: bool) -> None:
        if armed == self.armed:
            return
        self.armed = armed
        self.bus.send("set_armed", armed=armed)
        self._on_armed(armed)
        self._broadcast("armed_changed", {"armed": armed})

    def _cmd_set_armed(self, client, data) -> None:
        self._set_armed(bool(data.get("armed")))

    def _cmd_set_preview(self, client: _Client, data) -> None:
        client.preview = bool(data.get("on"))
        if not client.preview:
            client.frame = None
        self._update_preview()

    def _cmd_settings_changed(self, client, data) -> dict:
        changes = data.get("settings")
        if not isinstance(changes, dict):
            raise CommandError("settings_changed: wrong fields.")
        login = changes.get("start_on_login")
        if isinstance(login, bool) and login != self.settings.start_on_login:
            error = autostart.set_enabled(login)
            if error:
                raise CommandError(error)
        store.update_settings(self.settings, changes)
        store.save_settings(self.settings)
        settings = self.settings.to_dict()
        self.bus.send("settings_changed", settings=settings)
        self._broadcast("settings_changed", {"settings": settings})
        return {"settings": settings}

    # the action screens (file 04): answered here, they only read ---------------

    def _gesture(self, data: dict) -> dict | None:
        wanted = data.get("gesture_id")
        return next((g for g in self._cache["gestures"] if g.get("id") == wanted), None)

    def _cmd_gesture_data(self, client, data) -> dict:
        """What a saved gesture was trained on, for the data dashboard."""
        gesture = self._gesture(data)
        if gesture is None:
            raise CommandError("There is no such gesture.")
        return {"motion": gesture.get("motion", ""), "notes": gesture.get("notes", ""),
                "code": store.load_code(gesture["id"]) or "",
                "samples": Dataset().sample_views(gesture["id"])}

    def _cmd_action_info(self, client, data) -> dict:
        gesture = self._gesture(data)
        return {"catalog": actions.catalog(),
                "hardcoded": bool(gesture) and gesture["id"] in actions.HARDCODED,
                **script_runner.info(gesture)}

    def _cmd_check_action(self, client, data) -> dict:
        """Check an edited action before it is saved, and say in plain words
        what it will do, so the window can ask the user to confirm."""
        gesture = self._gesture(data)
        if gesture and gesture["id"] in actions.HARDCODED:
            raise CommandError(actions.FIXED)
        try:
            clean = actions.check(data.get("action"))
        except ValueError as problem:
            raise CommandError(str(problem)) from None
        reply = {"action": clean, "summary": actions.describe(clean),
                 "confirm": actions.needs_confirmation(clean)}
        if clean["kind"] == "script":
            reply.update(script_runner.preview(gesture["id"] if gesture else None, clean))
            reply["summary"] = f"Run a {clean['language']} script in {reply['working_dir']}"
        return reply

    def _cmd_set_api_key(self, client, data) -> dict:
        key = data.get("key")
        if not isinstance(key, str) or not key.strip():
            raise CommandError("Paste a key first.")
        return self._key_changed(keychain.set_key(key.strip()))

    def _cmd_remove_api_key(self, client, data) -> dict:
        return self._key_changed(keychain.delete_key())

    def _key_changed(self, error: str | None) -> dict:
        if error:
            raise CommandError(error)
        info = api_key_info()
        self._broadcast("api_key", info)
        return info

    def _cmd_quit(self, client, data) -> None:
        if self._loop:
            self._loop.call_later(0.1, self._on_quit)   # after the reply has gone out
