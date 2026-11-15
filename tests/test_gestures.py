import base64
"""File 03: sandbox, dataset, detector, trainer and their wiring into the back end."""

import queue
import random
import threading
import time

import pytest

from gesture_control import parts as P
from gesture_control import sandbox, store, trainer
from gesture_control.contract import Bus
from gesture_control.dataset import Dataset, NegativePool, segment_from_dict, segment_to_dict
from gesture_control.detector import Detector
from gesture_control.model_client import FireworksClient, ModelError
from gesture_control.segmenter import Segment
from gesture_control.store import DETECTION_DEFAULTS

HAND, HEAD = 0.1, 0.2       # sizes in image heights


def motion(dx=0.0, dy=0.0, n=20, t0=0.0, seed=0, hand=True) -> Segment:
    """The right hand moving in a straight line by (dx, dy), over a still head."""
    rng = random.Random(seed)
    frames = []
    for i in range(n):
        parts = dict.fromkeys(P.ALL_PARTS, P.MISSING)
        if hand:
            x = 0.3 + dx * i / (n - 1) + rng.uniform(-0.003, 0.003)
            y = 0.5 + dy * i / (n - 1) + rng.uniform(-0.003, 0.003)
            for j, name in enumerate(P.HAND_NAMES):
                parts["right_" + name] = P.Part(x, y + j * 0.01, 0.0, P.VISIBLE)
        for j, name in enumerate(P.HEAD_PARTS[:5]):
            parts[name] = P.Part(0.5 + j * 0.01, 0.3, 0.0, P.VISIBLE)
        frames.append(P.Frame(t0 + i / 30, i, (480, 480), parts,
                              {"right_palm_facing": "camera",
                               "scale": {"right_hand": HAND, "head": HEAD}}))
    return Segment(tuple(frames), t0 + 0.15, t0 + (n - 1) / 30, "right_hand")


def everyday(count: int, seed: int = 1) -> list[Segment]:
    """Motions that are not a swipe to the right."""
    rng = random.Random(seed)
    moves = [(-0.3, 0.0), (0.0, 0.3), (0.0, -0.3), (-0.2, 0.2), (0.05, 0.0), (0.02, 0.25)]
    return [motion(*moves[i % len(moves)], n=rng.randint(12, 30), seed=rng.random())
            for i in range(count)]


def swipe_code(threshold: float = 1.5, name: str = "right_index_tip") -> str:
    """A hand-written detect: the hand moves right by `threshold` hand sizes."""
    return f'''import numpy as np

NEEDS = ["{name}"]

def detect(frames) -> bool:
    if len(frames) < 5 or visible_fraction(frames, "{name}") < 0.8:
        return False
    xy = positions(frames, "{name}", unit="hand")
    xy = xy[~np.isnan(xy[:, 0])]
    if len(xy) < 5:
        return False
    dx, dy = xy[-1] - xy[0]
    steady = np.mean(np.diff(xy[:, 0]) > -0.05)
    return bool(dx > {threshold} and abs(dy) < 0.5 * dx and steady > 0.9)
'''


LEFT_CODE = swipe_code().replace("dx > 1.5", "dx < -1.5").replace("0.5 * dx", "0.5 * -dx") \
    .replace("> -0.05", "< 0.05")


# sandbox: the static check ----------------------------------------------

@pytest.mark.parametrize("bad", [
    "import os",
    "import numpy.lib",
    "from numpy import load",
    "x = frames.__class__",
    "x = eval('1')",
    "x = open('f')",
    "x = np.load('f')",
    "x = getattr(frames, 'a')",
    "x = '{0.__class__}'.format(frames)",
    "x = '{}'.format(1)",
    "while True:\n        pass",
    "x = frames[0].part('nose')",           # not listed in NEEDS
    "x = frames[0]._frame",
    "x = lambda: (yield)",
])
def test_check_rejects(bad):
    code = f"import numpy as np\nNEEDS = ['right_wrist']\ndef detect(frames):\n    {bad}\n" \
           "    return False\n"
    needs, problems = sandbox.check(code)
    assert problems, bad


def test_check_rejects_bad_shapes():
    assert sandbox.check("def detect(frames):\n    return False\n")[1]         # no NEEDS
    assert sandbox.check("NEEDS = ['elbow']\ndef detect(frames):\n    return False\n")[1]
    assert sandbox.check("NEEDS = ['nose']\n")[1]                              # no detect
    assert sandbox.check("NEEDS = ['nose']\ndef detect(a, b):\n    return False\n")[1]
    assert sandbox.check("NEEDS = ['nose']\nclass A: pass\ndef detect(f):\n    return 1\n")[1]
    assert sandbox.check("NEEDS = ['nose']\nprint(1)\ndef detect(f):\n    return False\n")[1]
    assert sandbox.check("def detect(frames:\n")[1]                             # syntax


