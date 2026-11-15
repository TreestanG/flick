"""Splits the Frame stream into discrete motions with a speed-based start/stop
state machine (file 02).

Speed is measured per group (left hand, right hand, head) as the mean speed of
that group's visible parts, in hand or head sizes per second. The overall speed
is the fastest group, not the mean of every part, so a still head does not
dilute a moving hand and training (all parts tracked) splits motions the same
way as runtime (only the needed parts).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from . import parts as P

PRE_ROLL_S = 0.15
SMOOTH_S = 0.05      # time constant of the speed filter
HELD_S = 0.15         # a part unchanged this long is still, not just between updates
LOST_S = 0.1          # the moving group may drop out this long before the motion is discarded
MIN_SCALE = 0.02


@dataclass(frozen=True)
class Segment:
    frames: tuple          # Frames, starting with the pre-roll
    t0: float              # when the speed crossed start_speed
    t1: float              # when the motion went quiet
    lead: str              # the group that was moving

    @property
    def duration_ms(self) -> float:
        return (self.t1 - self.t0) * 1000


class Segmenter:
    def __init__(self, settings: dict, parts=P.ALL_PARTS):
        self.configure(settings)
        self.set_parts(parts)
        self.speed = 0.0            # smoothed, for the debug readout
        self.moving = False
        self.last: dict | None = None      # outcome of the last motion: ms, kept, reason
        self._prev: dict = {}       # part -> (x, y, t)
        self._part_speed: dict = {}
        self._last_t: float | None = None
        self._buffer: deque = deque()
        self._frames: list = []
        self._lead = ""
        self._t0 = 0.0
        self._quiet: tuple | None = None    # (t, number of frames) when the speed dropped
        self._lost_at: float | None = None
        self._wait_quiet = False

    def configure(self, settings: dict) -> None:
        self.start_speed = float(settings["start_speed"])
        self.stop_speed = float(settings["stop_speed"])
        self.quiet_s = settings["quiet_ms"] / 1000
        self.min_s = settings["min_dur_ms"] / 1000
        self.max_s = settings["max_dur_ms"] / 1000

    def set_parts(self, parts) -> None:
        self.parts = tuple(parts)

    # speed -------------------------------------------------------------

    def _measure(self, frame: P.Frame) -> tuple[float, str]:
        """Raw speed of the fastest group, and which group that is."""
        for name in self.parts:
            p = frame.parts.get(name, P.MISSING)
            if p.state != P.VISIBLE:        # estimated parts only repeat what others say
                self._prev.pop(name, None)
                self._part_speed.pop(name, None)
                continue
            prev = self._prev.get(name)
            if prev is None:
                self._prev[name] = (p.x, p.y, frame.t)
                continue
            if (p.x, p.y) == prev[:2]:      # held between updates (slow head rate, idle skip)
                if frame.t - prev[2] > HELD_S:
                    self._part_speed[name] = 0.0
                continue
            dt = frame.t - prev[2]
            scale = frame.scale(P.group(name))
            if dt > 0 and scale and scale > MIN_SCALE:
                self._part_speed[name] = P.dist((p.x, p.y), prev, frame.aspect) / scale / dt
            self._prev[name] = (p.x, p.y, frame.t)
        totals: dict[str, list] = {}
        for name, speed in self._part_speed.items():
            totals.setdefault(P.group(name), []).append(speed)
        best, lead = 0.0, ""
        for group_name, speeds in totals.items():
            mean = sum(speeds) / len(speeds)
            if mean > best:
                best, lead = mean, group_name
        return best, lead

    def _group_visible(self, frame: P.Frame, group_name: str) -> bool:
        return any(frame.state(name) == P.VISIBLE for name in self.parts
                   if P.group(name) == group_name)

    # state machine -----------------------------------------------------

    def update(self, frame: P.Frame) -> Segment | None:
        raw, lead = self._measure(frame)
        dt = frame.t - self._last_t if self._last_t is not None else 0.0
        self._last_t = frame.t
        self.speed += (raw - self.speed) * (1 - math.exp(-dt / SMOOTH_S)) if dt > 0 else 0.0
        if self.moving:
            return self._while_moving(frame)
        self._buffer.append(frame)
        while frame.t - self._buffer[0].t > PRE_ROLL_S:
            self._buffer.popleft()
        if self._wait_quiet:
            self._wait_quiet = self.speed >= self.stop_speed
        elif self.speed > self.start_speed and lead:
            self.moving = True
            self._frames = list(self._buffer)
            self._lead, self._t0 = lead, frame.t
            self._quiet = self._lost_at = None
        return None

    def _while_moving(self, frame: P.Frame) -> Segment | None:
        self._frames.append(frame)
        if self._group_visible(frame, self._lead):
            self._lost_at = None
        else:
            self._lost_at = frame.t if self._lost_at is None else self._lost_at
            if frame.t - self._lost_at > LOST_S:
                return self._end(frame.t, "lost tracking")
        if self.speed < self.stop_speed:
            if self._quiet is None:
                self._quiet = (frame.t, len(self._frames))
            if frame.t - self._quiet[0] >= self.quiet_s:
                t1, count = self._quiet
                if self._lost_at is not None:
                    return self._end(t1, "lost tracking")
                if t1 - self._t0 < self.min_s:
                    return self._end(t1, "too short")
                segment = Segment(tuple(self._frames[:count]), self._t0, t1, self._lead)
                self._end(t1, "")
                return segment
        else:
            self._quiet = None
        if frame.t - self._t0 > self.max_s:
            self._wait_quiet = True         # do not start again in the middle of this motion
            return self._end(frame.t, "too long")
        return None

    def _end(self, t1: float, reason: str) -> None:
        self.last = {"ms": round((t1 - self._t0) * 1000), "kept": not reason,
                     "reason": reason or "kept", "t": round(t1, 3)}
        self.moving = False
        self._frames = []
        self._buffer.clear()
        return None
