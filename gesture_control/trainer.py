"""The agent loop that writes one gesture function (file 03).

The model drives; the app enforces. On each turn the model replies with one
action in plain text, which works on any model without native tool calling:

    REQUEST k     ask the user for k more training positives (plus 3k negatives)
    STATS ...     ask for statistics about the training samples
    REDO P3 P5    ask the user to perform those training positives again
    ```python     write or revise the function; the app runs it on the training data
    TEST k        try the function on k fresh positives and 3k fresh negatives
    CONFIDENT     declare the function finished

The model never reads the samples themselves. It writes the function mostly
from the user's description, with a short overview of each batch, and may ask
for statistics (where a part is on average over the motion, how far it moves)
when it needs a number. The samples are what the app tests each candidate on.

The app decides what counts: a test always uses samples the model has never
seen (it may ask about them only afterwards, as training data), `CONFIDENT` is
accepted only after a test round with no errors and enough fresh data overall,
and at most `BUDGET` positives are ever asked of the user.
"""

from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass, field

from . import parts as P
from . import sandbox
from .dataset import NegativePool
from .model_client import ModelClient
from .segmenter import Segment

BUDGET = 40                 # positives asked of the user, in total
K_MIN, K_MAX = 3, 10        # per request
FIRST_BATCH = 10            # the first request: at least this many to learn from
REDO_MAX = 15               # samples the agent may ask the user to perform again, per run
CALLS_PER_ROUND = 12        # model calls (code and STATS) between two sample collections
MIN_FRESH_POSITIVES = 5     # across all test rounds
MIN_FRESH_NEGATIVES = 15

NOT_PASSED = ("Couldn't tell this gesture apart from your other movements. "
              "Try a more distinctive motion.")
MORE_IDLE = ("Ran out of recorded everyday motions to test against. Record more idle video "
             "in settings, moving around as you normally do, then try again.")


class Cancelled(Exception):
    """The user cancelled training. Nothing is saved."""


@dataclass
class Result:
    passed: bool
    message: str = ""               # why it did not pass, or the agent's note when it did
    code: str = ""
    needs: tuple = ()
    positives: list = field(default_factory=list)      # every positive the user performed


def minimum_negatives(ratio: int) -> int:
    """The fewest negatives a run can succeed with: the first training batch
    plus the fresh ones the finish rule asks for."""
    return FIRST_BATCH * ratio + MIN_FRESH_NEGATIVES


# what the model reads ----------------------------------------------------

MOMENTS = (0.0, 0.25, 0.5, 0.75, 1.0)      # where along a motion positions are reported
STATS_PARTS, STATS_SAMPLES, STATS_CHARS = 6, 8, 5000


def measure(segment: Segment, name: str) -> dict | None:
    """One part in one sample, or None if it is never visible there: how often
    it is visible, where it is at five moments, and how far it goes, in sizes
    of its own hand or head."""
    frames = segment.frames
    seen = [(f.t, f.part(name)) for f in frames]
    points = [(t, p[0], p[1]) for t, p in seen if p is not None]
    if not points:
        return None
    sizes = [f.scale(P.group(name)) for f in frames if f.scale(P.group(name))]
    size = statistics.median(sizes) if sizes else None
    aspect = frames[0].aspect
    t0 = frames[0].t
    span = max(frames[-1].t - t0, 1e-6)
    track = []
    for moment in MOMENTS:
        target = t0 + moment * span
        t, x, y = min(points, key=lambda point: abs(point[0] - target))
        track.append((x, y) if abs(t - target) <= 0.2 * span + 0.05 else None)
    net = travel = None
    if size:
        net = ((points[-1][1] - points[0][1]) * aspect / size, (points[-1][2] - points[0][2]) / size)
        travel = sum(P.dist((a[1], a[2]), (b[1], b[2]), aspect)
                     for a, b in zip(points, points[1:])) / size
    return {"visible": len(points) / len(frames), "track": track, "net": net,
            "travel": travel, "size": size, "aspect": aspect}


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _spread(values, signed: bool = True) -> str:
    """Mean and range of a number across samples, as text."""
    values = [v for v in values if v is not None]
    if not values:
        return "--"
    form = "{:+.1f}" if signed else "{:.1f}"
    text = form.format(sum(values) / len(values))
    if len(values) > 1:
        text += f" ({form.format(min(values))}..{form.format(max(values))})"
    return text


