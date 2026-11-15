"""The preview stream (file 02): one JPEG per frame, made only while a UI
window asks for it. The newest-frame slot it reads from is `PreviewSlot` in
`contract.py`."""

from __future__ import annotations

import io

from PIL import ImageDraw

from .contract import PreviewFrame

QUALITY = 70
FPS = 15
DOT = "#6CB8F0"


def encode(frame: PreviewFrame, show_points: bool = True) -> bytes:
    """A complete JPEG of the frame, not mirrored (the UI mirrors it), with a
    dot on every tracked part if `show_points`."""
    image = frame.image
    if show_points and frame.points:
        image = image.copy()
        draw = ImageDraw.Draw(image)
        r = max(2, image.width // 200)
        for x, y in frame.points:
            px, py = x * image.width, y * image.height
            draw.ellipse((px - r, py - r, px + r, py + r), fill=DOT)
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=QUALITY)
    return out.getvalue()