def test_check_accepts_a_good_function():
    needs, problems = sandbox.check(swipe_code())
    assert needs == ["right_index_tip"] and not problems


# sandbox: loading and running -------------------------------------------

def test_run_tells_a_swipe_from_other_motions():
    gesture = sandbox.load(swipe_code())
    assert gesture.needs == ("right_index_tip",)
    assert sandbox.run(gesture, motion(0.3).frames) is True
    assert sandbox.run(gesture, motion(-0.3).frames) is False
    assert sandbox.run(gesture, motion(0.3, hand=False).frames) is False    # None handled
    assert sandbox.run(gesture, []) is False


def test_reading_a_part_outside_needs_is_an_error():
    # The name is built at run time, so only the FrameView can catch it.
    sneaky = ("NEEDS = ['right_wrist']\ndef detect(frames):\n"
              "    return frames[0].part('no' + 'se') is None\n")
    assert not sandbox.check(sneaky)[1]
    with pytest.raises(sandbox.NeedsError):
        sandbox.run(sandbox.load(sneaky), motion(0.3).frames)
    scale = ("NEEDS = ['right_wrist']\ndef detect(frames):\n"
             "    return frames[0].scale('head') is None\n")
    with pytest.raises(sandbox.NeedsError):
        sandbox.run(sandbox.load(scale), motion(0.3).frames)


def test_detect_must_return_a_bool_and_cannot_import_at_run_time():
    with pytest.raises(sandbox.SandboxError):
        sandbox.run(sandbox.load("NEEDS = ['nose']\ndef detect(frames):\n    return 1\n"), [])
    with pytest.raises(sandbox.SandboxError):
        sandbox.load("import os\nNEEDS = ['nose']\ndef detect(frames):\n    return False\n")


def test_helpers_use_hand_and_head_sizes():
    code = ("NEEDS = ['right_index_tip', 'nose']\n"
            "def detect(frames):\n"
            "    d = distance(frames[0], 'right_index_tip', 'nose', unit='head')\n"
            "    far = distance(frames[0], 'right_index_tip', 'nose', unit='hand')\n"
            "    rel = positions(frames, 'right_index_tip', relative_to='nose', unit='head')\n"
            "    ok = part_state(frames[0], 'nose') == 'visible' and times(frames)[-1] > 0.5\n"
            "    return bool(ok and abs(far - 2 * d) < 1e-6 and abs(rel[0][0] + 1.0) < 0.05)\n")
    # index tip starts at x=0.3 and the nose is at x=0.5: 0.2 image heights = 1 head size
    assert sandbox.run(sandbox.load(code), motion(0.3).frames) is True


# sandbox: the separate process ------------------------------------------

def test_evaluate_runs_in_a_separate_process():
    report = sandbox.evaluate(swipe_code(), [motion(0.3).frames, motion(-0.3).frames])
    assert report.ok and report.results == [True, False]
    assert report.needs == ("right_index_tip",) and report.slow_ms < sandbox.SLOW_MS
    # numpy loads parts of itself on first use; that must not look like a banned import
    lazy = ("import numpy as np\nimport math\nNEEDS = ['nose']\ndef detect(frames):\n"
            "    a = np.ones((3, 3))\n"
            "    return bool(a.sum() + np.linalg.norm(a) + np.median(a) + math.sqrt(4) < 0)\n")
    assert sandbox.evaluate(lazy, [[]]).results == [False]


def test_evaluate_rejects_true_on_empty_constant_or_random_input():
    always = "NEEDS = ['nose']\ndef detect(frames):\n    return True\n"
    report = sandbox.evaluate_in_process(always, [])
    assert not report.ok and len(report.sanity) == 4       # empty, hidden, still, random
    jump = ("NEEDS = ['right_wrist']\ndef detect(frames):\n"
            "    if len(frames) < 2:\n        return False\n"
            "    a, b = frames[0].part('right_wrist'), frames[-1].part('right_wrist')\n"
            "    return bool(a is not None and b is not None and b[0] - a[0] > 0.1)\n")
    report = sandbox.evaluate_in_process(jump, [motion(0.3).frames])
    assert report.results == [True] and any("random" in s for s in report.sanity)
    assert sandbox.evaluate_in_process(swipe_code(), []).ok


