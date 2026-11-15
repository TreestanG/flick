"""A stand-in for the tracker, agent and actions (files 02 to 04).

It speaks the contract in `contract.py` with made-up data so every screen can
be built and exercised before the real parts exist. Nothing here touches the
camera, the network, the keychain or the disk.

Training always passes, unless the motion description contains the word
"fail", which walks through the budget and ends in the not-passed outcome.
"""

from __future__ import annotations

import math
import queue
import random
import re
import threading
import time

from PIL import Image, ImageDraw

from .contract import Bus, PreviewFrame

BUDGET = 40
PREVIEW_SIZE = (480, 360)

SAMPLE_GESTURES = [
    {"id": "close-tab", "name": "close tab",
     "action": {"kind": "builtin", "id": "chrome.close_tab", "params": {}}},
    {"id": "minimize", "name": "minimize",
     "action": {"kind": "builtin", "id": "os.minimize_window", "params": {}}},
    {"id": "my-script", "name": "my script",
     "action": {"kind": "script", "language": "python", "file": "action.py",
                "working_dir": "", "timeout_s": 30, "skip_if_running": True}},
]

SAMPLE_CODE = '''NEEDS = ["right_index_tip", "nose"]

def detect(frames) -> bool:
    """Placeholder written by the fake back end."""
    xs = [f.part("right_index_tip") for f in frames]
    xs = [p[0] for p in xs if p is not None]
    if len(xs) < 5:
        return False
    return xs[-1] - xs[0] > 0.25
'''

HAND_PARTS = ("right_wrist", "right_palm_center", "right_thumb_tip", "right_index_tip",
              "right_middle_tip", "right_ring_tip", "right_pinky_tip")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "gesture"


def _guess_action(sentence: str) -> dict:
    text = sentence.lower()
    if "script" in text:
        return {"kind": "script", "language": "python", "file": "action.py",
                "working_dir": "", "timeout_s": 30, "skip_if_running": True}
    if any(word in text for word in ("tab", "chrome", "url", "browser")):
        return {"kind": "builtin", "id": "chrome.close_tab", "params": {}}
    return {"kind": "builtin", "id": "os.hotkey", "params": {}}


