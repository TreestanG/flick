"""The engine's WebSocket server (file 01), driven by the fake back end, and
the theme values in the frontend's stylesheet."""

import base64
import json
import re
import threading
import time
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from gesture_control import store
from gesture_control.contract import Bus
from fake_backend import FakeBackend
from gesture_control.server import VERSION, Server


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class Window:
    """A stand-in for one UI window."""

    def __init__(self, server, token=None, version=VERSION):
        self.ws = connect(f"ws://127.0.0.1:{server.port}/ui", max_queue=None)   # never stop reading
        self.next_id = 1
        self.events = []
        self.frames = []
        self.snapshot = self.command("hello", token=server.token if token is None else token,
                                     version=version)

    def command(self, name, /, **data):
        """Send a command and return its reply, keeping what arrives meanwhile."""
        number, self.next_id = self.next_id, self.next_id + 1
        self.ws.send(json.dumps({"type": "command", "id": number, "name": name, "data": data}))
        while True:
            message = self._read(5)
            if message and message["type"] == "reply" and message["id"] == number:
                return message

    def _read(self, timeout):
        raw = self.ws.recv(timeout=timeout)
        if isinstance(raw, bytes):
            self.frames.append(raw)
            return None
        message = json.loads(raw)
        if message["type"] == "event":
            self.events.append(message)
        return message

    def wait_event(self, name, timeout=10.0):
        end = time.monotonic() + timeout
        seen = len(self.events)
        while time.monotonic() < end:
            for event in self.events[seen:]:
                if event["name"] == name:
                    return event["data"]
            seen = len(self.events)
            try:
                self._read(0.5)
            except TimeoutError:
                pass
        raise AssertionError(f"no {name} event; saw {[e['name'] for e in self.events]}")

    def wait_frame(self, timeout=5.0):
        end = time.monotonic() + timeout
        while not self.frames and time.monotonic() < end:
            try:
                self._read(0.5)
            except TimeoutError:
                pass
        assert self.frames, "no preview frame arrived"
        return self.frames[-1]

    def close(self):
        self.ws.close()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTURE_CONTROL_HOME", str(tmp_path))
    bus = Bus()
    backend = FakeBackend(bus, speed=40)
    armed = []
    quit_asked = threading.Event()
    server = Server(bus, store.Settings(), backend, on_quit=quit_asked.set, on_armed=armed.append)
    backend.start()
    server.start()
    time.sleep(0.3)                     # the back end's first events reach the server
    server.armed_seen, server.quit_asked, server.windows = armed, quit_asked, []

    def window(**kw):
        made = Window(server, **kw)
        server.windows.append(made)
        return made
    server.window = window
    yield server
    for made in server.windows:
        made.close()
    server.stop()
    backend.stop()


def test_hello_gives_a_snapshot(engine):
    snapshot = engine.window().snapshot
    assert snapshot["ok"]
    data = snapshot["data"]
    assert data["armed"] is True and data["version"] == VERSION and data["training"] is None
    assert [g["id"] for g in data["gestures"]] == ["close-tab", "minimize", "my-script"]
    assert data["settings"]["detection"] == store.DETECTION_DEFAULTS
    assert set(data["api_key"]) == {"present", "masked", "from_env", "env_var"}


def test_wrong_token_is_closed_without_a_reply(engine):
    with pytest.raises(ConnectionClosed):
        Window(engine, token="not the token")


def test_wrong_version_is_told_to_update(engine):
    ws = connect(f"ws://127.0.0.1:{engine.port}/ui")
    ws.send(json.dumps({"type": "command", "id": 1, "name": "hello",
                        "data": {"token": engine.token, "version": VERSION + 1}}))
    reply = json.loads(ws.recv(timeout=5))
    assert not reply["ok"] and "Update" in reply["error"]
    ws.close()


def test_first_message_must_be_hello(engine):
    ws = connect(f"ws://127.0.0.1:{engine.port}/ui")
    ws.send(json.dumps({"type": "command", "id": 1, "name": "set_armed", "data": {"armed": False}}))
    with pytest.raises(ConnectionClosed):
        ws.recv(timeout=5)
    assert engine.armed is True


def test_commands_reach_the_back_end_and_events_reach_every_window(engine):
    one, two = engine.window(), engine.window()
    assert one.command("delete_gesture", gesture_id="minimize")["ok"]
    for window in (one, two):
        gestures = window.wait_event("gestures_changed")["gestures"]
        assert [g["id"] for g in gestures] == ["close-tab", "my-script"]
    assert [g["id"] for g in engine.window().snapshot["data"]["gestures"]] == ["close-tab", "my-script"]


def test_bad_commands_get_an_error_reply(engine):
    window = engine.window()
    assert "Unknown" in window.command("format_disk")["error"]
    assert "wrong fields" in window.command("delete_gesture")["error"]
    assert "wrong fields" in window.command("delete_gesture", gesture_id="x", extra=1)["error"]
    assert window.command("test_action", gesture_id="minimize")["ok"]     # still connected
    assert window.wait_event("action_result")["gesture"] == "minimize"


def test_arming_is_shared_by_windows_and_tray(engine):
    one, two = engine.window(), engine.window()
    assert one.command("set_armed", armed=False)["ok"]
    assert two.wait_event("armed_changed") == {"armed": False}
    engine.toggle_armed()                           # as the tray menu does
    assert one.wait_event("armed_changed") == {"armed": True}
    assert two.wait_event("armed_changed") == {"armed": True}
    assert engine.armed_seen == [False, True]


