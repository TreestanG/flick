"""The built-in actions (file 04): a fixed catalog, and how each one runs.

An action is stored with its gesture in `gestures.json`:
{"kind": "builtin", "id": "chrome.close_tab", "params": {}} or
{"kind": "script", ...} (see `script_runner.py`).

Chrome actions go through the extension when it is connected
(`chrome_bridge.py`) and fall back to a key combination when it is not.
Nothing here is decided at run time by a model: `run` does exactly what the
stored action says.

`run` blocks for as long as the action takes, so the back end calls it on a
thread of its own and tracking never waits for it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import webbrowser
from dataclasses import dataclass

MAC = sys.platform == "darwin"
WIN = sys.platform == "win32"
_CMD = "command" if MAC else "ctrl"         # the key the tab and window shortcuts use

NONE = {"kind": "builtin", "id": "none", "params": {}}     # nothing in the catalog fits

# Actions fixed in code for gestures that already exist, by gesture id. These
# win over whatever gestures.json or the planner says.
HARDCODED = {
    "swipe": {"kind": "builtin", "id": "os.switch_window", "params": {}},
}


@dataclass(frozen=True)
class Entry:
    text: str                   # plain words, shown to the user and to the planner
    keys: str = ""              # key combination: the action itself, or Chrome's fallback
    call: str = ""              # the Chrome extension call, for chrome.* actions
    param: str = ""             # the one parameter it needs, if any
    hint: str = ""              # what that parameter is, for the planner


CATALOG: dict[str, Entry] = {
    "chrome.close_tab": Entry("Close the current tab in Chrome", f"{_CMD}+w", "close_tab"),
    "chrome.new_tab": Entry("Open a new tab in Chrome", f"{_CMD}+t", "new_tab"),
    "chrome.reopen_tab": Entry("Reopen the last closed tab in Chrome", f"{_CMD}+shift+t",
                               "reopen_tab"),
    "chrome.next_tab": Entry("Go to the next tab in Chrome", "ctrl+tab", "next_tab"),
    "chrome.prev_tab": Entry("Go to the previous tab in Chrome", "ctrl+shift+tab", "prev_tab"),
    "chrome.close_window": Entry("Close the Chrome window", f"{_CMD}+shift+w", "close_window"),
    "chrome.minimize_window": Entry("Minimize the Chrome window", "", "minimize_window"),
    "chrome.open_url": Entry("Open a web page in Chrome", "", "open_url", "url",
                             "the full address, starting with https://"),
    "os.close_window": Entry("Close the front window", "command+w" if MAC else "alt+f4"),
    "os.minimize_window": Entry("Minimize the front window"),
    "os.switch_window": Entry("Switch to the previous window",
                              "command+tab" if MAC else "alt+tab"),
    "os.show_desktop": Entry("Show the desktop",
                             "f11" if MAC else "win+d" if WIN else "ctrl+alt+d"),
    "os.mute": Entry("Mute or unmute the sound", "volumemute"),
    "os.volume_up": Entry("Turn the volume up", "volumeup"),
    "os.volume_down": Entry("Turn the volume down", "volumedown"),
    "os.play_pause": Entry("Play or pause media", "playpause"),
    "os.hotkey": Entry("Press a key combination", "", "", "keys",
                       "the combination, such as ctrl+shift+t"),
    "os.open": Entry("Open an app or file", "", "", "path",
                     "the full path of the app or file"),
}

_ALIASES = {"control": "ctrl", "cmd": "command", "windows": "win", "super": "win",
            "meta": "win", "option": "alt", "return": "enter", "escape": "esc",
            "del": "delete", "spacebar": "space", "pgup": "pageup", "pgdn": "pagedown"}

PRESSED: list = []              # what a dry run would have done, for the tests


def dry_run() -> bool:
    """True under the tests: nothing is pressed, opened or minimized."""
    return bool(os.environ.get("GESTURE_CONTROL_DRY_RUN") or os.environ.get("PYTEST_CURRENT_TEST"))


# checking ---------------------------------------------------------------

def _keyboard():
    import pyautogui
    pyautogui.PAUSE = 0             # file 04: no delays between keys
    pyautogui.FAILSAFE = False      # the mouse resting in a corner must not block actions
    return pyautogui


def parse_keys(text) -> list[str]:
    """"Ctrl + Shift+T" -> ["ctrl", "shift", "t"]. Raises ValueError with a
    message for the user if a key is unknown."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Type a key combination, such as ctrl+shift+t.")
    keys = [_ALIASES.get(k.strip().lower(), k.strip().lower()) for k in text.split("+")]
    if not all(keys) or len(keys) > 5:
        raise ValueError(f"{text!r} is not a key combination.")
    try:
        known = set(_keyboard().KEYBOARD_KEYS)
    except Exception:               # no display: the names cannot be checked here
        return keys
    for key in keys:
        if key not in known:
            raise ValueError(f"Unknown key: {key}")
    return keys


