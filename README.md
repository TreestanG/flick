# Gesture Control

Desktop app that maps webcam gestures to actions. The design is in four files:
`01-desktop-app-ui.md` to `04-action-mapping.md`.

**Built so far: files 01, 02 and 03** (the UI, as a Tauri window talking to a
headless engine; the webcam, body tracking and
motion segmenter; the agent that writes gesture functions, training, and
recognition at runtime). The actions (file 04) are still stand-ins: a
recognised gesture shows as a match, but nothing is pressed or closed, and
the action is guessed from keywords in the sentence instead of planned.

File 02 lives in `camera.py`, `parts.py`, `tracker.py`, `segmenter.py` and
`backend.py`. The three MediaPipe model files (about 17 MB) are downloaded on
first run into the config folder. The camera card shows the live speed and the
last motion's length and outcome, for tuning the detection settings.
`--dump-frames FILE` writes every Frame as JSONL.

File 03 lives in `sandbox.py`, `dataset.py`, `detector.py`, `model_client.py`,
`trainer.py` and the `Training` class in `backend.py`. To train a gesture you
need a Fireworks API key and an idle video with at least 45 motions in it
(settings, Idle videos: record yourself working normally for a few minutes).
The model name in settings may be empty; the default is
`accounts/fireworks/models/deepseek-v4p1-flash-1017`.

Choices made where file 02 left them open: speed is the fastest group (each
hand, head) rather than the mean of all parts, so training and runtime split
motions the same way; nose and eyes come from the face model when it runs
anyway, otherwise from pose; pose and face each run at 10 Hz on alternate
frames; `cooldown_ms` is left to the detector in file 03. Not yet checked
against a real hand: the left/right conversion and the palm-facing value.

## Run

Two programs (file 01): the **engine** (Python, headless, owns the camera and
the tray icon) and the **window** (the Tauri app in `app/`). The window finds
the engine, or starts it, and can be closed while the engine keeps running.

Once:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
cd app
npm install
```

The window needs Rust (`cargo`) and Node. Then, from `app/`:

```
npm run dev
```

That builds and opens the window, which starts the engine from `.venv` by
itself (the first build takes about ten minutes). To run the engine alone:
`.venv\Scripts\python -m gesture_control`, with `--no-tray` or
`--dump-frames FILE`. Quit it from the tray
icon, or from the window's Gesture Control menu.

Tests: `.venv\Scripts\pip install pytest`, then `.venv\Scripts\python -m pytest`.

Not built yet: the installer. File 01 (Packaging) describes it: the engine
frozen with PyInstaller and shipped inside the Tauri bundle. The window
already looks for `engine/gesture-control-engine(.exe)` in its resources
before falling back to `.venv`, and `GESTURE_CONTROL_ENGINE` can point it at
any other executable.

## Layout

```
gesture_control/    the engine
  __main__.py       starts everything; one engine at a time; writes engine.json
  server.py         local WebSocket server for the windows: commands, replies,
                    events, preview frames (file 01)
  preview.py        JPEG encoder for the preview stream
  tray.py           tray icon and menu (Arm or Pause, Open window, Quit)
  contract.py       events, commands, the Bus (two queues + latest-frame slot)
  backend.py        camera loop; sends each motion to training, the idle clip
                    being recorded, or the detector; stand-in actions
  sandbox.py        AST allowlist, FrameView (NEEDS), helpers, subprocess runner
  dataset.py        positives per gesture, idle clips, the pool of negatives
  detector.py       runs every saved function on a motion; fires on exactly one
  model_client.py   ModelClient interface + Fireworks
  trainer.py        the agent loop: REQUEST, code, TEST, CONFIDENT
  store.py          settings.json, gestures.json, gestures/<id>.py
  keychain.py       Fireworks API key in the OS keychain
  autostart.py      start on login (starts the engine)
  ui/               the old tkinter window; no longer used, see below
app/                the window
  src/              index.html, styles.css (both themes as CSS variables),
                    js/engine.js (the socket), js/app.js (screens), js/icons.js
  src-tauri/        the Rust shell: one window, one copy, finds or starts the
                    engine, hands its port and token to the page