def test_evaluate_survives_slow_and_greedy_functions():
    slow = ("NEEDS = ['nose']\ndef detect(frames):\n"
            "    for a in range(10 ** 5):\n        for b in range(10 ** 5):\n            pass\n"
            "    return False\n")
    assert "timed out" in sandbox.evaluate(slow, [], timeout=3).error
    greedy = ("import numpy as np\nNEEDS = ['nose']\ndef detect(frames):\n"
              "    return bool(np.ones((60000, 60000)).sum() < 0)\n")
    report = sandbox.evaluate(greedy, [motion(0.3).frames], memory_mb=512)
    assert report.error or str(report.results[0]).startswith("error")
    assert sandbox.evaluate("import os\n", []).error            # never reaches a process


# dataset ----------------------------------------------------------------

def test_segment_round_trip_and_idle_clips(tmp_path):
    data = Dataset(tmp_path)
    one = motion(0.3)
    assert segment_from_dict(segment_to_dict(one)).frames[3] == P.Frame.from_dict(
        one.frames[3].to_dict())
    assert data.idle_clips() == [] and not data.recording
    data.start_idle()
    for segment in everyday(4):
        data.add_idle(segment)
    data.stop_idle()
    data.start_idle()
    data.stop_idle()                        # a clip with no motion in it
    clips = data.idle_clips()
    assert [c["id"] for c in clips] == [1, 2] and "4 motions" in clips[0]["name"]
    assert clips[0]["seconds"] >= 1 and len(data.idle_segments()) == 4
    data.delete_idle(1)
    assert [c["id"] for c in data.idle_clips()] == [2] and not data.idle_segments()


def test_idle_clip_survives_quitting_without_stop(tmp_path):
    data = Dataset(tmp_path)
    data.start_idle()
    for i, segment in enumerate(everyday(3)):
        data.add_idle(motion(-0.3, t0=10.0 * i))
    # the app dies here: no stop_idle. A new session reads the same folder.
    again = Dataset(tmp_path)
    assert len(again.idle_segments()) == 3
    clip = again.idle_clips()[0]
    assert "3 motions" in clip["name"] and clip["seconds"] == 20
    data.stop_idle()
    assert Dataset(tmp_path).idle_clips()[0]["seconds"] >= 1
    assert len(Dataset(tmp_path).idle_segments()) == 3


def test_everything_saved_is_there_in_the_next_session(backend, tmp_path):
    from gesture_control.backend import Backend
    backend._cmd_record_idle(True)
    for segment in everyday(60):
        backend._route(segment, 0.0)
    backend.stop()                      # quit mid-recording: the clip is closed, not lost
    backend._cmd_record_idle(True)
    backend._cmd_record_idle(False)
    backend._cmd_start_training("Swipe right", "close the current tab", "swipe right")
    perform(backend)
    backend._cmd_save_gesture()

    later = Backend(Bus(), store.Settings().to_dict())       # the next launch
    assert [g["name"] for g in later.gestures] == ["Swipe right"]
    assert list(later.detector.gestures) == ["swipe-right"]
    assert [c["id"] for c in later.dataset.idle_clips()] == [1, 2]
    assert len(later.dataset.idle_segments()) == 60
    assert later.detector.on_segment(motion(0.3)) == "swipe-right"


def test_only_one_engine_runs_at_a_time(tmp_path):
    from gesture_control.__main__ import claim_instance
    first = claim_instance(tmp_path)
    assert first is not None
    assert claim_instance(tmp_path) is None     # a second engine gives up
    first.close()
    again = claim_instance(tmp_path)            # the first one quit (or died): free again
    assert again is not None
    again.close()


# detector ---------------------------------------------------------------

def detector_with(codes: dict) -> Detector:
    detector = Detector(cooldown_ms=800)
    detector.load([{"id": key} for key in codes], read=codes.get)
    return detector


def test_detector_fires_only_when_exactly_one_matches():
    detector = detector_with({"right": swipe_code(), "left": LEFT_CODE})
    assert detector.needed_parts() == {"right_index_tip"}
    assert detector.on_segment(motion(0.3, t0=0)) == "right"
    assert detector.on_segment(motion(-0.3, t0=10)) == "left"
    assert detector.on_segment(motion(0.0, 0.3, t0=20)) is None
    twins = detector_with({"a": swipe_code(), "b": swipe_code(1.0)})
    assert twins.matches(motion(0.3)) == ["a", "b"] and twins.on_segment(motion(0.3)) is None


