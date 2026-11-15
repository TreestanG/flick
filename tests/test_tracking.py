import json
from types import SimpleNamespace as NS

from gesture_control import parts as P
from gesture_control.camera import device_index, device_name
from gesture_control.segmenter import Segmenter
from gesture_control.store import DETECTION_DEFAULTS
from gesture_control.tracker import IdleGate

ASPECT = 640 / 480


# parts -----------------------------------------------------------------

def test_models_follow_needed_parts():
    assert P.models_for(["right_index_tip"]) == {"hand"}
    assert P.models_for(["nose"]) == {"pose"}
    assert P.models_for(["nose", "bottom_of_head"]) == {"face"}
    assert P.models_for(["left_ear", "top_of_head", "left_wrist"]) == {"hand", "pose", "face"}
    assert P.models_for(P.ALL_PARTS) == {"hand", "pose", "face"}
    assert P.group("left_eye") == "head" and P.group("left_wrist") == "left_hand"


def hand(thumb_side: int):
    """An upright hand at the image centre. thumb_side is +1 when the thumb
    (and index knuckle) is toward the image's right."""
    lm = [NS(x=0.5, y=0.6, z=0.0) for _ in range(21)]
    for i, dx in ((P.INDEX_MCP, 0.04), (P.MIDDLE_MCP, 0.015), (P.RING_MCP, -0.015),
                  (P.PINKY_MCP, -0.04)):
        lm[i] = NS(x=0.5 + dx * thumb_side, y=0.48, z=0.0)
    for i in P.TIPS.values():
        lm[i] = NS(x=0.5, y=0.35, z=0.0)
    return lm


def test_hand_parts_and_palm_facing():
    # A right palm shown to the camera has its thumb on the image's right.
    parts, facing, size = P.hand_parts("right", hand(+1), ASPECT)
    assert facing == "camera" and set(parts) == {p for p in P.HAND_PARTS if p[0] == "r"}
    assert abs(size - P.dist((0.5, 0.6), (0.515, 0.48), ASPECT)) < 1e-9
    assert abs(parts["right_palm_center"].y - (0.6 + 4 * 0.48) / 5) < 1e-9
    assert P.palm_facing("right", hand(-1), ASPECT) == "away"
    assert P.palm_facing("left", hand(-1), ASPECT) == "camera"
    edge_on = hand(+1)
    edge_on[P.PINKY_MCP] = NS(x=0.538, y=0.49, z=0.0)
    assert P.palm_facing("right", edge_on, ASPECT) == "unknown"
    assert P.person_side("Left") == "right" and P.person_side("Right") == "left"


def test_out_of_frame_and_low_confidence_are_not_visible():
    lm = hand(+1)
    lm[P.TIPS["index_tip"]] = NS(x=0.5, y=-0.1, z=0.0)
    parts, _, _ = P.hand_parts("left", lm, ASPECT)
    assert parts["left_index_tip"].state == P.NOT_VISIBLE
    pose = [NS(x=0.5, y=0.4, z=0.0, visibility=0.9, presence=0.9) for _ in range(33)]
    pose[P.POSE["left_eye"]] = NS(x=0.53, y=0.37, z=0, visibility=0.9, presence=0.9)
    pose[P.POSE["right_eye"]] = NS(x=0.47, y=0.37, z=0, visibility=0.9, presence=0.9)
    pose[P.POSE["left_ear"]] = NS(x=0.6, y=0.4, z=0, visibility=0.2, presence=0.9)
    head, size = P.head_from_pose(pose, ASPECT)
    assert head["left_ear"].state == P.NOT_VISIBLE and head["right_ear"].state == P.VISIBLE
    assert abs(size - 0.06 * ASPECT * P.EYES_TO_HEAD) < 1e-9


def test_top_of_head_is_estimated():
    face = [NS(x=0.5, y=0.5, z=0.0) for _ in range(478)]
    face[P.FACE_CHIN] = NS(x=0.5, y=0.6, z=0.0)
    face[P.FACE_FOREHEAD] = NS(x=0.5, y=0.4, z=0.0)
    head, size = P.head_from_face(face, ASPECT)
    assert head["top_of_head"].state == P.ESTIMATED
    assert abs(head["top_of_head"].y - 0.35) < 1e-9 and abs(size - 0.25) < 1e-9
    assert head["bottom_of_head"].state == P.VISIBLE