def test_preview_frames_only_for_windows_that_ask(engine):
    asked, quiet = engine.window(), engine.window()
    assert engine.backend.preview is False          # nothing is made until someone asks
    asked.command("set_preview", on=True)
    assert asked.wait_frame()[:2] == b"\xff\xd8"    # a JPEG
    assert engine.backend.preview is True
    quiet.command("test_action", gesture_id="minimize")
    quiet.wait_event("action_result")
    assert not quiet.frames
    asked.command("set_preview", on=False)
    assert engine.backend.preview is False


def test_closing_the_window_cancels_its_training(engine):
    one, two = engine.window(), engine.window()
    assert one.command("start_training", name="wave", action_text="mute",
                       motion_text="wave", gesture_id=None)["ok"]
    assert "Another window" in two.command("start_training", name="nod", action_text="mute",
                                           motion_text="nod")["error"]
    one.wait_event("training_prompt")
    one.close()
    end = time.monotonic() + 5
    while time.monotonic() < end:
        reply = two.command("start_training", name="nod", action_text="mute", motion_text="nod")
        if reply["ok"]:
            break
        time.sleep(0.1)
    assert reply["ok"]
    assert two.wait_event("training_result", timeout=20)["passed"]
    assert two.command("save_gesture")["ok"]
    names = [g["name"] for g in two.wait_event("gestures_changed")["gestures"]]
    assert "nod" in names and "wave" not in names


def test_samples_are_reviewed_before_they_are_submitted(engine):
    engine.backend.review = True
    one, two = engine.window(), engine.window()
    assert one.command("start_training", name="wave", action_text="mute", motion_text="wave")["ok"]
    review = one.wait_event("training_review")
    assert review["phase"] == "collecting" and review["count"] == 4
    samples = [e["data"] for e in one.events if e["name"] == "training_sample"]
    assert [s["index"] for s in samples] == [0, 1, 2, 3]
    assert base64.b64decode(samples[0]["frames"][0])[:2] == b"\xff\xd8"      # JPEG images

    # Only the window that is training may answer the review.
    assert "not training" in two.command("submit_samples")["error"]
    assert "not training" in two.command("rerecord_sample", index=1)["error"]
    assert "wrong fields" in one.command("rerecord_sample", sample=1)["error"]

    one.events.clear()
    assert one.command("rerecord_sample", index=2)["ok"]         # one sample again
    assert one.wait_event("training_review")["count"] == 4
    assert [e["data"]["index"] for e in one.events if e["name"] == "training_sample"] == [2]
    one.events.clear()
    assert one.command("rerecord_sample", index=None)["ok"]      # the whole batch again
    one.wait_event("training_review")
    assert [e["data"]["index"] for e in one.events if e["name"] == "training_sample"] == [0, 1, 2, 3]
    assert not any(e["name"] == "training_result" for e in one.events)   # still waiting

    assert one.command("submit_samples")["ok"]
    assert one.wait_event("training_review")["phase"] == "testing"       # the fresh test batch
    assert one.command("submit_samples")["ok"]
    assert one.wait_event("training_result", timeout=20)["passed"]


def test_settings_are_merged_saved_and_shared(engine, tmp_path):
    one, two = engine.window(), engine.window()
    reply = one.command("settings_changed", settings={
        "mirror": False, "junk": 1, "model_name": 7, "detection": {"quiet_ms": 100, "x": 2,
                                                                  "stop_speed": -1}})
    settings = reply["data"]["settings"]
    assert settings["mirror"] is False and settings["model_name"] == ""
    assert settings["detection"]["quiet_ms"] == 100 and "x" not in settings["detection"]
    assert settings["detection"]["stop_speed"] == store.DETECTION_DEFAULTS["stop_speed"]
    assert two.wait_event("settings_changed")["settings"] == settings
    assert store.load_settings().to_dict() == settings


def test_quit_and_not_running_in_the_background(engine):
    window = engine.window()
    assert window.command("quit")["ok"]
    assert engine.quit_asked.wait(2)


def test_engine_file(tmp_path):
    from gesture_control.__main__ import engine_file, remove_engine_file, write_engine_file
    path = write_engine_file(51823, "secret", tmp_path)
    assert path == engine_file(tmp_path)
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["port"] == 51823 and body["token"] == "secret" and body["version"] == VERSION
    remove_engine_file(tmp_path)
    remove_engine_file(tmp_path)        # already gone is fine
    assert not path.exists()


# theme (file 01): text contrast in both themes -----------------------------

CSS = Path(__file__).resolve().parent.parent / "app" / "src" / "styles.css"

TEXT_PAIRS = [
    ("text", "surface"), ("text", "surface-raised"), ("text", "hover"), ("text", "bg"),
    ("text-soft", "surface-raised"),
    ("text-muted", "surface"), ("text-muted", "surface-raised"), ("text-muted", "hover"),
    ("error-text", "surface"), ("error-text", "hover"),
    ("accent-text", "surface"), ("accent-text", "surface-raised"), ("accent-text", "hover"),
    ("warn-text", "surface"), ("purple", "surface-raised"), ("teal", "surface-raised"),
    ("on-accent", "accent"), ("on-accent", "accent-hover"),
]


def _theme(selector: str) -> dict:
    css = CSS.read_text(encoding="utf-8")
    block = css[css.index(selector + " {"):]
    block = block[:block.index("}")]
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})", block))


def _luminance(color: str) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("selector", [":root", ':root[data-theme="dark"]', ":root:not([data-theme])"])
@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_contrast_in_both_themes(selector, fg, bg):
    theme = _theme(selector)
    ratio = _contrast(theme[fg], theme[bg])
    assert ratio >= 4.5, f"{fg} on {bg} in {selector}: {ratio:.2f}"


def test_system_dark_matches_dark():
    assert _theme(":root:not([data-theme])") == _theme(':root[data-theme="dark"]')
