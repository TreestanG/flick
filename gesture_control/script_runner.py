"""Custom scripts (file 04): the user's own script, run in a folder they chose.

Stored with the gesture as
{"kind": "script", "language": "python", "file": "action.py",
 "working_dir": "", "timeout_s": 30, "skip_if_running": true}.

The script is a file in `<config dir>/scripts/<gesture-id>/`. It is started
as a separate process from a list of arguments, never through a shell
string, so a gesture's name or any other text cannot inject a command. It
runs with the user's own permissions. Only the user writes scripts; the
agent never does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import store

DEFAULT_TIMEOUT_S = 30.0
LOG_LIMIT = 1_000_000           # bytes; the log starts again when it passes this
LOG_TAIL = 20_000               # characters of the log a window is sent
MAX_CODE = 200_000
SUFFIXES = {"python": ".py", "powershell": ".ps1", "shell": ".sh"}


def script_dir(gesture_id: str) -> Path:
    """The folder the app makes for one gesture's script. Also the default
    working directory."""
    return store.config_dir() / "scripts" / gesture_id


def log_path(gesture_id: str) -> Path:
    return script_dir(gesture_id) / "run.log"


def _language(name: str) -> str:
    """"shell" means the system shell: PowerShell on Windows."""
    if name == "shell" and sys.platform == "win32":
        return "powershell"
    return name


def check(action: dict) -> dict:
    """A clean copy of a script action as a window sends it (with the script
    text in "code"), or ValueError saying what is wrong."""
    language = action.get("language")
    if language not in SUFFIXES:
        raise ValueError("Choose a script language.")
    code = action.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("The script is empty.")
    if len(code) > MAX_CODE:
        raise ValueError("The script is too long.")
    working_dir = action.get("working_dir") or ""
    if not isinstance(working_dir, str):
        raise ValueError("The working folder must be a path.")
    working_dir = working_dir.strip()
    if working_dir and not (os.path.isabs(working_dir) and os.path.isdir(working_dir)):
        raise ValueError(f"The working folder {working_dir} does not exist.")
    timeout = action.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not 1 <= timeout <= 3600:
        raise ValueError("The timeout must be between 1 and 3600 seconds.")
    return {"kind": "script", "language": language, "code": code, "working_dir": working_dir,
            "timeout_s": timeout, "skip_if_running": bool(action.get("skip_if_running", True))}


def preview(gesture_id: str | None, action: dict) -> dict:
    """What a checked script action will run and where, in words, before
    anything is written. `gesture_id` is None for a gesture not saved yet."""
    language = _language(action["language"])
    program = {"python": "python", "powershell": "powershell", "shell": "sh"}[language]
    folder = action["working_dir"] or (str(script_dir(gesture_id)) if gesture_id
                                       else "a new folder the app makes for this gesture")
    return {"command": f"{program} action{SUFFIXES[language]}", "working_dir": folder}


def info(gesture: dict | None) -> dict:
    """What the action editor shows for a gesture: its script, if it has one,
    its default folder and the end of its log."""
    out = {"default_dir": "", "code": "", "log": "",
           "shell_name": "PowerShell" if sys.platform == "win32" else "Shell script"}
    if not gesture:
        return out
    gesture_id = str(gesture["id"])
    out["default_dir"] = str(script_dir(gesture_id))
    action = gesture.get("action") or {}
    if action.get("kind") == "script":
        file = Path(str(action.get("file") or ""))
        try:
            out["code"] = (file if file.is_absolute() else script_dir(gesture_id) / file) \
                .read_text(encoding="utf-8")
        except OSError:
            pass
    try:
        out["log"] = log_path(gesture_id).read_text(encoding="utf-8")[-LOG_TAIL:]
    except OSError:
        pass
    return out


def save_script(gesture_id: str, language: str, code: str, working_dir: str = "",
                timeout_s: float = DEFAULT_TIMEOUT_S, skip_if_running: bool = True,
                stem: str = "action") -> dict:
    """Write the user's script to its folder and return the action to store."""
    if language not in SUFFIXES:
        raise ValueError(f"Unknown script language: {language}")
    file = stem + SUFFIXES[_language(language)]
    folder = script_dir(gesture_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / file).write_text(code, encoding="utf-8")
    return {"kind": "script", "language": language, "file": file, "working_dir": working_dir,
            "timeout_s": timeout_s, "skip_if_running": bool(skip_if_running)}


