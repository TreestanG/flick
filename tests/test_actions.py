"""File 04: the catalog, the planner, the Chrome bridge and the script runner.
Under pytest `actions.dry_run()` is true, so no key is really pressed."""

import asyncio
import json
import sys
import threading
import time

import pytest
from websockets.asyncio.client import connect

from gesture_control import actions, planner, script_runner
from gesture_control.chrome_bridge import CALLS, ChromeBridge
from gesture_control.model_client import ModelError


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTURE_CONTROL_HOME", str(tmp_path))
    actions.PRESSED.clear()
    return tmp_path


def builtin(action_id, **params):
    return {"kind": "builtin", "id": action_id, "params": params}


def gesture(action, gesture_id="g", name="G"):
    return {"id": gesture_id, "name": name, "action": action}


# catalog ----------------------------------------------------------------

def test_every_chrome_action_has_an_allowed_call_and_the_rest_do_something():
    for action_id, entry in actions.CATALOG.items():
        if action_id.startswith("chrome."):
            assert entry.call in CALLS
        else:
            assert not entry.call
            assert entry.keys or entry.param or action_id == "os.minimize_window"
    assert {e.call for e in actions.CATALOG.values() if e.call} == set(CALLS)


def test_validate_cleans_and_rejects():
    assert actions.validate({"id": "os.mute", "params": {"junk": 1}}) == builtin("os.mute")
    assert actions.validate(builtin("os.hotkey", keys="Ctrl + Shift+T"))["params"] == {
        "keys": "ctrl+shift+t"}
    assert actions.validate(builtin("chrome.open_url", url="example.com"))["params"] == {
        "url": "https://example.com"}
    assert actions.validate({"id": "none"}) == actions.NONE
    for bad in ({"id": "os.format_disk"}, builtin("os.hotkey"), builtin("os.hotkey", keys="ctrl+nope"),
                builtin("chrome.open_url", url="javascript:alert(1)"), "os.mute", {"id": 3}):
        with pytest.raises(ValueError):
            actions.validate(bad)


def test_run_presses_the_keys_and_reports_problems():
    assert actions.run(gesture(builtin("os.switch_window"))) == (True, "ok")
    assert actions.run(gesture(builtin("os.hotkey", keys="ctrl+shift+t"))) == (True, "ok")
    assert actions.run(gesture(builtin("os.minimize_window"))) == (True, "ok")
    assert actions.PRESSED == ["command+tab" if actions.MAC else "alt+tab", "ctrl+shift+t",
                               "minimize"]
    ok, message = actions.run(gesture(dict(actions.NONE)))
    assert not ok and "custom script" in message
    ok, message = actions.run(gesture(builtin("os.open", path="/no/such/file")))
    assert not ok and "does not exist" in message
    assert not actions.run(gesture({"kind": "builtin", "id": "os.hotkey", "params": {}}))[0]


def test_chrome_actions_use_the_extension_or_say_they_fell_back():
    class Chrome:
        connected = True
        calls = []

        def call(self, name, params):
            self.calls.append((name, params))

    chrome = Chrome()
    assert actions.run(gesture(builtin("chrome.close_tab")), chrome) == (True, "ok")
    assert chrome.calls == [("close_tab", {})] and not actions.PRESSED
    chrome.connected = False
    ok, message = actions.run(gesture(builtin("chrome.close_tab")), chrome)
    assert ok and "Chrome is not connected" in message and len(actions.PRESSED) == 1
    ok, message = actions.run(gesture(builtin("chrome.open_url", url="https://example.com")))
    assert ok and actions.PRESSED[-1] == "url https://example.com"


def test_the_existing_swipe_is_hardcoded_to_switch_window():
    swipe = gesture(builtin("os.hotkey"), "swipe", "Swipe")
    assert actions.for_gesture(swipe)["id"] == "os.switch_window"
    assert actions.run(swipe) == (True, "ok")
    assert actions.PRESSED == ["command+tab" if actions.MAC else "alt+tab"]
    shown = actions.apply_hardcoded([swipe, gesture(builtin("os.mute"), "other")])
    assert [g["action"]["id"] for g in shown] == ["os.switch_window", "os.mute"]


