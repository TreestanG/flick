# 2. Webcam and Body Tracking (MediaPipe)

Part 2 of 4. Turns the webcam into a stream of body-part positions with visibility states, and splits that stream into motions.

Related files: `01-desktop-app-ui.md`, `03-gesture-agent-and-detection.md`, `04-action-mapping.md`.

## Goals

- Always-on webcam with low CPU use.
- Track fingertips, the palm and back of the hand, ears, eyes, nose, bottom of the head and top of the head.
- Each part has a state, including "not visible".
- Run only the models that saved gestures actually need.

## Pipeline

```
Camera thread -> MediaPipe models (only those needed) -> Frame -> Segmenter -> Segment
                                                           |
                                                           +--> preview events to the UI
```

- Camera and tracking run on a dedicated thread.
- The segmenter output goes to the detector (file 03) on the same thread, to keep latency low.

## Module layout

```
camera.py      # open the webcam from the main thread, read only the latest frame
parts.py       # part catalog, which model provides each part, derived values
tracker.py     # runs the needed MediaPipe models, outputs a Frame
segmenter.py   # speed-based start/stop state machine, outputs a Segment
```

## Camera

- Open the webcam on the main thread, then hand it to the camera thread. This avoids a permission problem on macOS.
- 640x480 at 30 fps is enough. Keep the buffer to one frame so old frames are never processed.
- If the camera disappears, mark the tracker `Camera error` and retry every few seconds.

## Models and where each part comes from

| What | Source model | Notes |
|---|---|---|
| Fingertips, wrist, finger joints | MediaPipe hand landmarker (21 points per hand) | Direct output |
| Palm or back of hand facing the camera | Computed from the hand points | Derived, can be wrong when the hand is edge-on |
| Nose, eyes, ears | MediaPipe pose landmarker (33 points) | Gives a confidence score per point |
| Nose, eyes, chin or jaw | MediaPipe face landmarker (about 478 points) | More precise for the face, heavier |
| Bottom of head | Face landmarker, chin point | |
| Top of head | Estimated from the face (forehead point plus a typical head proportion) | No model outputs this; always marked `estimated` |

Notes on MediaPipe:
- In the version I tested here (0.10.32) the older `mp.solutions` interface is gone, and the newer Tasks interface with a `HandLandmarker` is what exists. The face and pose landmarkers should be there too, but check when building.
- The Tasks models need a model file. Download it on first run and cache it.

## Tracking data format (Frame)

One `Frame` per camera frame, stored in datasets as one JSON object per line (JSONL).

```json
{
  "t": 12.3456,
  "frame": 1042,
  "image": {"w": 640, "h": 480},
  "parts": {
    "right_index_tip": {"x": 0.52, "y": 0.31, "z": -0.04, "state": "visible"},
    "nose":            {"x": 0.50, "y": 0.42, "z": 0.00,  "state": "visible"},
    "top_of_head":     {"x": 0.50, "y": 0.18, "z": 0.00,  "state": "estimated"},
    "left_ear":        {"state": "not_visible"}
  },
  "derived": {
    "right_palm_facing": "camera",
    "scale": {"right_hand": 0.11, "head": 0.16}
  }
}
```

**Coordinates:** `x` and `y` are fractions of the image (0 to 1, x to the right, y down). `z` is optional relative depth and is only comparable within the same source model.

**States:**
- `visible`: detected, above the confidence threshold.
- `estimated`: computed from other parts (for example top of head). Usable but less reliable.
- `not_visible`: out of frame, not detected, or below the confidence threshold. No coordinates are given, and a gesture function gets `None`, never a guess.

The state is part of the data on purpose. A hand covering an ear makes the ear `not_visible`, and a gesture can use that.

**Part catalog:**

| Group | Parts |
|---|---|
| Hand (each of `left_` and `right_`) | `wrist`, `palm_center`, `thumb_tip`, `index_tip`, `middle_tip`, `ring_tip`, `pinky_tip` |
| Head | `nose`, `left_eye`, `right_eye`, `left_ear`, `right_ear`, `bottom_of_head`, `top_of_head` |

`palm_center` is computed from the wrist and knuckle points.

**Derived values:**
- `*_palm_facing`: `camera`, `away`, or `unknown`.
- `scale`: palm size per hand and head size, so distances do not depend on how far you sit from the camera.

**Left and right:** always the person's own left and right. Convert the model's labels once and verify against a real camera.

**Size:** about 30 parts at 3 numbers each is a few hundred bytes per frame, so a one-second motion is roughly 10 KB, and a full set of training samples is a few MB.

## Keeping it cheap

Running all three models all the time costs roughly three times as much as the hand model alone. Three measures:

1. **Record everything, run only what is needed.** During training all models run, so the agent can see every part. Each saved gesture lists the parts it uses. At runtime the app runs only the models needed by the union of saved gestures. A hand-only set costs the same as hand tracking alone.
2. **Slower rates for slow parts.** The head moves slowly, so the face or pose model can run at about 10 times a second while the hand runs at 30. Hold the last head position between updates.
3. **Idle skipping.** With no hand or motion for about 2 seconds, run tracking on every third frame. Return to full rate as soon as something appears.

Rough cost, from general knowledge and not measured: the hand model takes about 5 to 15 ms per frame on a recent CPU, and the other two are in the same range. Measure on the target machine before tuning.

## Motion segmentation

The segmenter turns the Frame stream into discrete motions.

- **Speed:** smoothed mean speed of the tracked parts, in units of hand or head size per second. Head parts count, so head-only gestures can be segmented.
- **Which parts:** all tracked parts while training, and the parts that saved gestures need at runtime.
- **Start:** speed rises above `start_speed`. A short pre-roll (about 150 ms) keeps the start of the motion.
- **End:** speed stays below `stop_speed` for `quiet_ms`.
- **Discard:** motions shorter than `min_dur_ms` or longer than `max_dur_ms`, and any motion where the tracked parts disappear.

Starting settings, all needing tuning on a real camera:

| Setting | Start value |
|---|---|
| `start_speed` | 1.2 |
| `stop_speed` | 0.6 |
| `quiet_ms` | 150 |
| `min_dur_ms` | 250 |
| `max_dur_ms` | 2500 |
| `cooldown_ms` | 800 |

Show the live speed and segment boundaries in a debug readout so these can be tuned by eye.

## Latency in this stage

Estimated, not measured:
- Camera pipeline (exposure, USB, driver): 50 to 100 ms. This is the least certain figure, and webcams lower their frame rate in dim light.
- Waiting for the next frame: up to 33 ms at 30 fps.
- Tracking: about 10 to 40 ms per frame, depending on how many models run.

The full end-to-end estimate is in file 03.

## Platform notes

- macOS asks for camera permission, and the first request must come from the main thread.
- Windows can open the camera faster with the DirectShow backend.
- Linux needs access to the video device.

## Build order

1. Frames from the camera into a preview window.
2. Hand tracking with the Frame format.
3. Segmenter with the debug readout.
4. Head parts (pose first, then face), with states.
5. Runtime model selection from a list of needed parts.
6. Idle skipping and slower head rate.

## Open questions

- Training segments motions using all tracked parts, but runtime uses only the needed ones. Check on a real camera that both split motions the same way.
- Pose or face model for the nose and eyes: whether the extra precision of the face model is worth its cost.
- How to estimate the top of the head well enough to be useful.
- Whether the palm-facing value is reliable enough to expose to the agent.