def command(action: dict, gesture_id: str) -> tuple[list[str], Path]:
    """Exactly what will run and where: (arguments, working directory)."""
    language = _language(str(action.get("language", "")))
    if language not in SUFFIXES:
        raise RuntimeError(f"Unknown script language: {action.get('language')!r}")
    file = Path(str(action.get("file") or "action" + SUFFIXES[language]))
    if not file.is_absolute():
        file = script_dir(gesture_id) / file
    if not file.is_file():
        raise RuntimeError(f"The script {file} does not exist.")
    cwd = Path(action.get("working_dir") or script_dir(gesture_id))
    if not cwd.is_dir():
        raise RuntimeError(f"The working folder {cwd} does not exist.")
    # Microsoft Store Python redirects %APPDATA% into its own package folder,
    # which a child process outside the package cannot see. The real path works
    # for both.
    file, cwd = Path(os.path.realpath(file)), Path(os.path.realpath(cwd))
    if language == "python":
        frozen = getattr(sys, "frozen", False)      # the packaged engine is not a Python
        python = (shutil.which("python3") or shutil.which("python")) if frozen else sys.executable
        if not python:
            raise RuntimeError("Python is not installed, so this script cannot run.")
        return [python, str(file)], cwd
    if language == "powershell":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            raise RuntimeError("PowerShell is not installed.")
        # Bypass applies to this one process only; the machine's policy is untouched.
        return [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(file)], cwd
    return ["sh", str(file)], cwd


def _kill(process: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":             # the whole tree, not just the shell
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=5,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        process.kill()
    except (OSError, subprocess.SubprocessError):
        pass


class ScriptRunner:
    def __init__(self):
        self._running: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def run(self, gesture: dict, action: dict) -> tuple[bool, str]:
        """Run the script and wait for it (the caller is on its own thread).
        Returns (ok, message). Output goes to the gesture's log."""
        gesture_id = str(gesture["id"])
        try:
            timeout = float(action.get("timeout_s") or DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        with self._lock:
            before = self._running.get(gesture_id)
            if before is not None and before.poll() is None:
                if action.get("skip_if_running", True):
                    return False, "Skipped: the previous run is still going"
            try:
                argv, cwd = command(action, gesture_id)
                env = dict(os.environ, GESTURE_NAME=str(gesture.get("name", "")),
                           GESTURE_ID=gesture_id,
                           GESTURE_TIME=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           creationflags=flags)
            except (RuntimeError, OSError) as error:
                self._log(gesture_id, f"could not start: {error}", b"")
                return False, str(error)
            self._running[gesture_id] = process

        started = time.monotonic()
        try:
            output, _ = process.communicate(timeout=timeout)
            took = time.monotonic() - started
            ok = process.returncode == 0
            message = "ok" if ok else f"The script failed (exit code {process.returncode}). See its log."
            self._log(gesture_id, f"exit code {process.returncode} after {took:.1f} s", output)
        except subprocess.TimeoutExpired:
            _kill(process)
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                output = b""
            ok, message = False, f"The script was stopped after {timeout:g} s. See its log."
            self._log(gesture_id, f"stopped after {timeout:g} s (timeout)", output)
        finally:
            with self._lock:
                if self._running.get(gesture_id) is process:
                    del self._running[gesture_id]
        return ok, message

    @staticmethod
    def _log(gesture_id: str, line: str, output: bytes) -> None:
        path = log_path(gesture_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "w" if path.exists() and path.stat().st_size > LOG_LIMIT else "a"
            with open(path, mode, encoding="utf-8") as file:
                file.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")
                text = output.decode("utf-8", errors="replace").replace("\r\n", "\n")
                if text:
                    file.write(text if text.endswith("\n") else text + "\n")
        except OSError:
            pass
