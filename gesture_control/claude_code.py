"""Claude Code in a Docker container, as the writer of gesture functions.

The engine starts one container when it starts (`start_in_background`) and
removes it when it quits (`stop`). A training run copies its samples into the
container's /work folder, Claude Code works there with its own tools (it can
read the samples and run the function on them), and the app pulls
/work/detect.py back out. The trainer's loop, tests and budget stay as they
are: `ClaudeCodeClient` is one more `ModelClient`.

Claude Code signs in with the long-lived token from `claude setup-token`. The
token is kept in the OS keychain (or read from CLAUDE_CODE_OAUTH_TOKEN) and is
handed to each `docker exec` through the environment, so it is never written
into the image, the container's settings or a command line.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

from .model_client import ModelError

NAME = "gesture-control-claude"             # the image and the one container
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
FILES = Path(__file__).resolve().parent / "container"
MODEL = "claude-opus-5-5"
# The app tests every answer on the user's samples, so a wrong first try is
# cheap and a slow one is not: keep the model's deliberation short.
EFFORT = "low"
TIMEOUT_S = 600.0           # one turn: Claude Code may read samples and run checks
BUILD_TIMEOUT_S = 900.0     # the first start downloads the image's parts
DAEMON_WAIT_S = 90.0        # Docker Desktop starting up

_lock = threading.Lock()


def _docker(*args: str, data: bytes | None = None, timeout: float = 60,
            env: dict | None = None) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        return subprocess.run(["docker", *args], input=data, capture_output=True,
                              timeout=timeout, env=env, creationflags=flags)
    except FileNotFoundError:
        raise ModelError("Docker is not installed. Claude Code runs in a Docker "
                         "container; install Docker Desktop first.") from None
    except subprocess.TimeoutExpired:
        raise ModelError("Docker took too long to answer.") from None


def _last_line(done: subprocess.CompletedProcess) -> str:
    lines = (done.stderr or done.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    return lines[-1][:300] if lines else "no output"


def _daemon_up() -> bool:
    return _docker("info", "--format", "{{.ServerVersion}}", timeout=20).returncode == 0


def _wake_daemon() -> bool:
    """Start Docker Desktop and wait for it. Only macOS has one way to do this."""
    if sys.platform != "darwin":
        return False
    subprocess.run(["open", "-ga", "Docker"], capture_output=True)
    deadline = time.monotonic() + DAEMON_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(3)
        if _daemon_up():
            return True
    return False


def image() -> str:
    """The image's tag follows the Dockerfile, so an edit to it builds a new one."""
    digest = hashlib.sha256((FILES / "Dockerfile").read_bytes()).hexdigest()[:10]
    return f"{NAME}:{digest}"


def start(launch_docker: bool = True) -> None:
    """Make sure the container is running. Does nothing if it already is.
    Raises ModelError with a message for the user."""
    with _lock:
        if not _daemon_up() and not (launch_docker and _wake_daemon()):
            raise ModelError("Docker is not running. Start Docker Desktop and try again.")
        tag = image()
        state = _docker("inspect", "--format", "{{.State.Running}} {{.Config.Image}}", NAME)
        if state.returncode == 0 and state.stdout.decode().split() == ["true", tag]:
            return
        if _docker("image", "inspect", tag).returncode != 0:
            built = _docker("build", "-t", tag, str(FILES), timeout=BUILD_TIMEOUT_S)
            if built.returncode != 0:
                raise ModelError(f"Could not build the Claude Code container: {_last_line(built)}")
        _docker("rm", "-f", NAME)
        ran = _docker("run", "-d", "--name", NAME, "--label", "gesture_control=1", tag)
        if ran.returncode != 0:
            raise ModelError(f"Could not start the Claude Code container: {_last_line(ran)}")


def start_in_background(launch_docker: bool = True) -> threading.Thread:
    """For the engine's start: the first build takes minutes and nothing waits for it."""
    def work() -> None:
        try:
            start(launch_docker)
        except ModelError as error:
            print(f"Claude Code container: {error}", file=sys.stderr, flush=True)

    thread = threading.Thread(target=work, name="claude-container", daemon=True)
    thread.start()
    return thread


def stop() -> None:
    try:
        _docker("rm", "-f", NAME, timeout=30)
    except ModelError:
        pass


def put(files: dict[str, bytes]) -> None:
    """Write files into the container, by path under /work."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
            tar.addfile(info, io.BytesIO(data))
    done = _docker("exec", "-i", NAME, "tar", "-xf", "-", "-C", "/work",
                   data=buffer.getvalue(), timeout=120)
    if done.returncode != 0:
        raise ModelError(f"Could not copy files into the container: {_last_line(done)}")


def pull(name: str) -> str:
    """One file from /work, or "" if it is not there."""
    done = _docker("exec", NAME, "cat", f"/work/{name}")
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else ""


def _tools() -> dict[str, bytes]:
    """The app's own checks, so Claude Code judges a function the way the app will."""
    package = Path(__file__).resolve().parent
    return {"tools/check.py": (FILES / "check.py").read_bytes(),
            "tools/gesture_control/__init__.py": b"",
            "tools/gesture_control/parts.py": (package / "parts.py").read_bytes(),
            "tools/gesture_control/sandbox.py": (package / "sandbox.py").read_bytes(),
            "tools/gesture_control/segmenter.py": (package / "segmenter.py").read_bytes()}


