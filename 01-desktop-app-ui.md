# 1. Desktop App: Clean UI (Tauri)

Part 1 of 4. The UI window, built with Tauri: screens, theme, how it talks to the engine, and how the two are packaged.

Related files: `02-webcam-tracking.md`, `03-gesture-agent-and-detection.md`, `04-action-mapping.md`.

## How the four files fit together

| File | Owns |
|---|---|
| 01 (this file) | The Tauri window, screens, themes, the WebSocket messages, finding and starting the engine, packaging |
| 02 | Webcam, MediaPipe, body-part data, motion segmentation, the preview stream |
| 03 | The agent that writes gesture functions, training, and recognition at runtime |
| 04 | What happens when a gesture fires: built-in actions, Chrome, custom scripts |

Files 02, 03 and 04 all describe the engine. This file describes the UI and the link between the two.

Suggested overall build order: 01 (empty shell and socket) then 02 (preview and tracking) then 04 (actions, tested with buttons) then 03 (agent and recognition).

## Architecture

Two separate programs:

| | Engine | UI |
|---|---|---|
| What | Headless Python process | Tauri app |
| Owns | Webcam, MediaPipe, agent, detection, actions, storage, tray icon | The window and nothing else |
| Runs | In the background, all the time | Only while the window is open |
| Built from | Python | Web frontend (HTML, CSS, JS) in the system webview, plus a small Rust shell |

```
+------------------- Engine (Python) -------------------+        +------ UI (Tauri) ------+
| camera -> tracking -> segmenter -> detector -> action |        |  Rust shell            |
| trainer, store, tray icon                             |        |  (find/start engine)   |
|                                                       |        |                        |
| WebSocket server, localhost only  <---- JSON + JPEG ----------->  web frontend         |
+-------------------------------------------------------+        +------------------------+
```

- Closing the window closes only the UI. The engine keeps tracking and firing gestures.
- The UI holds no state of its own. On every connect it gets a full snapshot from the engine.
- The engine works with zero, one or several UI windows connected.

## Principles

- Very simple and minimal. Few colors, no decoration, generous spacing.
- The camera preview and the gesture list are the whole main screen.
- Nothing blocks the tracker. The UI only reads events and sends commands.
- Every state is readable at a glance: armed or paused, hand seen or not, last result.
- The engine never waits for the UI. If no UI is connected, events are dropped.

## Themes

Two themes, switchable in settings (System, Dark, Light). The values are CSS variables in `app/src/styles.css`, to be adjusted by eye. As built, Light is the white and light blue look the app already had, and Dark is the dark brown one.

| Role | CSS variable | Dark (dark brown, black, dark gray) | Light (white, light gray, light blue) |
|---|---|---|---|
| Window background | `--bg` | `#14110F` | `#FFFFFF` |
| Surface (cards, rail, dialogs) | `--surface` | `#1E1A17` | `#FFFFFF` |
| Raised surface (blocks in cards, fields) | `--surface-raised` | `#2A2420` | `#F5F5F5` |
| Border | `--border` | `#3A322C` | `#E4E4E4` |
| Primary text | `--text` | `#EDE6DF` | `#111111` |
| Muted text | `--text-muted` | `#A39A91` | `#666666` |
| Accent (used sparingly) | `--accent` | `#C98B5A` | `#6CB8F0` |
| Armed / success | `--ok` | `#8FB573` | `#3E9B5A` |
| Error | `--error` | `#D96C5B` | `#C95A4A` |

The stylesheet has a few more variables of the same kind (hover, text on an accent fill, accent as text, warning, icon tints).