def validate(action) -> dict:
    """A clean copy of a built-in action, or ValueError saying what is wrong.
    The planner's replies and anything a window sends go through this."""
    if not isinstance(action, dict):
        raise ValueError("An action is an object with an id.")
    action_id = action.get("id")
    if action_id == "none":
        return dict(NONE)
    entry = CATALOG.get(action_id) if isinstance(action_id, str) else None
    if entry is None:
        raise ValueError(f"Unknown action: {action_id!r}. Use an id from the catalog.")
    params = action.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object.")
    clean: dict = {}
    if entry.param:
        value = params.get(entry.param)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{action_id} needs \"{entry.param}\": {entry.hint}.")
        value = value.strip()
        if entry.param == "keys":
            value = "+".join(parse_keys(value))
        elif entry.param == "url":
            if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
                value = "https://" + value
            if not re.match(r"^https?://[^\s/]+\.[^\s]+$|^https?://localhost[^\s]*$", value, re.I):
                raise ValueError(f"{value!r} is not a web address.")
        clean[entry.param] = value
    return {"kind": "builtin", "id": action_id, "params": clean}


FIXED = ("This gesture's action is fixed in code (HARDCODED in actions.py) and cannot be "
         "changed here.")
CONFIRM = ("os.hotkey", "os.open")      # built-in, but the user typed what they do


def catalog() -> list[dict]:
    """The catalog as a window shows it."""
    return [{"id": action_id, "text": entry.text, "param": entry.param, "hint": entry.hint}
            for action_id, entry in CATALOG.items()]


def check(action) -> dict:
    """A clean copy of an action a window sent, built-in or script (a script
    still carries its text in "code"). ValueError says what is wrong."""
    if isinstance(action, dict) and action.get("kind") == "script":
        from . import script_runner
        return script_runner.check(action)
    return validate(action)


def needs_confirmation(action: dict) -> bool:
    """File 04: a script, a hotkey, or anything the user typed must be
    confirmed before it is saved."""
    return action.get("kind") == "script" or action.get("id") in CONFIRM


def store_action(gesture_id: str, action) -> dict:
    """Check an action from a window and return it in the stored format. A
    script's text is written to the gesture's folder here."""
    if gesture_id in HARDCODED:
        raise ValueError(FIXED)
    clean = check(action)
    if clean["kind"] != "script":
        return clean
    from . import script_runner
    return script_runner.save_script(gesture_id, clean["language"], clean["code"],
                                     clean["working_dir"], clean["timeout_s"],
                                     clean["skip_if_running"])