WORKSPACE = """

YOUR WORKSPACE

You are Claude Code, running in a container made for this job. Unlike what is said above, \
here you do have the training samples, as files:

- /work/samples/positives/P1.json, /work/samples/negatives/N1.json, ...: one file per \
training sample: {"label", "kind", "lead", "frames": [...]}. A frame is {"t", "frame", \
"image": {"w", "h"}, "parts": {name: {"x", "y", "z", "state"}}, "derived": {...}}; a part \
that is not visible has only its state. The app adds files as it collects samples and \
replaces one when the user performs it again. The samples of a fresh test appear only after \
the test.
- /work/detect.py: where the function goes (the whole file: NEEDS and detect). The app \
pulls this file out.
- `python3 /work/tools/check.py` runs /work/detect.py on every sample with the app's own \
checks and prints what it got wrong, including the stretches of each negative the live \
loop would have checked (`frames[start:end]` of that sample). python3 has numpy for your own look at the samples.

To write or revise the function: edit /work/detect.py, run the check until it is clean (or \
as good as a few tries get it), then end your reply with a line holding only the word CODE. \
The app then takes /work/detect.py as if you had sent it in a python code block, and runs \
it again itself. Do not paste the file into your reply.

The other actions (REQUEST k, STATS, REDO, TEST k, CONFIDENT) are unchanged: reply with \
them in plain text. You rarely need STATS here: read the samples instead. Still exactly one \
action per reply, and do not take long: the user is waiting between samples."""

_WROTE = re.compile(r"^\W*CODE\W*$", re.M)


class ClaudeCodeClient:
    """One conversation with Claude Code in the container."""

    def __init__(self, token: str, model: str = "", timeout: float = TIMEOUT_S):
        self.token = token
        model = model.strip()
        self.model = model if model.startswith("claude-") else MODEL
        self.timeout = timeout
        self.session = ""               # Claude Code's own id for this conversation
        self.workspace = False          # this run's samples are in /work
        self.synced: dict = {}          # label -> the Segment that was copied in

    def sync(self, positives: list, negatives: list) -> None:
        """Copy the training samples the container does not have yet. The
        trainer calls this before each turn with (label, Segment) lists."""
        start()
        files: dict[str, bytes] = {}
        if not self.workspace:
            # A new run: nothing of the last one may be left to read.
            _docker("exec", NAME, "sh", "-c",
                    "rm -rf /work/samples /work/tools /work/detect.py")
            files.update(_tools())
            self.workspace = True
        for kind, samples in (("positives", positives), ("negatives", negatives)):
            for label, segment in samples:
                if self.synced.get(label) is segment:
                    continue
                self.synced[label] = segment
                files[f"samples/{kind}/{label}.json"] = json.dumps(
                    {"label": label, "kind": kind[:-1], "lead": segment.lead,
                     "frames": [frame.to_dict() for frame in segment.frames]}).encode()
        if files:
            put(files)

    def complete(self, messages: list[dict]) -> str:
        start()
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if self.workspace:
            system += WORKSPACE
        said = [m for m in messages if m["role"] != "system"]
        command = ["exec", "-i", "-e", TOKEN_ENV, "-w", "/work", NAME, "claude", "-p",
                   "--output-format", "json", "--model", self.model, "--effort", EFFORT,
                   "--dangerously-skip-permissions",    # the container is the boundary
                   "--append-system-prompt", system]
        if self.session:
            command += ["--resume", self.session]
            prompt = said[-1]["content"]        # Claude Code keeps what came before
        else:
            prompt = "\n\n".join(m["content"] for m in said)
        try:
            done = _docker(*command, data=prompt.encode(), timeout=self.timeout,
                           env={**os.environ, TOKEN_ENV: self.token})
        except ModelError as error:
            if "too long" not in str(error):
                raise
            _docker("exec", NAME, "pkill", "-f", "claude")      # it would go on working
            raise ModelError("Claude Code took too long to reply. Try again.") from None
        try:
            raw = json.loads(done.stdout)
            text, failed = raw.get("result") or "", bool(raw.get("is_error"))
            self.session = raw.get("session_id") or self.session
        except (ValueError, AttributeError):
            text, failed = _last_line(done), True
        if failed or done.returncode != 0:
            low = text.lower()
            if any(word in low for word in ("401", "authenticat", "/login", "oauth", "api key")):
                raise ModelError("Claude Code could not sign in with the token. Run "
                                 "`claude setup-token` again and paste the new token in "
                                 "settings.")
            raise ModelError(f"Claude Code failed: {text[:300] or 'no answer'}")
        if not text.strip():
            raise ModelError("Claude Code sent an empty answer. Try again.")
        if self.workspace and _WROTE.search(text) and "def detect" not in text:
            code = pull("detect.py")
            if code.strip():
                return f"```python\n{code.rstrip()}\n```"
            return "CODE was the reply, but there is no /work/detect.py."
        return text