# planner ----------------------------------------------------------------

class Scripted:
    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def complete(self, messages):
        self.seen.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.mark.parametrize("sentence, action_id", [
    ("close the current tab", "chrome.close_tab"),
    ("Reopen the tab I just closed", "chrome.reopen_tab"),
    ("go to the next tab", "chrome.next_tab"),
    ("minimize the window", "os.minimize_window"),
    ("switch to the previous window", "os.switch_window"),
    ("show the desktop", "os.show_desktop"),
    ("mute", "os.mute"),
    ("turn the volume up", "os.volume_up"),
    ("pause the music", "os.play_pause"),
    ("press ctrl+shift+t", "os.hotkey"),
    ("open github.com", "chrome.open_url"),
    ("order me a pizza", "none"),
])
def test_keyword_rules(sentence, action_id):
    assert planner.guess(sentence)["id"] == action_id


def test_plan_takes_a_valid_reply_and_retries_a_bad_one_once():
    client = Scripted('<think>tabs</think> {"id": "chrome.new_tab", "params": {}}')
    assert planner.plan("open a new tab", client) == builtin("chrome.new_tab")
    assert "chrome.new_tab" in client.seen[0][0]["content"]         # it was shown the catalog
    assert len(client.seen[0]) == 2 and "open a new tab" in client.seen[0][1]["content"]

    client = Scripted('{"id": "os.format_disk"}', '```json\n{"id": "os.hotkey", "params": '
                                                   '{"keys": "alt+f4"}}\n```')
    assert planner.plan("press alt f4", client) == builtin("os.hotkey", keys="alt+f4")
    assert "Unknown action" in client.seen[1][-1]["content"]


def test_plan_falls_back_to_the_rules_and_accepts_none():
    assert planner.plan("mute", Scripted("no idea", "still no idea"))["id"] == "os.mute"
    assert planner.plan("mute", Scripted(ModelError("no key")))["id"] == "os.mute"
    assert planner.plan("mute")["id"] == "os.mute"
    assert planner.plan("mute the sound", Scripted('{"id": "none"}')) == actions.NONE


# scripts ----------------------------------------------------------------

def test_a_script_runs_in_its_folder_with_the_gesture_in_the_environment(home, tmp_path):
    code = ("import os\n"
            "print(os.environ['GESTURE_NAME'], os.environ['GESTURE_ID'], os.getcwd())\n"
            "open('ran.txt', 'w').write('yes')\n")
    action = script_runner.save_script("wave", "python", code)
    wave = gesture(action, "wave", "Wave; rm -rf")
    assert actions.run(wave, scripts=script_runner.ScriptRunner()) == (True, "ok")
    folder = script_runner.script_dir("wave")
    assert (folder / "ran.txt").read_text() == "yes"
    log = script_runner.log_path("wave").read_text(encoding="utf-8")
    assert "exit code 0" in log and "Wave; rm -rf wave" in log

    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    action["working_dir"] = str(elsewhere)
    assert script_runner.ScriptRunner().run(wave, action)[0]
    assert (elsewhere / "ran.txt").exists()
    assert script_runner.command(action, "wave")[1] == elsewhere


def test_script_failures_timeouts_and_overlaps_are_reported():
    runner = script_runner.ScriptRunner()
    fail = gesture(script_runner.save_script("fail", "python", "raise SystemExit(3)"), "fail")
    ok, message = runner.run(fail, fail["action"])
    assert not ok and "exit code 3" in message

    slow = gesture(script_runner.save_script("slow", "python", "import time; time.sleep(30)",
                                             timeout_s=1), "slow")
    results = []
    first = threading.Thread(target=lambda: results.append(runner.run(slow, slow["action"])))
    first.start()
    time.sleep(0.4)
    assert runner.run(slow, slow["action"]) == (False, "Skipped: the previous run is still going")
    first.join(15)
    assert results and not results[0][0] and "stopped after 1 s" in results[0][1]

    missing = gesture({"kind": "script", "language": "python", "file": "action.py"}, "nothing")
    assert "does not exist" in runner.run(missing, missing["action"])[1]
    assert "Unknown script language" in runner.run(
        fail, dict(fail["action"], language="ruby"))[1]


