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
tracker_status     fps, visible (list of part names), speed, camera_ok; live (while
                   recognition runs: windows checked in its last look, and ms it took)
match              gesture (name), id, ms; test is true in test mode (set_testing),
                   when the match is only shown and no action runs
training_prompt    phase, count, index, asked, budget, ready (the countdown before
                   a sample: 1, 2, then 0 when it is time to perform the motion),
                   back (restart_sample would now redo the sample before this one)
training_progress  round, phase; while the agent works also step (the number of the
                   model call now running) and note (what the previous one led to)
training_sample    index, count, phase, fps, frames (a short video of the sample the
                   user just performed: JPEG images as base64 text, not mirrored)
training_review    phase, count, asked, budget, more (the batch is complete: the
                   user checks the videos, re-records any, adds samples while
                   `more` is true, then submits)
training_result    passed, summary, message, parts, code, action (the planned action,
                   in the stored format; the user may change it before saving),
                   retry (false when trying again would fail the same way)
action_result      ok, message, gesture (name); run_now is true when it answers
                   run_script

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
add_sample         (on the review page: record one more sample for this batch)
restart_sample     (while samples are being taken: during a countdown, go back to
                   the sample just recorded when the prompt said back; once
                   recording has begun, take this one again. From the countdown)
save_gesture       action (optional: the user's choice instead of the planned one)
delete_gesture     gesture_id
test_action        gesture_id
connect_chrome

Added:

settings_changed   settings (dict)
record_idle        on
delete_idle_video  clip_id

For the action screens (file 04). A script action from a window carries the
script's text in "code"; the engine writes the file.

set_action         gesture_id, action (replace a saved gesture's action)
set_testing        on. Test mode: recognition keeps running for every gesture,
                   armed or paused, but no action fires; each match comes back
                   with test set. Ends when the window that asked goes away
run_script         action, gesture_id (None while training: the gesture in training).
                   "Run now": runs the script as typed, once, without saving it

For the data dashboard (the Re-train button on a gesture's card):

update_gesture     gesture_id, and any of: motion (the description), notes (what
                   the user adds about how the gesture should work; the agent reads
                   it as part of the specification), remove (positions of saved
                   samples to drop) with count (how many samples the window saw).
                   The function is not rewritten: this applies at the next training.
                   Also complements (ids of the gestures that complement this
                   one, both ways; saved as "complements" on each gesture): when
                   one of a pair fires, the other cannot for the next 5 seconds.
                   This one applies at once

Answered by `server.py`, with data in the reply:

gesture_data       gesture_id -> motion, notes, code, samples (one per saved positive:
                   seconds, lead, aspect, frames of {t, points: {part: [x, y]}})

action_info        gesture_id (optional) -> catalog, hardcoded, default_dir, code, log,
                   shell_name
check_action       action, gesture_id (optional) -> action (clean), summary, confirm,
                   and for a script: command, working_dir
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
    "submit_samples", "rerecord_sample", "restart_sample", "add_sample",
    "delete_gesture", "test_action", "connect_chrome",
    "settings_changed", "record_idle", "delete_idle_video",
    "set_action", "run_script", "set_testing", "update_gesture",
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
