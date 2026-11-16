"""Splits the Frame stream into discrete motions with a speed-based start/stop
state machine (file 02).

Speed is measured per group (left hand, right hand, head) as the mean speed of
that group's visible parts, in hand or head sizes per second. The overall speed
is the fastest group, not the mean of every part, so a still head does not
dilute a moving hand and training (all parts tracked) splits motions the same
way as runtime (only the needed parts).
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from dataclasses import dataclass

from . import parts as P

PRE_ROLL_S = 0.15
SMOOTH_S = 0.05      # time constant of the speed filter
HELD_S = 0.15         # a part unchanged this long is still, not just between updates
LOST_S = 0.1          # the moving group may drop out this long before the motion is discarded
MIN_SCALE = 0.02
HISTORY_S = 5.0       # live: how far back frames are kept
LOOK_BACK_S = (0.4, 0.6, 0.8, 1.0, 1.5, 2.0, 3.0)    # live: the stretches checked, ending now
CHECK_S = 0.1         # live recognition looks at most this often
# A motion has stopped when its speed is this small a share of the fastest it
# reached. A fixed stop speed alone sits below the tracker's own jitter on some
# cameras, so a hand held still after a motion never counted as stopped.
END_SHARE = 0.15


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
        # A training sample starts when its countdown ends (see `begin`). If that
        # one is discarded, the next motion is taken to begin as soon as anything
        # moves faster than stop_speed, not start_speed, so a slow performance
        # is recorded too.
        self.eager = False
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
        self._history: deque = deque()      # (frame, smoothed speed, lead), the last HISTORY_S
        self._begin = False         # start a motion on the next frame, whatever the speed
        self._seen = True           # this motion has moved, so going quiet ends it
        self._peak = 0.0            # the fastest this motion has been

    def begin(self) -> None:
        """Take a motion to start on the next frame: the countdown of a training
        sample has just ended and the user is assumed to be moving. It replaces
        a motion already under way and ends, as usual, when the motion stops."""
        self._begin = True

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

    # looking back (live recognition) -------------------------------------

    def windows(self) -> list:
        """What live recognition checks: stretches of the last few seconds
        that all end at the newest frame. Nothing has to start, stop or hold
        still first. One goes back for as long as there has been motion (to
        where the speed last fell to a small share of the fastest since); the
        others go back fixed lengths, since a gesture function is written for
        a whole motion and nothing says where this one began. Longest last."""
        items = list(self._history)
        if len(items) < 3:
            return []
        times = [item[0].t for item in items]
        found = []
        for i in _starts(times, [item[1] for item in items]):
            fastest = max(range(i, len(items)), key=lambda j: items[j][1])
            found.append(Segment(tuple(item[0] for item in items[i:]), times[i], times[-1],
                                 items[fastest][2]))
        return found

    # state machine -----------------------------------------------------

    def _note(self, frame: P.Frame) -> str:
        """Take in one frame: the smoothed speed, and which group is moving."""
        raw, lead = self._measure(frame)
        dt = frame.t - self._last_t if self._last_t is not None else 0.0
        self._last_t = frame.t
        self.speed += (raw - self.speed) * (1 - math.exp(-dt / SMOOTH_S)) if dt > 0 else 0.0
        return lead

    def update(self, frame: P.Frame) -> Segment | None:
        lead = self._note(frame)
        self._history.append((frame, self.speed, lead))
        while frame.t - self._history[0][0].t > HISTORY_S:
            self._history.popleft()
        if self._begin:
            self._begin = self._wait_quiet = False
            self.moving = True
            self._frames = []               # no pre-roll: the sample starts at "go"
            self._lead, self._t0 = lead, frame.t
            self._quiet = self._lost_at = None
            # Someone a moment late has not moved yet; that is not the end of it.
            self._seen = self.speed >= self.stop_speed and bool(lead)
            self._peak = self.speed
        if self.moving:
            return self._while_moving(frame, lead)
        self._buffer.append(frame)
        while frame.t - self._buffer[0].t > PRE_ROLL_S:
            self._buffer.popleft()
        if self._wait_quiet:
            self._wait_quiet = self.speed >= self.stop_speed
        elif self.speed > (self.stop_speed if self.eager else self.start_speed) and lead:
            self.moving = True
            self._frames = list(self._buffer)
            self._lead, self._t0 = lead, frame.t
            self._quiet = self._lost_at = None
            self._seen = True
            self._peak = self.speed
        return None

    def _while_moving(self, frame: P.Frame, lead: str = "") -> Segment | None:
        self._frames.append(frame)
        if not self._seen:                  # begun on "go": wait for the first movement
            if self.speed >= self.stop_speed and lead:
                self._seen, self._lead = True, lead
        elif self._group_visible(frame, self._lead):
            self._lost_at = None
        else:
            self._lost_at = frame.t if self._lost_at is None else self._lost_at
            if frame.t - self._lost_at > LOST_S:
                return self._end(frame.t, "lost tracking")
        self._peak = max(self._peak, self.speed)
        if not self._seen:
            pass
        elif self.speed < max(self.stop_speed, END_SHARE * self._peak):
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


# the stretches live recognition checks -------------------------------------

def _starts(times: list, speeds: list) -> list:
    """Where the stretches that end at the newest frame begin, shortest first.
    One goes back for as long as there has been motion; the others go back
    the fixed lengths of LOOK_BACK_S, as far as there are frames."""
    count, now = len(times), times[-1]
    starts = []
    peak = 0.0
    for i in range(count - 1, -1, -1):
        if peak > 0 and speeds[i] <= END_SHARE * peak:
            starts.append(i)
            break
        peak = max(peak, speeds[i])
    for back in LOOK_BACK_S:
        starts.append(bisect.bisect_left(times, now - back))
        if times[0] > now - back:
            break                       # there are no frames further back than this
    return [i for i in sorted(set(starts), reverse=True) if count - i >= 3]


# `cuts` only follows the speed; the start and stop settings play no part in it.
_NO_SETTINGS = {"start_speed": 0, "stop_speed": 0, "quiet_ms": 0, "min_dur_ms": 0, "max_dur_ms": 0}


def cuts(frames, every: float = CHECK_S) -> list:
    """The stretches of one recorded motion that live recognition would have
    checked while it was happening, as (start, end) indexes into `frames`
    (end not included). Live, `Segmenter.windows` is looked at every `every`
    seconds, so a gesture function also sees a motion half done, and its
    parts on their own. The whole motion is left out: that is the sample
    itself. Frames from before the motion are not in a sample, so the longer
    stretches stop at its first frame. All parts count towards the speed
    here, as in training; live, only the parts the functions need do."""
    watch = Segmenter(_NO_SETTINGS)
    times, speeds, found = [], [], []
    checked = None
    for count, frame in enumerate(frames, 1):
        watch._note(frame)
        times.append(frame.t)
        speeds.append(watch.speed)
        if count < len(frames) and checked is not None and frame.t - checked < every:
            continue
        checked = frame.t
        found += [(i, count) for i in _starts(times, speeds)]
    return [cut for cut in dict.fromkeys(found) if cut != (0, len(frames))]
