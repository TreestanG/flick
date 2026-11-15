"""Recorded motions (file 03): positives per gesture, idle clips, and the pool
of negatives a training run draws from.

    data/gestures/<gesture id>.jsonl   one positive Segment per line
    data/idle/<clip id>.jsonl          a header line, then one Segment per line

Everything is written as it happens: an idle clip is on disk from the moment
recording starts and grows with each motion, so quitting the app (or losing
power) in the middle of a recording keeps what was recorded so far.

An idle clip is the user doing nothing in particular while the same tracker
and segmenter run, so it holds exactly the motions that would otherwise reach
the detector by accident.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

from . import parts as P
from . import store
from .segmenter import Segment


def segment_to_dict(segment: Segment) -> dict:
    return {"t0": round(segment.t0, 4), "t1": round(segment.t1, 4), "lead": segment.lead,
            "frames": [f.to_dict() for f in segment.frames]}


def segment_from_dict(raw: dict) -> Segment:
    return Segment(tuple(P.Frame.from_dict(f) for f in raw["frames"]), raw["t0"], raw["t1"],
                   raw.get("lead", ""))


def sample_view(segment: Segment) -> dict:
    """One sample for a window to draw: its length, which group moved, and
    where each visible part is in every frame. No video is kept of a saved
    sample, so the tracked points are all there is to show."""
    frames = segment.frames
    t0 = frames[0].t if frames else 0.0
    return {"seconds": round(frames[-1].t - t0, 2) if frames else 0.0,
            "lead": segment.lead,
            "aspect": round(frames[0].aspect, 3) if frames else 1.0,
            "frames": [{"t": round(f.t - t0, 3),
                        "points": {name: [round(p.x, 4), round(p.y, 4)]
                                   for name, p in f.parts.items() if p.state != P.NOT_VISIBLE}}
                       for f in frames]}


def _read(path: Path) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as file:
            return [json.loads(line) for line in file if line.strip()]
    except (OSError, ValueError):
        return []


class NegativePool:
    """Negatives for one training run. Each is handed out once, so a test
    batch is always made of motions the agent has never seen."""

    def __init__(self, segments, seed: int | None = None):
        self._left = list(segments)
        random.Random(seed).shuffle(self._left)

    def __len__(self) -> int:
        return len(self._left)

    def take(self, count: int) -> list[Segment]:
        taken, self._left = self._left[:count], self._left[count:]
        return taken


class Dataset:
    def __init__(self, root: Path | None = None):
        self.root = root or store.data_dir()
        self._idle: dict | None = None      # the clip being recorded

    # positives ---------------------------------------------------------

    def _gesture_path(self, gesture_id: str) -> Path:
        return self.root / "gestures" / f"{gesture_id}.jsonl"

    def save_positives(self, gesture_id: str, segments) -> None:
        path = self._gesture_path(gesture_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            for segment in segments:
                file.write(json.dumps(segment_to_dict(segment)) + "\n")

    def positives(self, gesture_id: str) -> list[Segment]:
        return [segment_from_dict(raw) for raw in _read(self._gesture_path(gesture_id))]

    def remove_positives(self, gesture_id: str, indices) -> int:
        """Drop saved positives by position (the data dashboard). Returns how
        many are left."""
        gone = set(indices)
        kept = [raw for i, raw in enumerate(_read(self._gesture_path(gesture_id)))
                if i not in gone]
        self.save_positives(gesture_id, [segment_from_dict(raw) for raw in kept])
        return len(kept)

    def sample_views(self, gesture_id: str) -> list[dict]:
        """The saved positives as the data dashboard draws them."""
        return [sample_view(segment) for segment in self.positives(gesture_id)]

    # left over from unsaved runs --------------------------------------------

    def clear_pending(self, gesture_id: str) -> None:
        """Remove the samples an older version kept from a run that was not
        saved. Nothing writes these files any more."""
        (self.root / "in_progress" / f"{gesture_id}.jsonl").unlink(missing_ok=True)

    def delete_gesture(self, gesture_id: str) -> None:
        self._gesture_path(gesture_id).unlink(missing_ok=True)

    # idle clips --------------------------------------------------------

    def _idle_paths(self) -> list[Path]:
        paths = [p for p in (self.root / "idle").glob("*.jsonl") if p.stem.isdigit()]
        return sorted(paths, key=lambda p: int(p.stem))

    def idle_clips(self) -> list[dict]:
        """For the settings screen: id, name, seconds."""
        clips = []
        for path in self._idle_paths():
            lines = _read(path)
            if not lines:
                continue
            motions = len(lines) - 1
            seconds = lines[0].get("seconds", 0)
            if not seconds and motions:     # never stopped properly: go by the motions
                seconds = max(1, round(lines[-1]["t1"] - lines[1]["t0"]))
            clips.append({"id": int(path.stem), "seconds": seconds,
                          "name": f"Idle clip {path.stem}  ·  {motions} "
                                  f"motion{'' if motions == 1 else 's'}"})
        return clips

    @property
    def recording(self) -> bool:
        return self._idle is not None

    def _header(self, seconds: int, created: float) -> str:
        """A fixed-width first line, so the length can be filled in at the end."""
        return json.dumps({"seconds": seconds, "created": round(created)}).ljust(63) + "\n"

    def start_idle(self) -> None:
        self.stop_idle()
        numbers = [int(p.stem) for p in self._idle_paths()]
        path = self.root / "idle" / f"{max(numbers, default=0) + 1}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        created = time.time()
        file = open(path, "w+", encoding="utf-8", newline="\n")
        file.write(self._header(0, created))
        file.flush()
        self._idle = {"started": time.monotonic(), "created": created, "file": file}

    def add_idle(self, segment: Segment) -> None:
        if self._idle is not None:
            self._idle["file"].write(json.dumps(segment_to_dict(segment)) + "\n")
            self._idle["file"].flush()

    def stop_idle(self) -> None:
        clip, self._idle = self._idle, None
        if clip is None:
            return
        seconds = max(1, round(time.monotonic() - clip["started"]))
        with clip["file"] as file:
            file.seek(0)
            file.write(self._header(seconds, clip["created"]))

    def delete_idle(self, clip_id: int) -> None:
        (self.root / "idle" / f"{clip_id}.jsonl").unlink(missing_ok=True)

    def idle_segments(self) -> list[Segment]:
        return [segment_from_dict(raw) for path in self._idle_paths()
                for raw in _read(path)[1:]]

    # negatives ---------------------------------------------------------

    def negatives(self, gesture_ids, exclude: str | None = None) -> list[Segment]:
        """Everything that is not the gesture being trained: the other saved
        gestures' positives and every motion from the idle clips."""
        found = []
        for gesture_id in gesture_ids:
            if gesture_id != exclude:
                found += self.positives(gesture_id)
        return found + self.idle_segments()
