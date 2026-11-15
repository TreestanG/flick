"""The webcam: 640x480 at 30 fps with a one-frame buffer, so old frames are
never processed. A lost camera is retried every few seconds."""

from __future__ import annotations

import re
import sys
import time

import cv2

RETRY_S = 3.0
SIZE = (640, 480)
FPS = 30


def device_index(name: str) -> int:
    """'Default camera' and '' are 0, 'Camera 2' is 2."""
    found = re.search(r"(\d+)$", name or "")
    return int(found.group(1)) if found else 0


def device_name(index: int) -> str:
    return "Default camera" if index == 0 else f"Camera {index}"


def _capture(index: int):
    if sys.platform == "win32":
        return cv2.VideoCapture(index, cv2.CAP_DSHOW)    # opens faster than the default
    return cv2.VideoCapture(index)


def probe(indexes=(1, 2, 3)) -> list[int]:
    """Which other camera indexes open. Slow, so call it off the tracking thread."""
    found = []
    for index in indexes:
        cap = _capture(index)
        if cap.isOpened():
            found.append(index)
        cap.release()
    return found


class Camera:
    def __init__(self, index: int = 0):
        self.index = index
        self._cap = None
        self._next_try = 0.0

    @property
    def ok(self) -> bool:
        return self._cap is not None

    def open(self) -> bool:
        """On macOS the first call must come from the main thread (permission prompt)."""
        self.close()
        self._next_try = time.monotonic() + RETRY_S
        cap = _capture(self.index)
        if not cap.isOpened():
            cap.release()
            return False
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, SIZE[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, SIZE[1])
        cap.set(cv2.CAP_PROP_FPS, FPS)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap
        return True

    def switch(self, index: int) -> None:
        if index != self.index:
            self.index = index
            self.close()
            self._next_try = 0.0

    def read(self):
        """The next BGR image, or None while the camera is missing."""
        if self._cap is None:
            if time.monotonic() < self._next_try or not self.open():
                return None
        ok, image = self._cap.read()
        if not ok or image is None:
            self.close()
            self._next_try = time.monotonic() + RETRY_S
            return None
        return image

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
