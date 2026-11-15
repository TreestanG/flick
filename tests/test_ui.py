"""Widgets that need a real window. Skipped where there is no display."""

import time

import pytest


@pytest.fixture(scope="module")
def window():
    import customtkinter as ctk
    try:
        made = ctk.CTk()
    except Exception as error:          # no display
        pytest.skip(str(error))
    made.geometry("400x300+40+40")
    yield made
    made.destroy()


@pytest.fixture
def root(window):
    yield window
    for child in window.winfo_children():
        child.destroy()


def pump(window, seconds: float, sample=lambda: None) -> list:
    seen, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        window.update()
        seen.append(sample())
        time.sleep(0.004)
    return seen


def test_wheel_scroll_glides_instead_of_jumping(root):
    from gesture_control.ui.widgets import Scroll, label
    area = Scroll(root)
    area.pack(fill="both", expand=True)
    rows = [label(area, f"row {i}") for i in range(80)]
    for row in rows:
        row.pack()
    pump(root, 0.3)
    canvas = area._parent_canvas
    assert canvas.canvasy(0) == 0 and area._limit() > 500

    drawn = []                          # where the view was after each painted step
    inner = area._glide

    def glide():
        inner()
        drawn.append((canvas.canvasy(0), Scroll.busy()))
    area._glide = glide

    rows[3].event_generate("<MouseWheel>", delta=-120)          # one notch down
    pump(root, 0.8)
    positions = [y for y, _ in drawn]
    steps = [b - a for a, b in zip([0] + positions, positions) if b != a]
    assert positions[-1] == Scroll.NOTCH_PX and drawn[0][1]     # busy while it glides
    # Several painted steps, none of them most of the way: a glide, not a jump.
    # (Steps follow elapsed time, so a busy machine makes fewer, larger ones.)
    assert len(steps) >= 5 and all(0 < step < Scroll.NOTCH_PX * 0.7 for step in steps)
    assert steps[-1] <= 2                                       # eases out

    for _ in range(3):                                          # notches add up mid-glide
        rows[3].event_generate("<MouseWheel>", delta=-120)
        pump(root, 0.02)
    assert pump(root, 0.8, lambda: canvas.canvasy(0))[-1] == 4 * Scroll.NOTCH_PX

    rows[3].event_generate("<MouseWheel>", delta=120 * 50)      # far past the top
    assert pump(root, 1.2, lambda: canvas.canvasy(0))[-1] == 0
    rows[3].event_generate("<MouseWheel>", delta=-120 * 500)    # and past the bottom
    assert pump(root, 1.5, lambda: canvas.canvasy(0))[-1] == area._limit()
    time.sleep(0.25)
    assert not Scroll.busy()


def test_short_content_does_not_scroll(root):
    from gesture_control.ui.widgets import Scroll, label
    area = Scroll(root)
    area.pack(fill="both", expand=True)
    only = label(area, "one row")
    only.pack()
    pump(root, 0.3)
    only.event_generate("<MouseWheel>", delta=-120)
    assert set(pump(root, 0.2, lambda: area._parent_canvas.canvasy(0))) == {0}