```

File 04 plugs in by replacing `_run_action`, `_cmd_connect_chrome` and
`_guess_action` in `backend.py`. There is no fake mode: the app always uses
the camera. `tests/fake_backend.py` is a made-up back end for the tests only.

## Choices made where file 03 left them open

- **Who writes the function:** a setting. "Fireworks model" calls the hosted
  model. "Claude Code" writes each question to `%TEMP%/gesture_control/handoff/request.json`
  and waits (up to 30 minutes) for an open Claude Code session to write
  `reply.json` beside it. The loop, the tests and the budget are the same
  either way; only who answers differs. No API key is needed for Claude Code,
  and it only works while that session is open and watching the folder.
- **How the model acts:** each reply holds one action in plain text:
  `REQUEST k`, `STATS ...`, `REDO P1 P4`, a python code block, `TEST k`, or `CONFIDENT` (with an optional
  `NOTE:` line, shown to the user under "Note"). The app decides what is
  allowed: test samples are shown to the model only after the test, and
  `CONFIDENT` needs a clean last test plus 5 fresh positives and 15 fresh
  negatives in total.
- **What the model sees:** not the samples. It writes the function mostly from
  the description, with a short overview of each batch (per part: how often
  it is visible and how far it moves, positives against negatives). It can
  reply `STATS part ... [FOR P3 N7]` for a part's average position over the
  motion, its movement, and the distance between two parts. Sending every
  sample as a table made the prompt six times larger and the models spent
  minutes thinking over it; an empty reply from that is now an error.
- **NEEDS is enforced twice:** part names in the code must be in NEEDS, and at
  run time `detect` gets a view of each frame that refuses any other part (and
  the size of a hand or head it did not list a part of).
- **Idle clips store motions, not video or every frame:** only the Segments the
  segmenter produced while recording. A clip's name shows how many it holds.
- **Negatives:** each is used once per training run. When fewer than 3k are
  left the batch is smaller; when none are left before 15 fresh ones were
  tested, training stops and asks for more idle video.
- **Collecting samples:** one at a time. Before each, the dialog counts
  "1, 2" (a second each) and then says go; only a motion that starts after
  that counts, so moving the hand back in between is ignored.
- **Bad samples:** the agent can reply `REDO P1 P4` when a training positive is
  plainly not the gesture (no movement, a twitch, the hand moving back). The
  app asks the user to perform those again; the new ones replace the old, in
  the run and in the in-progress file, and do not use up the budget.
- **Checking samples:** each sample is kept as a short video. When a batch is
  complete the dialog shows the videos; any sample (or all) can be recorded
  again, and nothing reaches the agent until Submit. Recording again does not
  use up the budget. The videos are only in memory and only go to the window.
- **At least ten to learn from:** the first request always collects 10
  positives, whatever the model asks for, so a gesture takes at least 15
  (10 to learn from, 5 fresh to test).
- **Random input:** 8 inputs of pure noise; one True is forgiven, two reject
  the function. A stricter rule rejects honest "moved right" functions too
  often.
- **Extra limits on generated code:** no `while` and no classes, a function
  that takes over 50 ms on one motion is rejected, and the test process cannot
  start other processes (Windows) and is capped at 1 GB.
- **Model output:** `<think>` blocks are dropped before reading the reply.
- **Saving:** the positives you performed are kept with the gesture, so they
  become negatives for the next gesture you train.

Not yet checked, because they need a person in front of the camera or an API
key: a full training run against Fireworks, how well the default model does,
and whether 5 minutes of idle video gives enough motions. One known gap: when
a head gesture's nose or eyes come from the pose model at runtime but came
from the face model during training (file 02's choice), positions and head
size differ slightly. The memory limit is only tested on Windows.

## Working on the page in a browser

Serve `app/src` and open it with `#port=<port>&token=<token>` from
`engine.json` after the address. It talks to the running engine like the
window does.

## Choices made where file 01 left them open

- **Tauri window:** plain HTML, CSS and JS with no build step and no
  framework. The old customtkinter window (`gesture_control/ui/`,
  `tests/test_ui.py`) is still in the repository but nothing uses it.
- **Look:** carried over from the tkinter window rather than the wireframe in
  file 01. An icon rail on the left, which is the only page menu; a top bar
  with the app menu (Armed, Paused, Quit), a search field that filters
  gestures, and the Armed/Paused toggle; then a two-column grid of cards. The
  camera is the first card. Each gesture is a card with Re-train, Delete and
  Test, and a "New" badge if it was added since the window opened.
- **Themes:** System, Dark and Light, in settings. Light is the white and
  light blue look the app had; Dark is file 01's dark brown. The choice is
  kept by the window, not the engine. `tests/test_server.py` checks every
  text pair in both at 4.5:1.
- **Where the two programs meet:** `engine.json` (port, token), `ui.json` (the
  window's path, for the tray's Open window) and `engine.log` are in
  `%TEMP%\gesture_control`, not the config folder. The Microsoft Store Python
  redirects files written under `AppData`, so the window could not see them
  there. Settings, gestures and recordings are still in the config folder,
  which with that Python is really under
  `AppData\Local\Packages\PythonSoftwareFoundation...\LocalCache\Roaming`.
- **After the engine stops:** the window shows "Engine not running" and looks
  for one every second, but only starts it again when you press Start engine,
  so Quit in the tray stays quit.
- **Run in background:** on by default. When off, the engine quits about three
  seconds after its last window closes, as closing the window used to.
- **Training and windows:** one training at a time. Closing the window that
  started it, or losing its connection, cancels it.
- **API key:** read from the `FIREWORKS_API_KEY` environment variable if set
  (it wins over the keychain); otherwise pasted in settings, sent to the
  engine once, and stored by it in the keychain. Windows only get a masked form.
- **Preview:** 640x480 JPEG, up to 15 a second, with the tracked points drawn
  by the engine; made only while a window shows the camera card.
- **Messages added to file 01's list:** see the top of `contract.py`.
- **One engine at a time:** a second one exits (a locked `engine.lock` in the
  config folder). Two would fight over the camera.
- **Not checked:** macOS and Linux (the tray needing the main thread on macOS,
  WebKitGTK showing the preview), and Open window from the tray.
