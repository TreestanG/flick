"""Runtime recognition (file 03): run every saved gesture function on each
Segment and fire only when exactly one says yes. Fully local."""

from __future__ import annotations

import sys

from . import sandbox, store
from .segmenter import Segment


class Detector:
    def __init__(self, cooldown_ms: float = 800):
        self.cooldown_s = cooldown_ms / 1000
        self.gestures: dict[str, sandbox.Gesture] = {}     # gesture id -> loaded function
        self.problems: dict[str, str] = {}                  # gesture id -> why it is not loaded
        self._quiet_until = float("-inf")

    def load(self, gestures: list[dict], read=store.load_code) -> None:
        """(Re)load the functions of the saved gestures. Each is checked again
        here, so a file edited by hand gets the same rules as a new one."""
        self.gestures, self.problems = {}, {}
        for gesture in gestures:
            code = read(gesture["id"])
            if code is None:
                self.problems[gesture["id"]] = "the function file is missing"
                continue
            try:
                self.gestures[gesture["id"]] = sandbox.load(code)
            except sandbox.SandboxError as error:
                self.problems[gesture["id"]] = str(error)
        for gesture_id, problem in self.problems.items():
            print(f"gesture {gesture_id} is not active: {problem}", file=sys.stderr)

    def needed_parts(self) -> set[str]:
        return {part for gesture in self.gestures.values() for part in gesture.needs}

    def matches(self, segment: Segment) -> list[str]:
        """The ids of every gesture whose function returns True."""
        found = []
        for gesture_id, gesture in self.gestures.items():
            try:
                if sandbox.run(gesture, segment.frames):
                    found.append(gesture_id)
            except sandbox.SandboxError as error:
                print(f"gesture {gesture_id} failed: {error}", file=sys.stderr)
        return found

    def on_segment(self, segment: Segment) -> str | None:
        """The id of the gesture to fire, or None."""
        if segment.t0 < self._quiet_until:
            return None                     # still cooling down from the last one
        found = self.matches(segment)
        if len(found) > 1:
            print(f"ambiguous motion, nothing fired: {', '.join(found)}", file=sys.stderr)
        if len(found) != 1:
            return None
        self._quiet_until = segment.t1 + self.cooldown_s
        return found[0]
