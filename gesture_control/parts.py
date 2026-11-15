"""The part catalog, which model provides each part, the Frame format and the
values derived from raw landmarks (file 02).

Coordinates are fractions of the image: x to the right, y down, both 0 to 1,
in the camera's own (not mirrored) view. Left and right are always the
person's own left and right.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

VISIBLE, ESTIMATED, NOT_VISIBLE = "visible", "estimated", "not_visible"

SIDES = ("left", "right")
HAND_NAMES = ("wrist", "palm_center", "thumb_tip", "index_tip", "middle_tip", "ring_tip",
              "pinky_tip")
HAND_PARTS = tuple(f"{side}_{name}" for side in SIDES for name in HAND_NAMES)
HEAD_PARTS = ("nose", "left_eye", "right_eye", "left_ear", "right_ear", "bottom_of_head",
              "top_of_head")
ALL_PARTS = HAND_PARTS + HEAD_PARTS
GROUPS = ("left_hand", "right_hand", "head")

FACE_ONLY = frozenset({"bottom_of_head", "top_of_head"})
POSE_ONLY = frozenset({"left_ear", "right_ear"})
EITHER = frozenset({"nose", "left_eye", "right_eye"})   # face if it runs anyway, else pose

POSE_CONFIDENCE = 0.5
TOP_OF_HEAD_RATIO = 0.25    # hairline to crown, as a share of chin to hairline
EDGE_ON = 0.25              # below this the palm is too edge-on to call


def group(part: str) -> str:
    """left_hand, right_hand or head."""
    if part in HEAD_PARTS:
        return "head"
    return part.split("_", 1)[0] + "_hand"


def models_for(parts) -> set[str]:
    """The models needed to track `parts`: a subset of {hand, pose, face}."""
    parts = set(parts)
    models = set()
    if parts & set(HAND_PARTS):
        models.add("hand")
    if parts & FACE_ONLY:
        models.add("face")
    if parts & POSE_ONLY:
        models.add("pose")
    if parts & EITHER and not models & {"face", "pose"}:
        models.add("pose")      # the lighter of the two
    return models


@dataclass(frozen=True)
class Part:
    x: float | None = None
    y: float | None = None
    z: float | None = None
    state: str = NOT_VISIBLE

    def to_dict(self) -> dict:
        if self.state == NOT_VISIBLE:
            return {"state": NOT_VISIBLE}
        return {"x": round(self.x, 4), "y": round(self.y, 4), "z": round(self.z or 0.0, 4),
                "state": self.state}


MISSING = Part()


@dataclass(frozen=True)
class Frame:
    t: float                      # seconds since tracking started
    frame: int
    image: tuple[int, int]        # w, h
    parts: dict = field(default_factory=dict)       # name -> Part, every catalog part
    derived: dict = field(default_factory=dict)     # *_palm_facing, scale

    def part(self, name: str):
        """(x, y, z), or None when the part is not visible. Never a guess."""
        p = self.parts.get(name, MISSING)
        return None if p.state == NOT_VISIBLE else (p.x, p.y, p.z)

    def state(self, name: str) -> str:
        return self.parts.get(name, MISSING).state

    def visible(self) -> list[str]:
        return [name for name, p in self.parts.items() if p.state != NOT_VISIBLE]

    def scale(self, group_name: str) -> float | None:
        return self.derived.get("scale", {}).get(group_name)

    @property
    def aspect(self) -> float:
        return self.image[0] / self.image[1] if self.image[1] else 1.0

    def to_dict(self) -> dict:
        """One JSONL line of a dataset."""
        return {"t": round(self.t, 4), "frame": self.frame,
                "image": {"w": self.image[0], "h": self.image[1]},
                "parts": {name: p.to_dict() for name, p in self.parts.items()},
                "derived": self.derived}

    @classmethod
    def from_dict(cls, raw: dict) -> "Frame":
        parts = {name: Part(p.get("x"), p.get("y"), p.get("z"), p["state"])
                 for name, p in raw["parts"].items()}
        return cls(raw["t"], raw["frame"], (raw["image"]["w"], raw["image"]["h"]), parts,
                   raw.get("derived", {}))


# derived values --------------------------------------------------------

def dist(a, b, aspect: float) -> float:
    """Distance between two (x, y, ...) points in units of image height."""
    return math.hypot((a[0] - b[0]) * aspect, a[1] - b[1])


def _in_frame(x: float, y: float) -> bool:
    return 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


def _part(x, y, z=0.0, state=VISIBLE, confident=True) -> Part:
    if not confident or not _in_frame(x, y):
        return MISSING
    return Part(x, y, z, state)


def _mean(points) -> tuple[float, float, float]:
    n = len(points)
    return (sum(p.x for p in points) / n, sum(p.y for p in points) / n,
            sum(p.z for p in points) / n)


# hand landmarker: 21 points
WRIST, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP = 0, 5, 9, 13, 17
TIPS = {"thumb_tip": 4, "index_tip": 8, "middle_tip": 12, "ring_tip": 16, "pinky_tip": 20}


def person_side(model_label: str) -> str:
    """The hand model labels hands as if the image were a mirrored selfie.
    The camera image here is not mirrored, so its label is the opposite of the
    person's own side."""
    return "right" if model_label.lower() == "left" else "left"