def test_frame_json_round_trip():
    parts = dict.fromkeys(P.ALL_PARTS, P.MISSING)
    parts["nose"] = P.Part(0.5, 0.42, 0.0, P.VISIBLE)
    parts["top_of_head"] = P.Part(0.5, 0.18, 0.0, P.ESTIMATED)
    frame = P.Frame(12.3456, 1042, (640, 480), parts,
                    {"right_palm_facing": "camera", "scale": {"head": 0.16}})
    line = json.dumps(frame.to_dict())
    raw = json.loads(line)
    assert raw["parts"]["left_ear"] == {"state": "not_visible"}
    assert raw["parts"]["nose"] == {"x": 0.5, "y": 0.42, "z": 0.0, "state": "visible"}
    again = P.Frame.from_dict(raw)
    assert again == frame
    assert again.part("left_ear") is None and again.part("nose") == (0.5, 0.42, 0.0)
    assert again.visible() == ["nose", "top_of_head"] and len(line) < 2000


# segmenter -------------------------------------------------------------

HAND_SIZE = 0.1


def stream(xs, fps=30, hand=True, head_x=0.5, t0=0.0):
    """Frames with the right hand at each x (None hides it) and a head."""
    for i, x in enumerate(xs):
        parts = dict.fromkeys(P.ALL_PARTS, P.MISSING)
        if x is not None:
            x += 1e-6 * (-1) ** i       # a real tracker never repeats a position exactly
        if x is not None and hand:
            for j, name in enumerate(P.HAND_NAMES):
                parts["right_" + name] = P.Part(x, 0.5 + j * 0.01, 0.0, P.VISIBLE)
        for j, name in enumerate(P.HEAD_PARTS[:5]):
            parts[name] = P.Part(head_x + j * 0.01, 0.3, 0.0, P.VISIBLE)
        yield P.Frame(t0 + i / fps, i, (480, 480), parts,
                      {"scale": {"right_hand": HAND_SIZE, "head": 0.2}})


def run(xs, **kwargs):
    seg = Segmenter(DETECTION_DEFAULTS, kwargs.pop("parts", P.ALL_PARTS))
    found = [s for s in map(seg.update, stream(xs, **kwargs)) if s is not None]
    return seg, found


def swipe(still=15, moving=15, step=0.02, after=15):
    """step 0.02 per frame at 30 fps is 6 hand sizes per second."""
    xs = [0.2] * still + [0.2 + step * (i + 1) for i in range(moving)]
    return xs + [xs[-1]] * after


def test_swipe_becomes_one_segment_with_pre_roll():
    seg, found = run(swipe())
    assert len(found) == 1 and seg.last["kept"] and not seg.moving
    s = found[0]
    assert s.lead == "right_hand" and 400 <= s.duration_ms <= 650
    assert 0.1 <= s.t0 - s.frames[0].t <= 0.2          # about 150 ms of pre-roll
    assert s.frames[-1].t <= s.t1 + 1e-9               # the quiet tail is trimmed


def test_same_split_with_all_parts_or_only_the_needed_ones():
    _, everything = run(swipe())
    _, needed = run(swipe(), parts=["right_index_tip"])
    assert len(needed) == 1
    assert abs(everything[0].t0 - needed[0].t0) < 1e-9
    assert abs(everything[0].t1 - needed[0].t1) < 1e-9


def test_short_and_long_motions_are_discarded():
    seg, found = run(swipe(moving=3))
    assert not found and seg.last["reason"] == "too short"
    seg, found = run(swipe(moving=100, step=0.005, after=30))
    assert not found and seg.last["reason"] == "too long" and not seg.moving


def test_motion_is_discarded_when_the_hand_disappears():
    xs = swipe(after=0) + [None] * 20
    seg, found = run(xs)
    assert not found and seg.last["reason"] == "lost tracking"


def test_still_hand_and_jitter_do_not_start_a_motion():
    xs = [0.5 + 0.0005 * (-1) ** i for i in range(90)]
    seg, found = run(xs)
    assert not found and seg.last is None and seg.speed < DETECTION_DEFAULTS["stop_speed"]


def test_settings_apply():
    seg = Segmenter(dict(DETECTION_DEFAULTS, start_speed=50))
    assert not [s for s in map(seg.update, stream(swipe())) if s]
    seg.configure(DETECTION_DEFAULTS)
    assert seg.start_speed == 1.2 and seg.quiet_s == 0.15


# idle skipping and camera names ------------------------------------------

def test_idle_gate_skips_two_of_three_frames():
    gate = IdleGate()
    gate.note(1.0, True)
    assert all(gate.should_track(2.5, n) for n in range(6))
    assert [gate.should_track(3.5, n) for n in range(6)] == [True, False, False] * 2
    gate.note(3.6, True)
    assert gate.should_track(3.7, 1) and not gate.idle(3.7)


def test_camera_names():
    assert device_index("") == 0 and device_index("Default camera") == 0
    assert device_index("Camera 2") == 2 and device_name(2) == "Camera 2"