def describe(action: dict) -> str:
    """The action in plain words, for the confirmation step."""
    if not isinstance(action, dict):
        return "No action"
    if action.get("kind") == "script":
        where = action.get("working_dir") or "its own folder"
        return f"Run a {action.get('language', 'custom')} script in {where}"
    entry = CATALOG.get(action.get("id"))
    if entry is None:
        return "No built-in action fits this. Use a custom script instead."
    value = (action.get("params") or {}).get(entry.param) if entry.param else None
    if entry.param == "keys" and value:
        return f"Press {value}"
    if entry.param == "url" and value:
        return f"Open {value} in Chrome"
    if entry.param == "path" and value:
        return f"Open {value}"
    return entry.text


def for_gesture(gesture: dict) -> dict:
    """The action a gesture runs: the hardcoded one if it has one."""
    fixed = HARDCODED.get(gesture.get("id"))
    return dict(fixed) if fixed else gesture.get("action") or dict(NONE)


def apply_hardcoded(gestures: list[dict]) -> list[dict]:
    """Put the hardcoded actions into a list of gestures, so the windows show
    what will really run."""
    for gesture in gestures:
        if gesture.get("id") in HARDCODED:
            gesture["action"] = dict(HARDCODED[gesture["id"]])
    return gestures


# doing ------------------------------------------------------------------

def press(combination: str) -> None:
    keys = parse_keys(combination)
    if dry_run():
        PRESSED.append("+".join(keys))
        return
    _keyboard().hotkey(*keys)


def _minimize() -> None:
    if dry_run():
        PRESSED.append("minimize")
    elif WIN:
        import ctypes
        user32 = ctypes.windll.user32
        window = user32.GetForegroundWindow()
        if not window:
            raise RuntimeError("There is no front window to minimize.")
        user32.ShowWindow(window, 6)            # SW_MINIMIZE
    elif MAC:
        press("command+m")
    else:
        try:
            subprocess.run(["xdotool", "getactivewindow", "windowminimize"], check=True,
                           timeout=3, capture_output=True)
        except FileNotFoundError:
            raise RuntimeError("Minimizing needs xdotool (X11) on Linux.") from None


def _open(path: str) -> None:
    if not os.path.exists(path):
        raise RuntimeError(f"{path} does not exist.")
    if dry_run():
        PRESSED.append("open " + path)
    elif WIN:
        os.startfile(path)                      # noqa: S606 - the user picked this file
    else:
        subprocess.Popen(["open" if MAC else "xdg-open", path])


def _open_url(url: str) -> None:
    if dry_run():
        PRESSED.append("url " + url)
    elif not webbrowser.open(url):
        raise RuntimeError("Could not open a browser.")


def _run_builtin(action: dict, chrome) -> str:
    action = validate(action)
    action_id, params = action["id"], action["params"]
    if action_id == "none":
        raise RuntimeError("No action is set for this gesture. The request did not match "
                           "a built-in action; use a custom script instead.")
    entry = CATALOG[action_id]
    if entry.call:                              # Chrome: the extension first
        if chrome is not None and chrome.connected:
            chrome.call(entry.call, params)
            return "ok"
        note = "Chrome is not connected, so "
        if action_id == "chrome.open_url":
            _open_url(params["url"])
            return note + "the page opened in the default browser"
        if action_id == "chrome.minimize_window":
            _minimize()
            return note + "the front window was minimized"
        press(entry.keys)
        return note + f"{entry.keys} was pressed instead"
    if action_id == "os.minimize_window":
        _minimize()
    elif action_id == "os.hotkey":
        press(params["keys"])
    elif action_id == "os.open":
        _open(params["path"])
    else:
        press(entry.keys)
    return "ok"


def run(gesture: dict, chrome=None, scripts=None) -> tuple[bool, str]:
    """Run a gesture's action now. Returns (ok, message); never raises."""
    action = for_gesture(gesture)
    try:
        if action.get("kind") == "script":
            if scripts is None:
                return False, "Scripts cannot run here."
            return scripts.run(gesture, action)
        return True, _run_builtin(action, chrome)
    except Exception as error:                  # noqa: BLE001 - reported as the result
        return False, str(error) or type(error).__name__