def _place(point) -> str:
    return "  --   " if point is None else f"{point[0]:.2f},{point[1]:.2f}".replace("0.", ".")


def _labels(samples: list) -> str:
    return f"{samples[0][0]}-{samples[-1][0]}" if len(samples) > 1 else samples[0][0]


def _lengths(samples: list) -> str:
    seconds = sorted(s.frames[-1].t - s.frames[0].t for _, s in samples if s.frames)
    if not seconds:
        return "--"
    return f"{seconds[0]:.2f}-{seconds[-1]:.2f} s (median {statistics.median(seconds):.2f})"


def _leads(samples: list) -> str:
    counts: dict = {}
    for _, segment in samples:
        counts[segment.lead or "unknown"] = counts.get(segment.lead or "unknown", 0) + 1
    return ", ".join(f"{name} {count}" for name, count in sorted(counts.items(),
                                                                 key=lambda item: -item[1]))


def overview(positives: list, negatives: list) -> str:
    """A few lines about the training samples as a whole: no single sample,
    only how the positives differ from the negatives part by part."""
    lines = [f"Training samples: {len(positives)} positives ({_labels(positives)}), "
             f"{len(negatives)} negatives ({_labels(negatives) if negatives else 'none'}).",
             f"Length: positives {_lengths(positives)}; negatives {_lengths(negatives)}.",
             f"Moving group: positives {_leads(positives)}; negatives {_leads(negatives) or '--'}.",
             "Per part, positives / negatives. Travel and net movement are in sizes of the "
             "part's own hand or head; net is end minus start (dx right, dy down).",
             f"  {'part':<18}{'visible':>11}  {'travel':>9}  {'net dx,dy positives':>20}  "
             f"{'net dx,dy negatives':>20}"]
    hidden = []
    for name in P.ALL_PARTS:
        both = [[measure(segment, name) for _, segment in group] for group in (positives, negatives)]
        if not any(both[0]):
            hidden.append(name)
            continue
        cells = []
        for found in both:
            seen = _mean([m["visible"] if m else 0.0 for m in found]) or 0.0
            travel = _mean([m["travel"] for m in found if m])
            dx = _mean([m["net"][0] for m in found if m and m["net"]])
            dy = _mean([m["net"][1] for m in found if m and m["net"]])
            cells.append((f"{seen:.0%}", "--" if travel is None else f"{travel:.1f}",
                          "--" if dx is None else f"{dx:+.1f},{dy:+.1f}"))
        (pv, pt, pn), (nv, nt, nn) = cells
        lines.append(f"  {name:<18}{pv + ' / ' + nv:>11}  {pt + ' / ' + nt:>9}  {pn:>20}  {nn:>20}")
    if hidden:
        lines.append("  not visible in any positive: " + ", ".join(hidden))
    return "\n".join(lines)


