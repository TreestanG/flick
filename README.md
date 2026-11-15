# Gesture Control

Desktop app that maps webcam gestures to actions. The design is in four files:
`01-desktop-app-ui.md` to `04-action-mapping.md`.

**Built so far: files 01 and 02** (the UI, and the webcam, body tracking and
motion segmenter). The agent and the actions (files 03 and 04) are still the
fake ones, so training always "passes" and no gesture is recognised yet.

File 02 lives in `camera.py`, `parts.py`, `tracker.py`, `segmenter.py` and
`backend.py`. The three MediaPipe model files (about 17 MB) are downloaded on
first run into the config folder. The camera card shows the live speed and the
last motion's length and outcome, for tuning the detection settings.
`--fake` runs the old made-up back end; `--dump-frames FILE` writes every
Frame as JSONL. Segments go to `Backend.on_segment`, where file 03 plugs in.

Choices made where file 02 left them open: speed is the fastest group (each
hand, head) rather than the mean of all parts, so training and runtime split
motions the same way; nose and eyes come from the face model when it runs
anyway, otherwise from pose; pose and face each run at 10 Hz on alternate
frames; `cooldown_ms` is left to the detector in file 03. Not yet checked
against a real hand: the left/right conversion and the palm-facing value.

## Run

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m gesture_control
```

Tests: `.venv\Scripts\pip install pytest`, then `.venv\Scripts\python -m pytest`.

## Layout

```
gesture_control/
  contract.py       events, commands, the Bus (two queues + latest-frame slot)
  fake_backend.py   stand-in for files 02 to 04
  store.py          settings.json in the config folder
  keychain.py       Fireworks API key in the OS keychain
  autostart.py      start on login
  tray.py           tray icon and menu
  ui/               theme, icons, widgets, window shell, main screen, New command
                    dialog, settings, setup checklist
```

Files 02 to 04 plug in by replacing `FakeBackend` in `__main__.py` with something
that reads `bus.commands`, writes `bus.events` and puts camera images in
`bus.preview`. The UI needs no changes for that.

## Trying the fake back end

- A test image stands in for the camera. The "hand" leaves the frame every few
  seconds, and a fake match fires about every 7 seconds while armed.
- Training always passes, unless the motion description contains the word
  `fail`, which uses up the 40-sample budget and shows the not-passed outcome.
- Chrome gestures report "Chrome is not connected" until you press Connect in settings.
- Gestures and idle clips live in memory only. Settings, the API key and start
  on login are real.

## Choices made where file 01 left them open

- **Toolkit:** customtkinter (6.0).
- **Look:** follows a reference design rather than the wireframe in file 01.
  An icon rail on the left; a top bar with two pills (the app menu: Armed,
  Paused, Quit; and the page menu), a search field that filters gestures, and
  the Armed/Paused toggle; then a two-column grid of white cards, each with an
  icon, a badge, a gray inner block and a footer with an outlined button on the
  left and a filled one on the right. The camera is the first card, with a
  small preview. Each gesture is a card with Re-train, Delete and Test always
  visible, and a "New" badge if it was added since the app started.
- **Colors:** one light theme only (file 01 asked for System, Dark and Light):
  white, `#F5F5F5` blocks, `#E4E4E4` hairlines, near-black text, and light blue
  `#6CB8F0` where the reference uses green. Text on a blue fill is dark navy and
  blue *text* is the deeper `#1B6CA8`, because white-on-light-blue and
  light-blue-on-white are unreadable. `tests/test_core.py` checks every text
  pair at 4.5:1.
- **Type:** four sizes, not three: page title, card heading, body, small.
- **API key:** read from the `FIREWORKS_API_KEY` environment variable if set
  (it wins over the keychain); otherwise pasted in settings and stored in the keychain.
- **Preview in the background:** not drawn while the window is hidden.
- **Contract additions:** events `gestures_changed`, `chrome_status`, `idle_videos`,
  `cameras`; commands `settings_changed`, `record_idle`, `delete_idle_video`;
  `training_prompt` also carries the index of the current sample. See `contract.py`.
- **Tray on macOS:** not started, because pystray and tkinter both need the main thread there.
