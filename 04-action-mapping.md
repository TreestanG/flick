# 4. Mapping a Gesture to an Action

Part 4 of 4. What happens when a gesture fires: how the user's plain-language request becomes an action, the built-in actions, Chrome control, and custom scripts.

Related files: `01-desktop-app-ui.md`, `02-webcam-tracking.md`, `03-gesture-agent-and-detection.md`.

## Goals

- The user says what should happen in a sentence, and the agent maps it to an action.
- Built-in actions are basic, direct, and programmable. No guessing at runtime.
- Anything beyond the built-in list is a script the user pastes in themselves, run in a folder the user can see and change.
- Firing an action is fast and never blocks tracking.

## Module layout

```
planner.py          # sentence -> action, validated against the catalog
actions.py          # built-in OS actions
chrome_bridge.py    # local WebSocket server the Chrome extension connects to
script_runner.py    # runs custom scripts in their working directory
chrome_extension/
  manifest.json
  background.js     # connects to the app, runs chrome.tabs / chrome.windows calls
```

## Three kinds of action

| Kind | What it is | Who picks it |
|---|---|---|
| **Built-in** | One action from a fixed catalog (below) | The agent, from the user's sentence, then the user confirms |
| **Chrome** | A built-in action that runs through the Chrome extension | Same |
| **Custom script** | A script the user pastes in | The user |

The agent only chooses from the catalog. It does not write scripts for actions. If a request does not fit the catalog, the agent says so and points the user to the custom script option.

## The planner (sentence to action)

1. The user types what should happen, for example "close the current tab".
2. `planner.py` sends the sentence and the catalog to the model (model access is in file 03) and asks for JSON with the action id and any parameters.
3. The reply is checked against the catalog. If it does not parse or names an unknown action, retry once, then ask the user to pick from a list.
4. The UI shows the resolved action in plain words, such as "Close the current tab in Chrome". The user confirms or changes it before training starts.

The model sees only the user's sentence and the catalog. It does not see any files or tool output.

## Built-in catalog

Basic actions that can be programmed directly:

| Action | Notes |
|---|---|
| Close tab, new tab, reopen closed tab | Chrome through the extension, key combo as fallback |
| Next tab, previous tab | Same |
| Close window, minimize window | Chrome window through the extension, or the front window through the OS |
| Open a URL | Chrome |
| Switch window | OS |
| Show desktop | OS |
| Mute, volume up and down, play or pause | Media keys |
| Press a hotkey | The user types a combination such as `ctrl+shift+t` |
| Open an app or file | The user picks it |

**OS-level implementations:**

| Action | Windows | macOS | Linux |
|---|---|---|---|
| Close window | Alt+F4 | Accessibility call to the front window's close button | Alt+F4 |
| Minimize | `ShowWindow(SW_MINIMIZE)` on the foreground window | Cmd+M | `xdotool` (X11) |
| Others | key combos | key combos | key combos |

Key presses use `pyautogui` with its delays turned off for speed. macOS needs Accessibility permission, and Wayland on Linux limits input injection.

## Chrome connection

- A small Chrome extension connects to a local WebSocket server run by the app (`localhost` only, with a shared token).
- The extension runs Chrome API calls directly, such as `chrome.tabs.remove`, `chrome.tabs.create` and `chrome.windows.update` with `minimized`.
- It works even when Chrome is not the focused window and does not depend on keyboard shortcuts.
- The user installs the extension once and clicks Connect Chrome in settings.
- If Chrome is not connected, Chrome actions fall back to key combos or are disabled, and the app says so.
- Only a fixed set of Chrome calls is allowed, and only from localhost with the token.

## Custom scripts

The user can paste in their own script instead of using a built-in action.

**In the UI (file 01):**
- A script box for the code, and a language choice (for example Python, or the system shell: PowerShell on Windows, a shell script on macOS and Linux).
- A **working directory** field, with a clear line under it:
  *"When this gesture runs, this script runs in `<folder>`. You can change this folder."*
- A **Choose folder** button.
- A **Run now** button to test it.
- The app shows exactly what will run and where, and asks the user to confirm before saving.

**Defaults and behavior:**
- The default working directory is a folder the app creates per gesture, such as `<config dir>/scripts/<gesture-id>/`. The script file is saved there. The user can change it to any folder, for example a project folder.
- The script runs as a separate process with the working directory set to that folder. It runs with the user's own permissions, so it can do anything the user can do.
- It runs in the background and never blocks tracking. A timeout applies (default 30 seconds, adjustable).
- If the previous run of the same script is still going, skip the new one (a setting).
- The app passes information through environment variables: `GESTURE_NAME`, `GESTURE_ID`, and a timestamp.
- Output and errors go to a log the user can open from the gesture's page. A failure also shows a short message in the main window.
- Scripts are saved as files and not passed through a shell string, so gesture names and other text cannot inject commands.

## Safety

- The agent never creates or edits scripts. Only the user does.
- A script, a hotkey, or any action outside the catalog needs explicit confirmation before it is saved.
- The confirmation always shows the working directory and the script text.
- The Chrome extension accepts only the fixed list of calls.
- A cooldown applies after any action fires (default about 0.8 s), so one motion cannot trigger a burst.

## Stored action format

In `gestures.json`:

```json
{
  "id": "close-tab",
  "name": "close tab",
  "action": {
    "kind": "builtin",
    "id": "chrome.close_tab",
    "params": {}
  }
}
```

```json
{
  "id": "my-script",
  "name": "my script",
  "action": {
    "kind": "script",
    "language": "python",
    "file": "action.py",
    "working_dir": "C:/Users/me/projects/notes",
    "timeout_s": 30,
    "skip_if_running": true
  }
}
```

## Test and failure handling

- Every gesture has a **Test action** button that runs the action without the gesture.
- Each run reports back to the UI as `action_result` with ok or an error message.
- If Chrome is not connected, say so in the message instead of failing silently.

## Build order

1. Catalog and the OS actions, each with a test button.
2. Chrome extension and bridge.
3. Custom script runner with the working directory field and log.
4. Planner (sentence to action) with the confirmation step.

## Open questions

- Default location of the per-gesture script folder.
- Which script languages to support first.
- On Windows, whether PowerShell execution policy will block scripts and how to handle it.
- Whether other connected tools beyond Chrome (for example apps with their own plugins) should become action providers later. The catalog and runner are structured so a new provider can be added.
- Whether browsers other than Chrome should be supported (Edge and Brave also take the same extension).