def test_detector_cooldown_and_bad_functions():
    detector = detector_with({"right": swipe_code(), "broken": "import os\n", "gone": None})
    assert set(detector.problems) == {"broken", "gone"} and list(detector.gestures) == ["right"]
    assert detector.on_segment(motion(0.3, t0=0.0)) == "right"
    assert detector.on_segment(motion(0.3, t0=1.0)) is None         # ended at 0.63, +0.8 s
    assert detector.on_segment(motion(0.3, t0=2.0)) == "right"
    crash = "NEEDS = ['nose']\ndef detect(frames):\n    return frames[99].t > 0\n"
    assert detector_with({"crash": crash}).on_segment(motion(0.3)) is None


def test_a_gesture_silences_its_complements_for_five_seconds():
    detector = Detector(cooldown_ms=800)
    codes = {"right": swipe_code(), "left": LEFT_CODE}
    detector.load([{"id": "right", "complements": ["left"]},
                   {"id": "left", "complements": ["right"]}], read=codes.get)
    assert detector.on_segment(motion(0.3, t0=0.0)) == "right"      # ended at 0.63
    assert detector.on_segment(motion(-0.3, t0=2.0)) is None        # the hand coming back
    assert detector.why == "its complement just triggered"
    assert detector.on_segment(motion(0.3, t0=3.0)) == "right"      # itself is not silenced
    assert detector.on_segment(motion(-0.3, t0=7.0)) is None        # 5 s from the later one
    assert detector.on_segment(motion(-0.3, t0=9.0)) == "left"
    assert detector.on_segment(motion(0.3, t0=11.0)) is None        # and the other way round
    plain = detector_with(codes)                                    # not a pair: no wait
    assert plain.on_segment(motion(0.3, t0=0.0)) == "right"
    assert plain.on_segment(motion(-0.3, t0=2.0)) == "left"


def test_complements_are_kept_both_ways(request):
    backend = request.getfixturevalue("backend")                    # defined further down
    backend.gestures = [{"id": i, "name": i, "action": {"kind": "none"}} for i in "abc"]
    pairs = lambda: {g["id"]: g.get("complements") for g in store.load_gestures()}
    backend._cmd_update_gesture("a", complements=["b", "a", "nope", "b"])
    assert pairs() == {"a": ["b"], "b": ["a"], "c": None}
    assert backend.detector.complements["b"] == {"a"}
    backend._cmd_update_gesture("a", complements=["c"])
    assert pairs() == {"a": ["c"], "b": None, "c": ["a"]}
    backend._cmd_update_gesture("a", notes="unrelated")             # left alone when not sent
    assert pairs()["a"] == ["c"]
    backend._cmd_delete_gesture("a")
    assert pairs() == {"b": None, "c": None}
    backend.stop()


# trainer ----------------------------------------------------------------

