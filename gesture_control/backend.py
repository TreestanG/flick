"""The real camera and tracker (file 02) behind the contract in `contract.py`.

Camera -> Tracker -> Frame -> Segmenter -> Segment, all on one thread. Each
Segment then goes to one place (file 03): the training run if one is
collecting samples, the idle clip if one is being recorded, otherwise the
detector, which fires a gesture when exactly one saved function says yes.

The agent runs on its own thread (`Training`), because a model call takes
seconds and tracking must not wait for it. Each action (file 04) runs on a
thread of its own for the same reason: `actions.py` for the built-in ones,
`chrome_bridge.py` for Chrome, `script_runner.py` for the user's scripts.
"""

from __future__ import annotations

import base64
import collections
import dataclasses
import importlib
import json
import sys
import queue
import re
import threading
import time
import traceback

from . import actions, claude_code, planner
from . import parts as P
from . import store
from .camera import Camera, device_index, device_name, probe
from .chrome_bridge import ChromeBridge
from .contract import Bus, PreviewFrame
from .dataset import Dataset, NegativePool
from .detector import COMPLEMENT_MS, Detector
from .model_client import ModelError, default_client
from . import script_runner
from .script_runner import ScriptRunner
from .segmenter import CHECK_S, Segment, Segmenter
from .tracker import IdleGate, Tracker
from .trainer import Cancelled, Result, Trainer, minimum_negatives

STATUS_S = 0.25
# Hands only. The face and pose models tripled the cost of every frame, and
# the frame rate with them was too low. Head parts are never visible now.
TRACKED = P.HAND_PARTS
CLIP_SIZE = (320, 240)      # the videos of training samples, shown on the review page
CLIP_QUALITY = 60
CLIP_KEEP_S = 6.0           # longer than the longest motion plus its pre-roll and quiet time
PREVIEW_SIZE = (640, 480)   # file 02: the preview stream is 640x480


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "gesture"


