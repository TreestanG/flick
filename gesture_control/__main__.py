"""The engine. Run with `python -m gesture_control`.

A headless process: camera, tracking, agent, detection, actions and the tray
icon. It has no window. The window is the Tauri app in `app/`, which connects
over a local WebSocket (file 01) and can be closed while this keeps running.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

from . import store
from .contract import Bus
from .server import VERSION, Server
from .tray import Tray


def _lock(file) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def claim_instance(folder: Path | None = None):
    """Only one engine may run: a second one would fight over the camera and
    keep its own, separate idea of what is saved. Returns the locked file if
    this is the only copy (close it to let go; the lock also goes away by
    itself if the engine dies), or None if another engine holds it."""
    folder = folder or store.config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    lock = open(folder / "engine.lock", "a+")
    if _lock(lock):
        return lock
    lock.close()
    return None


def engine_file(folder: Path | None = None) -> Path:
    return (folder or store.runtime_dir()) / "engine.json"


def write_engine_file(port: int, token: str, folder: Path | None = None) -> Path:
    """How a UI window finds this engine: port and token, readable only by
    the current user."""
    path = engine_file(folder)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = json.dumps({"port": port, "token": token, "pid": os.getpid(), "version": VERSION})
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(body)
    os.replace(tmp, path)
    return path


def remove_engine_file(folder: Path | None = None) -> None:
    try:
        engine_file(folder).unlink()
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="gesture_control")
    parser.add_argument("--dump-frames", metavar="FILE",
                        help="write every tracked Frame to FILE as JSONL")
    parser.add_argument("--no-tray", action="store_true", help="do not show the tray icon")
    parser.add_argument("--port", type=int, default=0,
                        help="port for the UI socket (default: any free port)")
    parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)  # old login items
    args = parser.parse_args()

    lock = claim_instance()
    if lock is None:
        print("The Gesture Control engine is already running.")
        return

    bus = Bus()
    settings = store.load_settings()
    from .backend import Backend            # imported late: it loads OpenCV and MediaPipe
    backend = Backend(bus, settings.to_dict(), dump_path=args.dump_frames)

    stop = threading.Event()
    tray = Tray(on_toggle=lambda: server.toggle_armed(), on_quit=stop.set,
                enabled=not args.no_tray)
    server = Server(bus, settings, backend, port=args.port, on_quit=stop.set,
                    on_armed=tray.set_armed)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())

    backend.start()             # on macOS this opens the camera on the main thread first
    try:
        port = server.start()
        write_engine_file(port, server.token)
        print(f"Gesture Control engine listening on 127.0.0.1:{port}", flush=True)
        tray.run(stop)          # blocks until Quit
    finally:
        remove_engine_file()
        tray.stop()
        server.stop()
        backend.stop()
        lock.close()


if __name__ == "__main__":
    main()
