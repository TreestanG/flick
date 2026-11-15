import queue
import time

import pytest

from gesture_control import store
from gesture_control.contract import Bus, action_type
from fake_backend import BUDGET, FakeBackend
from gesture_control.ui import theme


# theme -----------------------------------------------------------------

TEXT_PAIRS = [
    ("TEXT", "SURFACE"), ("TEXT", "INSET"), ("TEXT", "HOVER"),
    ("SOFT", "INSET"), ("MUTED", "SURFACE"), ("MUTED", "INSET"), ("MUTED", "HOVER"),
    ("ERROR_TEXT", "SURFACE"), ("ERROR_TEXT", "HOVER"),
    ("ACCENT_TEXT", "SURFACE"), ("ACCENT_TEXT", "INSET"), ("ACCENT_TEXT", "HOVER"),
    ("WARN_TEXT", "SURFACE"), ("PURPLE", "SURFACE"), ("TEAL", "SURFACE"),
    ("ON_ACCENT", "ACCENT"), ("ON_ACCENT", "ACCENT_HOVER"),
]


@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_contrast(fg, bg):
    ratio = theme.contrast(getattr(theme, fg), getattr(theme, bg))
    assert ratio >= 4.5, f"{fg} on {bg}: {ratio:.2f}"


# settings --------------------------------------------------------------

def test_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTURE_CONTROL_HOME", str(tmp_path))
    assert store.load_settings() == store.Settings()
    s = store.Settings(show_points=True, mirror=False)
    s.detection["quiet_ms"] = 100
    store.save_settings(s)
    assert store.load_settings() == s


def test_settings_ignore_bad_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTURE_CONTROL_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text('{"theme": "pink", "junk": 1, "detection": {"x": 2}}')
    s = store.load_settings()
    assert not hasattr(s, "junk") and s.detection == store.DETECTION_DEFAULTS


# contract and fake back end ---------------------------------------------

def test_bus_rejects_unknown_kinds():
    bus = Bus()
    with pytest.raises(ValueError):
        bus.emit("nope")
    with pytest.raises(ValueError):
        bus.send("nope")


def test_action_type():
    assert action_type({"action": {"kind": "script"}}) == "Script"
    assert action_type({"action": {"kind": "builtin", "id": "chrome.close_tab"}}) == "Chrome"
    assert action_type({"action": {"kind": "builtin", "id": "os.minimize_window"}}) == "System"


@pytest.fixture
def backend():
    bus = Bus()
    be = FakeBackend(bus, speed=40, preview=False)
    be.start()
    yield bus
    be.stop()


def wait_for(bus, kind, timeout=10.0):
    """Collect events until one of `kind` arrives; return (that event, all seen)."""
    seen = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            msg = bus.events.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(msg)
        if msg.kind == kind:
            return msg, seen
    raise AssertionError(f"no {kind} event; saw {[m.kind for m in seen]}")


def test_training_pass_then_save(backend):
    bus = backend
    bus.send("start_training", name="wave", action_text="close the current tab",
             motion_text="wave my right hand")
    result, seen = wait_for(bus, "training_result")
    assert result.data["passed"] and result.data["summary"] == "Close the current tab"
    phases = [m.data["phase"] for m in seen if m.kind == "training_progress"]
    assert phases == ["collecting", "revising", "testing"]
    assert all(m.data["asked"] <= BUDGET for m in seen if m.kind == "training_prompt")
    bus.send("save_gesture")
    changed, _ = wait_for(bus, "gestures_changed")
    assert [g["name"] for g in changed.data["gestures"]][-1] == "wave"


def test_training_fail_stops_at_budget(backend):
    bus = backend
    bus.send("start_training", name="x", action_text="mute", motion_text="this will fail")
    result, seen = wait_for(bus, "training_result", timeout=20)
    assert not result.data["passed"] and "distinctive" in result.data["message"]
    assert max(m.data["asked"] for m in seen if m.kind == "training_prompt") == BUDGET
    bus.send("save_gesture")            # nothing to save
    bus.send("test_action", gesture_id="minimize")
    _, after = wait_for(bus, "action_result")
    assert not any(m.kind == "gestures_changed" for m in after)


def test_cancel_training_saves_nothing(backend):
    bus = backend
    bus.send("start_training", name="y", action_text="mute", motion_text="nod")
    wait_for(bus, "training_prompt")
    bus.send("cancel_training")
    bus.send("save_gesture")
    bus.send("delete_gesture", gesture_id="minimize")
    changed, seen = wait_for(bus, "gestures_changed")
    assert not any(m.kind == "training_result" for m in seen)
    assert [g["id"] for g in changed.data["gestures"]] == ["close-tab", "my-script"]


def test_chrome_connects_by_itself(backend):
    bus = backend
    first, _ = wait_for(bus, "chrome_status")
    assert not first.data["connected"] and first.data["message"] == "Connecting…"
    while True:                                         # nobody sends connect_chrome
        status, _ = wait_for(bus, "chrome_status")
        if status.data["connected"]:
            break
    bus.send("test_action", gesture_id="close-tab")
    while True:
        result, _ = wait_for(bus, "action_result")
        if result.data["gesture"] == "close tab":
            break
    assert result.data["ok"]


# api key -----------------------------------------------------------------

def test_env_key_wins_over_keychain(monkeypatch):
    from gesture_control import keychain

    class FakeRing:
        @staticmethod
        def get_password(service, account):
            return "from-keychain"

    monkeypatch.setattr(keychain, "_keyring", lambda: FakeRing)
    monkeypatch.delenv(keychain.ENV_VAR, raising=False)
    assert keychain.get_key() == "from-keychain" and not keychain.from_env()
    monkeypatch.setenv(keychain.ENV_VAR, " from-env ")
    assert keychain.get_key() == "from-env" and keychain.from_env()
    assert keychain.masked("abcdefgh").endswith("efgh")
