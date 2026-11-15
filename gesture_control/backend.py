"""The real camera and tracker (file 02) behind the contract in `contract.py`.

Camera -> Tracker -> Frame -> Segmenter -> Segment, all on one thread. Each
Segment then goes to one place (file 03): the training run if one is
collecting samples, the idle clip if one is being recorded, otherwise the
detector, which fires a gesture when exactly one saved function says yes.

The agent runs on its own thread (`Training`), because a model call takes
seconds and tracking must not wait for it. Actions and Chrome are still
stand-ins until file 04 exists: an action reports ok without doing anything.
"""

from __future__ import annotations

import base64
import collections
import dataclasses
import json
import sys
import queue
import re
import threading
import time
import traceback

from . import parts as P
from . import store
from .camera import Camera, device_index, device_name, probe
from .contract import Bus, PreviewFrame
from .dataset import Dataset, NegativePool
from .detector import Detector
from .model_client import ChatClient, ModelError, default_client
from .segmenter import Segment, Segmenter
from .tracker import IdleGate, Tracker
from .trainer import Cancelled, Result, Trainer, minimum_negatives

STATUS_S = 0.25
CLIP_SIZE = (320, 240)      # the videos of training samples, shown on the review page
CLIP_QUALITY = 60
CLIP_KEEP_S = 6.0           # longer than the longest motion plus its pre-roll and quiet time
PREVIEW_SIZE = (640, 480)   # file 02: the preview stream is 640x480


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "gesture"


def _guess_action(sentence: str) -> dict:
    """Stand-in for file 04's planner: an action guessed from keywords."""
    text = sentence.lower()
    if "script" in text:
        return {"kind": "script", "language": "python", "file": "action.py",
                "working_dir": "", "timeout_s": 30, "skip_if_running": True}
    if any(word in text for word in ("tab", "chrome", "url", "browser")):
        return {"kind": "builtin", "id": "chrome.close_tab", "params": {}}
    return {"kind": "builtin", "id": "os.hotkey", "params": {}}