def palm_facing(side: str, lm, aspect: float) -> str:
    """camera, away or unknown, from which way the knuckles wind around the wrist."""
    ax, ay = (lm[INDEX_MCP].x - lm[WRIST].x) * aspect, lm[INDEX_MCP].y - lm[WRIST].y
    bx, by = (lm[PINKY_MCP].x - lm[WRIST].x) * aspect, lm[PINKY_MCP].y - lm[WRIST].y
    size = math.hypot(ax, ay) * math.hypot(bx, by)
    if size < 1e-9:
        return "unknown"
    cross = (ax * by - ay * bx) / size
    if abs(cross) < EDGE_ON:
        return "unknown"
    return "camera" if (cross < 0) == (side == "right") else "away"


def hand_parts(side: str, lm, aspect: float) -> tuple[dict, str, float]:
    """Parts, palm facing and palm size for one hand's 21 landmarks."""
    parts = {f"{side}_wrist": _part(lm[WRIST].x, lm[WRIST].y, lm[WRIST].z)}
    for name, i in TIPS.items():
        parts[f"{side}_{name}"] = _part(lm[i].x, lm[i].y, lm[i].z)
    palm = _mean([lm[i] for i in (WRIST, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)])
    parts[f"{side}_palm_center"] = _part(*palm)
    size = dist((lm[WRIST].x, lm[WRIST].y), (lm[MIDDLE_MCP].x, lm[MIDDLE_MCP].y), aspect)
    return parts, palm_facing(side, lm, aspect), size


# pose landmarker: 33 points, named for the person's own sides
POSE = {"nose": 0, "left_eye": 2, "right_eye": 5, "left_ear": 7, "right_ear": 8}
EYES_TO_HEAD = 3.6      # head height over the distance between the eyes


def head_from_pose(lm, aspect: float) -> tuple[dict, float | None]:
    parts = {}
    for name, i in POSE.items():
        p = lm[i]
        sure = min(getattr(p, "visibility", None) or 1.0,
                   getattr(p, "presence", None) or 1.0) >= POSE_CONFIDENCE
        parts[name] = _part(p.x, p.y, p.z, confident=sure)
    size = None
    left, right = parts["left_eye"], parts["right_eye"]
    if left.state == VISIBLE and right.state == VISIBLE:
        size = dist((left.x, left.y), (right.x, right.y), aspect) * EYES_TO_HEAD
    return parts, size


# face landmarker: about 478 points
FACE_NOSE, FACE_CHIN, FACE_FOREHEAD = 1, 152, 10
FACE_RIGHT_EYE, FACE_LEFT_EYE = (33, 133), (263, 362)   # corners; the person's own sides


def head_from_face(lm, aspect: float) -> tuple[dict, float]:
    chin, brow = lm[FACE_CHIN], lm[FACE_FOREHEAD]
    top = (brow.x + (brow.x - chin.x) * TOP_OF_HEAD_RATIO,
           brow.y + (brow.y - chin.y) * TOP_OF_HEAD_RATIO, brow.z)
    parts = {
        "nose": _part(lm[FACE_NOSE].x, lm[FACE_NOSE].y, lm[FACE_NOSE].z),
        "left_eye": _part(*_mean([lm[i] for i in FACE_LEFT_EYE])),
        "right_eye": _part(*_mean([lm[i] for i in FACE_RIGHT_EYE])),
        "bottom_of_head": _part(chin.x, chin.y, chin.z),
        # No model outputs this, so it is never better than `estimated`.
        "top_of_head": _part(*top, state=ESTIMATED),
    }
    return parts, dist((chin.x, chin.y), top, aspect)
