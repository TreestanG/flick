"""Checks and runs model-written gesture functions (file 03).

A gesture function is a small Python file:

    NEEDS = ["right_index_tip", "nose"]

    def detect(frames) -> bool: ...

`check` is an AST allowlist: only `numpy` and `math` may be imported, and there
is no dunder access, no `exec`/`eval`, no file or process access, no `while`
loop and no class. `load` then runs the file with a short list of builtins and
the helpers below. `detect` never sees a real Frame, only a `FrameView` that
refuses any part not listed in NEEDS, so a function cannot depend on a part the
tracker will not run at runtime.

During training `evaluate` runs the function in a separate process
(`python -m gesture_control.sandbox`) with a timeout and a memory limit. At
runtime the detector calls `load` and `run` in-process after the same check.
Python cannot be fully sandboxed in-process, which is why the user can read
each function before saving it.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import parts as P

ALLOWED_IMPORTS = ("numpy", "math")
TIMEOUT_S = 15.0
CUT_S = 0.005           # more time for each stretch of a segment that is run as well
MEMORY_MB = 1024
SLOW_MS = 50.0          # one call; the runtime budget is a few ms per function
RANDOM_INPUTS = 8
RANDOM_ALLOWED = 1      # tolerate one fluke on pure noise

BANNED_NAMES = frozenset({
    "exec", "eval", "compile", "open", "input", "getattr", "setattr", "delattr", "globals",
    "locals", "vars", "dir", "breakpoint", "help", "exit", "quit", "memoryview", "type",
    "object", "super", "print", "id", "callable", "classmethod", "staticmethod", "property",
})
BANNED_ATTRS = frozenset({
    # numpy's file, buffer and module doors
    "load", "save", "savez", "savez_compressed", "loadtxt", "savetxt", "genfromtxt",
    "fromfile", "tofile", "fromregex", "memmap", "open_memmap", "DataSource", "dump", "dumps",
    "frombuffer", "ctypes", "ctypeslib", "lib", "core", "compat", "f2py", "testing",
    "distutils", "sys", "os", "random", "show_config", "source", "info", "lookfor",
    # string formatting can reach attributes the AST check never sees
    "format", "format_map",
})
BANNED_NODES = (ast.While, ast.ClassDef, ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith,
                ast.Await, ast.Yield, ast.YieldFrom, ast.Global, ast.Nonlocal)

_BUILTINS = (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance",
    "len", "list", "map", "max", "min", "range", "reversed", "round", "set", "sorted", "str",
    "sum", "tuple", "zip", "divmod", "pow", "frozenset", "slice",
    "ArithmeticError", "Exception", "FloatingPointError", "IndexError", "KeyError",
    "OverflowError", "TypeError", "ValueError", "ZeroDivisionError",
)


class SandboxError(Exception):
    """The function is not allowed, or did not follow the contract."""


class NeedsError(SandboxError):
    """The function read a part it did not list in NEEDS."""


# static check -----------------------------------------------------------

def _bad_name(name: str) -> bool:
    return name != "_" and (name.startswith("_") or "__" in name)


def check(code: str) -> tuple[list[str], list[str]]:
    """(NEEDS, problems). The function may be loaded only if problems is empty."""
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        return [], [f"syntax error on line {error.lineno}: {error.msg}"]
    problems: list[str] = []
    needs: list[str] | None = None
    has_detect = False

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.FunctionDef):
            if node.name == "detect":
                has_detect = True
                a = node.args
                if len(a.args) != 1 or a.vararg or a.kwarg or a.kwonlyargs or a.posonlyargs:
                    problems.append("detect must take exactly one argument, frames")
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue                        # a docstring
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id == "NEEDS":
                try:
                    value = ast.literal_eval(node.value)
                except ValueError:
                    value = None
                if isinstance(value, (list, tuple)) and value \
                        and all(isinstance(v, str) for v in value):
                    needs = list(dict.fromkeys(value))
                else:
                    problems.append("NEEDS must be a non-empty list of part names")
            continue
        problems.append(f"line {node.lineno}: only imports, NEEDS, constants and functions "
                        "are allowed at the top level")

    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, BANNED_NODES):
            problems.append(f"line {line}: {type(node).__name__.lower()} is not allowed")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS:
                    problems.append(f"line {line}: import of {alias.name} is not allowed "
                                    "(only numpy and math)")
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in ALLOWED_IMPORTS:
                problems.append(f"line {line}: import from {node.module} is not allowed "
                                "(only numpy and math)")
            for alias in node.names:
                if alias.name == "*" or alias.name in BANNED_ATTRS or _bad_name(alias.name):
                    problems.append(f"line {line}: importing {alias.name} is not allowed")
        elif isinstance(node, ast.Name):
            if _bad_name(node.id) or node.id in BANNED_NAMES:
                problems.append(f"line {line}: the name {node.id} is not allowed")
        elif isinstance(node, ast.Attribute):
            if _bad_name(node.attr) or node.attr in BANNED_ATTRS:
                problems.append(f"line {line}: the attribute .{node.attr} is not allowed")
        elif isinstance(node, (ast.FunctionDef, ast.arg, ast.alias, ast.keyword)):
            name = getattr(node, "name", None) or getattr(node, "arg", None) \
                or getattr(node, "asname", None)
            if name and _bad_name(name):
                problems.append(f"line {line}: the name {name} is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "__" in node.value:
                problems.append(f"line {line}: strings containing __ are not allowed")
            elif node.value in P.ALL_PARTS and needs is not None and node.value not in needs:
                problems.append(f"line {line}: {node.value} is used but not listed in NEEDS")

    if needs is None and not any("NEEDS" in p for p in problems):
        problems.append("NEEDS is missing: list the parts the function uses")
    for name in needs or ():
        if name not in P.ALL_PARTS:
            problems.append(f"NEEDS lists an unknown part: {name}")
    if not has_detect:
        problems.append("there is no function called detect")
    return needs or [], list(dict.fromkeys(problems))


# what the function sees -------------------------------------------------

class FrameView:
    """One Frame, limited to the parts in NEEDS."""

    __slots__ = ("_frame", "_needs", "_groups", "t")

    def __init__(self, frame: P.Frame, needs: frozenset, groups: frozenset):
        self._frame, self._needs, self._groups, self.t = frame, needs, groups, frame.t

    def _allow(self, name: str) -> None:
        if name not in self._needs:
            if name not in P.ALL_PARTS:
                raise SandboxError(f"unknown part: {name!r}")
            raise NeedsError(f"{name} is read but not listed in NEEDS")

    def _allow_group(self, group_name: str) -> None:
        if group_name not in self._groups:
            if group_name not in P.GROUPS:
                raise SandboxError(f"unknown group: {group_name!r}")
            raise NeedsError(f"{group_name} is used but no part of it is listed in NEEDS")

    def part(self, name: str):
        """(x, y, z), or None when the part is not visible."""
        self._allow(name)
        return self._frame.part(name)

    def state(self, name: str) -> str:
        self._allow(name)
        return self._frame.state(name)

    def scale(self, group_name: str):
        """Palm size (left_hand, right_hand) or head size, in image heights, or None."""
        self._allow_group(group_name)
        return self._frame.scale(group_name)

    def palm_facing(self, side: str) -> str:
        self._allow_group(f"{side}_hand")
        return self._frame.derived.get(f"{side}_palm_facing", "unknown")

    @property
    def aspect(self) -> float:
        return self._frame.aspect


def _unit_scale(frame: FrameView, unit: str, *parts_used):
    """The size of one `unit` in image heights for this frame, or None."""
    if unit == "image":
        return 1.0
    if unit == "hand":
        hands = [P.group(p) for p in parts_used if p and P.group(p) != "head"]
        if not hands:
            raise SandboxError("unit='hand' needs a hand part; use 'head', 'left_hand' "
                               "or 'right_hand'")
        unit = hands[0]
    if unit not in P.GROUPS:
        raise SandboxError(f"unknown unit: {unit!r}")
    scale = frame.scale(unit)
    return scale if scale and scale > 0 else None


def _helpers() -> dict:
    import numpy as np

    def positions(frames, part, relative_to=None, unit="image"):
        """An (n, 2) array of x, y for `part`, one row per frame, NaN where it
        (or `relative_to`, or the unit's size) is not visible. x is multiplied
        by the image's aspect ratio so x and y use the same length, then both
        are divided by the unit: 'image' (image heights), 'hand', 'head',
        'left_hand' or 'right_hand'. With `relative_to`, the other part's
        position is subtracted first."""
        out = np.full((len(frames), 2), np.nan)
        for i, f in enumerate(frames):
            p = f.part(part)
            q = f.part(relative_to) if relative_to else (0.0, 0.0)
            scale = _unit_scale(f, unit, part, relative_to)
            if p is None or q is None or scale is None:
                continue
            out[i] = ((p[0] - q[0]) * f.aspect / scale, (p[1] - q[1]) / scale)
        return out

    def part_state(frame, part):
        """'visible', 'estimated' or 'not_visible'."""
        return frame.state(part)

    def distance(frame, a, b, unit="hand"):
        """Distance between parts a and b in one frame, in `unit`s, or None if
        either is not visible."""
        p, q = frame.part(a), frame.part(b)
        scale = _unit_scale(frame, unit, a, b)
        if p is None or q is None or scale is None:
            return None
        return P.dist(p, q, frame.aspect) / scale

    def visible_fraction(frames, part):
        """The share of frames (0 to 1) in which `part` is visible or estimated."""
        if not frames:
            return 0.0
        return sum(f.state(part) != P.NOT_VISIBLE for f in frames) / len(frames)

    def times(frames):
        """Seconds since the first frame, as an array."""
        return np.array([f.t - frames[0].t for f in frames], dtype=float)

    return {f.__name__: f for f in (positions, part_state, distance, visible_fraction, times)}


HELPER_DOCS = '''positions(frames, part, relative_to=None, unit="image") -> numpy array (n, 2)
    x, y of `part` per frame; rows are NaN where it is not visible. x is multiplied by the
    aspect ratio so x and y share a length, then both are divided by the unit:
    "image" (image heights), "hand" (the part's own palm size), "head", "left_hand",
    "right_hand". With relative_to, that part's position is subtracted first, e.g.
    positions(frames, "right_index_tip", relative_to="nose", unit="head").
part_state(frame, part) -> "visible" | "estimated" | "not_visible"
distance(frame, a, b, unit="hand") -> float, or None if either part is not visible
visible_fraction(frames, part) -> float between 0 and 1
times(frames) -> numpy array of seconds since the first frame
Each frame also has: frame.t, frame.part(name) -> (x, y, z) or None, frame.state(name),
frame.scale("right_hand" | "left_hand" | "head") -> float or None,
frame.palm_facing("left" | "right") -> "camera" | "away" | "unknown", frame.aspect.'''


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    # numpy imports its own submodules lazily, and those imports arrive here
    # too. The AST check has already limited the function's own import lines
    # to exactly numpy and math.
    if level or name.split(".")[0] not in ALLOWED_IMPORTS:
        raise ImportError(f"import of {name} is not allowed")
    return importlib.import_module(name)


@dataclass(frozen=True)
class Gesture:
    needs: tuple
    detect: object
    code: str = field(repr=False, default="")


def load(code: str) -> Gesture:
    """Check the code and build its `detect`. Raises SandboxError."""
    needs, problems = check(code)
    if problems:
        raise SandboxError("; ".join(problems))
    import builtins
    allowed = {name: getattr(builtins, name) for name in _BUILTINS}
    allowed["__import__"] = _safe_import
    namespace = {"__builtins__": allowed, "__name__": "gesture", **_helpers()}
    try:
        exec(compile(code, "<gesture>", "exec"), namespace)     # checked above
    except Exception as error:
        raise SandboxError(f"could not load: {type(error).__name__}: {error}") from None
    return Gesture(tuple(needs), namespace["detect"], code)


def run(gesture: Gesture, frames) -> bool:
    """Call detect on Frames. Raises SandboxError if it fails or does not return a bool."""
    needs = frozenset(gesture.needs)
    groups = frozenset(P.group(name) for name in needs)
    views = [FrameView(f, needs, groups) for f in frames]
    try:
        result = gesture.detect(views)
    except SandboxError:
        raise
    except Exception as error:
        raise SandboxError(f"{type(error).__name__}: {error}") from None
    if type(result).__name__ not in ("bool", "bool_"):
        raise SandboxError(f"detect returned {type(result).__name__}, not a bool")
    return bool(result)


# inputs that must never count as a gesture -------------------------------

def sanity_inputs(seed: int = 0) -> list[tuple[str, list]]:
    """Empty, constant and random input, as (label, frames)."""
    rng = random.Random(seed)
    derived = {"left_palm_facing": "camera", "right_palm_facing": "camera",
               "scale": {"left_hand": 0.1, "right_hand": 0.1, "head": 0.2}}

    def frames(make) -> list:
        return [P.Frame(i / 30, i, (640, 480), {name: make(name) for name in P.ALL_PARTS},
                        derived) for i in range(30)]

    still = {name: P.Part(rng.uniform(0.2, 0.8), rng.uniform(0.2, 0.8), 0.0, P.VISIBLE)
             for name in P.ALL_PARTS}
    inputs = [("an empty list of frames", []),
              ("frames where nothing is visible", frames(lambda name: P.MISSING)),
              ("frames where nothing moves", frames(still.get))]
    for i in range(RANDOM_INPUTS):
        inputs.append((f"random positions #{i + 1}", frames(
            lambda name: P.Part(rng.random(), rng.random(), 0.0, P.VISIBLE))))
    return inputs


# running in a separate process -------------------------------------------

@dataclass
class Report:
    error: str = ""                 # the whole run failed: not allowed, crashed, timed out
    needs: tuple = ()
    results: list = field(default_factory=list)     # per segment: True, False or an error text
    sanity: list = field(default_factory=list)      # problems with empty/constant/random input
    slow_ms: float = 0.0            # the slowest single call
    hits: list = field(default_factory=list)        # per segment: the first of its cuts that
                                                    # gave True as [start, end], or None

    @property
    def ok(self) -> bool:
        return not self.error and not self.sanity


def _call(gesture: Gesture, frames) -> tuple:
    started = time.perf_counter()
    try:
        result = run(gesture, frames)
    except SandboxError as error:
        result = f"error: {error}"
    except BaseException as error:          # MemoryError, RecursionError, ...
        result = f"error: {type(error).__name__}"
    return result, (time.perf_counter() - started) * 1000


def _evaluate_here(code: str, segments: list, cuts=()) -> dict:
    try:
        gesture = load(code)
    except SandboxError as error:
        return {"error": str(error)}
    results, slow = [], 0.0
    for frames in segments:
        result, ms = _call(gesture, frames)
        results.append(result)
        slow = max(slow, ms)
    hits = []
    for frames, spans in zip(segments, cuts or ()):
        hit = None
        for start, end in spans:
            result, ms = _call(gesture, frames[start:end])
            slow = max(slow, ms)
            if result is True:
                hit = [start, end]
                break
        hits.append(hit)
    sanity, random_hits = [], 0
    for label, frames in sanity_inputs():
        result, ms = _call(gesture, frames)
        slow = max(slow, ms)
        if result is True and label.startswith("random"):
            random_hits += 1
        elif result is True:
            sanity.append(f"detect returned True for {label}")
        elif result is not False and not label.startswith("random"):
            sanity.append(f"detect failed on {label}: {result}")
    if random_hits > RANDOM_ALLOWED:
        sanity.append(f"detect returned True for {random_hits} of {RANDOM_INPUTS} inputs of "
                      "pure random positions; require the shape of the motion, not just a "
                      "difference between two frames")
    if slow > SLOW_MS:
        sanity.append(f"detect took {slow:.0f} ms on one input; it must stay under "
                      f"{SLOW_MS:.0f} ms")
    return {"needs": list(gesture.needs), "results": results, "sanity": sanity, "slow_ms": slow,
            "hits": hits}


def evaluate(code: str, segments, cuts=(), timeout: float | None = None,
             memory_mb: int = MEMORY_MB) -> Report:
    """Run `code` on each segment (a sequence of Frames) in a separate process,
    along with the empty, constant and random inputs. `cuts` may give, for
    each segment, the (start, end) stretches of it to run the function on as
    well; `Report.hits` then says which of them, if any, returned True."""
    _, problems = check(code)
    if problems:
        return Report(error="; ".join(problems))
    cuts = [[list(span) for span in spans] for spans in cuts or ()]
    if timeout is None:
        timeout = TIMEOUT_S + CUT_S * sum(len(spans) for spans in cuts)
    request = json.dumps({"code": code, "memory_mb": memory_mb, "cuts": cuts,
                          "segments": [[f.to_dict() for f in frames] for frames in segments]})
    env = {k: v for k, v in os.environ.items() if "KEY" not in k.upper()
           and "TOKEN" not in k.upper() and "SECRET" not in k.upper()}
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        done = subprocess.run(
            [sys.executable, "-m", "gesture_control.sandbox"], input=request, text=True,
            capture_output=True, timeout=timeout, env=env, creationflags=flags,
            cwd=str(Path(__file__).resolve().parent.parent))
    except subprocess.TimeoutExpired:
        return Report(error=f"timed out after {timeout:.0f} s; detect must be fast and finite")
    try:
        raw = json.loads(done.stdout)
    except ValueError:
        tail = (done.stderr or "").strip().splitlines()[-1:] or ["no output"]
        return Report(error=f"the function crashed its process ({tail[0][:200]}); "
                            "it may have used too much memory")
    return _report(raw)


def _report(raw: dict) -> Report:
    return Report(raw.get("error", ""), tuple(raw.get("needs", ())), raw.get("results", []),
                  raw.get("sanity", []), raw.get("slow_ms", 0.0), raw.get("hits", []))


def evaluate_in_process(code: str, segments, cuts=()) -> Report:
    """The same checks as `evaluate` without the separate process, timeout or
    memory limit. For functions that are already trusted, and for tests."""
    return _report(_evaluate_here(code, [list(frames) for frames in segments], cuts))


def _limit_memory(mb: int) -> bool:
    """Cap this process's memory, and on Windows stop it starting other processes."""
    limit = mb * 1024 * 1024
    try:
        if sys.platform != "win32":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            return True
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic),
                        ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD)
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x100 | 0x8     # process memory, process count
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = limit
        job = k32.CreateJobObjectW(None, None)
        return bool(job and k32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                                        ctypes.sizeof(info))
                    and k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()))
    except Exception:
        return False


def _child() -> None:
    request = json.loads(sys.stdin.read())
    import numpy  # noqa: F401  loaded before the limit so the import itself always fits
    if not _limit_memory(request["memory_mb"]):
        print("warning: no memory limit on this platform", file=sys.stderr)
    segments = [[P.Frame.from_dict(f) for f in frames] for frames in request["segments"]]
    out = _evaluate_here(request["code"], segments, request.get("cuts", ()))
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    _child()