# the Chrome bridge ------------------------------------------------------

EXTENSION = {"Origin": "chrome-extension://abcdefghijklmnop"}


def test_bridge_pairs_once_then_needs_the_token_and_runs_only_fixed_calls():
    statuses = []
    bridge = ChromeBridge(lambda connected, message: statuses.append((connected, message)), port=0)
    bridge.start()
    url = f"ws://127.0.0.1:{bridge.port}/chrome"
    results = {}

    async def extension(token, origin=EXTENSION, serve=False):
        async with connect(url, additional_headers=origin) as ws:
            await ws.send(json.dumps({"type": "hello", "token": token}))
            first = json.loads(await ws.recv())
            if serve:
                call = json.loads(await ws.recv())
                results["call"] = call
                await ws.send(json.dumps({"type": "result", "id": call["id"], "ok": True}))
                call = json.loads(await ws.recv())
                await ws.send(json.dumps({"type": "result", "id": call["id"], "ok": False,
                                          "error": "Chrome has no open tab"}))
                await asyncio.sleep(0.2)
            return first

    try:
        assert asyncio.run(extension(None))["type"] == "denied"        # nobody pressed Connect
        with pytest.raises(Exception):                                  # a web page, not an extension
            asyncio.run(extension(bridge.token, {"Origin": "https://evil.example"}))
        bridge.pair()
        paired = asyncio.run(extension(None))
        assert paired == {"type": "paired", "token": bridge.token}
        assert asyncio.run(extension(None))["type"] == "denied"        # the window closed again

        thread = threading.Thread(target=lambda: results.update(
            hello=asyncio.run(extension(paired["token"], serve=True))))
        thread.start()
        deadline = time.monotonic() + 5
        while not bridge.connected and time.monotonic() < deadline:
            time.sleep(0.02)
        assert bridge.connected and (True, "Connected") in statuses
        bridge.call("close_tab")
        assert results["call"]["name"] == "close_tab" and results["call"]["type"] == "call"
        with pytest.raises(RuntimeError, match="no open tab"):
            bridge.call("close_tab")
        with pytest.raises(RuntimeError, match="Not a Chrome call"):
            bridge.call("eval")
        thread.join(5)
        assert results["hello"]["type"] == "welcome"
        deadline = time.monotonic() + 5
        while bridge.connected and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not bridge.connected and not statuses[-1][0]
        with pytest.raises(RuntimeError, match="not connected"):
            bridge.call("close_tab")
    finally:
        bridge.stop()


# the back end -----------------------------------------------------------

def test_backend_runs_the_hardcoded_swipe_and_reports_the_result(home):
    from gesture_control import store
    from gesture_control.backend import Backend
    from gesture_control.contract import Bus
    store.save_gestures([gesture(builtin("os.hotkey"), "swipe", "Swipe")])
    bus = Bus()
    backend = Backend(bus, store.Settings().to_dict())      # never started: no camera
    assert backend.gestures[0]["action"]["id"] == "os.switch_window"
    backend._cmd_test_action("swipe")
    result = bus.events.get(timeout=5)
    assert result.kind == "action_result"
    assert result.data == {"ok": True, "gesture": "Swipe", "message": "ok"}
    assert actions.PRESSED == ["command+tab" if actions.MAC else "alt+tab"]
    backend.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell on Windows")
def test_shell_means_powershell_on_windows():
    action = script_runner.save_script("ps", "shell", "Write-Output \"hi $env:GESTURE_ID\"")
    assert action["file"] == "action.ps1"
    assert script_runner.ScriptRunner().run(gesture(action, "ps"), action) == (True, "ok")
    assert "hi ps" in script_runner.log_path("ps").read_text(encoding="utf-8")