class Training:
    """One run of the agent, on its own thread. The tracking thread hands it
    motions through `offer`; the trainer takes them through `collect`.

    Samples are taken one at a time, each after a "1, 2" countdown, so the
    user is warned before every one and only a motion made after the countdown
    counts. Moving the hand back, or fidgeting in between, is ignored.

    Each sample comes with a short video. When a batch is complete the user
    reviews the videos and may record any sample again; the batch goes to the
    agent only when they submit it. Recording again replaces a sample, so it
    does not use up the budget."""

    COUNT_TO = 2            # "1", "2", then perform the motion
    STEP_S = 1.0            # how long each number stays up
    EARLY_S = 0.2           # a motion may start this much before the countdown ends
    QUIET_S = 0.15          # a motion is reported about this long after it ends

    def __init__(self, bus: Bus, dataset: Dataset, gestures: list[dict], settings: dict,
                 name: str, action_text: str, motion_text: str, gesture_id: str | None,
                 client_factory=default_client):
        self.bus, self.dataset, self.settings = bus, dataset, settings
        self.name, self.action_text, self.motion_text = name, action_text, motion_text
        taken = {g["id"] for g in gestures}
        self.gesture_id = gesture_id or _slug(name)
        while not gesture_id and self.gesture_id in taken:
            self.gesture_id += "-2"
        self.other_ids = [g["id"] for g in gestures if g["id"] != self.gesture_id]
        # Samples performed in an earlier run of this gesture that was not saved.
        # They are used first, in order, before the user is asked for any more.
        self._kept: list = dataset.pending(self.gesture_id)
        self._held: list = []           # the positives handed to the agent in this run
        self.client_factory = client_factory
        self.result: Result | None = None
        self.cancelled = threading.Event()
        self._segments: queue.Queue = queue.Queue()     # (Segment, clip) while one is awaited
        self._reviews: queue.Queue = queue.Queue()      # ("submit", None) or ("rerecord", index)
        self._open_at: float | None = None      # when the countdown ended; None while it runs
        self.thread = threading.Thread(target=self._work, name="trainer", daemon=True)

    @property
    def active(self) -> bool:
        """Still running, so tracking keeps every part on and nothing fires."""
        return self.thread.is_alive() and not self.cancelled.is_set()

    def offer(self, segment: Segment, clip: dict | None = None) -> None:
        """A motion the user made, with its video if the camera loop has one."""
        opened = self._open_at
        if opened is None:
            return                          # during the countdown and the review nothing counts
        started = time.monotonic() - (segment.t1 - segment.t0) - self.QUIET_S
        if started >= opened - self.EARLY_S:
            self._segments.put((segment, clip))

    def review(self, action: str, index: int | None = None) -> None:
        """The user's answer on the review page: "submit", or "rerecord" one
        sample (or all of them, with no index)."""
        self._reviews.put((action, index))

    def _wait(self, source: queue.Queue):
        while True:
            if self.cancelled.is_set():
                raise Cancelled
            try:
                return source.get(timeout=0.1)
            except queue.Empty:
                continue

    def collect(self, count: int, phase: str, asked: int, budget: int) -> list[Segment]:
        got: list[Segment | None] = [None] * count

        def prompt(index: int, ready: int) -> None:
            self.bus.emit("training_prompt", phase=phase, count=count, index=index,
                          asked=asked + count, budget=budget, ready=ready)

        def record(index: int) -> None:
            for number in range(1, self.COUNT_TO + 1):
                prompt(index, number)
                if self.cancelled.wait(self.STEP_S):
                    raise Cancelled
            while not self._segments.empty():
                self._segments.get_nowait()
            self._open_at = time.monotonic()
            prompt(index, 0)                # go
            try:
                got[index], clip = self._wait(self._segments)
            finally:
                self._open_at = None
            clip = clip or {"fps": 0, "frames": []}
            self.bus.emit("training_sample", index=index, count=count, phase=phase,
                          fps=clip["fps"], frames=clip["frames"])

        reused = self._kept[:count]         # from an earlier, unsaved run of this gesture
        self._kept = self._kept[count:]
        got[:len(reused)] = reused
        if len(reused) == count:
            prompt(count, 0)
            return self._hold(got)          # nothing new to perform or to review
        for index in range(len(reused), count):
            record(index)
        prompt(count, 0)                    # the bar shows the batch as complete
        while True:                         # nothing reaches the agent until the user submits
            while not self._reviews.empty():
                self._reviews.get_nowait()
            self.bus.emit("training_review", phase=phase, count=count, asked=asked + count,
                          budget=budget)
            action, index = self._wait(self._reviews)
            if action == "submit":
                return self._hold(got)
            again = range(count) if index is None else [index]
            for index in again:
                if isinstance(index, int) and 0 <= index < count:
                    record(index)

    def _hold(self, got: list) -> list:
        """Remember a finished batch, on disk too, until the gesture is saved."""
        self._held += got
        self.dataset.set_pending(self.gesture_id, self._held + self._kept)
        return list(got)

    def _drop(self, old: list) -> None:
        """The agent had these performed again: forget the ones they replace."""
        self._held = [s for s in self._held if not any(s is gone for gone in old)]
        self.dataset.set_pending(self.gesture_id, self._held + self._kept)

    def _work(self) -> None:
        try:
            result = self._train()
        except Cancelled:
            return
        except ModelError as error:
            result = Result(False, str(error))
        except Exception:
            traceback.print_exc()
            result = Result(False, "Training stopped because of an internal error.")
        if self.cancelled.is_set():
            return
        self.result = result
        sentence = self.action_text.strip().rstrip(".")
        self.bus.emit("training_result", passed=result.passed, message=result.message,
                      summary=sentence[:1].upper() + sentence[1:] if result.passed else "",
                      parts=list(result.needs), code=result.code)

    def _train(self) -> Result:
        ratio = int(self.settings["detection"]["negatives_per_positive"])
        negatives = self.dataset.negatives(self.other_ids)
        if len(negatives) < minimum_negatives(ratio):
            return Result(False, f"Training needs at least {minimum_negatives(ratio)} recorded "
                                 f"everyday motions to test against, and there are "
                                 f"{len(negatives)}. Record more idle video in settings, "
                                 "moving around as you normally do.")
        if self.settings.get("writer") == "chat" and self.client_factory is default_client:
            client = ChatClient()           # a Claude Code session answers, through a folder
        else:
            client = self.client_factory(self.settings.get("model_name", ""))
        log = store.runtime_dir() / "training.log"     # the last run, step by step

        def trace(line: str) -> None:
            try:
                with open(log, "a", encoding="utf-8") as file:
                    file.write(f"{time.strftime('%H:%M:%S')}  {line}\n")
            except OSError:
                pass
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  training \"{self.name}\": "
                           f"{self.motion_text}\n", encoding="utf-8")
        except OSError:
            pass
        trainer = Trainer(
            client, self.motion_text, NegativePool(negatives), self.collect, ratio=ratio,
            progress=lambda round, phase: self.bus.emit("training_progress", round=round,
                                                        phase=phase),
            on_step=lambda step, note: self.bus.emit("training_progress", round=trainer.round or 1,
                                                     phase="revising", step=step, note=note),
            trace=trace, replaced=self._drop)
        result = trainer.run()
        trace(f"finished: {'passed' if result.passed else 'not passed'} {result.message}".rstrip())
        return result

    def draft(self) -> dict:
        """The gesture as it will be saved (file 04 replaces the guessed action)."""
        return {"id": self.gesture_id, "name": self.name, "motion": self.motion_text,
                "parts": list(self.result.needs),
                "action": _guess_action(self.action_text.strip().rstrip("."))}


