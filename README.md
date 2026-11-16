# Gesture Control

Desktop app that maps webcam gestures to actions. The design is in four files:
`01-desktop-app-ui.md` to `04-action-mapping.md`.

**Built so far: all four files** (the UI, as a Tauri window talking to a
headless engine; the webcam, body tracking and motion segmenter; the agent
that writes gesture functions, training, and recognition at runtime; and the
actions). A recognised gesture now really presses keys, drives Chrome or runs
a script. What file 04 still lacks is its screens: see "File 04" below.

File 02 lives in `camera.py`, `parts.py`, `tracker.py`, `segmenter.py` and
`backend.py`. The three MediaPipe model files (about 17 MB) are downloaded on
first run into the config folder. The camera card shows the live speed and the
last motion's length and outcome, for tuning the detection settings.
`--dump-frames FILE` writes every Frame as JSONL.

File 03 lives in `sandbox.py`, `dataset.py`, `detector.py`, `model_client.py`,
`trainer.py` and the `Training` class in `backend.py`. To train a gesture you
need Docker, a Claude Code token (`claude setup-token`, pasted in settings)
and an idle video with at least 45 motions in it
(settings, Idle videos: record yourself working normally for a few minutes).
The model name in settings may be empty; the default is `claude-opus-5-5`.

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

In this mode the page is served live from `app/src`: a change to the HTML,
CSS or JS shows up in the open window about a second after saving, with no
rebuild (the page reloads, which ends a training run that is in progress).
The engine reads `trainer.py` and `model_client.py` again at the start of
every training run, so changes there need no restart either; the rest of the
engine does.

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
                    being recorded, or the detector; fires each action on
                    its own thread
  actions.py        the built-in catalog, the OS actions, the hardcoded ones
  chrome_bridge.py  the local socket the Chrome extension connects to
  script_runner.py  runs a custom script in its working folder, with a log
  planner.py        sentence -> action from the catalog (model, then keywords)
  sandbox.py        AST allowlist, FrameView (NEEDS), helpers, subprocess runner
  dataset.py        positives per gesture, idle clips, the pool of negatives
  detector.py       runs every saved function on a motion; fires on exactly one
  model_client.py   ModelClient interface
  claude_code.py    Claude Code in a Docker container, as a ModelClient
  trainer.py        the agent loop: REQUEST, code, TEST, CONFIDENT
  store.py          settings.json, gestures.json, gestures/<id>.py
  keychain.py       Claude Code token in the OS keychain
  autostart.py      start on login (starts the engine)
  ui/               the old tkinter window; no longer used, see below
chrome_extension/   the Chrome extension: manifest.json, background.js
app/                the window
  src/              index.html, styles.css (both themes as CSS variables),
                    js/engine.js (the socket), js/app.js (screens), js/icons.js
  src-tauri/        the Rust shell: one window, one copy, finds or starts the
                    engine, hands its port and token to the page
```

There is no fake mode: the app always uses the camera.
`tests/fake_backend.py` is a made-up back end for the tests only.

## File 04: actions

- **Hardcoded action:** the saved gesture with id `swipe` always runs
  `os.switch_window` (Alt+Tab; Cmd+Tab on macOS), whatever `gestures.json` or
  the planner says. The table is `HARDCODED` in `actions.py`.
- **Catalog:** `CATALOG` in `actions.py`: close, new, reopen, next and
  previous tab, close and minimize the Chrome window, open a URL
  (`chrome.*`); close, minimize and switch window, show desktop, mute, volume
  up and down, play or pause, a hotkey, open an app or file (`os.*`). Keys are
  pressed with `pyautogui`, delays off. Under pytest (or with
  `GESTURE_CONTROL_DRY_RUN` set) nothing is really pressed.
- **Chrome:** load `chrome_extension/` in `chrome://extensions` (Developer
  mode, Load unpacked), then press Retry under Chrome in settings within two
  minutes: the extension is given its token over the socket and connects by
  itself from then on. The bridge is on its own port, 38917, path `/chrome`,
  with its own token (`chrome_token` in the config folder), and only takes
  `chrome-extension://` origins. While Chrome is not connected, Chrome
  actions press the matching shortcut in whatever window is in front, and the
  result says so.
- **Scripts:** an action of kind `script` runs `scripts/<gesture-id>/<file>`
  from the config folder, in `working_dir` (that same folder if empty), as an
  argument list, with `GESTURE_NAME`, `GESTURE_ID` and `GESTURE_TIME` set, a
  timeout, and skip-if-running. Output goes to `run.log` beside the script.
  Languages: `python`, `powershell`, `shell` (PowerShell on Windows, `sh`
  elsewhere). PowerShell runs with `-ExecutionPolicy Bypass` for that one
  process.
- **Planner:** when training passes, the sentence goes to the model with the
  catalog and the JSON reply is checked (one retry), then keyword rules if
  there is no model or no usable reply. The result shows as "Action" on the
  training result page. A request nothing fits is saved as `none`, and
  running it says to use a custom script.
- **Each action runs on its own thread** and reports an `action_result`, also
  printed to `engine.log`.

