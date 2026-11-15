# 3. Gesture Agent and Detection

Part 3 of 4. The agent that writes a Python function per gesture, how it is trained and tested, and how those functions decide at runtime that a gesture has occurred.

Related files: `01-desktop-app-ui.md`, `02-webcam-tracking.md`, `04-action-mapping.md`.

Everything in this file runs in the engine: a headless Python process that keeps running with no UI window open. The UI is a separate Tauri app (file 01) that connects over a local WebSocket.

## Goals

- Train a new gesture by describing the motion and performing it when asked. No code written by hand.
- The agent decides how much data it needs and tests itself on fresh samples it has never seen.
- Runtime is fully local. The agent is only used while setting up a gesture.
- Recognition needs no UI. Training needs a UI window, because the user has to see the prompts.

## Module layout

```
dataset.py       # store positives and negatives, idle videos, fresh-sample requests
trainer.py       # agent loop: request samples, write function, test, decide
model_client.py  # ModelClient interface + Fireworks backend, key from the OS keychain
sandbox.py       # AST allowlist check + subprocess runner with timeout
detector.py      # runtime: load saved functions, run each on every Segment
store.py         # gestures and settings on disk
```

## Gesture function contract

```python
# stored as <config dir>/gestures/<id>.py
NEEDS = ["right_index_tip", "nose"]   # parts this gesture uses; decides which trackers run

def detect(frames) -> bool:
    """frames: list of Frame (see file 02).
    Return True only if this motion is the gesture."""
```

- Only `numpy` and `math` are allowed. No file, network, or process access.
- The agent is given helpers: get a part's position as an array, check a part's state, distance between two parts in hand-size or head-size units, and the fraction of frames a part was visible.
- During training the agent sees every part. It then lists the ones it used in `NEEDS`, and the app rejects the function if it reads a part not listed there.
- Parts with state `not_visible` come back as `None`. The function must handle that.

## What the user provides

In the New command dialog (file 01):
- **Name.**
- **"What should happen?"** goes to the action planner (file 04).
- **"Briefly describe the motion."** goes to the agent here, for example "swipe my right index finger across my forehead".

The agent does not read the samples. It writes the function mostly from the motion description. After each batch it gets a short overview (for each part: how often it is visible and how far it moves, positives against negatives), and it can ask for statistics about a few parts at a time: the average position over the motion, net movement, travel, and the distance between two parts, for all samples or for named ones. The samples themselves are what the app tests each candidate on. This keeps each model call small and fast.

The motion description is a hint, not ground truth. People describe motions loosely. The agent uses it to pick parts and a starting idea, and the tests on fresh samples decide whether the function is right. If the description and the data disagree, the agent says so before finishing.

## Training loop

The agent chooses how many samples to ask for. Each request is "k positives plus 3k negatives".

1. **Start:** the agent receives the motion description and the data format, and sees how many samples have been asked and how many remain.
2. **First batch:** the agent picks k and requests k positive samples. The user performs the motion that many times.
3. **Negatives for that batch (3k):** drawn from two sources: the user's other saved gestures, and videos of the user doing nothing (recorded once during setup and run through the same tracker and segmenter). If the pool is too small, the app asks the user to record more idle video. Fresh test negatives come from the same two sources and are held back from training.
4. **Write:** the agent writes `detect` from everything collected so far and the app runs it in the sandbox. Failures on training data are fed back until it passes.
5. **Test:** the agent chooses a new k and calls test. The app collects k fresh positives and 3k fresh negatives the agent has never seen, and runs the function on them.
6. **Decide:**
   - If everything is correct, the agent may declare it is confident.
   - If anything is wrong, the test samples become training data, the agent revises, and it tests again with another fresh batch.
7. **Finish:** the app accepts the declaration only if the last test round had zero errors and the total fresh test data meets a minimum (for example at least 5 positives and 15 negatives across rounds). The user reviews the function and saves.

**Budget:**
- The hard cap is 40 positive samples asked of the user.
- Every time the agent plans a request it is told how many samples have been asked and how many remain, so it can plan its rounds around the budget.
- k is between 3 and 10 per request.
- If the budget runs out before the function passes a fresh test, the agent stops and tells the user it was unable to tell this gesture apart from the negatives, and suggests a more distinctive motion. Nothing is saved.
- Each round also has a cap on model calls (for example 8), separate from the sample budget.

**Rules:**
- Every test uses fresh samples, so the agent cannot memorize them. Samples are only shown to the agent after they have been used to test it.
- "Confident" is the agent's claim. The app's check on fresh data decides.
- Reject any function that returns true on empty, constant, or random input.
- More negatives matter more than more positives. The 3k ratio is a starting default and can be changed in settings.

**Reviewing samples before they are used:**
- Every sample the user performs is also kept as a short video (320x240, about 15 frames a second), cut from just before the motion to just after it.
- When a batch is complete (training or test), the user sees the videos, can record any sample again, and presses Submit. Only then does the batch go to the agent or the test.
- Recording a sample again replaces it. It does not count toward the 40-sample budget, and the loop above is unchanged.
- The videos live in the engine's memory for the length of the batch and are sent only to the UI window. They are not saved to disk and never leave the machine. The agent still sees only body-part numbers.