def stats(names: list, positives: list, negatives: list, picked: list | None = None) -> str:
    """The answer to a STATS action: each named part's average position over
    the motion and how far it moves, for positives and negatives, or for the
    picked samples one by one. With two parts, also the distance between them."""
    moments = "/".join(f"{m:.0%}" for m in MOMENTS)
    lines = []
    for name in names:
        lines.append(f"{name} (x,y as image fractions at {moments} of the motion; "
                     f"movement in {P.group(name)} sizes)")
        if picked:
            for label, segment in picked:
                m = measure(segment, name)
                if m is None:
                    lines.append(f"  {label}: never visible")
                    continue
                net = "--" if not m["net"] else f"{m['net'][0]:+.1f},{m['net'][1]:+.1f}"
                travel = "--" if m["travel"] is None else f"{m['travel']:.1f}"
                lines.append(f"  {label}: {' '.join(_place(point) for point in m['track'])} | "
                             f"net dx,dy {net} | travel {travel} | visible {m['visible']:.0%} | "
                             f"{segment.frames[-1].t - segment.frames[0].t:.2f} s")
            continue
        for kind, samples in (("positives", positives), ("negatives", negatives)):
            found = [measure(segment, name) for _, segment in samples]
            present = [m for m in found if m]
            if not present:
                lines.append(f"  {kind} ({len(samples)}): never visible")
                continue
            track = []
            for i in range(len(MOMENTS)):
                points = [m["track"][i] for m in present if m["track"][i]]
                track.append((_mean([q[0] for q in points]), _mean([q[1] for q in points]))
                             if points else None)
            seen = _mean([m["visible"] if m else 0.0 for m in found])
            lines.append(
                f"  {kind} ({len(samples)}): visible {seen:.0%} | average position "
                f"{' '.join(_place(point) for point in track)} | "
                f"net dx {_spread([m['net'][0] for m in present if m['net']])}, "
                f"dy {_spread([m['net'][1] for m in present if m['net']])} | "
                f"travel {_spread([m['travel'] for m in present], signed=False)}")
    if len(names) == 2:
        a, b = names
        lines.append(f"distance {a} to {b}, in {P.group(a)} sizes, at {moments} of the motion")
        groups = [(label, [(label, segment)]) for label, segment in picked] if picked else \
            [("positives", positives), ("negatives", negatives)]
        for title, samples in groups:
            cells = []
            for i in range(len(MOMENTS)):
                values = []
                for _, segment in samples:
                    ma, mb = measure(segment, a), measure(segment, b)
                    if ma and mb and ma["size"] and ma["track"][i] and mb["track"][i]:
                        values.append(P.dist(ma["track"][i], mb["track"][i], ma["aspect"])
                                      / ma["size"])
                cells.append("--" if not values else f"{sum(values) / len(values):.1f}")
            lines.append(f"  {title}: {' '.join(cells)}")
    if not picked:
        lines.append(f"length: positives {_lengths(positives)}; negatives {_lengths(negatives)}")
    text = "\n".join(lines)
    return text if len(text) <= STATS_CHARS else text[:STATS_CHARS] + "\n(cut short)"


SYSTEM = f"""You write a small Python function that recognises one hand or head gesture \
from webcam tracking data. You work with an app that collects samples from the user, runs \
your function, and tests it on samples you have not seen.

THE FUNCTION

```python
NEEDS = ["right_index_tip", "nose"]   # every part the function reads, and nothing else

def detect(frames) -> bool:
    \"\"\"Return True only if this motion is the gesture.\"\"\"
```

- `frames` is one motion: the frames from about 0.15 s before the motion started until it \
went quiet, usually 0.3 to 2.5 s long at about 30 frames per second. The frame rate varies \
and head parts update about 10 times a second (held in between), so use time, not frame \
counts.
- Parts: {", ".join(P.ALL_PARTS)}.
- Left and right are the person's own. x and y are fractions of the image (x to the right \
as the camera sees it, y down, 0 to 1). The image is 4:3, so the helpers correct for that.
- A part's state is "visible", "estimated" (top_of_head always is) or "not_visible". A part \
that is not visible has no position: frame.part gives None and positions gives NaN. Your \
function must handle that, and should return False when the parts it relies on are missing.
- Sizes differ with distance from the camera, so measure in hand or head sizes, not image \
fractions.

Helpers, already defined for you (do not import them):

{sandbox.HELPER_DOCS}

Rules the app enforces:
- Only `import numpy` (or `as np`) and `import math`. No other imports, no files, no network.
- No `while` loops, no classes, no names or attributes starting with an underscore, no \
`print`, `open`, `eval`, `exec`, `getattr`, or `.format`.
- NEEDS must list every part the function reads. Reading a part that is not in NEEDS is an \
error. At runtime only the parts in NEEDS are tracked, so list as few as the gesture needs: \
a hand-only gesture is much cheaper than one that also needs the head.
- detect must return a bool, quickly, and must return False for an empty list, for frames \
where nothing moves, and for random positions.

WHAT MAKES A GOOD FUNCTION

- It runs on every motion the user makes all day. Negatives are the user's other gestures \
and their everyday movements. A false trigger is worse than a miss, but it must still \
accept the user's natural variation.
- Work mostly from the user's description: write the function it implies. You never see \
the samples themselves. After each batch you get a short overview (which parts are visible \
and how far they move, positives against negatives), and the app tests every function you \
write on all the samples and tells you which ones it got wrong.
- Use STATS when you need a number: where a part is on average over the motion, how far it \
moves, how far two parts are from each other. Ask about a few parts at a time.
- Look for what all positives share and negatives lack: which part moves, in which \
direction, how far in hand or head sizes, where it is relative to another part, how long.
- Put thresholds in the gap between positives and negatives, not at the edge of the \
positives.
- People describe motions loosely. If the statistics disagree with the description, trust \
the statistics and say so when you finish.

HOW TO REPLY

Each reply must contain exactly one action:

REQUEST k
    Ask the user to perform the motion k more times ({K_MIN} to {K_MAX}). You also get \
about 3k negatives. These are training samples. You get an overview of them, not the \
samples. The first request always collects {FIRST_BATCH}, whatever k you give.
STATS part [part ...] [FOR label ...]
    Statistics about the training samples for up to {STATS_PARTS} parts, for example \
`STATS right_index_tip nose`: each part's average position at five moments of the motion, \
its net movement and travel, for positives and for negatives. With exactly two parts you \
also get the distance between them. Add `FOR P3 N7` (up to {STATS_SAMPLES} labels) to see \
those samples one by one, for example the ones your function got wrong.
REDO label [label ...]
    Ask the user to perform some training positives again, for example `REDO P1 P4`. Use \
this when a sample is plainly not the gesture: almost no movement, a twitch, or the hand \
moving back afterwards. A function has to be right on every training positive, so bad \
samples must be replaced, not fitted. The new samples take the old labels and do not use \
up the budget (at most {REDO_MAX} in a run).
A ```python code block with the whole file (NEEDS and detect)
    The app runs it on all training samples and tells you what it got wrong.
TEST k
    The app collects k fresh positives ({K_MIN} to {K_MAX}) and about 3k fresh negatives \
that you have never seen, and runs your current function on them. Afterwards they \
become training samples, which STATS covers.
CONFIDENT
    Declare the function finished. Accepted only if the last test had no errors and the \
tests so far used at least {MIN_FRESH_POSITIVES} fresh positives and \
{MIN_FRESH_NEGATIVES} fresh negatives in total. You may add one line starting with \
`NOTE:` to tell the user something, for example that the motion differs from their \
description.

The user performs every positive by hand, so samples are precious: at most {BUDGET} in \
total across REQUEST and TEST. If the budget runs out before a clean test, training fails. \
Plan for it: the first {FIRST_BATCH} to learn from, then enough for the tests. Do not \
deliberate at length: write a first function straight from the description and the \
overview, and let the app's tests tell you what to fix."""