- **Dark** and **Light** set `data-theme` on the root element.
- **System** leaves it unset and follows the OS through the `prefers-color-scheme` media query, live.
- The theme choice is the one thing the window keeps itself (in the webview's local storage).
- No color is written anywhere except in these variables.
- `tests/test_server.py` checks every text pair in both themes at 4.5:1.

Check text contrast in both themes (aim for at least 4.5:1 for body text) before settling on the values.

## Type, spacing, shape

Also CSS variables, so they are set once.

| What | CSS variable | Value |
|---|---|---|
| Font | `--font` | `system-ui` |
| Sizes: title, body, small | `--text-title`, `--text-body`, `--text-small` | Three sizes only |
| Spacing unit | `--space` | 4 px. Panels padded 16 px, gaps 8 or 16 px |
| Corner radius | `--radius` | About 8 px |

- One-pixel borders. No shadows, no gradients.
- Motion: none beyond a short fade for dialogs.

## Screens

### Main window

```
+------------------------------------------------------+
|  Gesture Control                   [ Armed * ]   [ = ] |
+---------------------------+--------------------------+
|                           |  Gestures                |
|     camera preview        |  ----------------------  |
|     (mirrored)            |  close tab        Chrome |
|                           |  minimize         System |
|                           |  my script        Script |
|  hand: seen   fps: 30     |                          |
|  last: close tab  (212ms) |  [ + New command ]       |
+---------------------------+--------------------------+
```

- **As built**, the layout follows the look the app already had instead of this wireframe: an icon rail on the left, which is the only page menu (Gestures, Setup, Settings); a top bar with the app menu (Armed, Paused, Quit), a search field and the arm toggle; then a two-column grid of cards, the camera first.
- **Header:** title on the left. The arm toggle (Armed or Paused) and a settings button on the right.
- **Preview:** mirrored camera image, with optional small dots on tracked parts (setting). Below it, a single status line.
- **Gesture list:** name and action type for each gesture. Selecting one shows buttons for Test action, Re-train, Delete.
- **Status chips:** `Armed`, `Paused`, `No hand`, `Camera error`, and `Engine not running` (new, see "Finding and starting the engine").

### New command (dialog)

One short form:

1. **Name**
2. **What should happen?** (plain sentence, see file 04)
3. **Briefly describe the motion** (plain sentence, see file 03)
4. **Start** button

After Start, the dialog becomes the training view:

- A large prompt: "Perform the motion" with a count such as "Sample 3 of 6".
- A thin progress bar and a small line: "Samples asked: 12 of 40".
- A label for the phase: collecting, testing, or revising.
- **Cancel** at any time. Nothing is saved on cancel.

When a batch of samples is complete, the dialog becomes the review page:

- One short looping video per sample, so the user can see what was recorded.
- **Re-record** under each video, and **Re-record all**. Re-recording goes back to the prompt for that sample, then returns here.
- **Submit** hands the batch to the agent. Nothing reaches the agent before that.
- Re-recording replaces a sample, so it does not use up the 40-sample budget.
- Closing the window during training counts as Cancel (see file 03).

Outcomes:
- **Passed:** shows a short summary of the resolved action and the parts the gesture uses, a "View code" link, and **Save**.
- **Not passed:** "Couldn't tell this gesture apart from your other movements. Try a more distinctive motion." with **Try again** and **Close**.

### Settings

Grouped, one column:
- **Camera:** device, preview mirror, show tracked points.
- **Appearance:** System, Dark, Light.
- **Model:** Fireworks API key (sent to the engine once, stored by the engine in the OS keychain, shown masked) and model name.
- **Chrome:** connection status and a Connect button.
- **Idle videos:** record and manage the "doing nothing" clips used as negatives.
- **Start on login** (starts the engine, not the window) and **Run in background** (on by default; when off, the engine quits a few seconds after its last window closes).
- **Advanced** (collapsed): detection settings from file 02 and file 03.

All settings except Appearance live in the engine's `settings.json`. The UI reads them from the snapshot and sends changes back.

### First run

A short checklist, each item opens its setting:
1. Allow camera access.
2. Record an idle video (needed for training).
3. Add a Fireworks API key.
4. Optionally connect Chrome.

### Tray

Icon with a menu: Arm or Pause, Open window, Quit. The icon changes when paused.

**The engine owns the tray.** Reasons:
- The tray has to be there when the window is closed, and the engine is the only process that is always running.
- Arm, Pause and the paused icon are engine state, so no round trip is needed.
- A Tauri tray would mean keeping the Tauri process alive with a hidden window, which defeats the point of a UI that can be closed.

Menu behavior:
- **Arm or Pause:** changes engine state directly.
- **Open window:** starts the Tauri app. If it is already running, the app's single-instance check brings the existing window to the front.
- **Quit:** stops the engine. Connected windows show `Engine not running`.

The tray uses `pystray`, as before.

## Finding and starting the engine

The Rust shell does this, not the web frontend.

**The engine, on start:**
1. Takes a single-instance lock. A second engine exits at once.
2. Picks a free port on `127.0.0.1` and creates a random token.
3. Writes `engine.json` in the runtime folder, readable only by the current user:

```json
{"port": 51823, "token": "<random>", "pid": 12345, "version": 1}
```

4. Deletes the file on a clean exit.

The runtime folder is a per-user temporary folder (`%TEMP%\gesture_control` on Windows), not the config folder. Reason: some Python installs, such as the Microsoft Store one, redirect files written under `AppData` to a private place the UI cannot see. `ui.json` and `engine.log` live there too.

**The UI, on start:**
1. Read `engine.json`. If it exists, try to connect and say hello.
2. If the file is missing, the connection is refused, or the hello fails (stale file), start the engine.
3. Start the engine as a detached process, so it keeps running after the window closes. Wait for `engine.json` to appear (up to about 10 seconds), then connect.
4. Give the port and token to the web frontend through one Tauri command. The frontend opens the WebSocket itself.
5. If the engine cannot be started, show `Engine not running` with a Start engine button and the reason.
6. Write `ui.json` in the runtime folder with the path of the UI program, so the tray's Open window knows what to start.

**While running:**
- If the socket drops, show `Engine not running`, and look for a running engine about once a second. On reconnect, take a fresh snapshot.
- After a drop the UI only looks. It does not start the engine again by itself, so Quit in the tray stays quit. The Start engine button starts it.
- If `version` in the hello does not match what the UI expects, say so and ask the user to update. Do not guess.

## WebSocket messages

One WebSocket connection per window, `ws://127.0.0.1:<port>/ui`. The server binds to localhost only.

There are two kinds of message: JSON text for everything, and binary for preview frames.

### Text messages (JSON)

| Direction | Shape |
|---|---|
| UI to engine, command | `{"type": "command", "id": 7, "name": "set_armed", "data": {"armed": true}}` |
| Engine to UI, reply | `{"type": "reply", "id": 7, "ok": true, "data": {}}` or `{"type": "reply", "id": 7, "ok": false, "error": "short message"}` |
| Engine to UI, event | `{"type": "event", "name": "match", "data": {"gesture": "close tab", "ms": 212}}` |

- `id` is a number the UI picks. The reply carries the same `id`.
- Events have no `id` and need no reply. They go to every connected window.
- Unknown `name` values get an error reply and are otherwise ignored.

### Connecting

The first message must be `hello`, within 2 seconds, or the engine closes the socket.

```json
{"type": "command", "id": 1, "name": "hello", "data": {"token": "<from engine.json>", "version": 1}}
```

The reply's `data` is the snapshot: `armed`, `settings`, `gestures`, `chrome_status`, `idle_videos`, `cameras`, and `training` (the current training state, or null). After that, the UI stays current through events.

A wrong token closes the socket with no detail. The token is never put in the URL.

### Events (engine to UI)

Same names and contents as before.

| Event | Contents |
|---|---|
| `tracker_status` | fps, which parts are visible, current motion speed, camera ok or not. Sent about 5 times a second |
| `match` | gesture name, time taken in ms |
| `training_prompt` | phase, how many samples to perform, index, samples asked so far, budget, and the ready countdown before a sample |
| `training_progress` | round, phase |
| `training_sample` (new) | index, count, phase, and a short video of the sample just performed: frames per second and a list of JPEG images as base64 text |
| `training_review` (new) | phase, count, samples asked so far, budget. The batch is complete and waits for the user |
| `training_result` | passed or not, summary, message, parts, code |
| `action_result` | ok or not, short message, gesture name |
| `gestures_changed` | the gesture list, in the `gestures.json` format of file 04 |
| `chrome_status` | connected or not, message |
| `idle_videos` | list of clips: id, name, seconds |
| `cameras` | list of device names |
| `armed_changed` (new) | armed or not. Needed because the tray can now change it while a window is open |
| `settings_changed` (new) | all settings, after any window changed them |
| `api_key` (new) | present or not, a masked form, and whether it comes from the environment. Never the key |

### Commands (UI to engine)

| Command | Data |
|---|---|
| `hello` (new) | token, version |
| `set_armed` | armed |
| `start_training` | name, action text, motion text, gesture id (null for a new one) |
| `cancel_training` | |
| `submit_samples` (new) | Only from the window that is training |
| `rerecord_sample` (new) | index, or null for the whole batch. Only from the window that is training |
| `save_gesture` | |
| `delete_gesture` | gesture id |
| `test_action` | gesture id |
| `connect_chrome` | |
| `settings_changed` | settings (only the changed keys) |
| `record_idle` | on |
| `delete_idle_video` | clip id |
| `set_preview` (new) | on |
| `set_api_key`, `remove_api_key` (new) | key (only for set) |
| `quit` (new) | Stops the engine. Sent by Quit in the window's app menu |

### Preview frames (binary)

- Each binary WebSocket message is one complete JPEG image and nothing else.
- The engine sends them only to windows that sent `set_preview` with `on: true`, and stops when the window sends `on: false` or disconnects.
- The UI turns the preview on when the main window is visible, and off when it is minimized or hidden.
- About 15 fps. If a window is slow, the engine keeps only the newest frame for it and drops the rest.
- The image is not mirrored. The UI mirrors it with CSS (`transform: scaleX(-1)`) when the mirror setting is on.
- Tracked-point dots are drawn onto the image by the engine when that setting is on, so the UI only has to show an image.
- The UI shows each frame by making an object URL from the bytes, setting it on one `<img>`, and releasing the previous URL.

How the frames are made is in file 02.

## Technology

| Piece | Choice |
|---|---|
| Shell | Tauri 2. A small Rust program that opens the window, finds or starts the engine, and enforces a single UI instance |
| Webview | The system one: WebView2 on Windows, WKWebView on macOS, WebKitGTK on Linux. Nothing is bundled |
| Frontend | HTML, CSS and plain JS (or TypeScript) with a small build step. Four screens do not need a framework, see open questions |
| Link to the engine | The browser's built-in `WebSocket`, opened by the frontend |
| Engine | Python, unchanged in role. Adds a WebSocket server (for example the `websockets` package) and keeps `pystray` for the tray |

- The Rust shell stays small: window, single instance, read `engine.json`, start the engine, one command to hand over port and token. No app logic.
- The webview's content security policy allows connections only to `ws://127.0.0.1:*` and loads no remote content.
- Folder and file pickers (file 04) use Tauri's dialog plugin.
- Check the current Tauri version and plugin names when building.

## Packaging

One installer that contains both programs.

| Part | How | Rough size |
|---|---|---|
| Tauri shell and frontend | Normal Tauri bundle. Uses the system webview | About 5 to 15 MB |
| Python engine | Frozen with PyInstaller (or Nuitka), shipped inside the Tauri bundle as a sidecar | About 150 to 300 MB on disk, about 80 to 150 MB inside a compressed installer |
| MediaPipe model files | Downloaded on first run and cached (file 02) | About 10 to 30 MB, depending on which models |

The sizes are estimates from general knowledge, not measured. The engine is almost all of it: MediaPipe, OpenCV and numpy are each tens of MB, and MediaPipe pulls in a few more packages.

**Sidecar notes:**
- Tauri's sidecar setting (`externalBin`) expects one executable file per platform, named with the target triple.
- PyInstaller's one-file mode fits that, but unpacks itself to a temp folder on every start, which adds a few seconds.
- One-folder mode starts faster. It ships as a bundle resource folder instead, and the Rust shell starts the executable inside it. Prefer this unless it causes trouble.
- The engine must be started detached. Check that the chosen way of starting it does not stop the engine when the Tauri app exits.
- **Start on login** registers the engine executable, not the Tauri app.
- The engine can also be run on its own (`python -m gesture_control`) during development. The UI finds it through `engine.json` either way.

**To keep the engine smaller:**
- Use the headless OpenCV build, and exclude packages MediaPipe lists but the engine never imports.
- Build in a clean environment so nothing extra is collected.
- Measure the real size early. If it is far over the estimate, find out why before adding features.

**Signing and permissions:**
- Both executables need code signing on Windows and macOS, or the OS warns about each one.
- On macOS the camera and Accessibility permissions belong to the engine, not the UI. Check how the OS names the engine in the permission prompt when it is a bundled helper.

## Build order (UI only)

1. Empty Tauri window with both themes and the theme switch.
2. A fake engine: a small script that serves the WebSocket, answers `hello`, and sends fake events and a test image. All UI work can be done against it. (As built, this exists only for the tests, in `tests/fake_backend.py`. The app has no fake mode.)
3. Gesture list and New command form, using the fake engine.
4. Preview panel fed by the fake engine, then by the real camera (file 02).
5. Training view driven by fake events, then real ones (file 03).
6. Settings and first-run checklist.
7. Finding and starting the real engine, and the `Engine not running` state.
8. Tray (in the engine) and start on login.
9. Packaging: frozen engine as a sidecar, one installer.

## Open questions

- Final color values after seeing them on screen.
- Plain JS or a small framework (for example Svelte) for the frontend.
- One-file or one-folder for the frozen engine, after measuring start time and size.
- Whether the real engine size is acceptable, and whether a smaller MediaPipe install is possible.
- Whether WebKitGTK on Linux handles 15 fps of JPEG frames smoothly. If not, lower the rate or size there.
- How macOS attributes camera permission to a bundled helper process.
- Whether **Quit** in the tray should also close open windows, or leave them showing `Engine not running`.
- Whether closing the last window should show a one-time note that the engine is still running in the tray.
- How updates work when the UI and engine versions differ for a moment.