# the action screens: checking, saving and Run now -------------------------

def script(code="print('hi')", **more):
    return {"kind": "script", "language": "python", "code": code, **more}


def test_check_cleans_scripts_and_says_which_actions_need_confirming(tmp_path):
    clean = actions.check(script(working_dir=str(tmp_path), junk=1))
    assert clean == {"kind": "script", "language": "python", "code": "print('hi')",
                     "working_dir": str(tmp_path), "timeout_s": 30.0, "skip_if_running": True}
    assert actions.needs_confirmation(clean)
    assert actions.needs_confirmation(actions.check(builtin("os.hotkey", keys="alt+f4")))
    assert not actions.needs_confirmation(actions.check(builtin("chrome.close_tab")))
    for bad in (script(""), script(language="ruby"), script(working_dir="/no/such/folder"),
                script(working_dir="relative"), script(timeout_s=0), script(timeout_s="5")):
        with pytest.raises(ValueError):
            actions.check(bad)
    assert [e["id"] for e in actions.catalog()] == list(actions.CATALOG)


def test_store_action_writes_the_script_and_refuses_hardcoded_gestures():
    stored = actions.store_action("wave", script("print(1)", timeout_s=5))
    assert stored == {"kind": "script", "language": "python", "file": "action.py",
                      "working_dir": "", "timeout_s": 5, "skip_if_running": True}
    assert (script_runner.script_dir("wave") / "action.py").read_text() == "print(1)"
    assert actions.store_action("wave", builtin("os.mute")) == builtin("os.mute")
    with pytest.raises(ValueError, match="fixed in code"):
        actions.store_action("swipe", builtin("os.mute"))


def test_server_answers_action_info_and_check_action(tmp_path):
    from gesture_control import store
    from gesture_control.contract import Bus
    from gesture_control.server import CommandError, Server
    server = Server(Bus(), store.Settings())                # never started
    wave = gesture(actions.store_action("wave", script("print(2)")), "wave")
    server._cache["gestures"] = [wave, gesture(builtin("os.switch_window"), "swipe")]
    ScriptRunnerLog = script_runner.ScriptRunner()
    assert ScriptRunnerLog.run(wave, wave["action"])[0]

    info = server._cmd_action_info(None, {"gesture_id": "wave"})
    assert info["code"] == "print(2)" and "exit code 0" in info["log"] and not info["hardcoded"]
    assert info["default_dir"] == str(script_runner.script_dir("wave"))
    assert len(info["catalog"]) == len(actions.CATALOG)
    assert server._cmd_action_info(None, {"gesture_id": "swipe"})["hardcoded"]
    assert server._cmd_action_info(None, {})["default_dir"] == ""       # still in training

    reply = server._cmd_check_action(None, {"gesture_id": "wave", "action": script("print(3)")})
    assert reply["confirm"] and reply["command"] == "python action.py"
    assert reply["working_dir"] == str(script_runner.script_dir("wave"))
    assert reply["action"]["code"] == "print(3)"
    assert (script_runner.script_dir("wave") / "action.py").read_text() == "print(2)"   # only checked
    reply = server._cmd_check_action(None, {"action": builtin("os.hotkey", keys="Ctrl+W")})
    assert reply == {"action": builtin("os.hotkey", keys="ctrl+w"), "summary": "Press ctrl+w",
                     "confirm": True}
    assert not server._cmd_check_action(None, {"action": builtin("os.mute")})["confirm"]
    for data in ({"action": builtin("os.hotkey")}, {"action": script("")},
                 {"gesture_id": "swipe", "action": builtin("os.mute")}):
        with pytest.raises(CommandError):
            server._cmd_check_action(None, data)


def wait_for(bus, kind):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        message = bus.events.get(timeout=10)
        if message.kind == kind:
            return message.data
    raise AssertionError(f"no {kind}")