class Backend:
    def __init__(self, bus: Bus, settings: dict, dump_path: str | None = None,
                 client_factory=default_client):
        self.bus = bus
        self.preview = True                 # the server turns frame making on and off
        self.armed = True
        self.chrome_connected = False
        self._timers: list[tuple[float, object]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="backend", daemon=True)
        self.settings = settings
        self.gestures = store.load_gestures()
        self.dataset = Dataset()
        self.detector = Detector(settings["detection"]["cooldown_ms"])
        self.detector.load(self.gestures)
        self.training: Training | None = None
        self.client_factory = client_factory
        self.camera = Camera(device_index(settings.get("camera_device", "")))
        self.tracker = Tracker()
        self.segmenter = Segmenter(settings["detection"])
        self.gate = IdleGate()
        self.on_segment: list = []          # callables taking a Segment, run on this thread
        self._clip: collections.deque = collections.deque()     # (t, JPEG) of recent frames
        self._dump_path = dump_path

    def start(self) -> None:
        if sys.platform == "darwin":
            self.camera.open()              # the permission prompt needs the main thread
        self._thread.start()

    def stop(self) -> None:
        if self.training:
            self.training.cancelled.set()
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        self.dataset.stop_idle()            # quitting while recording keeps the clip

    # commands (run on the tracking thread) --------------------------------

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _later(self, delay: float, fn) -> None:
        self._timers.append((self._now() + delay, fn))

    def _drain_commands(self) -> None:
        while True:
            try:
                msg = self.bus.commands.get_nowait()
            except queue.Empty:
                return
            handler = getattr(self, "_cmd_" + msg.kind, None)
            if handler:
                handler(**msg.data)

    def _cmd_set_armed(self, armed: bool) -> None:
        self.armed = bool(armed)

    # actions and Chrome: stand-ins until file 04 ---------------------------

    def _cmd_connect_chrome(self) -> None:
        self.bus.emit("chrome_status", connected=False, message="Connecting…")

        def done():
            self.chrome_connected = True
            self.bus.emit("chrome_status", connected=True, message="Connected")
        self._later(1.0, done)

    def _cmd_test_action(self, gesture_id: str) -> None:
        for gesture in self.gestures:
            if gesture["id"] == gesture_id:
                self._run_action(gesture)

    def _run_action(self, gesture: dict) -> None:
        chrome = str(gesture["action"].get("id", "")).startswith("chrome.")
        if chrome and not self.chrome_connected:
            self.bus.emit("action_result", ok=False, gesture=gesture["name"],
                          message="Chrome is not connected")
        else:
            self.bus.emit("action_result", ok=True, gesture=gesture["name"], message="ok")

    def _cmd_settings_changed(self, settings: dict) -> None:
        self.settings.clear()
        self.settings.update(settings)      # in place: a running Training shares this dict
        self.segmenter.configure(settings["detection"])
        self.detector.cooldown_s = settings["detection"]["cooldown_ms"] / 1000
        self.camera.switch(device_index(settings.get("camera_device", "")))

    def _needed_parts(self) -> tuple:
        """Everything while training or recording an idle clip; otherwise only
        what the saved gestures use."""
        needed = self.detector.needed_parts()
        busy = (self.training and self.training.active) or self.dataset.recording
        if busy or not needed:
            return P.ALL_PARTS
        return tuple(p for p in P.ALL_PARTS if p in needed)

    # segments (file 03) -------------------------------------------------

    def _route(self, segment: Segment, t: float) -> None:
        if self.training and self.training.active:
            self.training.offer(segment, self._clip_of(segment))
        elif self.dataset.recording:
            self.dataset.add_idle(segment)
        elif self.armed:
            gesture_id = self.detector.on_segment(segment)
            gesture = next((g for g in self.gestures if g["id"] == gesture_id), None)
            if gesture:
                self.bus.emit("match", gesture=gesture["name"],
                              ms=round((t - segment.t1) * 1000))
                self._run_action(gesture)

    # training ----------------------------------------------------------

    def _keep_for_clips(self, image, t: float, frame_no: int) -> None:
        """While training, remember the last few seconds of camera images
        (small, every second frame) so each sample can be shown as a video."""
        import cv2
        if not (self.training and self.training.active):
            self._clip.clear()
            return
        if frame_no % 2:
            return
        ok, data = cv2.imencode(".jpg", cv2.resize(image, CLIP_SIZE),
                                [cv2.IMWRITE_JPEG_QUALITY, CLIP_QUALITY])
        if ok:
            self._clip.append((t, data.tobytes()))
        while self._clip and self._clip[0][0] < t - CLIP_KEEP_S:
            self._clip.popleft()

    def _clip_of(self, segment: Segment) -> dict:
        """The video of one motion: from its pre-roll to just after it went quiet."""
        first = segment.frames[0].t if segment.frames else segment.t0
        shots = [(t, data) for t, data in self._clip if first - 0.1 <= t <= segment.t1 + 0.15]
        if len(shots) < 2:
            return {"fps": 0, "frames": []}
        fps = (len(shots) - 1) / max(shots[-1][0] - shots[0][0], 1e-3)
        return {"fps": round(fps, 1),
                "frames": [base64.b64encode(data).decode("ascii") for _, data in shots]}

    def _cmd_submit_samples(self) -> None:
        if self.training:
            self.training.review("submit")

    def _cmd_rerecord_sample(self, index: int | None = None) -> None:
        if self.training:
            self.training.review("rerecord", index)

    def _cmd_start_training(self, name: str, action_text: str, motion_text: str,
                            gesture_id: str | None = None) -> None:
        self._cmd_cancel_training()
        self.training = Training(self.bus, self.dataset, self.gestures, self.settings, name,
                                 action_text, motion_text, gesture_id, self.client_factory)
        self.training.thread.start()

    def _cmd_cancel_training(self) -> None:
        if self.training:
            self.training.cancelled.set()
        self.training = None

    def _cmd_save_gesture(self) -> None:
        run, self.training = self.training, None
        if not run or not run.result or not run.result.passed:
            return
        draft = run.draft()
        store.save_code(draft["id"], run.result.code)
        self.dataset.save_positives(draft["id"], run.result.positives)
        self.dataset.clear_pending(draft["id"])
        self.gestures = [g for g in self.gestures if g["id"] != draft["id"]] + [draft]
        self._gestures_changed()

    def _cmd_delete_gesture(self, gesture_id: str) -> None:
        self.gestures = [g for g in self.gestures if g["id"] != gesture_id]
        store.delete_code(gesture_id)
        self.dataset.delete_gesture(gesture_id)
        self._gestures_changed()

    def _gestures_changed(self) -> None:
        store.save_gestures(self.gestures)
        self.detector.load(self.gestures)
        self.bus.emit("gestures_changed", gestures=list(self.gestures))

    # idle clips --------------------------------------------------------

    def _cmd_record_idle(self, on: bool) -> None:
        if on:
            self.dataset.start_idle()
        else:
            self.dataset.stop_idle()
            self.bus.emit("idle_videos", clips=self.dataset.idle_clips())

    def _cmd_delete_idle_video(self, clip_id: int) -> None:
        self.dataset.delete_idle(clip_id)
        self.bus.emit("idle_videos", clips=self.dataset.idle_clips())

    def _probe_cameras(self) -> None:
        names = [device_name(i) for i in [0] + probe()]
        self.bus.emit("cameras", devices=names)

    def _run(self) -> None:
        import cv2
        from PIL import Image

        emit = self.bus.emit
        emit("cameras", devices=[device_name(0)])
        emit("gestures_changed", gestures=list(self.gestures))
        emit("idle_videos", clips=self.dataset.idle_clips())
        self._cmd_connect_chrome()          # connects by itself; nobody has to ask
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
                self._keep_for_clips(image, t, frame_no)
                segment = self.segmenter.update(frame)
                hand = any(P.group(name) != "head" for name in frame.visible())
                self.gate.note(t, hand or self.segmenter.speed > self.segmenter.stop_speed)
                if segment is not None:
                    self._route(segment, t)
                    for callback in self.on_segment:
                        callback(segment)
                if dump:
                    dump.write(json.dumps(frame.to_dict()) + "\n")

                if self.preview:            # the newest frame; preview.py takes 15 a second
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