class FakeBackend:
    def __init__(self, bus: Bus, speed: float = 1.0, preview: bool = True):
        self.bus = bus
        self.speed = speed          # time multiplier, raised in tests
        self.preview = preview
        self.armed = True
        self.gestures = [dict(g) for g in SAMPLE_GESTURES]
        self.idle_videos: list[dict] = []
        self.chrome_connected = False
        self._idle_started: float | None = None
        self._training: dict | None = None
        self._draft: dict | None = None
        self._timers: list[tuple[float, object]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-backend", daemon=True)
        self._rng = random.Random(7)

    # lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    # loop --------------------------------------------------------------

    def _now(self) -> float:
        return time.monotonic() * self.speed

    def _later(self, delay: float, fn) -> None:
        self._timers.append((self._now() + delay, fn))

    def _run(self) -> None:
        emit = self.bus.emit
        emit("cameras", devices=["Default camera", "Test image"])
        emit("gestures_changed", gestures=list(self.gestures))
        emit("idle_videos", clips=[])
        emit("chrome_status", connected=False, message="Not connected")
        start = self._now()
        next_status = next_match = 0.0
        while not self._stop.is_set():
            now = self._now()
            t = now - start
            hand = (t % 11.0) < 8.0
            self._drain_commands()
            for item in [x for x in self._timers if x[0] <= now]:
                self._timers.remove(item)
                item[1]()
            if self._training:
                self._training_step(now)
            if self.preview:
                self.bus.preview.put(self._frame(t, hand))
            if now >= next_status:
                next_status = now + 0.5
                emit("tracker_status", fps=30, camera_ok=True,
                     visible=list(HAND_PARTS) + ["nose"] if hand else ["nose"],
                     speed=round(abs(math.sin(t * 1.3)) * 1.5, 2))
            if now >= next_match:
                next_match = now + 7.0
                if self.armed and hand and self.gestures and not self._training and t > 3:
                    gesture = self._rng.choice(self.gestures)
                    emit("match", gesture=gesture["name"], ms=self._rng.randint(180, 320))
                    self._run_action(gesture)
            time.sleep(1 / 15 / max(self.speed, 1.0))

    def _frame(self, t: float, hand: bool) -> PreviewFrame:
        w, h = PREVIEW_SIZE
        image = Image.new("RGB", (w, h), (46, 44, 42))
        draw = ImageDraw.Draw(image)
        draw.ellipse((w * 0.36, h * 0.2, w * 0.64, h * 0.72), fill=(70, 66, 62))
        points = [(0.5, 0.46)]
        if hand:
            cx = 0.7 + 0.12 * math.sin(t * 1.3)
            cy = 0.55 + 0.1 * math.cos(t * 0.9)
            draw.ellipse(((cx - 0.06) * w, (cy - 0.07) * h, (cx + 0.06) * w, (cy + 0.09) * h),
                         fill=(96, 90, 84))
            points += [(cx + dx, cy - 0.1 - abs(dx) * 0.2) for dx in (-0.05, -0.025, 0, 0.025)]
            points += [(cx + 0.07, cy), (cx, cy + 0.02)]
        return PreviewFrame(image, tuple(points))

    # commands ----------------------------------------------------------

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

    def _cmd_settings_changed(self, settings: dict) -> None:
        pass

    def _cmd_connect_chrome(self) -> None:
        self.bus.emit("chrome_status", connected=False, message="Connecting…")

        def done():
            self.chrome_connected = True
            self.bus.emit("chrome_status", connected=True, message="Connected")
        self._later(1.0, done)

    def _cmd_record_idle(self, on: bool) -> None:
        if on:
            self._idle_started = self._now()
        elif self._idle_started is not None:
            seconds = max(1, round(self._now() - self._idle_started))
            self._idle_started = None
            number = 1 + max((c["id"] for c in self.idle_videos), default=0)
            self.idle_videos.append({"id": number, "name": f"Idle clip {number}", "seconds": seconds})
            self.bus.emit("idle_videos", clips=list(self.idle_videos))

    def _cmd_delete_idle_video(self, clip_id: int) -> None:
        self.idle_videos = [c for c in self.idle_videos if c["id"] != clip_id]
        self.bus.emit("idle_videos", clips=list(self.idle_videos))

    def _cmd_delete_gesture(self, gesture_id: str) -> None:
        self.gestures = [g for g in self.gestures if g["id"] != gesture_id]
        self.bus.emit("gestures_changed", gestures=list(self.gestures))

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

    # training ----------------------------------------------------------

    def _cmd_start_training(self, name: str, action_text: str, motion_text: str,
                            gesture_id: str | None = None) -> None:
        will_fail = "fail" in motion_text.lower()
        self._draft = None
        self._training = {
            "name": name, "action_text": action_text, "gesture_id": gesture_id,
            "fail": will_fail, "asked": 0, "round": 0,
            "sample_gap": 0.35 if will_fail else 0.9,
        }
        self._begin_batch("collecting")

    def _cmd_cancel_training(self) -> None:
        self._training = None
        self._draft = None

    def _cmd_save_gesture(self) -> None:
        if not self._draft:
            return
        draft, self._draft = self._draft, None
        self.gestures = [g for g in self.gestures if g["id"] != draft["id"]] + [draft]
        self.bus.emit("gestures_changed", gestures=list(self.gestures))

    def _begin_batch(self, phase: str) -> None:
        tr = self._training
        remaining = BUDGET - tr["asked"]
        if remaining < 3:
            self._finish(False)
            return
        if phase == "collecting":
            tr["round"] += 1
        count = min(remaining, 10 if tr["fail"] else (4 if phase == "collecting" else 3))
        tr.update(phase=phase, count=count, index=0, asked=tr["asked"] + count,
                  next_at=self._now() + tr["sample_gap"])
        self.bus.emit("training_progress", round=tr["round"], phase=phase)
        self._prompt()

    def _prompt(self) -> None:
        tr = self._training
        self.bus.emit("training_prompt", phase=tr["phase"], count=tr["count"],
                      index=tr["index"], asked=tr["asked"], budget=BUDGET)

    def _training_step(self, now: float) -> None:
        tr = self._training
        if now < tr["next_at"]:
            return
        if tr["phase"] == "revising":
            self._begin_batch("testing" if not tr.get("retest") else "collecting")
            return
        tr["index"] += 1
        tr["next_at"] = now + tr["sample_gap"]
        self._prompt()
        if tr["index"] < tr["count"]:
            return
        if tr["phase"] == "collecting":
            self._revise(retest=False)       # write the function, then test it
        elif tr["fail"]:
            self._revise(retest=True)        # test failed: revise and collect again
        else:
            self._finish(True)

    def _revise(self, retest: bool) -> None:
        tr = self._training
        tr.update(phase="revising", retest=retest, next_at=self._now() + 1.2)
        self.bus.emit("training_progress", round=tr["round"], phase="revising")

    def _finish(self, passed: bool) -> None:
        tr, self._training = self._training, None
        if not passed:
            self.bus.emit(
                "training_result", passed=False, summary="", parts=[], code="",
                message="Couldn't tell this gesture apart from your other movements. "
                        "Try a more distinctive motion.")
            return
        sentence = tr["action_text"].strip().rstrip(".")
        self._draft = {"id": tr["gesture_id"] or _slug(tr["name"]), "name": tr["name"],
                       "action": _guess_action(sentence)}
        self.bus.emit("training_result", passed=True,
                      summary=sentence[:1].upper() + sentence[1:],
                      parts=["right_index_tip", "nose"], code=SAMPLE_CODE, message="")
