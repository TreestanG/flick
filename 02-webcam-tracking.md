# 2. Webcam and Body Tracking (MediaPipe)

Part 2 of 4. Turns the webcam into a stream of body-part positions with visibility states, and splits that stream into motions.

Related files: `01-desktop-app-ui.md`, `03-gesture-agent-and-detection.md`, `04-action-mapping.md`.

Everything in this file runs in the engine: a headless Python process that keeps running with no UI window open. The UI is a separate Tauri app (file 01) that connects over a local WebSocket.

## Goals

- Always-on webcam with low CPU use, whether or not a UI window is open.
- Track fingertips, the palm and back of the hand, ears, eyes, nose, bottom of the head and top of the head.
- Each part has a state, including "not visible".
- Run only the models that saved gestures actually need.

## Pipeline

```
Camera thread -> MediaPipe models (only those needed) -> Frame -> Segmenter -> Segment
                                                           |
                                                           +--> preview stream and status events,
                                                                only if a UI window is connected
```

- Camera and tracking run on a dedicated thread.
- The segmenter output goes to the detector (file 03) on the same thread, to keep latency low.
- Nothing in the pipeline depends on a UI. With no window connected, the preview branch does no work.

## Module layout

```
camera.py      # open the webcam from the main thread, read only the latest frame
parts.py       # part catalog, which model provides each part, derived values
tracker.py     # runs the needed MediaPipe models, outputs a Frame
segmenter.py   # speed-based start/stop state machine, outputs a Segment
preview.py     # newest-frame slot and JPEG encoder for the preview stream
server.py      # local WebSocket server the UI connects to (messages are in file 01)
```

## Camera

- Open the webcam on the engine's main thread, then hand it to the camera thread. This avoids a permission problem on macOS.
- The tray icon (file 01) also needs the main thread on macOS. Open the camera first, then start the tray loop there.
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
3. **Idle skipping (off).** With no hand or motion for about 2 seconds, tracking could run on every third frame only. It is off: every frame is tracked, because a hand that came into view already moving was picked up late and fluid gestures were missed. `IdleGate(every=3)` turns it back on.

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
| `start_speed` | 0.5 |
| `stop_speed` | 0.25 |
| `quiet_ms` | 150 |
| `min_dur_ms` | 250 |
| `max_dur_ms` | 2500 |
| `cooldown_ms` | 800 |

Show the live speed and segment boundaries in a debug readout so these can be tuned by eye. The speed travels in the `tracker_status` event, and the readout lives in the UI's Advanced settings.

## Preview stream

The UI shows a live camera image. The engine makes it only when asked.

- **On and off:** a window sends `set_preview` (file 01). The engine counts the windows that want the preview. At zero, it does no copying, drawing or encoding.
- **Source:** the tracking thread puts its newest camera frame and tracked points in a single slot. It never waits for the encoder.
- **Encoder:** a separate thread takes the newest frame from the slot about 15 times a second, so roughly every second camera frame.
- **Dots:** if "show tracked points" is on, small dots are drawn on the image before encoding.
- **Format:** JPEG, 640x480, quality about 70. Not mirrored, since the UI mirrors it.
- **Sending:** one binary WebSocket message per frame. Each window has its own one-frame slot, so a slow window drops frames instead of building a backlog.

Rough cost, estimated and not measured:

| What | Estimate |
|---|---|
| Size of one frame | 20 to 40 KB |
| Data rate at 15 fps | about 0.3 to 0.6 MB per second, on localhost only |
| Encoding time | about 1 to 3 ms per frame |

If this turns out to be too heavy, lower the size (for example 480x360) before lowering the rate.

## Latency in this stage

Estimated, not measured:
- Camera pipeline (exposure, USB, driver): 50 to 100 ms. This is the least certain figure, and webcams lower their frame rate in dim light.
- Waiting for the next frame: up to 33 ms at 30 fps.
- Tracking: about 10 to 40 ms per frame, depending on how many models run.

The full end-to-end estimate is in file 03.

## Platform notes

- The camera permission belongs to the engine process, not the UI.
- macOS asks for camera permission, and the first request must come from the main thread.
- Windows can open the camera faster with the DirectShow backend.
- Linux needs access to the video device.

## Build order

1. Frames from the camera into the preview stream, shown in the UI (file 01).
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
- Preview size and JPEG quality, after measuring CPU use with a window open.
- Whether to draw the dots in the engine (simple) or send the points and draw them in the UI (sharper, more work).
