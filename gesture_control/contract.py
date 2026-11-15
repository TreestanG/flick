"""The contract between the UI and the rest of the app.

The UI is a separate program (the Tauri app in `app/`), and never calls the
tracker directly. Inside the engine, events go onto one queue and commands
come off another; `server.py` carries both to and from the UI windows over a
local WebSocket (file 01). Camera images travel through a single-slot
`PreviewSlot`, so a slow window drops frames instead of falling behind.

`server.py` answers a few commands itself, without the back end: `hello`,
`set_preview`, `set_api_key`, `remove_api_key`, `quit`, and the merging and
saving of `settings_changed`. It also sends the events `armed_changed`,
`settings_changed` and `api_key`.

Events (back end -> UI)
-----------------------
tracker_status     fps, visible (list of part names), speed, camera_ok
match              gesture (name), ms
training_prompt    phase, count, index, asked, budget, ready (the countdown before
                   a sample: 1, 2, then 0 when it is time to perform the motion)
training_progress  round, phase; while the agent works also step (the number of the
                   model call now running) and note (what the previous one led to)
training_sample    index, count, phase, fps, frames (a short video of the sample the
                   user just performed: JPEG images as base64 text, not mirrored)
training_review    phase, count, asked, budget (the batch is complete: the user
                   checks the videos, re-records any, then submits)
training_result    passed, summary, message, parts, code
action_result      ok, message, gesture (name)

Not in the original table, added because the screens need them:

gestures_changed   gestures (list in the gestures.json format of file 04)
chrome_status      connected, message
idle_videos        clips (list of {id, name, seconds})
cameras            devices (list of names)

Commands (UI -> back end)
-------------------------
set_armed          armed
start_training     name, action_text, motion_text, gesture_id (None for a new one)
cancel_training
submit_samples     (the videos look right: hand the batch to the agent)
rerecord_sample    index (None for the whole batch)
save_gesture
delete_gesture     gesture_id
test_action        gesture_id
connect_chrome

Added:

settings_changed   settings (dict)
record_idle        on
delete_idle_video  clip_id
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field

EVENTS = frozenset({
    "tracker_status", "match", "training_prompt", "training_progress",
    "training_sample", "training_review", "training_result", "action_result",
    "gestures_changed", "chrome_status", "idle_videos", "cameras",
})

COMMANDS = frozenset({
    "set_armed", "start_training", "cancel_training", "save_gesture",
    "submit_samples", "rerecord_sample",
    "delete_gesture", "test_action", "connect_chrome",
    "settings_changed", "record_idle", "delete_idle_video",
})

PHASES = ("collecting", "testing", "revising", "redoing")


@dataclass(frozen=True)
class Message:
    kind: str
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PreviewFrame:
    """One camera image for the preview. `image` is a PIL image, not mirrored.
    `points` are tracked parts as (x, y) fractions of the image."""
    image: object
    points: tuple = ()


class PreviewSlot:
    """Holds only the newest preview frame."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None

    def put(self, frame: PreviewFrame) -> None:
        with self._lock:
            self._frame = frame

    def take(self):
        """Return the newest frame once, or None if nothing new arrived."""
        with self._lock:
            frame, self._frame = self._frame, None
            return frame


class Bus:
    """The two queues and the preview slot shared by the UI and the back end."""

    def __init__(self):
        self.events: queue.Queue = queue.Queue()
        self.commands: queue.Queue = queue.Queue()
        self.preview = PreviewSlot()

    def emit(self, kind: str, **data) -> None:
        """Back end -> UI."""
        if kind not in EVENTS:
            raise ValueError(f"unknown event: {kind}")
        self.events.put(Message(kind, data))

    def send(self, kind: str, **data) -> None:
        """UI -> back end."""
        if kind not in COMMANDS:
            raise ValueError(f"unknown command: {kind}")
        self.commands.put(Message(kind, data))


def action_type(gesture: dict) -> str:
    """The short type shown in the gesture list: Chrome, System or Script."""
    action = gesture.get("action", {})
    if action.get("kind") == "script":
        return "Script"
    if str(action.get("id", "")).startswith("chrome."):
        return "Chrome"
    return "System"


def describe_action(gesture: dict) -> tuple[str, str]:
    """Two plain lines about a gesture's action, for its card."""
    action = gesture.get("action", {})
    kind = action_type(gesture)
    if kind == "Script":
        where = action.get("working_dir") or "its own folder"
        return (f"Runs {action.get('file', 'a script')} ({action.get('language', 'script')})",
                f"Working folder: {where}")
    words = str(action.get("id", "action")).split(".")[-1].replace("_", " ")
    first = words[:1].upper() + words[1:]
    if kind == "Chrome":
        return (f"{first} in Chrome", "Runs through the Chrome extension")
    return (first, "Runs through the operating system")