class Training:
    """One run of the agent, on its own thread. The tracking thread hands it
    motions through `offer`; the trainer takes them through `collect`.

    Samples are taken one at a time, each after a "1, 2" countdown, so the
    user is warned before every one. Recording starts the moment the countdown
    ends, on the assumption that the user is then moving, and stops when the
    motion does. Moving the hand back, or fidgeting in between, is ignored.

    Each sample comes with a short video. When a batch is complete the user
    reviews the videos and may record any sample again; the batch goes to the
    agent only when they submit it. Recording again replaces a sample, so it
    does not use up the budget."""

    COUNT_TO = 2            # "1", "2", then perform the motion
    STEP_S = 1.0            # how long each number stays up
    EARLY_S = 0.2           # a motion may start this much before the countdown ends
    QUIET_S = 0.15          # a motion is reported about this long after it ends
    USER_NEGATIVES = 0.5    # motions that are not the gesture, asked per positive

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
        # Re-training an existing gesture adds to what it was trained on.
        known = any(g["id"] == self.gesture_id for g in gestures)
        self.seed = dataset.positives(self.gesture_id) if known else []
        self.seed_code = (store.load_code(self.gesture_id) or "") if known else ""
        # Every run starts from samples performed in it. Samples of an earlier
        # run that was not saved used to be kept and used first, so a gesture
        # made again under the same name skipped its own samples and opened
        # with the motions that are not the gesture. What is left of those
        # files is removed here.
        dataset.clear_pending(self.gesture_id)
        self.client_factory = client_factory
        self.result: Result | None = None
        self.action: dict | None = None     # what the planner made of `action_text` (file 04)
        # Re-training only adds samples: with nothing new typed, the gesture
        # keeps the action and the motion description it already has.
        existing = next((g for g in gestures if g["id"] == self.gesture_id), None)
        if existing and not action_text.strip():
            self.action = dict(existing.get("action") or {}) or None
        if existing and not motion_text.strip():
            self.motion_text = existing.get("motion", "") or name
        # What the user added on the data dashboard about how it should work.
        self.notes = str((existing or {}).get("notes", "") or "")
        self.cancelled = threading.Event()
        self._restart = threading.Event()       # set by the Restart button while recording
        self._segments: queue.Queue = queue.Queue()     # (Segment, clip) while one is awaited
        self._reviews: queue.Queue = queue.Queue()      # ("submit", None) or ("rerecord", index)
        self._open_at: float | None = None      # when the countdown ended; None while it runs
        self.thread = threading.Thread(target=self._work, name="trainer", daemon=True)

    @property
    def active(self) -> bool:
        """Still running, so tracking keeps every part on and nothing fires."""
        return self.thread.is_alive() and not self.cancelled.is_set()

    @property
    def waiting(self) -> bool:
        """The countdown is over and the next motion will be taken as a sample."""
        return self._open_at is not None

    def offer(self, segment: Segment, clip: dict | None = None) -> None:
        """A motion the user made, with its video if the camera loop has one."""
        opened = self._open_at
        if opened is None:
            return                          # during the countdown and the review nothing counts
        started = time.monotonic() - (segment.t1 - segment.t0) - self.QUIET_S
        if started >= opened - self.EARLY_S:
            self._segments.put((segment, clip))

    def review(self, action: str, index: int | None = None) -> None:
        """The user's answer on the review page: "submit", "rerecord" one
        sample (or all of them, with no index), or "more" to add a sample."""
        self._reviews.put((action, index))

    def restart(self) -> None:
        """The user wants the sample now being taken again, from the countdown."""
        self._restart.set()

    def _pause(self, seconds: float) -> bool:
        """Wait this long. True as soon as the user asks for a restart."""
        end = time.monotonic() + seconds
        while True:
            if self.cancelled.is_set():
                raise Cancelled
            if self._restart.is_set():
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            self.cancelled.wait(min(left, 0.05))

    def _wait(self, source: queue.Queue):
        while True:
            if self.cancelled.is_set():
                raise Cancelled
            try:
                return source.get(timeout=0.1)
            except queue.Empty:
                continue

    def collect(self, count: int, phase: str, asked: int, budget: int,
                which: list | None = None, of: int | None = None) -> list[Segment]:
        got: list[Segment | None] = [None] * count
        # Negatives (motions that are not the gesture) are for this run only:
        # none are kept as the gesture's own samples, and they do not use up
        # the budget.
        negative = phase == "negatives"
        first = count                       # the user may add samples on the review page

        def total() -> int:
            """Samples asked of the user so far, this batch included."""
            return asked + (first if negative else count)

        def prompt(index: int, ready: int, back: bool = False) -> None:
            # When samples are being redone: which of the user's samples this
            # one replaces (`sample`, of `of`), and all that are being replaced.
            redo = which[index] if which and index < len(which) else None
            self.bus.emit("training_prompt", phase=phase, count=count, index=index,
                          asked=total(), budget=budget, ready=ready, back=back,
                          **({"sample": redo, "of": of, "which": list(which)} if which else {}))

        def record(index: int, back: bool = False) -> int:
            """Take sample `index` and return which one comes next. The user may
            restart: during the countdown that goes back to the sample just
            recorded (when `back` allows it), and once recording has begun it
            takes this one again. Either way from the countdown."""
            self._restart.clear()
            for number in range(1, self.COUNT_TO + 1):
                prompt(index, number, back)
                if self._pause(self.STEP_S):
                    return index - 1 if back else index
            while not self._segments.empty():
                self._segments.get_nowait()
            self._open_at = time.monotonic()
            prompt(index, 0)                # go
            try:
                while True:
                    if self._pause(0):
                        return index
                    try:
                        got[index], clip = self._segments.get(timeout=0.05)
                        break
                    except queue.Empty:
                        continue
            finally:
                self._open_at = None
            clip = clip or {"fps": 0, "frames": []}
            self.bus.emit("training_sample", index=index, count=count, phase=phase,
                          fps=clip["fps"], frames=clip["frames"])
            return index + 1

        index = 0
        while index < count:
            index = record(index, back=index > 0)
        prompt(count, 0)                    # the bar shows the batch as complete
        while True:                         # nothing reaches the agent until the user submits
            while not self._reviews.empty():
                self._reviews.get_nowait()
            # One more can be added while the budget allows (negatives are not
            # counted). Samples being redone replace named ones, so none there.
            more = phase != "redoing" and (negative or total() < budget)
            self.bus.emit("training_review", phase=phase, count=count, asked=total(),
                          budget=budget, more=more)
            action, index = self._wait(self._reviews)
            if action == "submit":
                return list(got)
            if action == "more":
                if more:
                    count += 1
                    got.append(None)
                    while record(count - 1) == count - 1:
                        pass
                    prompt(count, 0)
                continue
            again = range(count) if index is None else [index]
            for index in again:
                if isinstance(index, int) and 0 <= index < count:
                    while record(index) == index:
                        pass

    def _work(self) -> None:
        try:
            result = self._train()
        except Cancelled:
            return
        except ModelError as error:
            result = Result(False, str(error))
        except Exception as error:
            if type(error).__name__ == "ModelError":    # from a reloaded model_client
                result = Result(False, str(error))
            else:
                traceback.print_exc()
                result = Result(False, "Training stopped because of an internal error.")
        if self.cancelled.is_set():
            return
        self.result = result
        if result.passed:
            self.action = self.action or self._plan_action()
        self.bus.emit("training_result", passed=result.passed, message=result.message,
                      summary=actions.describe(self.action) if result.passed else "",
                      action=self.action if result.passed else None,
                      parts=list(result.needs), code=result.code,
                      retry=getattr(result, "retry", True))

    def _train(self) -> Result:
        ratio = int(self.settings["detection"]["negatives_per_positive"])
        negatives = self.dataset.negatives(self.other_ids)
        if len(negatives) < minimum_negatives(ratio):
            # Said before the user performs anything, and only when there is
            # next to nothing to test against. Trying again would end the same.
            return Result(False, f"Training needs at least {minimum_negatives(ratio)} recorded "
                                 f"everyday motions to test against, and there are "
                                 f"{len(negatives)}. Record more idle video in settings, "
                                 "moving around as you normally do.", retry=False)
        # The two files that decide how a run talks to the model are read again
        # at the start of every run, so a change to them applies to the next
        # run without restarting the engine. (Not when a test supplies its own
        # client.) Everything else in the engine still needs a restart.
        from . import model_client as clients, trainer as training
        if self.client_factory is default_client:
            try:
                clients = importlib.reload(clients)
                training = importlib.reload(training)
            except Exception:
                traceback.print_exc()           # a broken edit: keep what was loaded
            client = clients.default_client(self.settings.get("model_name", ""))
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
        trainer = training.Trainer(
            client, self.motion_text, NegativePool(negatives), self.collect, ratio=ratio,
            progress=lambda round, phase: self.bus.emit("training_progress", round=round,
                                                        phase=phase),
            on_step=lambda step, note: self.bus.emit("training_progress", round=trainer.round or 1,
                                                     phase="revising", step=step, note=note),
            trace=trace, seed=self.seed, seed_code=self.seed_code,
            user_negatives=self.USER_NEGATIVES, notes=self.notes)
        result = trainer.run()
        trace(f"finished: {'passed' if result.passed else 'not passed'} {result.message}".rstrip())
        return result

    def _plan_action(self) -> dict:
        """The sentence as an action from the catalog (file 04). A gesture
        with a hardcoded action keeps it. The model is asked only when this
        run uses the real one; otherwise the keyword rules decide."""
        fixed = actions.HARDCODED.get(self.gesture_id)
        if fixed:
            return dict(fixed)
        client = None
        if self.client_factory is default_client:
            try:
                client = default_client(self.settings.get("model_name", ""))
            except ModelError:
                client = None
        try:
            return planner.plan(self.action_text, client)
        except Exception:
            traceback.print_exc()
            return planner.guess(self.action_text)

    def draft(self) -> dict:
        """The gesture as it will be saved, with the planned action."""
        draft = {"id": self.gesture_id, "name": self.name, "motion": self.motion_text,
                 "parts": list(self.result.needs),
                 "action": self.action or self._plan_action()}
        if self.notes:
            draft["notes"] = self.notes
        return draft