- **Screens:** every gesture card has an Action button, which opens the
  action editor: a built-in action from the catalog (with its key combination,
  address or path), or a custom script with its language, working folder
  (Choose folder opens the OS picker through Tauri's dialog plugin), timeout,
  skip-if-running, Run now and View log. Saving a script, a hotkey or an app
  to open first shows exactly what will run and where, and asks. The training
  result page shows the planned action with a Change button that opens the
  same editor. The engine does the checking: `action_info` and `check_action`
  are answered by `server.py`; `set_action`, `run_script` and the optional
  `action` of `save_gesture` go to the back end (see `contract.py`).
- A hardcoded gesture's action cannot be changed in the editor; it says so.
- **Complementary gestures:** in a gesture's data dialog (Re-train on its
  card) you can tick the gestures that complement it, such as swipe left for
  swipe right. When one of a pair triggers, the other cannot for the next 1.5
  seconds (the `complement_ms` detection setting), so the hand coming back does not
  fire the opposite gesture. The link goes both ways and is saved as
  `complements` on each gesture (`update_gesture`).
- **Test mode:** one Test mode button beside New command, instead of a Test
  button on each card. It watches every gesture at once (`set_testing`):
  recognition runs, armed or paused, no action fires, and whichever gesture
  triggers, its card turns green for a moment and the camera card's last line
  names it. Stop testing, training, or closing the window ends it.

Not checked: macOS and Linux, the extension in a real Chrome, and the folder
picker in the built window (the shell compiles with the plugin; the page was
checked in a browser, where the button is hidden).

## Choices made where file 03 left them open

- **Who writes the function:** Claude Code, in a
  Docker container (`claude_code.py`, `container/Dockerfile`). The engine
  starts the container when it starts and removes it when it quits; the image
  is built on the first start. It signs in with the token that
  `claude setup-token` prints, pasted in settings and kept in the keychain (or
  read from `CLAUDE_CODE_OAUTH_TOKEN`), and handed to each `docker exec`
  through the environment, never written into the image. Before each turn the
  trainer copies the training samples into `/work/samples`; Claude Code reads
  them, writes `/work/detect.py`, checks it there with the app's own sandbox
  (`/work/tools/check.py`), and the app pulls the file out and tests it
  itself. Samples of a fresh test are copied in only after the test.
  `claude-opus-5-5` unless the model name in settings starts with `claude-`.
  The trainer's loop, tests and budget are enforced by the app as before.
- **How the model acts:** each reply holds one action in plain text:
  `REQUEST k`, `STATS ...`, `REDO P1 P4`, a python code block, `TEST k`, or `CONFIDENT` (with an optional
  `NOTE:` line, shown to the user under "Note"). The app decides what is
  allowed: test samples are shown to the model only after the test, and
  `CONFIDENT` needs a clean last test plus 5 fresh positives and 15 fresh
  negatives in total.
- **What the model sees:** not the samples. The description is the
  specification: the function must check everything it names (which fingers,
  which way, what the other fingers do), with statistics used only for
  thresholds and for the tracker's labels. It writes the function from
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
- **Re-training adds:** re-training a saved gesture starts from the samples
  it was trained on and its current function; new samples are added to them.
- **The data dashboard:** Re-train first opens what the gesture was trained
  on: its motion description, notes about how it should work (the agent
  reads them as part of the specification) and its saved samples, each drawn
  from its tracked points since no video is kept. The text can be edited and
  samples removed. Saving does not rewrite the function; the changes are what
  the next training starts from. "Add data and re-train" saves and starts it.
- **Recording a sample:** once the countdown is over, the sample starts as
  soon as anything moves faster than the stop speed (not the start speed), and
  ends at low motion, so slow performances are recorded too. Functions are
  asked not to depend on how fast the gesture is made.
- **Bad samples:** the agent can reply `REDO P1 P4` when a training positive is
  plainly not the gesture (no movement, a twitch, the hand moving back). The
  app asks the user to perform those again; the new ones replace the old
  and do not use up the budget.
- **Checking samples:** each sample is kept as a short video. When a batch is
  complete the dialog shows the videos; any sample (or all) can be recorded
  again, and nothing reaches the agent until Submit. "Record one more" adds a
  sample to the batch, as long as the budget of 40 allows (motions that are
  not the gesture are not counted). Every run starts from fresh
  samples: nothing is carried over from a run that was not saved. Recording again does not
  use up the budget. The videos are only in memory and only go to the window.
- **At least ten to learn from:** the app collects the first 10
  positives itself, before the model is asked anything, so a gesture takes at least 15
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
token: a full training run with Claude Code, how well the default model does,
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
- **Claude Code token:** read from the `CLAUDE_CODE_OAUTH_TOKEN` environment variable if set
  (it wins over the keychain); otherwise pasted in settings, sent to the
  engine once, and stored by it in the keychain. Windows only get a masked form.
- **Preview:** 640x480 JPEG, up to 15 a second, with the tracked points drawn
  by the engine; made only while a window shows the camera card.
- **Messages added to file 01's list:** see the top of `contract.py`.
- **One engine at a time:** a second one exits (a locked `engine.lock` in the
  config folder). Two would fight over the camera.
- **Not checked:** macOS and Linux (the tray needing the main thread on macOS,
  WebKitGTK showing the preview), and Open window from the tray.
