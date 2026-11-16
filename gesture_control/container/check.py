"""Runs /work/detect.py on every training sample, inside the container.

    python3 /work/tools/check.py

Prints what the function got wrong, the same way the app will judge it."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gesture_control import parts as P, sandbox      # noqa: E402
from gesture_control.segmenter import cuts           # noqa: E402

WORK = Path("/work")


def main() -> int:
    try:
        code = (WORK / "detect.py").read_text(encoding="utf-8")
    except OSError:
        print("There is no /work/detect.py yet.")
        return 1
    samples = []
    for kind, expected in (("positives", True), ("negatives", False)):
        files = sorted((WORK / "samples" / kind).glob("*.json"), key=lambda f: int(f.stem[1:]))
        for file in files:
            frames = [P.Frame.from_dict(f) for f in json.loads(file.read_text())["frames"]]
            samples.append((file.stem, expected, frames))
    # A negative is also run in pieces: the stretches the live loop would have
    # checked while that motion was happening.
    spans = [[] if expected else cuts(frames) for _, expected, frames in samples]
    report = sandbox.evaluate_in_process(code, [frames for _, _, frames in samples], spans)
    if report.error:
        print("Not allowed or could not load:", report.error)
        return 1
    wrong = [f"{label} is a {'positive' if expected else 'negative'} but detect "
             + (f"returned {result}" if isinstance(result, bool) else f"failed ({result})")
             for (label, expected, _), result in zip(samples, report.results)
             if result is not expected]
    for (label, expected, frames), result, hit in zip(samples, report.results, report.hits):
        if hit and result is False and not expected:
            t0 = frames[0].t
            wrong.append(f"{label} is a negative but detect returned True on a part of it, "
                         f"frames {hit[0]} to {hit[1] - 1} ({frames[hit[0]].t - t0:.2f} s to "
                         f"{frames[hit[1] - 1].t - t0:.2f} s of {frames[-1].t - t0:.2f} s)")
    wrong += list(report.sanity)
    for line in wrong:
        print("-", line)
    positives = sum(expected for _, expected, _ in samples)
    print(f"{len(wrong)} problems on {positives} positives and {len(samples) - positives} "
          f"negatives. NEEDS = {list(report.needs)}. Slowest call {report.slow_ms:.1f} ms.")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