**Bad samples:**
- The agent can name training positives that are plainly not the gesture (almost no movement, a twitch, the hand moving back). The app asks the user to perform those again.
- The new samples replace the old ones and do not count toward the 40-sample budget. At most 15 samples can be redone in one run.

**Training and the UI window:**
- The trainer runs in the engine. The window only shows `training_prompt`, `training_progress` and `training_result` events and sends `start_training`, `cancel_training` and `save_gesture` (file 01).
- One training session at a time, started by one window.
- If that window closes or its connection drops during training, the engine treats it as Cancel. Nothing is saved.
- Recognition is paused while training, as the user's motions are samples, not commands.

## Model access (Fireworks)

The agent uses open models hosted on Fireworks, through its OpenAI-compatible API.

- **Key:** the user pastes their own Fireworks API key in settings. The UI sends it to the engine once over the local socket and does not keep it. The engine stores it in the OS keychain (`keyring`), never in config files, and only ever sends back a masked form.
- **Claude Code instead of a hosted model:** a setting. The same questions go to a folder on disk and an open Claude Code session answers them, one action per reply. It needs no key and works only while that session is open.
- **Client:** one `ModelClient` interface with a single `complete(messages) -> text` method. Fireworks is the only backend for now. A small interface means another provider can be added later without touching the trainer.
- **Why not sign in with a Claude plan:** Anthropic's terms do not allow third-party apps to sign users in with Claude.ai plans or route requests through plan credentials, so it is not part of this design.
- **Function writing:** ask for plain text with one Python code block and extract it. This works on any model and does not depend on native tool calling.
- **Model choice:** a setting. Before settling on a default, run a few models on the same gestures and keep the one that passes the most.
- **Privacy:** only the body-part numbers and the user's sentences are sent. No video or images leave the machine. The user's Fireworks account terms apply to that data.
- **Check when building:** the base URL (OpenAI-compatible, `https://api.fireworks.ai/inference/v1` at the time of writing) and the current model names.

## Safety for generated functions

- Generated gesture functions are model-written code that runs on the user's machine.
- At training time, run them in a subprocess with a timeout and a memory limit, after an AST check for allowed imports and no dunder access or `exec`/`eval`.
- Python cannot be fully sandboxed in-process, so at runtime the same AST check applies and the user can read each saved function before enabling it.

## Runtime recognition

1. The segmenter (file 02) ends a motion and produces a Segment.
2. Run every saved `detect` on it.
3. Fire the action (file 04) only if exactly one function returns true. If several do, do nothing and log it.
4. Cooldown of about 0.8 s after firing.

All of this happens inside the engine, with or without a UI window. A `match` event is sent to any connected window, and dropped if there is none.

## Latency estimate

Measured from the moment the motion ends to the action running. These are estimates from general knowledge, not measurements. The camera figure is the least certain.

| Stage | Typical time |
|---|---|
| Camera pipeline | 50 to 100 ms |
| Waiting for the next frame (30 fps) | up to 33 ms |
| Tracking (hand, plus face or pose if needed) | about 10 to 40 ms per frame |
| Motion-end confirmation (`quiet_ms`) | 150 ms by default |
| Gesture functions, one pass over all saved gestures | about 1 to 5 ms per function |
| Action | key press a few ms; Chrome through the extension about 5 to 25 ms |
| **Total** | **about 250 to 350 ms** |

- It should feel quick, but not instant. Under about 100 ms usually feels instant.
- The 150 ms confirmation window is the biggest piece and is a setting. Lowering it to about 100 ms saves 50 ms but risks cutting off slow gestures.
- **Early fire** is the option for a big improvement: run the functions on a rolling window during the motion and fire as soon as one matches. That could bring the total near 100 to 150 ms. The cost is more false triggers and a harder job for the agent, since each function must work on unfinished motions. Add it only if the base version feels slow.

## Storage

`<config dir>/gesture_control/` holds `settings.json`, `gestures.json` (name, descriptions, resolved action, metadata), `gestures/<id>.py` (the functions), and `data/` (recorded positives, negatives and idle videos).

Only the engine reads and writes these. The UI gets what it needs over the socket. `engine.json` (port and token) is not here: it is in the runtime folder, see file 01.

## Build order

1. Dataset recording, including idle video.
2. Sandbox and test harness, using hand-written `detect` functions first.
3. Detector and runtime recognition with those hand-written functions.
4. Model client and trainer: adaptive requests, test rounds, finish rules.
5. Training view in the UI (file 01).

## Open questions

- Which Fireworks model to use as the default.
- How much idle video is enough. Start with about 5 minutes of everyday use.
- Whether to add an arming pose (for example, hold an open palm) to prevent accidental triggers.
- Whether a dropped connection during training should cancel at once, or wait a few seconds for the window to reconnect.
- Whether to keep a distance-based matcher (dynamic time warping) as a fallback or cross-check when the agent cannot find a function that passes.