class Scripted:
    """A ModelClient that replies from a list; the last reply repeats."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def complete(self, messages):
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class User:
    """Performs the motion on request, and remembers what it was asked."""

    def __init__(self, sizes=(0.3,)):
        self.sizes, self.asked, self.count = sizes, [], 0

    def collect(self, count, phase, asked, budget):
        self.asked.append((count, phase))
        made = []
        for _ in range(count):
            dx = self.sizes[min(self.count // 3, len(self.sizes) - 1)]
            made.append(motion(dx + 0.01 * (self.count % 3), seed=self.count))
            self.count += 1
        return made


def block(code: str) -> str:
    return f"Here is the function.\n```python\n{code}```"


def make(client, user=None, negatives=60, **kwargs):
    user = user or User()
    kwargs.setdefault("evaluate", sandbox.evaluate_in_process)
    kwargs.setdefault("first_batch", 0)         # small batches keep these tests short
    kwargs.setdefault("user_negatives", 0)      # the test user performs only the gesture
    return trainer.Trainer(client, "swipe my right index finger to the right",
                           NegativePool(everyday(negatives), seed=1), user.collect,
                           **kwargs), user


def test_parse():
    assert trainer.parse("I need data.\nREQUEST 4") == ("request", 4)
    assert trainer.parse("**TEST 5**") == ("test", 5)
    assert trainer.parse("<think>REQUEST 9</think>\nCONFIDENT\nNOTE: moves left, not right") \
        == ("confident", "moves left, not right")
    assert trainer.parse(block(swipe_code()))[0] == "code"
    assert trainer.parse("```python\nx = 1\n```\nREQUEST 3") == ("request", 3)
    assert trainer.parse("I need a number.\nSTATS right_index_tip nose FOR P3 N7") \
        == ("stats", "right_index_tip nose FOR P3 N7")
    assert trainer.parse("hmm") == ("unclear", "")


def test_overview_and_stats_are_summaries_not_samples():
    positives = [(f"P{i + 1}", motion(0.3 + 0.01 * i, seed=i)) for i in range(4)]
    negatives = [(f"N{i + 1}", s) for i, s in enumerate(everyday(12))]
    text = trainer.overview(positives, negatives)
    assert text.startswith("Training samples: 4 positives (P1-P4), 12 negatives (N1-N12).")
    assert "Moving group: positives right_hand 4" in text
    row = next(line for line in text.splitlines() if line.strip().startswith("right_index_tip"))
    assert "100%" in row and "+" in row                 # visible, and it moves right
    assert "not visible in any positive" in text and "left_wrist" in text
    assert len(text) < 2500                             # the same size however many samples

    answer = trainer.stats(["right_index_tip"], positives, negatives)
    assert "positives (4): visible 100% | average position" in answer
    assert "negatives (12)" in answer and "net dx +" in answer and "travel" in answer
    one = trainer.stats(["right_index_tip"], positives, negatives, [positives[2]])
    assert "  P3: " in one and "positives (4)" not in one
    pair = trainer.stats(["right_index_tip", "right_wrist"], positives, negatives)
    assert "distance right_index_tip to right_wrist, in right_hand sizes" in pair
    assert trainer.measure(positives[0][1], "left_wrist") is None


def test_stats_action_covers_only_what_the_model_may_see():
    client = Scripted("STATS right_index_tip", "REQUEST 4", "STATS right_index_tip FOR P5",
                      "STATS elbow", "STATS right_index_tip nose FOR P2 N3",
                      block(swipe_code()), "TEST 5", "STATS right_index_tip FOR P5",
                      "CONFIDENT")
    run, _ = make(client)
    assert run.run().passed
    said = [call[-1]["content"] for call in client.calls]
    assert "no samples yet" in said[1]
    assert "Collected 4 new positives (P1-P4) and 12 new negatives (N1-N12)" in said[2]
    assert "P5 is not a training sample" in said[3]         # not collected yet
    assert "elbow is not one" in said[4]
    assert "  P2: " in said[5] and "  N3: " in said[5] and "distance right_index_tip to nose" in said[5]
    assert "  P5: " in said[8]                              # tested, so now it may be asked about


def test_training_passes_and_fresh_samples_stay_hidden_until_tested():
    client = Scripted("REQUEST 4", block(swipe_code()), "TEST 5",
                      "CONFIDENT\nNOTE: matches the description")
    run, user = make(client, evaluate=sandbox.evaluate)        # the real separate process
    result = run.run()
    assert result.passed and result.needs == ("right_index_tip",)
    assert result.message == "matches the description" and "def detect" in result.code
    assert user.asked == [(4, "collecting"), (5, "testing")] and len(result.positives) == 9
    assert run.fresh_positives == 5 and run.fresh_negatives == 15
    seen_before_test = " ".join(m["content"] for m in client.calls[2])
    assert "(P1-P4)" in seen_before_test and "(N1-N12)" in seen_before_test
    assert "P5" not in seen_before_test and "N13" not in seen_before_test
    assert "(P5-P9 and N13-N27)" in client.calls[3][-1]["content"]      # named after the test
    assert len(seen_before_test) < 12000                # no sample tables, only an overview
    assert "4 of 40 (36 remain)" in client.calls[1][-1]["content"]


def test_notes_reach_the_model_with_the_description():
    client = Scripted("REQUEST 4", block(swipe_code()), "TEST 5", "CONFIDENT")
    run, _ = make(client, notes="the other fingers stay curled")
    assert run.run().passed
    first = client.calls[0][-1]["content"]
    assert "swipe my right index finger" in first and "the other fingers stay curled" in first


def test_confident_needs_a_clean_test_and_enough_fresh_data():
    client = Scripted("CONFIDENT", "REQUEST 3", "CONFIDENT", "TEST 3", block(swipe_code()),
                      "CONFIDENT", "TEST 3", "CONFIDENT", "TEST 3", "CONFIDENT")
    run, user = make(client)
    result = run.run()
    assert result.passed and run.fresh_positives == 6
    assert user.asked == [(3, "collecting"), (3, "testing"), (3, "testing")]
    assert len(client.calls) == 10 and not client.replies[1:]


def test_failed_test_becomes_training_data_and_the_function_is_revised():
    # Big swipes first, smaller ones later: a threshold fitted to the first
    # batch fails on fresh samples.
    client = Scripted("REQUEST 3", block(swipe_code(2.8)), "TEST 3", block(swipe_code(2.8)),
                      block(swipe_code(1.5)), "TEST 5", "CONFIDENT")
    run, user = make(client, User(sizes=(0.32, 0.2)))
    result = run.run()
    assert result.passed and "dx > 1.5" in result.code and run.round == 2
    failed = client.calls[3][-1]["content"]
    assert "Test failed" in failed and "P4 is a positive but detect returned False" in failed
    assert "not right on the training samples" in client.calls[4][-1]["content"]


def test_budget_is_a_hard_cap():
    run, user = make(Scripted("REQUEST 10"), negatives=200)
    result = run.run()
    assert not result.passed and result.message == trainer.NOT_PASSED and not result.code
    assert run.asked == 40 and sum(count for count, _ in user.asked) == 40
    run, user = make(Scripted("REQUEST 99"), negatives=200, budget=7)
    run.run()
    assert [count for count, _ in user.asked] == [7]            # clamped to what is left


def test_first_ten_are_collected_before_the_model_is_asked():
    client = Scripted(block(swipe_code()), "TEST 5", "CONFIDENT")
    run, user = make(client, first_batch=trainer.FIRST_BATCH)
    assert run.run().passed and trainer.FIRST_BATCH == 10
    assert user.asked == [(10, "collecting"), (5, "testing")] and len(run.negatives) == 45
    assert len(client.calls) == 3               # no call spent on asking for the first batch
    assert "collects the first 10 itself" in client.calls[0][0]["content"]
    assert "Collected 10 new positives" in client.calls[0][-1]["content"]


def test_gives_up_after_too_many_replies_in_a_round():
    client = Scripted("REQUEST 3", "let me think about it")
    run, _ = make(client)
    result = run.run()
    assert not result.passed and len(client.calls) == 1 + trainer.CALLS_PER_ROUND


def test_asks_for_more_idle_video_when_negatives_run_out():
    client = Scripted("REQUEST 3", block(swipe_code()), "TEST 3")
    run, _ = make(client, negatives=12)
    result = run.run()
    assert not result.passed and "idle video" in result.message
    assert trainer.minimum_negatives(3) == trainer.FEWEST_NEGATIVES


# model client -------------------------------------------------------------

def test_fireworks_client_names_and_errors(monkeypatch):
    from gesture_control import keychain, model_client
    assert FireworksClient("k").model == model_client.DEFAULT_MODEL
    assert FireworksClient("k", "glm-5p3").model == "accounts/fireworks/models/glm-5p3"
    assert FireworksClient("k", "moonshotai/kimi-k3.1-pro").model == model_client.DEFAULT_MODEL
    client = FireworksClient("k", base_url="http://127.0.0.1:9", timeout=2)
    with pytest.raises(ModelError, match="reach"):
        client.complete([{"role": "user", "content": "hi"}])
    monkeypatch.delenv(keychain.ENV_VAR, raising=False)
    monkeypatch.setattr(keychain, "_keyring", lambda: type("R", (), {
        "get_password": staticmethod(lambda service, account: None)}))
    with pytest.raises(ModelError, match="API key"):
        model_client.default_client()


# back end -----------------------------------------------------------------

@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("GESTURE_CONTROL_HOME", str(tmp_path))
    from gesture_control.backend import Backend, Training
    monkeypatch.setattr(Training, "STEP_S", 0.0)        # no waiting through the countdown
    monkeypatch.setattr(Training, "EARLY_S", 99.0)      # test motions have no real start time
    monkeypatch.setattr(Training, "USER_NEGATIVES", 0)  # the test user performs only the gesture
    client = Scripted("REQUEST 3", block(swipe_code()), "TEST 5", "CONFIDENT")
    settings = store.Settings().to_dict()
    settings["detection"] = dict(DETECTION_DEFAULTS, cooldown_ms=0)
    return Backend(Bus(), settings, client_factory=lambda model: client)   # never started


def events(bus, kind):
    found = []
    while True:
        try:
            msg = bus.events.get_nowait()
        except queue.Empty:
            return found
        if msg.kind == kind:
            found.append(msg.data)


def perform(backend, dx=0.3, timeout=20.0):
    """Keep making the motion until the training run stops asking."""
    end, t = time.monotonic() + timeout, 0.0
    while backend.training.thread.is_alive() and time.monotonic() < end:
        t += 2.0
        backend._route(motion(dx, t0=t, seed=t), t + 1)
        backend._cmd_submit_samples()       # the user is happy with every batch
        time.sleep(0.01)
    assert not backend.training.thread.is_alive()


def test_training_needs_idle_video_first(backend):
    backend._cmd_start_training("swipe", "close the current tab", "swipe right")
    backend.training.thread.join(5)
    result = events(backend.bus, "training_result")[-1]
    assert not result["passed"] and "idle video" in result["message"]
    backend._cmd_save_gesture()
    assert store.load_gestures() == []


def test_train_save_recognise_and_delete(backend):
    backend._cmd_record_idle(True)
    for segment in everyday(60):
        backend._route(segment, 0.0)             # recorded, not detected
    backend._cmd_record_idle(False)
    assert "60 motions" in events(backend.bus, "idle_videos")[-1]["clips"][0]["name"]

    backend._cmd_start_training("Swipe right", "close the current tab", "swipe right")
    assert backend._needed_parts() == P.HAND_PARTS
    perform(backend)
    prompts = events(backend.bus, "training_prompt")
    result = backend.training.result
    assert result.passed and len(result.positives) == 15    # ten to learn from, five to test
    assert prompts[0] == {"phase": "collecting", "count": 10, "index": 0, "asked": 10,
                          "budget": 40, "ready": 1, "back": False}
    assert [p["ready"] for p in prompts[:7]] == [1, 2, 0, 1, 2, 0, 1]   # 1, 2, go, per sample
    assert [p["index"] for p in prompts[:7]] == [0, 0, 0, 1, 1, 1, 2]
    assert prompts[-1] == {"phase": "testing", "count": 5, "index": 5, "asked": 15,
                           "budget": 40, "ready": 0, "back": False}
    assert not backend.gestures                  # nothing is saved until the user says so

    backend._cmd_save_gesture()
    saved = store.load_gestures()
    assert [g["id"] for g in saved] == ["swipe-right"]
    assert saved[0]["parts"] == ["right_index_tip"] and saved[0]["motion"] == "swipe right"
    assert saved[0]["action"]["id"] == "chrome.close_tab"
    assert "def detect" in store.load_code("swipe-right")
    assert len(backend.dataset.positives("swipe-right")) == 15
    assert backend._needed_parts() == ("right_index_tip",)

    events(backend.bus, "match")
    backend._route(motion(-0.3, t0=100), 100.9)
    backend._route(motion(0.3, t0=200), 200.9)
    matches = events(backend.bus, "match")
    assert [m["gesture"] for m in matches] == ["Swipe right"] and 200 < matches[0]["ms"] < 300
    backend.armed = False
    backend._route(motion(0.3, t0=300), 300.9)
    assert not events(backend.bus, "match")

    # The data dashboard: the description, notes and saved samples can be edited.
    backend._cmd_update_gesture("swipe-right", motion="swipe right, fast",
                                notes="only the index finger", remove=[0, 3], count=15)
    saved = store.load_gestures()[0]
    assert saved["motion"] == "swipe right, fast" and saved["notes"] == "only the index finger"
    assert len(backend.dataset.positives("swipe-right")) == 13
    backend._cmd_update_gesture("swipe-right", remove=[0], count=15)    # a stale list
    assert len(backend.dataset.positives("swipe-right")) == 13
    assert not events(backend.bus, "action_result")[-1]["ok"]
    views = backend.dataset.sample_views("swipe-right")
    assert len(views) == 13 and views[0]["lead"] == "right_hand" and views[0]["seconds"] > 0
    assert "right_index_tip" in views[0]["frames"][0]["points"]
    backend._cmd_start_training("Swipe right", "", "", "swipe-right")   # re-training reads them
    assert backend.training.notes == "only the index finger"
    assert backend.training.motion_text == "swipe right, fast" and len(backend.training.seed) == 13
    backend._cmd_cancel_training()
    backend._cmd_update_gesture("swipe-right", notes="")
    assert "notes" not in store.load_gestures()[0]

    backend._cmd_delete_gesture("swipe-right")
    assert not store.load_gestures() and store.load_code("swipe-right") is None
    assert not backend.detector.gestures and not backend.dataset.positives("swipe-right")


def test_motions_during_the_countdown_do_not_count(backend, monkeypatch):
    from gesture_control.backend import Training
    monkeypatch.setattr(Training, "STEP_S", 0.15)
    monkeypatch.setattr(Training, "EARLY_S", 0.2)
    run = Training(backend.bus, backend.dataset, [], backend.settings, "x", "mute", "swipe",
                   None)
    got = []
    worker = threading.Thread(target=lambda: got.extend(run.collect(1, "collecting", 0, 40)))
    worker.start()
    time.sleep(0.1)
    run.offer(motion(0.3))                      # still counting: ignored
    time.sleep(0.35)                            # the countdown (0.3 s) is over
    run.offer(motion(0.3, n=40))                # 1.3 s long, so it began before "go"
    assert not got and worker.is_alive()
    time.sleep(0.6)
    late = motion(0.3, n=12)                    # 0.37 s long: began after "go"
    run.offer(late)
    time.sleep(0.3)
    assert not got and worker.is_alive()        # the batch waits for the user's review
    run.review("submit")
    worker.join(5)
    assert got == [late]
    assert [p["ready"] for p in events(backend.bus, "training_prompt")] == [1, 2, 0, 0]
    run.offer(motion(0.3))                      # nothing is being collected any more
    assert run._segments.empty()


def test_a_batch_is_reviewed_and_samples_can_be_recorded_again(backend, monkeypatch):
    from gesture_control.backend import Training
    monkeypatch.setattr(Training, "STEP_S", 0.0)        # no waiting through the countdown
    monkeypatch.setattr(Training, "EARLY_S", 99.0)      # test motions have no real start time
    run = Training(backend.bus, backend.dataset, [], backend.settings, "x", "mute", "swipe",
                   None)
    got = []
    def work():
        try:
            got.extend(run.collect(2, "collecting", 0, 40))
        except Exception:
            pass                                # cancelled at the end of a failed test
    worker = threading.Thread(target=work, daemon=True)
    worker.start()

    def give(segment, clip=None):
        """Perform one motion once the run is waiting for it."""
        end = time.monotonic() + 5
        while run._open_at is None and time.monotonic() < end:
            time.sleep(0.01)
        run.offer(segment, clip)

    def wait_review():
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if events(backend.bus, "training_review"):
                return
            time.sleep(0.02)
        raise AssertionError("no review")

    first, fumbled, redone = motion(0.3, seed=1), motion(0.3, seed=2), motion(0.3, seed=3)
    try:
        give(first, {"fps": 15.0, "frames": ["aaaa", "bbbb"]})
        time.sleep(0.05)
        give(fumbled)
        wait_review()
        samples = events(backend.bus, "training_sample")
        run.offer(motion(0.3, seed=9))          # moving during the review counts for nothing
        assert worker.is_alive() and not got

        run.review("rerecord", 7)               # no such sample: asks again, changes nothing
        wait_review()
        run.review("rerecord", 1)
        give(redone)
        wait_review()
        run.review("submit")
        worker.join(5)
    finally:
        run.cancelled.set()
    assert got == [first, redone]               # the fumbled one never reaches the agent
    assert run._segments.empty()


def test_the_video_of_a_sample_covers_its_motion(backend):
    segment = motion(0.3, t0=10.0)
    start, end = segment.frames[0].t, segment.t1
    for i in range(200):                        # 20 s of camera images, ten a second
        backend._clip.append((i / 10, b"\xff\xd8 image %d" % i))
    clip = backend._clip_of(segment)
    times = [int(base64.b64decode(f).split()[-1]) / 10 for f in clip["frames"]]
    assert times and times[0] >= start - 0.11 and times[-1] <= end + 0.16
    assert times[0] <= start + 0.1 and times[-1] >= end - 0.1      # the whole motion
    assert 9 <= clip["fps"] <= 11
    backend._clip.clear()
    assert backend._clip_of(segment) == {"fps": 0, "frames": []}


def test_cancel_saves_nothing(backend):
    backend.dataset.save_positives("other", everyday(60))
    backend.gestures = [{"id": "other", "name": "other", "action": {}}]
    backend._cmd_start_training("swipe", "mute", "swipe right")
    run = backend.training
    time.sleep(0.3)
    backend._cmd_cancel_training()
    run.thread.join(5)
    assert not run.thread.is_alive() and run.result is None and backend.training is None
    backend._cmd_save_gesture()
    assert not events(backend.bus, "training_result") and store.load_gestures() == []