class Backend:
    def __init__(self, bus: Bus, settings: dict, dump_path: str | None = None,
                 client_factory=default_client):
        self.bus = bus
        self.preview = True                 # the server turns frame making on and off
        self.armed = True
        self.testing = False                # a window is in test mode: matches show, no actions
        self.chrome = ChromeBridge(
            lambda connected, message: bus.emit("chrome_status", connected=connected,
                                                message=message))
        self.scripts = ScriptRunner()
        self._timers: list[tuple[float, object]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="backend", daemon=True)
        self.settings = settings
        self.gestures = actions.apply_hardcoded(store.load_gestures())
        self.dataset = Dataset()
        self.detector = Detector(settings["detection"]["cooldown_ms"],
                                 settings["detection"].get("complement_ms", COMPLEMENT_MS))
        self.detector.load(self.gestures)
        self.training: Training | None = None
        self.client_factory = client_factory
        self.camera = Camera(device_index(settings.get("camera_device", "")))
        self.tracker = Tracker()
        self.segmenter = Segmenter(settings["detection"])
        self.gate = IdleGate()
        self.on_segment: list = []          # callables taking a Segment, run on this thread
        self._clip: collections.deque = collections.deque()     # (t, JPEG) of recent frames
        self._check_at = 0.0                # when live recognition looks next
        self._checked: dict | None = None   # its last look: how many stretches, how long it took
        self._dump_path = dump_path

    def start(self) -> None:
        if sys.platform == "darwin":
            self.camera.open()              # the permission prompt needs the main thread
        self._thread.start()
        # The container Claude Code writes gesture functions in starts with
        # the engine, so it is ready by the time a gesture is trained.
        claude_code.start_in_background()

    def stop(self) -> None:
        threading.Thread(target=claude_code.stop, name="claude-container-stop").start()
        if self.training:
            self.training.cancelled.set()
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        self.chrome.stop()
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

    # actions and Chrome (file 04) ------------------------------------------

    def _cmd_connect_chrome(self) -> None:
        """The user pressed Connect Chrome: a newly installed extension may
        pair for the next two minutes."""
        self.chrome.start()
        self.chrome.pair()

    def _cmd_set_testing(self, on: bool) -> None:
        """A window turned test mode on or off. While it is on, every gesture
        is watched and each match is reported, but no action runs, armed or not."""
        self.testing = bool(on)

    def _cmd_test_action(self, gesture_id: str) -> None:
        for gesture in self.gestures:
            if gesture["id"] == gesture_id:
                self._run_action(gesture)

    def _refuse(self, name: str, problem) -> None:
        self.bus.emit("action_result", ok=False, gesture=name, message=str(problem))

    def _cmd_set_action(self, gesture_id: str, action) -> None:
        """The user changed a saved gesture's action in the action editor."""
        for gesture in self.gestures:
            if gesture["id"] == gesture_id:
                try:
                    gesture["action"] = actions.store_action(gesture_id, action)
                except (ValueError, OSError) as problem:
                    self._refuse(gesture["name"], problem)
                    return
                self._gestures_changed()

    def _cmd_run_script(self, action, gesture_id: str | None = None) -> None:
        """Run now: the script as typed, once, from a draft file beside the
        saved one. The result comes back as an action_result with run_now."""
        if gesture_id is None and self.training:
            gesture_id = self.training.gesture_id
        name = next((g["name"] for g in self.gestures if g["id"] == gesture_id), "Run now")
        try:
            if not gesture_id:
                raise ValueError("There is no gesture to run this for.")
            if not isinstance(action, dict):
                raise ValueError("Not a script.")
            clean = script_runner.check(action)
            draft = script_runner.save_script(gesture_id, clean["language"], clean["code"],
                                              clean["working_dir"], clean["timeout_s"],
                                              clean["skip_if_running"], stem="draft")
        except (ValueError, OSError) as problem:
            self.bus.emit("action_result", ok=False, gesture=name, message=str(problem),
                          run_now=True)
            return

        def work() -> None:
            ok, message = self.scripts.run({"id": gesture_id, "name": name}, draft)
            self.bus.emit("action_result", ok=ok, gesture=name, message=message, run_now=True)
        threading.Thread(target=work, name="run-now", daemon=True).start()

    def _run_action(self, gesture: dict) -> None:
        """Fire a gesture's action on its own thread, so a slow script or a
        Chrome call never holds up tracking. The result goes to any window
        and always to the engine's log."""
        def work() -> None:
            ok, message = actions.run(gesture, self.chrome, self.scripts)
            print(f"{time.strftime('%H:%M:%S')}  action  {gesture['name']}: "
                  f"{'ok' if ok else 'FAILED'}  {message}", flush=True)
            self.bus.emit("action_result", ok=ok, gesture=gesture["name"], message=message)
        threading.Thread(target=work, name="action", daemon=True).start()

    def _cmd_settings_changed(self, settings: dict) -> None:
        self.settings.clear()
        self.settings.update(settings)      # in place: a running Training shares this dict
        self.segmenter.configure(settings["detection"])
        self.detector.cooldown_s = settings["detection"]["cooldown_ms"] / 1000
        self.detector.complement_s = settings["detection"].get("complement_ms", COMPLEMENT_MS) / 1000
        self.camera.switch(device_index(settings.get("camera_device", "")))

    def _needed_parts(self) -> tuple:
        """Everything while training or recording an idle clip; otherwise only
        what the saved gestures use."""
        needed = self.detector.needed_parts()
        busy = (self.training and self.training.active) or self.dataset.recording
        if busy or not needed:
            return TRACKED
        return tuple(p for p in TRACKED if p in needed) or TRACKED

    # segments (file 03) -------------------------------------------------

    def _route(self, segment: Segment, t: float) -> str | None:
        """Hand a motion to whoever wants it. The name of the gesture it matched."""
        if self.training and self.training.active:
            self.training.offer(segment, self._clip_of(segment))
        elif self.dataset.recording:
            self.dataset.add_idle(segment)
        elif self.armed or self.testing:
            gesture_id = self.detector.on_segment(segment)
            gesture = next((g for g in self.gestures if g["id"] == gesture_id), None)
            if gesture:
                self.bus.emit("match", gesture=gesture["name"], id=gesture["id"],
                              ms=round((t - segment.t1) * 1000), test=self.testing)
                if not self.testing:        # test mode only shows the match
                    self._run_action(gesture)
                return gesture["name"]
        return None

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

    def _cmd_add_sample(self) -> None:
        if self.training:
            self.training.review("more")

    def _cmd_restart_sample(self) -> None:
        if self.training:
            self.training.restart()

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

    def _cmd_save_gesture(self, action=None) -> None:
        run, self.training = self.training, None
        if not run or not run.result or not run.result.passed:
            return
        draft = run.draft()
        if action is not None:              # the user chose another one on the result page
            try:
                draft["action"] = actions.store_action(draft["id"], action)
            except (ValueError, OSError) as problem:    # saved with the planned one
                self._refuse(draft["name"], problem)
        if draft["action"].get("kind") == "script" and "code" in draft["action"]:
            # The planner's own script, shown on the result page and now
            # confirmed by saving: write it to the gesture's folder.
            try:
                draft["action"] = actions.store_action(draft["id"], draft["action"])
            except (ValueError, OSError) as problem:
                self._refuse(draft["name"], problem)
                draft["action"] = dict(actions.NONE)
        store.save_code(draft["id"], run.result.code)
        self.dataset.save_positives(draft["id"], run.result.positives)
        self.dataset.clear_pending(draft["id"])
        old = next((g for g in self.gestures if g["id"] == draft["id"]), None)
        if old and old.get("complements"):      # re-training keeps its complements
            draft["complements"] = list(old["complements"])
        self.gestures = [g for g in self.gestures if g["id"] != draft["id"]] + [draft]
        self._gestures_changed()

    def _set_complements(self, gesture: dict, ids) -> None:
        """Make `ids` the gestures that complement this one. It goes both
        ways: each of them lists this gesture too, and no other one does."""
        known = {g["id"] for g in self.gestures} - {gesture["id"]}
        chosen = [i for i in dict.fromkeys(ids) if isinstance(i, str) and i in known]
        for other in self.gestures:
            mine = chosen if other is gesture else [
                i for i in other.get("complements", ()) if i != gesture["id"]
            ] + ([gesture["id"]] if other["id"] in chosen else [])
            if mine:
                other["complements"] = mine
            else:
                other.pop("complements", None)

    def _cmd_update_gesture(self, gesture_id: str, motion=None, notes=None, remove=(),
                            count=None, complements=None) -> None:
        """The data dashboard: change a saved gesture's description, the notes
        about how it should work, and drop some of its saved samples (by
        position; `count` is how many the window saw, so a list that changed
        meanwhile is left alone). The function is not rewritten here: these
        take effect the next time the gesture is trained. `complements` (ids
        of other gestures) takes effect at once: when one of a pair fires, the
        other cannot for the next few seconds."""
        gesture = next((g for g in self.gestures if g["id"] == gesture_id), None)
        if gesture is None:
            return
        if isinstance(complements, (list, tuple)):
            self._set_complements(gesture, complements)
        if isinstance(motion, str) and motion.strip():
            gesture["motion"] = motion.strip()
        if isinstance(notes, str):
            gesture["notes"] = notes.strip()
            if not gesture["notes"]:
                del gesture["notes"]
        remove = [i for i in (remove or ()) if isinstance(i, int) and not isinstance(i, bool)]
        if remove:
            if count is not None and count != len(self.dataset.positives(gesture_id)):
                self._refuse(gesture["name"], "Its samples changed in the meantime; none "
                                              "were removed. Open the data again.")
            else:
                self.dataset.remove_positives(gesture_id, remove)
        self._gestures_changed()

    def _cmd_delete_gesture(self, gesture_id: str) -> None:
        self.gestures = [g for g in self.gestures if g["id"] != gesture_id]
        for other in self.gestures:             # nothing complements a deleted gesture
            if gesture_id in other.get("complements", ()):
                other["complements"] = [i for i in other["complements"] if i != gesture_id]
                if not other["complements"]:
                    del other["complements"]
        store.delete_code(gesture_id)
        self.dataset.delete_gesture(gesture_id)
        self.dataset.clear_pending(gesture_id)
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
        self.chrome.start()                 # an extension that has paired connects by itself
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
                waiting = bool(self.training and self.training.active
                               and self.training.waiting)
                if waiting and not self.segmenter.eager:
                    self.segmenter.begin()      # the countdown just ended: record from here
                self.segmenter.eager = waiting
                segment = self.segmenter.update(frame)
                hand = any(P.group(name) != "head" for name in frame.visible())
                self.gate.note(t, hand or self.segmenter.speed > self.segmenter.stop_speed)
                busy = (self.training and self.training.active) or self.dataset.recording
                if not busy and (self.armed or self.testing):
                    # Live: all the time, whether or not anything seems to be
                    # moving, stretches of the last few seconds that end now go
                    # to the gesture functions. How often depends on how long
                    # that takes, so tracking keeps most of the time.
                    if time.monotonic() >= self._check_at:
                        began = time.monotonic()
                        windows = self.segmenter.windows()
                        for window in windows:
                            if self._route(window, t):
                                break
                        took = time.monotonic() - began
                        self._check_at = began + max(CHECK_S, 3 * took)
                        self._checked = {"windows": len(windows), "ms": round(took * 1000, 1)}
                else:
                    self._checked = None
                    if segment is not None:
                        self._route(segment, t)
                if segment is not None:
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
                         moving=self.segmenter.moving,
                         segment=self.segmenter.last if busy else None,
                         live=self._checked,
                         loading=self.tracker.loading, idle=self.gate.idle(t),
                         track_ms=round(self.tracker.last_ms, 1))
        finally:
            if dump:
                dump.close()
            self.camera.close()
            self.tracker.close()
