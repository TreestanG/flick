"""The real camera and tracker (file 02) behind the contract in `contract.py`.

Camera -> Tracker -> Frame -> Segmenter -> Segment, all on one thread. Each
Segment goes to the callbacks in `on_segment`, where file 03's detector will
plug in. Training, saving and actions are still the fake ones inherited from
`FakeBackend` until files 03 and 04 exist, but there are no made-up matches.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
import time

from . import parts as P
from .camera import Camera, device_index, device_name, probe
from .contract import Bus, PreviewFrame
from .fake_backend import FakeBackend
from .segmenter import Segmenter
from .tracker import IdleGate, Tracker

STATUS_S = 0.25
PREVIEW_SIZE = (320, 240)


class Backend(FakeBackend):
    def __init__(self, bus: Bus, settings: dict, dump_path: str | None = None):
        super().__init__(bus)
        self.gestures = []                  # nothing is saved yet; no made-up gestures
        self.camera = Camera(device_index(settings.get("camera_device", "")))
        self.tracker = Tracker()
        self.segmenter = Segmenter(settings["detection"])
        self.gate = IdleGate()
        self.on_segment: list = []          # callables taking a Segment, run on this thread
        self._dump_path = dump_path

    def start(self) -> None:
        if sys.platform == "darwin":
            self.camera.open()              # the permission prompt needs the main thread
        super().start()

    def _cmd_settings_changed(self, settings: dict) -> None:
        self.segmenter.configure(settings["detection"])
        self.camera.switch(device_index(settings.get("camera_device", "")))

    def _needed_parts(self) -> tuple:
        """Everything while training; otherwise only what the saved gestures use."""
        needed = {part for g in self.gestures for part in g.get("parts", ())}
        if self._training or not needed:
            return P.ALL_PARTS
        return tuple(p for p in P.ALL_PARTS if p in needed)

    def _probe_cameras(self) -> None:
        names = [device_name(i) for i in [0] + probe()]
        self.bus.emit("cameras", devices=names)

    def _run(self) -> None:
        import cv2
        from PIL import Image

        emit = self.bus.emit
        emit("cameras", devices=[device_name(0)])
        emit("gestures_changed", gestures=list(self.gestures))
        emit("idle_videos", clips=[])
        emit("chrome_status", connected=False, message="Not connected")
        dump = open(self._dump_path, "w", encoding="utf-8") if self._dump_path else None
        start = time.monotonic()
        frame_no = frames_seen = 0
        last_status = start
        frame = None
        probed = False
        try:
            while not self._stop.is_set():
                now = self._now()
                self._drain_commands()
                for item in [x for x in self._timers if x[0] <= now]:
                    self._timers.remove(item)
                    item[1]()
                if self._training:
                    self._training_step(now)

                image = self.camera.read()
                wall = time.monotonic()
                if image is None:
                    if wall - last_status >= STATUS_S:
                        last_status = wall
                        emit("tracker_status", fps=0, camera_ok=False, visible=[], speed=0.0)
                    self._stop.wait(0.1)
                    continue
                if not probed:
                    probed = True
                    threading.Thread(target=self._probe_cameras, name="camera-probe",
                                     daemon=True).start()

                t = wall - start
                frame_no += 1
                frames_seen += 1
                if frame is None or self.gate.should_track(t, frame_no):
                    frame = self.tracker.process(image, t, frame_no)
                else:                       # idle: hold the last positions
                    frame = dataclasses.replace(frame, t=t, frame=frame_no)
                segment = self.segmenter.update(frame)
                hand = any(P.group(name) != "head" for name in frame.visible())
                self.gate.note(t, hand or self.segmenter.speed > self.segmenter.stop_speed)
                if segment is not None:
                    for callback in self.on_segment:
                        callback(segment)
                if dump:
                    dump.write(json.dumps(frame.to_dict()) + "\n")

                if self.preview and frame_no % 2 == 0:
                    small = cv2.cvtColor(cv2.resize(image, PREVIEW_SIZE), cv2.COLOR_BGR2RGB)
                    points = tuple((p.x, p.y) for p in frame.parts.values()
                                   if p.state != P.NOT_VISIBLE)
                    self.bus.preview.put(PreviewFrame(Image.fromarray(small), points))

                if wall - last_status >= STATUS_S:
                    fps = frames_seen / (wall - last_status)
                    last_status, frames_seen = wall, 0
                    needed = self._needed_parts()
                    self.tracker.set_needed(needed)
                    self.segmenter.set_parts(needed)
                    emit("tracker_status", fps=round(fps), camera_ok=True,
                         visible=frame.visible(), speed=round(self.segmenter.speed, 2),
                         moving=self.segmenter.moving, segment=self.segmenter.last,
                         loading=self.tracker.loading, idle=self.gate.idle(t),
                         track_ms=round(self.tracker.last_ms, 1))
        finally:
            if dump:
                dump.close()
            self.camera.close()
            self.tracker.close()
