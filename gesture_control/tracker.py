"""Runs the MediaPipe models that the needed parts call for and outputs a Frame.

- Only the needed models run (`set_needed`).
- The head models run at about 10 Hz and the last head position is held in between.
- Model files are downloaded once, in the background, and cached in the config folder.
"""

from __future__ import annotations

import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import parts as P
from .store import config_dir

_BASE = "https://storage.googleapis.com/mediapipe-models/"
MODEL_URLS = {
    "hand": _BASE + "hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
    "pose": _BASE + "pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task",
    "face": _BASE + "face_landmarker/face_landmarker/float16/1/face_landmarker.task",
}
DOWNLOAD_RETRY_S = 30.0
HEAD_HZ = 10.0


class IdleGate:
    """Knows when there has been no hand and no motion for a while. It can
    then track only every `every`-th frame, but by default every frame is
    still tracked: skipping made a hand that came into view already moving be
    picked up late, and a fluid gesture lost its beginning."""

    def __init__(self, idle_after: float = 2.0, every: int = 1):
        self.idle_after, self.every = idle_after, every
        self._active_at = 0.0

    def note(self, t: float, active: bool) -> None:
        if active:
            self._active_at = t

    def idle(self, t: float) -> bool:
        return t - self._active_at > self.idle_after

    def should_track(self, t: float, frame_no: int) -> bool:
        return not self.idle(t) or frame_no % self.every == 0


class Tracker:
    def __init__(self, needed=P.ALL_PARTS, models_dir: Path | None = None):
        self.models_dir = models_dir or config_dir() / "models"
        self.models = P.models_for(needed)
        self.last_ms = 0.0                     # time spent in the models for the last frame
        self.model_ms: dict[str, float] = {}   # the last run of each model
        self._landmarkers: dict = {}
        self._downloads: dict[str, float] = {}     # model -> time of the last failure, 0 if running
        self._lock = threading.Lock()
        self._ts: dict[str, int] = {}
        self._head_t = {"pose": -1.0, "face": -1.0}
        self._head: dict = {"pose": ({}, None), "face": ({}, None)}

    def set_needed(self, needed) -> None:
        self.models = P.models_for(needed)

    @property
    def loading(self) -> bool:
        return any(m not in self._landmarkers for m in self.models)

    # models ------------------------------------------------------------

    def _download(self, name: str, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            urllib.request.urlretrieve(MODEL_URLS[name], tmp)
            tmp.replace(path)
            with self._lock:
                self._downloads.pop(name, None)
        except Exception as error:              # no network, disk full, ...
            print(f"model download failed ({name}): {error}", file=sys.stderr)
            with self._lock:
                self._downloads[name] = time.monotonic()

    def _landmarker(self, name: str):
        """The loaded model, or None while its file is still downloading."""
        if name in self._landmarkers:
            return self._landmarkers[name]
        path = self.models_dir / Path(MODEL_URLS[name]).name
        with self._lock:
            failed_at = self._downloads.get(name)
            if not path.exists():
                if failed_at is None or (failed_at and
                                         time.monotonic() - failed_at > DOWNLOAD_RETRY_S):
                    self._downloads[name] = 0
                    threading.Thread(target=self._download, args=(name, path),
                                     name=f"download-{name}", daemon=True).start()
                return None
            if failed_at == 0:
                return None                     # still being written
        from mediapipe.tasks.python import BaseOptions, vision
        base = BaseOptions(model_asset_path=str(path))
        mode = vision.RunningMode.VIDEO
        if name == "hand":
            made = vision.HandLandmarker.create_from_options(
                vision.HandLandmarkerOptions(base_options=base, running_mode=mode, num_hands=2))
        elif name == "pose":
            made = vision.PoseLandmarker.create_from_options(
                vision.PoseLandmarkerOptions(base_options=base, running_mode=mode))
        else:
            made = vision.FaceLandmarker.create_from_options(
                vision.FaceLandmarkerOptions(base_options=base, running_mode=mode))
        self._landmarkers[name] = made
        return made

    def _detect(self, name: str, image, t: float):
        model = self._landmarker(name)
        if model is None:
            return None
        ts = max(int(t * 1000), self._ts.get(name, -1) + 1)     # must always increase
        self._ts[name] = ts
        started = time.perf_counter()
        result = model.detect_for_video(image, ts)
        self.model_ms[name] = (time.perf_counter() - started) * 1000
        return result

    # frames ------------------------------------------------------------

    def process(self, bgr, t: float, frame_no: int) -> P.Frame:
        import cv2
        import mediapipe as mp

        started = time.perf_counter()
        h, w = bgr.shape[:2]
        aspect = w / h
        image = mp.Image(image_format=mp.ImageFormat.SRGB,
                         data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        found = dict.fromkeys(P.ALL_PARTS, P.MISSING)
        derived: dict = {"scale": {}}

        if "hand" in self.models:
            result = self._detect("hand", image, t)
            if result is not None:
                for lm, handed in zip(result.hand_landmarks, result.handedness):
                    side = P.person_side(handed[0].category_name)
                    if f"{side}_hand" in derived["scale"]:
                        continue                # two hands given the same label: keep the first
                    hand, facing, size = P.hand_parts(side, lm, aspect)
                    found.update(hand)
                    derived[f"{side}_palm_facing"] = facing
                    derived["scale"][f"{side}_hand"] = round(size, 4)

        # Each head model runs at about HEAD_HZ, and at most one of them per
        # frame, so no single frame pays for all three models.
        head_models = [m for m in ("pose", "face") if m in self.models]
        due = [m for m in head_models if t - self._head_t[m] >= 1.0 / HEAD_HZ - 0.005]
        if due:
            name = min(due, key=self._head_t.get)
            self._head_t[name] = t
            self._head[name] = self._track_head(name, image, t, aspect)
        for name in head_models:                # the face model is the more precise one,
            head, size = self._head[name]       # so it goes last and wins
            found.update(head)
            if size:
                derived["scale"]["head"] = round(size, 4)

        self.last_ms = (time.perf_counter() - started) * 1000
        return P.Frame(t, frame_no, (w, h), found, derived)

    def _track_head(self, name: str, image, t: float, aspect: float):
        result = self._detect(name, image, t)
        if result is None:
            return {}, None
        if name == "pose" and result.pose_landmarks:
            return P.head_from_pose(result.pose_landmarks[0], aspect)
        if name == "face" and result.face_landmarks:
            return P.head_from_face(result.face_landmarks[0], aspect)
        return {}, None

    def close(self) -> None:
        for model in self._landmarkers.values():
            model.close()
        self._landmarkers.clear()