def test_backend_saves_an_edited_action_and_runs_a_script_now(home):
    from gesture_control import store
    from gesture_control.backend import Backend
    from gesture_control.contract import Bus
    store.save_gestures([gesture(builtin("os.mute"), "wave", "Wave"),
                         gesture(builtin("os.hotkey"), "swipe", "Swipe")])
    bus = Bus()
    backend = Backend(bus, store.Settings().to_dict())      # never started: no camera

    backend._cmd_set_action("wave", script("open('saved.txt', 'w').write('1')"))
    changed = wait_for(bus, "gestures_changed")["gestures"]
    assert changed[0]["action"]["kind"] == "script" and "code" not in changed[0]["action"]
    assert store.load_gestures()[0]["action"]["file"] == "action.py"
    backend._cmd_test_action("wave")
    assert wait_for(bus, "action_result") == {"ok": True, "gesture": "Wave", "message": "ok"}
    assert (script_runner.script_dir("wave") / "saved.txt").exists()

    backend._cmd_run_script(script("open('draft.txt', 'w').write('1')"), "wave")
    assert wait_for(bus, "action_result") == {"ok": True, "gesture": "Wave", "message": "ok",
                                              "run_now": True}
    folder = script_runner.script_dir("wave")
    assert (folder / "draft.txt").exists()
    assert "saved.txt" in (folder / "action.py").read_text()        # the saved one is untouched
    backend._cmd_run_script(script(""), "wave")
    failed = wait_for(bus, "action_result")
    assert not failed["ok"] and failed["run_now"] and "empty" in failed["message"]

    backend._cmd_set_action("wave", builtin("os.hotkey"))               # no keys: refused
    assert not wait_for(bus, "action_result")["ok"]
    backend._cmd_set_action("swipe", builtin("os.mute"))                # hardcoded: refused
    assert "fixed in code" in wait_for(bus, "action_result")["message"]
    assert [g["action"].get("id") for g in backend.gestures] == [None, "os.switch_window"]
    backend.stop()


# test mode: whichever gesture matches is shown, and nothing fires ----------

def test_test_mode_reports_matches_without_running_the_action(home):
    from gesture_control import store
    from gesture_control.backend import Backend
    from gesture_control.contract import Bus
    from test_gestures import motion, swipe_code
    store.save_gestures([gesture(builtin("os.mute"), "right", "Right")])
    store.save_code("right", swipe_code())
    bus = Bus()
    backend = Backend(bus, store.Settings().to_dict())      # never started: no camera

    backend._cmd_set_testing(True)
    backend._cmd_set_armed(False)                           # test mode runs even while paused
    backend._route(motion(0.3), 1.0)
    match = wait_for(bus, "match")
    assert match["id"] == "right" and match["gesture"] == "Right" and match["test"] is True
    time.sleep(0.3)
    assert not actions.PRESSED and bus.events.empty()       # nothing ran, nothing reported

    backend._cmd_set_testing(False)
    backend._route(motion(0.3, t0=10.0), 11.0)
    assert bus.events.empty()                               # paused again: not even a match
    backend._cmd_set_armed(True)
    backend._route(motion(0.3, t0=20.0), 21.0)
    assert wait_for(bus, "match")["test"] is False
    assert wait_for(bus, "action_result")["ok"] and actions.PRESSED == ["volumemute"]

    backend._cmd_set_testing(True)                          # deleting a gesture leaves it on
    backend._cmd_delete_gesture("right")
    assert backend.testing is True
    backend.stop()


def test_test_mode_ends_when_its_window_goes_away():
    from gesture_control import store
    from gesture_control.contract import Bus
    from gesture_control.server import Server
    bus = Bus()
    server = Server(bus, store.Settings())                  # never started
    window, other = object(), object()
    server._forward(window, "set_testing", {"on": True})
    assert bus.commands.get_nowait().data == {"on": True}
    server._left(other)
    assert bus.commands.empty()
    server._left(window)
    sent = bus.commands.get_nowait()
    assert sent.kind == "set_testing" and sent.data == {"on": False}