# reading the model's reply -----------------------------------------------

_CODE = re.compile(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", re.S)
_ASK = re.compile(r"^\W*(REQUEST|TEST)\W+(\d+)\b", re.M)
_STATS = re.compile(r"^\W*STATS\b(.*)$", re.M)
_REDO = re.compile(r"^\W*REDO\b(.*)$", re.M)
_CONFIDENT = re.compile(r"^\W*CONFIDENT\b", re.M)
_NOTE = re.compile(r"^\W*NOTE:\s*(.+)$", re.M)
_THINK = re.compile(r"<think>.*?(</think>|\Z)", re.S)


def parse(reply: str) -> tuple:
    """('code', text) | ('request', k) | ('test', k) | ('stats', words) |
    ('confident', note) | ('unclear', '')."""
    reply = _THINK.sub("", reply or "")
    blocks = [b for b in _CODE.findall(reply) if "def detect" in b]
    if blocks:
        return "code", blocks[-1].strip() + "\n"
    asks = _ASK.findall(reply)
    if asks:
        word, k = asks[-1]
        return word.lower(), int(k)
    queries = _STATS.findall(reply)
    if queries:
        return "stats", queries[-1].strip(" \t`*.:")
    again = _REDO.findall(reply)
    if again:
        return "redo", again[-1].strip(" \t`*.:")
    if _CONFIDENT.search(reply):
        note = _NOTE.search(reply)
        return "confident", note.group(1).strip() if note else ""
    return "unclear", ""


# the loop ----------------------------------------------------------------

class Trainer:
    """`collect(count, phase, asked, budget)` blocks until the user has
    performed `count` motions and returns them as Segments; it raises
    Cancelled if the user gives up. `progress(round, phase)` reports what
    the trainer is doing. `on_step(step, note)` is called before each model
    call with what the previous one led to, and `trace(line)` gets one line
    per step for a log."""

    def __init__(self, client: ModelClient, motion_text: str, pool: NegativePool, collect,
                 progress=lambda round, phase: None, ratio: int = 3, budget: int = BUDGET,
                 evaluate=sandbox.evaluate, first_batch: int = FIRST_BATCH,
                 on_step=lambda step, note: None, trace=lambda line: None,
                 replaced=lambda old: None):
        self.first_batch = first_batch
        self.replaced = replaced        # told which samples were dropped for new ones
        self.redone = 0
        self.on_step, self.trace = on_step, trace
        self.step = 0                   # model calls in this run
        self.note = ""                  # what the last step led to, in plain words
        self.client, self.motion_text, self.pool = client, motion_text, pool
        self.collect, self.progress, self.evaluate = collect, progress, evaluate
        self.ratio, self.budget = max(1, int(ratio)), budget
        self.positives: list = []       # (label, Segment), everything the model has seen
        self.negatives: list = []
        self.asked = 0
        self.fresh_positives = self.fresh_negatives = 0
        self.code = ""
        self.needs: tuple = ()
        self.passing = False            # the code is right on every training sample
        self.tested_clean = False       # ... and its last fresh test had no errors
        self.round = 0
        self.calls = 0                  # model calls since the last collection
        self.messages = [{"role": "system", "content": SYSTEM}]

    # state -------------------------------------------------------------

    @property
    def remaining(self) -> int:
        return self.budget - self.asked

    def _enough_fresh(self) -> bool:
        return (self.fresh_positives >= MIN_FRESH_POSITIVES
                and self.fresh_negatives >= MIN_FRESH_NEGATIVES)

    def _status(self) -> str:
        lines = [f"Samples asked of the user: {self.asked} of {self.budget} "
                 f"({self.remaining} remain). Negatives left to draw: {len(self.pool)}. "
                 f"Fresh test data so far: {self.fresh_positives} positives, "
                 f"{self.fresh_negatives} negatives.",
                 f"Replies left before the next REQUEST or TEST must happen: "
                 f"{CALLS_PER_ROUND - self.calls}."]
        if not self.positives:
            lines.append("Next: REQUEST k.")
        elif not self.passing:
            lines.append("Next: a python code block. (Or STATS for a number you need, REDO "
                         "for positives that are not the gesture, or REQUEST k for more "
                         "training samples.)")
        elif not self.tested_clean:
            lines.append("Next: TEST k. (Or revise the code, STATS, or REQUEST k.)")
        elif self._enough_fresh():
            lines.append("Next: CONFIDENT, or TEST k if you want more evidence.")
        else:
            lines.append("Next: TEST k. CONFIDENT is not allowed yet: the tests need at least "
                         f"{MIN_FRESH_POSITIVES} fresh positives and {MIN_FRESH_NEGATIVES} "
                         "fresh negatives in total.")
        return "\n".join(lines)

    def _ask(self, text: str) -> tuple:
        self.progress(max(self.round, 1), "revising")
        self.step += 1
        self.on_step(self.step, self.note)
        self.messages.append({"role": "user", "content": text + "\n\n" + self._status()})
        started = time.monotonic()
        try:
            reply = self.client.complete(self.messages)
        except Exception as error:
            self.trace(f"step {self.step}: failed after {time.monotonic() - started:.0f} s: {error}")
            raise
        self.messages.append({"role": "assistant", "content": reply})
        self.calls += 1
        action, value = parse(reply)
        shown = value.count("\n") + 1 if action == "code" else value
        self.trace(f"step {self.step}: model answered in {time.monotonic() - started:.0f} s "
                   f"with {action}" + (f" ({shown} lines)" if action == "code" else f" {shown}".rstrip()))
        return action, value

    def _did(self, action: str, answer: str) -> None:
        """Remember, in plain words, what a step led to."""
        if action == "request":
            self.note = "Got your samples"
        elif action == "stats":
            self.note = "Looked at statistics of your samples"
        elif action == "redo":
            self.note = "Asked you to redo samples that were not the gesture"
        elif action == "code":
            self.note = ("Wrote a function that fits every sample so far" if self.passing
                         else "Wrote a function, but it got some samples wrong")
        elif action == "test":
            self.note = ("The function passed a fresh test" if self.tested_clean
                         else "The function failed a fresh test")
        elif action == "confident":
            self.note = "Tried to finish, but more testing is needed"
        else:
            self.note = "The model's answer was not usable"
        self.trace(f"        app: {answer.splitlines()[0][:160] if answer else ''}")

    # samples -----------------------------------------------------------

    def _gather(self, k: int, phase: str) -> tuple[list, list]:
        """k positives from the user and ratio*k negatives from the pool, labelled."""
        k = max(K_MIN, min(K_MAX, k, self.remaining))
        segments = self.collect(k, phase, self.asked, self.budget)
        self.asked += len(segments)
        self.calls = 0
        first_p, first_n = len(self.positives) + 1, len(self.negatives) + 1
        positives = [(f"P{first_p + i}", s) for i, s in enumerate(segments)]
        negatives = [(f"N{first_n + i}", s)
                     for i, s in enumerate(self.pool.take(self.ratio * len(segments)))]
        return positives, negatives

    def _redo(self, words: str) -> str:
        """Have the user perform some training positives again."""
        labels = list(dict.fromkeys(token.strip("`'" + '"').upper()
                                    for token in re.split(r"[" + chr(92) + "s,]+", words) if token))
        where = {label: i for i, (label, _) in enumerate(self.positives)}
        unknown = [label for label in labels if label not in where]
        if not labels or unknown:
            return ("REDO needs the labels of training positives, for example REDO P1 P4. "
                    + (f"{', '.join(unknown)} is not one." if unknown else ""))
        if self.redone + len(labels) > REDO_MAX:
            return (f"Only {REDO_MAX - self.redone} more samples can be performed again in "
                    "this run.")
        self.progress(max(self.round, 1), "redoing")
        fresh = self.collect(len(labels), "redoing", self.asked - len(labels), self.budget)
        old = []
        for label, segment in zip(labels, fresh):
            old.append(self.positives[where[label]][1])
            self.positives[where[label]] = (label, segment)
        self.replaced(old)
        self.redone += len(fresh)
        self.calls = 0
        self.passing = self.tested_clean = False
        text = (f"The user performed {', '.join(labels[:len(fresh)])} again; the new samples "
                f"replace the old ones.\n\n{overview(self.positives, self.negatives)}")
        if self.code:
            text += f"\n\n" + self._try_code(self.code).replace("That function", "Your function")
        return text

    def _stats(self, words: str) -> str:
        """Answer a STATS action about the training samples."""
        if not self.positives:
            return "There are no samples yet. Ask for some first with REQUEST k."
        tokens = re.split(r"[\s,]+", words.strip())
        split = next((i for i, token in enumerate(tokens) if token.upper() == "FOR"), len(tokens))
        names = [token.strip("`'\"") for token in tokens[:split] if token]
        labels = [token.strip("`'\"").upper() for token in tokens[split + 1:] if token]
        unknown = [name for name in names if name not in P.ALL_PARTS]
        if unknown or not names:
            return (f"STATS needs part names. {', '.join(unknown) or 'Nothing'} is not one. "
                    f"The parts are: {', '.join(P.ALL_PARTS)}.")
        if len(names) > STATS_PARTS or len(labels) > STATS_SAMPLES:
            return (f"Ask about at most {STATS_PARTS} parts and {STATS_SAMPLES} samples "
                    "at a time.")
        known = dict(self.positives + self.negatives)
        missing = [label for label in labels if label not in known]
        if missing:
            return (f"{', '.join(missing)} is not a training sample. The training samples are "
                    f"{_labels(self.positives)} and {_labels(self.negatives)}.")
        picked = [(label, known[label]) for label in labels] or None
        return stats(names, self.positives, self.negatives, picked)

    def _score(self, code: str, positives: list, negatives: list) -> tuple[list, object]:
        """Run code on labelled samples. Returns (mistakes as text, the report)."""
        report = self.evaluate(code, [s.frames for _, s in positives + negatives])
        if report.error:
            return [report.error], report
        mistakes = []
        for (label, _), result in zip(positives, report.results):
            if result is not True:
                mistakes.append(f"{label} is a positive but detect "
                                + ("returned False" if result is False else f"failed ({result})"))
        for (label, _), result in zip(negatives, report.results[len(positives):]):
            if result is not False:
                mistakes.append(f"{label} is a negative but detect "
                                + ("returned True" if result is True else f"failed ({result})"))
        return mistakes + list(report.sanity), report

    def _try_code(self, code: str) -> str:
        """Run new code on the training samples; returns what to tell the model."""
        mistakes, report = self._score(code, self.positives, self.negatives)
        self.tested_clean = False
        if mistakes:
            self.passing = False
            shown = "\n".join("- " + m for m in mistakes[:15])
            more = f"\n(and {len(mistakes) - 15} more)" if len(mistakes) > 15 else ""
            return ("That function is not right on the training samples yet:\n"
                    f"{shown}{more}\nRevise it. `STATS <parts> FOR <labels>` shows the "
                    "samples it got wrong.")
        self.code, self.needs, self.passing = code, tuple(report.needs), True
        return (f"That function is right on all {len(self.positives)} positives and "
                f"{len(self.negatives)} negatives in the training samples. It is untested "
                "on fresh samples.")

    # actions -----------------------------------------------------------

    def _request(self, k: int) -> str:
        self.round += 1
        self.progress(self.round, "collecting")
        if not self.positives:
            k = max(k, self.first_batch)
        positives, negatives = self._gather(k, "collecting")
        self.positives += positives
        self.negatives += negatives
        text = (f"Collected {len(positives)} new positives ({_labels(positives)}) and "
                f"{len(negatives)} new negatives"
                f"{' (' + _labels(negatives) + ')' if negatives else ''} for training.\n\n"
                f"{overview(self.positives, self.negatives)}")
        if self.code:
            text += "\n\n" + self._try_code(self.code).replace("That function", "Your function")
        return text

    def _test(self, k: int) -> str:
        self.progress(max(self.round, 1), "testing")
        positives, negatives = self._gather(k, "testing")
        mistakes, _ = self._score(self.code, positives, negatives)
        self.fresh_positives += len(positives)
        self.fresh_negatives += len(negatives)
        self.positives += positives          # used for a test, so now they may be seen
        self.negatives += negatives
        shown = (f"{_labels(positives)}"
                 + (f" and {_labels(negatives)}" if negatives else ""))
        if not mistakes:
            self.tested_clean = True
            return (f"Test passed: all {len(positives)} fresh positives and {len(negatives)} "
                    f"fresh negatives were right. They are training samples now ({shown}).")
        self.passing = self.tested_clean = False
        self.round += 1
        return ("Test failed:\n" + "\n".join("- " + m for m in mistakes[:15])
                + f"\n\nThe test samples are training samples now ({shown}).\n\n"
                  f"{overview(self.positives, self.negatives)}\n\n"
                  "Revise the function so it is right on every training sample, then test "
                  "again. `STATS <parts> FOR <labels>` shows the samples it got wrong.")

    def _stuck(self) -> str | None:
        """Why this run can no longer succeed, or None if it still can."""
        finished = self.passing and self.tested_clean and self._enough_fresh()
        if finished:
            return None
        if self.remaining < K_MIN:
            return NOT_PASSED               # no samples left to pass a fresh test with
        if not len(self.pool) and self.fresh_negatives < MIN_FRESH_NEGATIVES:
            return MORE_IDLE
        if self.calls >= CALLS_PER_ROUND:
            return NOT_PASSED
        return None

    def run(self) -> Result:
        """Raises Cancelled, or ModelError if the model cannot be reached."""
        text = (f'The user describes the motion as: "{self.motion_text}"\n\n'
                "Start by requesting training samples.")
        while True:
            problem = self._stuck()
            if problem:
                return self._result(False, problem)
            action, value = self._ask(text)
            if action in ("request", "test") and self.remaining < K_MIN:
                text = "The sample budget is used up. Reply CONFIDENT."
            elif action == "request":
                text = self._request(value)
            elif action == "stats":
                text = self._stats(value)
            elif action == "redo":
                text = self._redo(value)
            elif action == "code":
                if not self.positives:
                    text = "There are no samples yet. Ask for some first with REQUEST k."
                else:
                    text = self._try_code(value)
            elif action == "test":
                if not self.passing:
                    text = ("There is nothing to test: write a function that is right on "
                            "every training sample first.")
                else:
                    text = self._test(value)
            elif action == "confident":
                if self.passing and self.tested_clean and self._enough_fresh():
                    return self._result(True, value)
                text = "CONFIDENT is not accepted yet."
            else:
                text = ("That reply had no action. Reply with exactly one of: REQUEST k, "
                        "STATS with part names, REDO with labels, a ```python code block with NEEDS and detect, "
                        "TEST k, or CONFIDENT.")
            self._did(action, text)

    def _result(self, passed: bool, message: str) -> Result:
        return Result(passed, message, self.code if passed else "",
                      self.needs if passed else (), [s for _, s in self.positives])
