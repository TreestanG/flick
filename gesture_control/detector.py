"""Runtime recognition (file 03): run every saved gesture function on each
Segment and fire only when exactly one says yes. Fully local."""

from __future__ import annotations

import sys

from . import sandbox, store
from .segmenter import Segment


COMPLEMENT_MS = store.DETECTION_DEFAULTS["complement_ms"]


class Detector:
    def __init__(self, cooldown_ms: float = 800, complement_ms: float = COMPLEMENT_MS):
        self.cooldown_s = cooldown_ms / 1000
        self.complement_s = complement_ms / 1000    # a setting: how long complements stay quiet
        self.complements: dict[str, set[str]] = {}          # gesture id -> ids it silences
        self._blocked: dict[str, float] = {}                # gesture id -> silenced until
        self.gestures: dict[str, sandbox.Gesture] = {}     # gesture id -> loaded function
        self.problems: dict[str, str] = {}                  # gesture id -> why it is not loaded
        self._quiet_until = float("-inf")
        self._said: list = []
        self.why = ""               # why the last motion fired nothing, in plain words

    def load(self, gestures: list[dict], read=store.load_code) -> None:
        """(Re)load the functions of the saved gestures. Each is checked again
        here, so a file edited by hand gets the same rules as a new one."""
        self.gestures, self.problems = {}, {}
        self.complements = {g["id"]: set(g.get("complements") or ()) for g in gestures}
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
            self.why = "too soon after the last gesture"
            return None                     # still cooling down from the last one
        found = self.matches(segment)
        # A complement of a gesture that just fired (the hand coming back from
        # a swipe looks like the opposite swipe) is left out for a while.
        allowed = [g for g in found if segment.t1 >= self._blocked.get(g, float("-inf"))]
        if found and not allowed:
            self.why = "its complement just triggered"
            return None
        found = allowed
        if len(found) > 1 and found != self._said:      # once, not for every stretch checked
            self._said = found
            print(f"ambiguous motion, nothing fired: {', '.join(found)}", file=sys.stderr)
        if len(found) != 1:
            self.why = ("no gestures to check" if not self.gestures else
                        "more than one gesture matched" if found else "no gesture matched")
            return None
        self.why = ""
        self._quiet_until = segment.t1 + self.cooldown_s
        self._blocked.pop(found[0], None)
        for other in self.complements.get(found[0], ()):
            self._blocked[other] = segment.t1 + self.complement_s
        return found[0]
