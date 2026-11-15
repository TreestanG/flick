"""Run with `python -m gesture_control`."""

from __future__ import annotations

import argparse

from . import store
from .contract import Bus
from .ui.app import App


def main() -> None:
    parser = argparse.ArgumentParser(prog="gesture_control")
    parser.add_argument("--background", action="store_true",
                        help="start hidden in the tray (used by start on login)")
    parser.add_argument("--fake", action="store_true",
                        help="use made-up tracking data instead of the camera")
    parser.add_argument("--dump-frames", metavar="FILE",
                        help="write every tracked Frame to FILE as JSONL")
    args = parser.parse_args()

    bus = Bus()
    settings = store.load_settings()
    if args.fake:
        from .fake_backend import FakeBackend
        backend = FakeBackend(bus)
    else:
        # Files 03 and 04 replace the fake training and actions this still inherits.
        from .backend import Backend
        backend = Backend(bus, settings.to_dict(), dump_path=args.dump_frames)
    backend.start()
    app = App(bus, settings, start_hidden=args.background)
    try:
        app.mainloop()
    finally:
        backend.stop()


if __name__ == "__main__":
    main()
