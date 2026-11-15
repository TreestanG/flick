# 1. Desktop App: Clean UI

Part 1 of 4. The baseline desktop app: window, screens, theme and the contract with the rest of the app.

Related files: `02-webcam-tracking.md`, `03-gesture-agent-and-detection.md`, `04-action-mapping.md`.

## How the four files fit together

| File | Owns |
|---|---|
| 01 (this file) | The window, screens, themes, tray, and what the UI sends and receives |
| 02 | Webcam, MediaPipe, body-part data, motion segmentation |
| 03 | The agent that writes gesture functions, training, and recognition at runtime |
| 04 | What happens when a gesture fires: built-in actions, Chrome, custom scripts |

Suggested overall build order: 01 (empty shell) then 02 (preview and tracking) then 04 (actions, tested with buttons) then 03 (agent and recognition).

## Principles

- Very simple and minimal. Few colors, no decoration, generous spacing.
- The camera preview and the gesture list are the whole main screen.
- Nothing blocks the tracker. The UI only reads events and sends commands.
- Every state is readable at a glance: armed or paused, hand seen or not, last result.

## Themes

Two themes, switchable in settings (System, Dark, Light). The values below are proposed starting points, to be adjusted by eye.

**Dark: dark brown tones, black, dark gray**

| Role | Value |
|---|---|
| Window background | `#14110F` (near black, warm) |
| Surface (panels, list) | `#1E1A17` (dark brown) |
| Raised surface (dialogs, hover) | `#2A2420` |
| Border | `#3A322C` |
| Primary text | `#EDE6DF` |
| Muted text | `#A39A91` |
| Accent | `#C98B5A` (warm copper, used sparingly) |
| Armed / success | `#8FB573` |
| Error | `#D96C5B` |

**Light: light blue and light gray**

| Role | Value |
|---|---|
| Window background | `#F2F4F6` (light gray) |
| Surface (panels, list) | `#E7EEF5` (light blue-gray) |
| Raised surface (dialogs, hover) | `#FFFFFF` |
| Border | `#D3DCE5` |
| Primary text | `#1B2430` |
| Muted text | `#5E6B7A` |
| Accent | `#6FA8DC` (light blue) |
| Armed / success | `#5E9E6E` |
| Error | `#C95A4A` |

Check text contrast in both themes (aim for at least 4.5:1 for body text) before settling on the values.

## Type, spacing, shape

- System font. Three sizes only: title, body, small.
- Spacing on a 4 px grid. Panels padded 16 px, gaps 8 or 16 px.
- Corner radius about 8 px. One-pixel borders. No shadows, no gradients.
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

- **Header:** title on the left. The arm toggle (Armed or Paused) and a settings button on the right.
- **Preview:** mirrored camera image, with optional small dots on tracked parts (setting). Below it, a single status line.
- **Gesture list:** name and action type for each gesture. Selecting one shows buttons for Test action, Re-train, Delete.
- **Status chips:** `Armed`, `Paused`, `No hand`, `Camera error`.

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

Outcomes:
- **Passed:** shows a short summary of the resolved action and the parts the gesture uses, a "View code" link, and **Save**.
- **Not passed:** "Couldn't tell this gesture apart from your other movements. Try a more distinctive motion." with **Try again** and **Close**.

### Settings

Grouped, one column:
- **Camera:** device, preview mirror, show tracked points.
- **Appearance:** System, Dark, Light.
- **Model:** Fireworks API key (stored in the OS keychain, shown masked) and model name.
- **Chrome:** connection status and a Connect button.
- **Idle videos:** record and manage the "doing nothing" clips used as negatives.
- **Start on login** and **Run in background**.
- **Advanced** (collapsed): detection settings from file 02 and file 03.

### First run

A short checklist, each item opens its setting:
1. Allow camera access.
2. Record an idle video (needed for training).
3. Add a Fireworks API key.
4. Optionally connect Chrome.

### Tray

Icon with a menu: Arm or Pause, Open window, Quit. The icon changes when paused.

## UI and back-end contract

The UI runs on the main thread and never calls the tracker directly.

**Events the UI receives** (over a queue):

| Event | Contents |
|---|---|
| `tracker_status` | fps, which parts are visible, current motion speed |
| `match` | gesture name, time taken in ms |
| `training_prompt` | phase, how many samples to perform, samples asked so far, budget |
| `training_progress` | round, phase |
| `training_result` | passed or not, summary, message |
| `action_result` | ok or not, short message |

**Commands the UI sends:** `set_armed`, `start_training`, `cancel_training`, `save_gesture`, `delete_gesture`, `test_action`, `connect_chrome`.

## Technology

- Python. A light, themeable toolkit built on tkinter (for example `customtkinter`) fits the minimal look and keeps memory low. `PySide6` (Qt) would look crisper but is heavier. Check the current state of either when building.
- The preview is a small image updated at about 15 fps, which is plenty for a status preview and keeps CPU use low.
- Tray icon through `pystray`.

## Build order (UI only)

1. Empty window with both themes and the theme switch.
2. Gesture list and New command form, using fake data.
3. Preview panel fed by a test image, then by the real camera (file 02).
4. Training view driven by fake events, then real ones (file 03).
5. Settings and first-run checklist.
6. Tray and start on login.

## Open questions

- Final color values after seeing them on screen.
- Toolkit choice: `customtkinter` or `PySide6`.
- Whether the live preview should be hidden by default when running in the background to save CPU.
