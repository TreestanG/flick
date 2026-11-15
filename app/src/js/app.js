// The window: icon rail (the only page menu), top bar, three pages and the dialogs. It keeps no
// state of its own beyond the theme choice: everything else comes from the
// engine's snapshot and events, and every change goes back as a command.

import { Engine } from "./engine.js";
import { icon } from "./icons.js";

const PAGES = [   // key, name, icon
  ["main", "Gestures", "grid"],
  ["first_run", "Setup", "list"],
  ["settings", "Settings", "sliders"],
];
const APP_MENU = [["armed", "Armed", "play"], ["paused", "Paused", "pause"], ["quit", "Quit", "power"]];
const ACTION_ICONS = { Chrome: ["globe", ""], System: ["monitor", "tint-purple"], Script: ["code", "tint-teal"] };
const HEAD_PARTS = new Set(["nose", "left_eye", "right_eye", "left_ear", "right_ear",
  "bottom_of_head", "top_of_head"]);
const DETECTION = {   // key -> [label, whole number]
  start_speed: ["Start speed (sizes per second)", false],
  stop_speed: ["Stop speed (sizes per second)", false],
  quiet_ms: ["Quiet time to end a motion (ms)", true],
  min_dur_ms: ["Shortest motion (ms)", true],
  max_dur_ms: ["Longest motion (ms)", true],
  cooldown_ms: ["Cooldown after an action (ms)", true],
  negatives_per_positive: ["Negatives per positive sample", true],
};
const DETECTION_DEFAULTS = {
  start_speed: 1.2, stop_speed: 0.6, quiet_ms: 150, min_dur_ms: 250, max_dur_ms: 2500,
  cooldown_ms: 800, negatives_per_positive: 3,
};
const CHECKLIST = [   // key, text, settings section, optional
  ["camera", "Allow camera access", "camera", false],
  ["idle", "Record an idle video (needed for training)", "idle", false],
  ["key", "Add a Fireworks API key", "model", false],
  ["chrome", "Connect Chrome", "chrome", true],
];
const FAILED_TEXT = "Couldn't tell this gesture apart from your other movements. " +
  "Try a more distinctive motion.";

const engine = new Engine();
const state = {
  ready: false,           // a snapshot has arrived at least once
  armed: true,
  settings: { detection: { ...DETECTION_DEFAULTS } },
  gestures: [],
  knownIds: null,
  newIds: new Set(),      // gestures added since this window opened
  chrome: { connected: false, message: "" },
  idle: [],
  cameras: [],
  apiKey: { present: false, masked: "", from_env: false, env_var: "FIREWORKS_API_KEY" },
  cameraOk: true,
  gotStatus: false,
  page: "",
  filter: "",
  pendingMatch: null,
  previewOn: false,
  recordingSince: null,
};
const ui = {};            // elements the event handlers update
let dialog = null;        // the open New command dialog, if any

// small helpers -----------------------------------------------------------

const $ = (id) => document.getElementById(id);

/** h("div", {class: "x", onclick: fn}, child, "text") */
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key in el && key !== "list") el[key] = value;
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child);
  }
  return el;
}

const button = (text, onclick, kind = "") =>
  h("button", { class: `btn ${kind}`.trim(), type: "button", text, onclick });

function toggle(checked, onchange) {
  const input = h("input", { type: "checkbox", checked, onchange: () => onchange(input.checked) });
  const box = h("span", { class: "switch" }, input, h("span"));
  box.input = input;
  return box;
}

const clock = (seconds) => {
  const s = Math.floor(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

/** Send a command; a refusal shows on the camera card's last line. */
function command(name, data) {
  return engine.send(name, data).catch((error) => setLast(error.message, true));
}

// gestures: the short type and two plain lines about the action ------------

function actionType(gesture) {
  const action = gesture.action || {};
  if (action.kind === "script") return "Script";
  return String(action.id || "").startsWith("chrome.") ? "Chrome" : "System";
}

function describeAction(gesture) {
  const action = gesture.action || {};
  const kind = actionType(gesture);
  if (kind === "Script") {
    return [`Runs ${action.file || "a script"} (${action.language || "script"})`,
      `Working folder: ${action.working_dir || "its own folder"}`];
  }
  const words = String(action.id || "action").split(".").pop().replaceAll("_", " ");
  const first = words.slice(0, 1).toUpperCase() + words.slice(1);
  return kind === "Chrome"
    ? [`${first} in Chrome`, "Runs through the Chrome extension"]
    : [first, "Runs through the operating system"];
}

// theme ---------------------------------------------------------------------

function storedTheme() {
  try { return localStorage.getItem("theme") || "system"; } catch { return "system"; }
}

function applyTheme(name) {
  if (name === "dark" || name === "light") document.documentElement.dataset.theme = name;
  else delete document.documentElement.dataset.theme;      // System: follow the OS
  try { localStorage.setItem("theme", name); } catch { /* private mode */ }
  for (const [key, el] of Object.entries(ui.themeButtons || {})) el.classList.toggle("on", key === name);
}

// menus ---------------------------------------------------------------------

let menu = null;

function closeMenu() {
  if (menu) menu.el.remove();
  menu = null;
}

function toggleMenu(anchor, items, selected, onPick) {
  const same = menu && menu.anchor === anchor;
  closeMenu();
  if (same) return;
  const box = h("div", { class: "menu", role: "menu" }, items.map(([key, text, iconName]) =>
    h("button", {
      type: "button", role: "menuitem", class: key === selected ? "current" : "",
      onclick: () => { closeMenu(); onPick(key); },
    }, icon(iconName), text)));
  const rect = anchor.getBoundingClientRect();
  box.style.left = `${rect.left}px`;
  box.style.top = `${rect.bottom + 6}px`;
  $("menus").append(box);
  menu = { el: box, anchor };
}

document.addEventListener("mousedown", (event) => {
  if (menu && !menu.el.contains(event.target) && !menu.anchor.contains(event.target)) closeMenu();
});
window.addEventListener("resize", closeMenu);
window.addEventListener("blur", closeMenu);

// dialogs -------------------------------------------------------------------

const dialogs = [];

/** A centered dialog with a short fade. Returns {body, close}. */
function openDialog(title, { wide = false, onClose = null } = {}) {
  const body = h("div");
  const box = h("div", { class: wide ? "dialog wide" : "dialog", role: "dialog", "aria-modal": "true" },
    h("h2", { text: title }), body);
  const overlay = h("div", { class: "overlay" }, box);
  const entry = {
    body,
    box,
    title: box.firstChild,
    close() {
      const at = dialogs.indexOf(entry);
      if (at < 0) return;
      dialogs.splice(at, 1);
      overlay.remove();
      if (onClose) onClose();
    },
  };
  overlay.addEventListener("mousedown", (event) => { if (event.target === overlay) entry.close(); });
  $("dialogs").append(overlay);
  dialogs.push(entry);
  return entry;
}

function confirmDialog(title, message, confirmText, onConfirm) {
  const d = openDialog(title);
  d.body.append(
    h("p", { text: message }),
    h("div", { class: "buttons" },
      button("Cancel", () => d.close()),
      button(confirmText, () => { d.close(); onConfirm(); }, "danger")));
}

function codeDialog(title, code) {
  const d = openDialog(title, { wide: true });
  d.body.append(h("pre", { text: code }),
    h("div", { class: "buttons" }, button("Close", () => d.close())));
}

// rail and top bar ------------------------------------------------------------

function buildShell() {
  const rail = $("rail");
  rail.append(icon("logo", "logo"), h("hr"));
  ui.nav = {};
  for (const [key, name, iconName] of PAGES) {
    if (key === "settings") rail.append(h("hr", { class: "mid" }));
    ui.nav[key] = h("button", { type: "button", title: name, "aria-label": name, onclick: () => show(key) },
      icon(iconName));
    rail.append(ui.nav[key]);
  }

  $("app-pill").append(icon("chevrons"), "Gesture Control");
  $("app-pill").addEventListener("click", () =>
    toggleMenu($("app-pill"), APP_MENU, state.armed ? "armed" : "paused", (key) => {
      if (key === "quit") quit();
      else setArmed(key === "armed");
    }));

  $("search-icon").replaceWith(icon("search"));
  const search = $("search");
  search.addEventListener("input", () => {
    const text = search.value.trim();
    if (text && state.page !== "main") show("main");
    setFilter(text);
  });
  search.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { search.value = ""; setFilter(""); search.blur(); }
  });

  $("arm-chip").addEventListener("click", () => setArmed(!state.armed));
  $("banner-start").addEventListener("click", () => {
    $("banner-reason").textContent = "Starting the engine…";
    engine.connect(true);
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
      event.preventDefault();
      search.focus();
      search.select();
    } else if (event.key === "Escape") {
      if (menu) closeMenu();
      else if (dialogs.length) dialogs[dialogs.length - 1].close();
    }
  });
  document.addEventListener("visibilitychange", updatePreview);
}

function show(name) {
  if (name === state.page) return;
  state.page = name;
  for (const [key] of PAGES) {
    $(`page-${key}`).hidden = key !== name;
    ui.nav[key].classList.toggle("active", key === name);
    ui.nav[key].setAttribute("aria-current", key === name ? "page" : "false");
  }
  if (name === "first_run") refreshChecklist();
  updatePreview();
}

function openSettings(section) {
  show("settings");
  const box = ui.sections[section];
  if (box) box.scrollIntoView({ block: "start" });
}

function renderConnection() {
  const chip = $("arm-chip");
  const text = chip.querySelector(".chip-text");
  $("banner").hidden = engine.connected || !engine.reason;
  $("banner-reason").textContent = engine.reason;
  chip.disabled = !engine.connected;
  chip.classList.toggle("down", !engine.connected && !!engine.reason);
  chip.classList.toggle("ok", engine.connected && state.armed);
  if (!engine.connected) text.textContent = engine.reason ? "Engine not running" : "Connecting";
  else text.textContent = state.armed ? "Armed" : "Paused";
  if (ui.armButton) {
    ui.armButton.textContent = state.armed ? "Pause  →" : "Arm  →";
    ui.armButton.disabled = !engine.connected;
  }
}

function setArmed(armed) {
  if (armed !== state.armed) command("set_armed", { armed });   // armed_changed comes back
}

async function quit() {
  try { await engine.send("quit"); } catch { /* already gone */ }
  const tauri = window.__TAURI__;
  if (tauri) tauri.window.getCurrentWindow().close();
}

// main page -------------------------------------------------------------------

function buildMain() {
  ui.cards = h("div", { class: "cards" }, buildCameraCard());
  $("page-main").append(
    h("div", { class: "page-head" },
      h("h1", { text: "Gestures" }),
      button("+  New command", () => newCommand(), "primary tall")),
    h("div", { class: "scroll" }, ui.cards));
}

function buildCameraCard() {
  ui.cameraBadge = h("span", { class: "badge", text: "Starting" });
  ui.previewImage = h("img", { alt: "Camera preview", hidden: true });
  ui.previewText = h("span", { text: "" });
  ui.handLine = h("li", { text: "Hand: –" });
  ui.fpsLine = h("li", { text: "– fps" });
  ui.speedLine = h("li", { text: "Speed: –" });      // the debug readout of file 02
  ui.motionLine = h("li", { text: "Motion: –" });
  ui.lastLine = h("li", { text: "Last: –" });
  ui.armButton = button("Pause  →", () => setArmed(!state.armed), "primary push");
  return h("article", { class: "card" },
    h("div", { class: "card-head" },
      h("span", { class: "icon-circle" }, icon("camera")),
      h("h2", { text: "Camera" }), ui.cameraBadge),
    h("div", { class: "inset camera" },
      h("div", { class: "preview" }, ui.previewImage, ui.previewText),
      h("ul", { class: "lines" }, ui.handLine, ui.fpsLine, ui.speedLine, ui.motionLine, ui.lastLine)),
    h("div", { class: "card-foot" },
      button("Camera settings", () => openSettings("camera"), "quiet"), ui.armButton));
}

function setBadge(text, kind) {
  ui.cameraBadge.textContent = text;
  ui.cameraBadge.className = `badge ${kind}`.trim();
}

function setLast(text, error = false) {
  ui.lastLine.textContent = text;
  ui.lastLine.classList.toggle("error", error);
}

function clearPreview(text) {
  if (ui.previewImage.dataset.url) URL.revokeObjectURL(ui.previewImage.dataset.url);
  delete ui.previewImage.dataset.url;
  ui.previewImage.removeAttribute("src");
  ui.previewImage.hidden = true;
  ui.previewText.textContent = text;
}

/** Ask for preview frames only while the camera card can be seen. */
function updatePreview() {
  const wanted = engine.connected && state.page === "main" && document.visibilityState === "visible";
  if (wanted === state.previewOn) return;
  state.previewOn = wanted;
  if (engine.connected) engine.send("set_preview", { on: wanted }).catch(() => {});
}

function setFilter(text) {
  text = text.toLowerCase();
  if (text === state.filter) return;
  state.filter = text;
  renderGestures();
}

function renderGestures() {
  while (ui.cards.children.length > 1) ui.cards.lastChild.remove();
  const shown = state.gestures.filter((g) => g.name.toLowerCase().includes(state.filter));
  if (!shown.length) {
    ui.cards.append(h("div", {
      class: "empty",
      text: state.gestures.length ? "No gestures match your search."
        : "No gestures yet. Add one with New command.",
    }));
    return;
  }
  for (const gesture of shown) ui.cards.append(gestureCard(gesture));
}

function gestureCard(gesture) {
  const kind = actionType(gesture);
  const [iconName, tint] = ACTION_ICONS[kind];
  const [first, second] = describeAction(gesture);
  const isNew = state.newIds.has(gesture.id);
  return h("article", { class: "card" },
    h("div", { class: "card-head" },
      h("span", { class: `icon-circle ${tint}` }, icon(iconName)),
      h("h2", { text: gesture.name, title: gesture.name }),
      h("span", { class: isNew ? "badge warn" : "badge", text: isNew ? "New" : kind })),
    h("div", { class: "inset" },
      h("ul", { class: "lines" }, h("li", { text: first }), h("li", { text: second }))),
    h("div", { class: "card-foot" },
      button("Re-train", () => newCommand(gesture), "quiet"),
      button("Delete", () => confirmDialog("Delete gesture",
        `Delete "${gesture.name}"? This cannot be undone.`, "Delete",
        () => command("delete_gesture", { gesture_id: gesture.id })), "danger"),
      button("Test  →", () => {
        state.pendingMatch = null;
        command("test_action", { gesture_id: gesture.id });
      }, "primary push")));
}

// new command dialog: a short form that becomes the training view -------------

function newCommand(gesture = null) {
  if (dialog) return;
  const values = { name: gesture ? gesture.name : "", action: "", motion: "" };
  let training = false;
  let round = 1;
  let phase = "collecting";
  let view = "form";          // form, record, review or result
  let clips = [];             // the video of each sample in the current batch
  let players = [];           // the videos now playing on the review page
  let els = {};
  const heading = gesture ? "Re-train" : "New command";
  let waitTimer = null;       // ticks while the agent works on a step
  const stopPlayers = () => {
    players.forEach((p) => p.stop());
    players = [];
    clearInterval(waitTimer);
    waitTimer = null;
  };
  const d = openDialog(heading, {
    onClose() {
      stopPlayers();
      if (training) engine.send("cancel_training").catch(() => {});   // nothing is saved on cancel
      training = false;
      dialog = null;
    },
  });
  /** Switch what the dialog shows. Only the review page is wide. */
  function swap(name, title, ...children) {
    stopPlayers();
    view = name;
    d.title.textContent = title;
    d.box.classList.toggle("review", name === "review");
    d.body.replaceChildren(...children);
  }
  const buttons = (...items) => h("div", { class: "buttons" }, items);

  function showForm() {
    const fields = {};
    const error = h("div", { class: "error" });
    const rows = [
      ["name", "Name", "close tab"],
      ["action", "What should happen?", "Close the current tab"],
      ["motion", "Briefly describe the motion", "Swipe my right index finger to the left"],
    ].flatMap(([key, title, hint]) => {
      fields[key] = h("input", {
        class: "field", id: `new-${key}`, placeholder: hint, value: values[key],
        autocomplete: "off", spellcheck: false,
        onkeydown: (event) => { if (event.key === "Enter") start(); },
      });
      return [h("label", { for: `new-${key}`, text: title }), fields[key]];
    });

    function start() {
      for (const key of Object.keys(fields)) values[key] = fields[key].value.trim();
      if (!values.name || !values.action || !values.motion) {
        error.textContent = "Fill in all three fields.";
        return;
      }
      const ownId = gesture ? gesture.id : null;
      if (state.gestures.some((g) => g.id !== ownId && g.name.toLowerCase() === values.name.toLowerCase())) {
        error.textContent = "Another gesture already has this name.";
        return;
      }
      engine.send("start_training", {
        name: values.name, action_text: values.action, motion_text: values.motion, gesture_id: ownId,
      }).then(() => { training = true; round = 1; phase = "collecting"; clips = []; els = {}; showTraining(); },
        (failure) => { error.textContent = failure.message; });
    }

    swap("form", heading, ...rows, error,
      buttons(button("Cancel", () => d.close()), button("Start", start, "primary")));
    fields.name.focus();
  }

  const phaseText = () => `${phase.slice(0, 1).toUpperCase() + phase.slice(1)}  ·  round ${round}`;

  /** The recording view: the prompt, the countdown and the progress bar. */
  function showTraining(prompt = "Perform the motion", count = "Getting ready…") {
    const asked = els.asked ? els.asked.textContent : "Samples asked: 0 of 40";
    els = {
      phase: h("div", { class: "phase", text: phaseText() }),
      prompt: h("div", { class: "prompt", text: prompt }),
      count: h("div", { class: "count", text: count }),
      fill: h("div"),
      asked: h("div", { class: "small asked", text: asked }),
    };
    swap("record", heading, els.phase, els.prompt, els.count, h("div", { class: "bar" }, els.fill),
      els.asked, buttons(button("Cancel", () => d.close())));
  }

  const setPhase = (name) => {
    phase = name;
    if (els.phase) els.phase.textContent = phaseText();
  };

  /** One sample's video, looping. The engine sends plain images, so this
   *  flips through them; nothing is decoded as a video file. */
  function player(clip) {
    if (!clip || !clip.frames.length) {
      return { el: h("div", { class: "clip-empty", text: "No video for this one" }), stop() {} };
    }
    const image = h("img", { alt: "", class: state.settings.mirror ? "mirror" : "" });
    let at = 0;
    const next = () => {
      image.src = `data:image/jpeg;base64,${clip.frames[at]}`;
      at = (at + 1) % clip.frames.length;
    };
    next();
    const timer = setInterval(next, 1000 / Math.max(5, clip.fps || 15));
    return { el: image, stop: () => clearInterval(timer) };
  }

  /** The review page: every sample of the batch as a video. Nothing goes to
   *  the agent until Submit; any sample, or all, can be recorded again. */
  function showReview({ count }) {
    const send = (name, data) => engine.send(name, data).catch((failure) => {
      note.textContent = failure.message;
    });
    const note = h("div", { class: "error" });
    const next = [];
    const cards = Array.from({ length: count }, (_, index) => {
      const p = player(clips[index]);
      next.push(p);
      return h("div", { class: "clip" }, p.el,
        h("div", { class: "clip-foot" },
          h("span", { text: `Sample ${index + 1}` }),
          button("Re-record", () => send("rerecord_sample", { index }), "small-btn")));
    });
    swap("review", "Check your samples",
      h("div", { class: "phase", text: phaseText() }),
      h("p", { text: "Watch each video. It should show the whole motion, from start to finish. " +
        "Re-record any that went wrong, then submit." }),
      h("div", { class: "clips-grid" }, cards), note,
      buttons(button("Cancel", () => d.close()),
        button("Re-record all", () => send("rerecord_sample", { index: null })),
        button(`Submit ${count} samples`, () => {
          send("submit_samples");
          clips = [];
          showTraining("One moment", "Sending your samples…");
        }, "primary")));
    players = next;
  }

  dialog = {
    close: () => d.close(),
    /** The engine went away: its training is gone with it. */
    drop() { training = false; d.close(); },
    onSample({ index, fps, frames }) {
      if (training) clips[index] = { fps, frames };
    },
    onReview(data) {
      if (!training) return;
      setPhase(data.phase);
      showReview(data);
    },
    onProgress(data) {
      if (!training) return;
      round = data.round;
      if (view !== "record") showTraining();
      setPhase(data.phase);
      if (data.phase === "revising") {
        // Each step is one question to the model; show which, what the last
        // one led to, and how long this one has taken, so a wait is not blank.
        const started = performance.now();
        const line = () => {
          const waited = clock((performance.now() - started) / 1000);
          const who = state.settings.writer === "chat" ? "Waiting for Claude Code" : "";
          els.count.textContent = data.step
            ? [`Step ${data.step}`, waited, who, data.note].filter(Boolean).join(" · ")
            : "Writing the gesture function…";
        };
        els.prompt.textContent = "Writing the gesture function";
        els.prompt.classList.remove("wait");
        clearInterval(waitTimer);
        waitTimer = setInterval(line, 1000);
        line();
      }
    },
    onPrompt({ phase, count, index, asked, budget, ready = 0 }) {
      if (!training) return;
      if (view !== "record") showTraining();      // back from the review, to record again
      clearInterval(waitTimer);
      waitTimer = null;
      setPhase(phase);
      const sample = phase === "redoing"
        ? `Redo ${Math.min(index + 1, count)} of ${count}: an earlier sample was not the gesture`
        : `Sample ${Math.min(index + 1, count)} of ${count}`;
      if (ready) {                        // the countdown: 1, 2, then go
        els.prompt.textContent = `Get ready…  ${ready}`;
        els.count.textContent = `${sample}. Keep still until it says go.`;
      } else {
        els.prompt.textContent = "Go: perform the motion";
        els.count.textContent = sample;
      }
      els.prompt.classList.toggle("wait", !!ready);
      els.fill.style.width = `${count ? (index / count) * 100 : 0}%`;
      els.asked.textContent = `Samples asked: ${asked} of ${budget}`;
    },
    onResult({ passed, summary = "", message = "", parts = [], code = "" }) {
      if (!training) return;
      if (!passed) {
        training = false;               // nothing is waiting in the engine
        swap("result", "Not passed", h("p", { text: message || FAILED_TEXT }),
          buttons(button("Close", () => d.close()), button("Try again", showForm, "primary")));
        return;
      }
      // `training` stays set: closing without Save tells the engine to drop the draft.
      const block = (title, text) => [h("label", { text: title }), h("p", { text })];
      swap("result", "Gesture ready",
        ...block("Action", summary),
        ...block("Uses", parts.map((p) => p.replaceAll("_", " ")).join(", ") || "–"),
        ...(message ? block("Note", message) : []),      // e.g. the motion differs from its description
        button("View code", () => codeDialog(`${values.name}: detect`, code), "link"),
        buttons(button("Cancel", () => d.close()),
          button("Save", () => {
            training = false;
            command("save_gesture");
            d.close();
          }, "primary")));
    },
  };
  showForm();
}

// first run -------------------------------------------------------------------

function buildFirstRun() {
  ui.marks = {};
  const items = CHECKLIST.map(([key, text, section, optional], i) => {
    ui.marks[key] = h("span", { class: "mark" });
    return h("div", { class: "item" }, ui.marks[key],
      h("span", { text: `${i + 1}. ${text}` }),
      optional && h("span", { class: "small", text: "optional" }),
      h("span", { class: "grow" }),
      button("Open", () => openSettings(section), "quiet"));
  });
  ui.finish = button("Continue  →", () => {
    saveSettings({ first_run_done: true });
    show("main");
  }, "primary tall");
  $("page-first_run").append(
    h("h1", { text: "Setup" }),
    h("div", { class: "muted", text: "A few things to do before your first gesture.", style: "margin-bottom:16px" }),
    h("div", { class: "card checklist" }, items),
    h("div", { class: "page-end" }, ui.finish));
}

function refreshChecklist() {
  const done = {
    camera: state.cameraOk && state.gotStatus, idle: state.idle.length > 0,
    key: state.apiKey.present, chrome: state.chrome.connected,
  };
  for (const [key, mark] of Object.entries(ui.marks)) mark.classList.toggle("done", done[key]);
  const required = CHECKLIST.every(([key, , , optional]) => optional || done[key]);
  ui.finish.textContent = required ? "Continue  →" : "Skip for now  →";
}

// settings --------------------------------------------------------------------

/** Send changed keys to the engine, which owns settings.json. Returns an
 *  error message, or "" when the change was taken. */
async function saveSettings(changes) {
  try {
    const reply = await engine.send("settings_changed", { settings: changes });
    state.settings = reply.settings;
    return "";
  } catch (error) {
    return error.message;
  } finally {
    syncSettings();
  }
}

function section(key, title, iconName, ...rows) {
  const box = h("section", { class: "card" },
    h("div", { class: "card-head" }, h("span", { class: "icon-circle" }, icon(iconName)), h("h2", { text: title })),
    rows);
  ui.sections[key] = box;
  return box;
}

const setting = (title, hint, ...controls) =>
  h("div", { class: "setting" },
    h("div", { class: "text" }, h("span", { text: title }), hint && h("span", { class: "small", text: hint })),
    controls);

function buildSettings() {
  ui.sections = {};
  const s = ui.settings = {};

  // camera
  s.camera = h("select", { class: "field", onchange: () => saveSettings({ camera_device: s.camera.value }) });
  s.mirror = toggle(true, (on) => saveSettings({ mirror: on }));
  s.points = toggle(true, (on) => saveSettings({ show_points: on }));

  // appearance: kept by this window, not the engine
  ui.themeButtons = {};
  const themes = h("div", { class: "segmented" }, ["system", "dark", "light"].map((name) => {
    ui.themeButtons[name] = h("button", {
      type: "button", text: name.slice(0, 1).toUpperCase() + name.slice(1), onclick: () => applyTheme(name),
    });
    return ui.themeButtons[name];
  }));

  // model
  s.keyHint = h("span", { class: "small" });
  s.keyControls = h("div", { class: "row" });
  s.keyError = h("div", { class: "note" });
  s.model = h("input", {
    class: "field", placeholder: "accounts/fireworks/models/…", style: "width:260px",
    autocomplete: "off", spellcheck: false,
    onchange: () => saveSettings({ model_name: s.model.value.trim() }),
  });

  s.writers = {};
  s.writer = h("div", { class: "segmented" }, [["fireworks", "Fireworks model"], ["chat", "Claude Code"]]
    .map(([key, text]) => {
      s.writers[key] = h("button", { type: "button", text, onclick: () => saveSettings({ writer: key }) });
      return s.writers[key];
    }));

  // chrome
  s.chromeStatus = h("span", { class: "muted", text: "Connecting…" });
  s.chromeRetry = button("Retry", () => command("connect_chrome"), "quiet");

  // idle videos
  s.recordTime = h("span", { class: "muted" });
  s.record = button("Record", toggleRecord, "quiet");
  s.clips = h("div", { class: "clips" });

  // startup
  s.startupError = h("div", { class: "note" });
  s.login = toggle(false, async (on) => { s.startupError.textContent = await saveSettings({ start_on_login: on }); });
  s.background = toggle(true, (on) => saveSettings({ run_in_background: on }));

  // advanced
  s.detection = {};
  const advancedBody = h("div", { hidden: true },
    Object.entries(DETECTION).map(([key, [title]]) => {
      s.detection[key] = h("input", { class: "field num", inputMode: "decimal", onchange: () => setDetection(key) });
      return setting(title, "", s.detection[key]);
    }),
    h("div", { class: "end-row" }, button("Reset to defaults", () => saveSettings({ detection: { ...DETECTION_DEFAULTS } }))));
  const advancedToggle = button("Show detection settings", () => {
    advancedBody.hidden = !advancedBody.hidden;
    advancedToggle.textContent = advancedBody.hidden ? "Show detection settings" : "Hide detection settings";
  }, "link");

  $("page-settings").append(
    h("div", { class: "page-head" }, h("h1", { text: "Settings" })),
    h("div", { class: "scroll settings" },
      section("camera", "Camera", "camera",
        setting("Device", "", s.camera),
        setting("Mirror the preview", "", s.mirror),
        setting("Show tracked points", "", s.points)),
      section("appearance", "Appearance", "monitor",
        setting("Theme", "System follows your computer's setting.", themes)),
      section("model", "Model", "code",
        setting("Who writes gesture functions",
          "Claude Code: the app leaves each question in a folder and waits for an open " +
          "Claude Code session to answer. No key needed.", s.writer),
        h("div", { class: "setting" },
          h("div", { class: "text" }, h("span", { text: "Fireworks API key" }), s.keyHint), s.keyControls),
        setting("Model name", "", s.model), s.keyError),
      section("chrome", "Chrome", "globe",
        setting("Connection", "Connects by itself when the engine starts.", s.chromeStatus, s.chromeRetry)),
      section("idle", "Idle videos", "hand",
        setting("Record yourself doing nothing",
          "Used as negatives in training. About 5 minutes is a good start.", s.recordTime, s.record),
        s.clips),
      section("startup", "Startup", "monitor",
        setting("Start on login", "Starts the engine in the tray, not this window.", s.login),
        setting("Run in background", "Closing the window keeps the engine running in the tray.", s.background),
        s.startupError),
      section("advanced", "Advanced", "sliders",
        h("div", { class: "link-row" }, advancedToggle), advancedBody)));
  applyTheme(storedTheme());
}

/** Put the engine's settings into the controls (except one being typed in). */
function syncSettings() {
  const s = ui.settings;
  const values = state.settings;
  s.mirror.input.checked = !!values.mirror;
  for (const [key, el] of Object.entries(s.writers)) el.classList.toggle("on", key === (values.writer || "fireworks"));
  s.points.input.checked = !!values.show_points;
  s.login.input.checked = !!values.start_on_login;
  s.background.input.checked = !!values.run_in_background;
  if (document.activeElement !== s.model) s.model.value = values.model_name || "";
  for (const [key, field] of Object.entries(s.detection)) {
    if (document.activeElement !== field) field.value = String(values.detection[key]);
  }
  ui.previewImage.classList.toggle("mirror", !!values.mirror);
  renderCameras();
}

function renderCameras() {
  const devices = state.cameras.length ? state.cameras : ["Default camera"];
  const current = state.settings.camera_device;
  ui.settings.camera.replaceChildren(...devices.map((name) => h("option", { value: name, text: name })));
  ui.settings.camera.value = devices.includes(current) ? current : devices[0];
}

function setDetection(key) {
  const field = ui.settings.detection[key];
  const current = state.settings.detection[key];
  let value = Number(field.value);
  if (DETECTION[key][1]) value = Math.trunc(value);
  if (!Number.isFinite(value) || value <= 0) value = current;
  field.value = String(value);
  if (value !== current) saveSettings({ detection: { [key]: value } });
}

function renderKey() {
  const s = ui.settings;
  const key = state.apiKey;
  s.keyHint.textContent = `Read from ${key.env_var} if set, otherwise stored in the OS keychain.`;
  const fail = (error) => { s.keyError.textContent = error.message; };
  if (key.present && key.from_env) {
    s.keyControls.replaceChildren(h("span", { class: "muted", text: `${key.masked}  from ${key.env_var}` }));
  } else if (key.present) {
    s.keyControls.replaceChildren(h("span", { class: "muted", text: key.masked }),
      button("Remove", () => { s.keyError.textContent = ""; engine.send("remove_api_key").catch(fail); }));
  } else {
    // The key goes to the engine once and is not kept here.
    const input = h("input", { class: "field", type: "password", placeholder: "Paste key", style: "width:180px", autocomplete: "off" });
    const save = () => {
      if (!input.value.trim()) return;
      s.keyError.textContent = "";
      engine.send("set_api_key", { key: input.value.trim() }).catch(fail);
      input.value = "";
    };
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") save(); });
    s.keyControls.replaceChildren(input, button("Save", save));
  }
}

function renderChrome() {
  const { connected, message } = state.chrome;
  ui.settings.chromeStatus.textContent = message || (connected ? "Connected" : "Not connected");
  ui.settings.chromeRetry.disabled = !!connected;
}

function toggleRecord() {
  const recording = state.recordingSince === null;
  state.recordingSince = recording ? performance.now() : null;
  command("record_idle", { on: recording });
  ui.settings.record.textContent = recording ? "Stop" : "Record";
  tickRecord();
}

function tickRecord() {
  if (state.recordingSince === null) {
    ui.settings.recordTime.textContent = "";
    return;
  }
  ui.settings.recordTime.textContent = `Recording  ${clock((performance.now() - state.recordingSince) / 1000)}`;
  setTimeout(tickRecord, 500);
}

function renderClips() {
  ui.settings.clips.replaceChildren(...state.idle.map((clip) =>
    h("div", { class: "row" },
      h("span", { class: "grow", text: clip.name }),
      h("span", { class: "muted", text: clock(clip.seconds) }),
      button("Delete", () => confirmDialog("Delete idle video",
        `Delete "${clip.name}"? This cannot be undone.`, "Delete",
        () => command("delete_idle_video", { clip_id: clip.id })), "danger small-btn"))));
}

// engine events -----------------------------------------------------------------

function setGestures(gestures) {
  const ids = new Set(gestures.map((g) => g.id));
  if (state.knownIds) {
    for (const id of ids) if (!state.knownIds.has(id)) state.newIds.add(id);
    for (const id of [...state.newIds]) if (!ids.has(id)) state.newIds.delete(id);
  }
  state.knownIds = ids;
  state.gestures = gestures;
  renderGestures();
}

engine.on("snapshot", (snapshot) => {
  const first = !state.ready;
  state.ready = true;
  state.armed = snapshot.armed;
  state.settings = snapshot.settings;
  state.chrome = snapshot.chrome_status || state.chrome;
  state.idle = snapshot.idle_videos || [];
  state.cameras = snapshot.cameras || [];
  state.apiKey = snapshot.api_key || state.apiKey;
  state.previewOn = false;            // a new connection starts with the preview off
  setGestures(snapshot.gestures || []);
  syncSettings();
  renderKey();
  renderChrome();
  renderClips();
  renderConnection();
  refreshChecklist();
  if (first) show(snapshot.settings.first_run_done ? "main" : "first_run");
  updatePreview();
});

engine.on("down", () => {
  state.previewOn = false;
  state.gotStatus = false;
  state.recordingSince = null;
  if (ui.settings) ui.settings.record.textContent = "Record";
  if (dialog) dialog.drop();
  if (ui.previewImage) {
    clearPreview("");
    setBadge("Engine not running", "error");
  }
  renderConnection();
  if (!state.page) show("main");
});

engine.on("frame", (blob) => {
  if (!state.cameraOk || state.page !== "main") return;
  const image = ui.previewImage;
  const old = image.dataset.url;
  image.dataset.url = URL.createObjectURL(blob);
  image.src = image.dataset.url;
  image.hidden = false;
  ui.previewText.textContent = "";
  if (old) URL.revokeObjectURL(old);
});

engine.on("tracker_status", ({ fps = 0, visible = [], speed = 0, camera_ok = true, moving, segment, loading = false }) => {
  state.cameraOk = camera_ok;
  state.gotStatus = true;
  const hand = visible.some((part) => !HEAD_PARTS.has(part));
  if (camera_ok) {
    ui.handLine.textContent = `Hand ${hand ? "seen" : "not seen"}`;
    ui.fpsLine.textContent = `${Math.round(fps)} fps`;
    if (loading) setBadge("Loading models", "");
    else if (hand) setBadge("Hand seen", "accent");
    else setBadge("No hand", "warn");
  } else {
    ui.handLine.textContent = "Camera error, retrying";
    ui.fpsLine.textContent = "– fps";
    setBadge("Camera error", "error");
    clearPreview("Camera error");
  }
  if (moving !== undefined && moving !== null) {      // the debug readout, for tuning by eye
    ui.speedLine.textContent = `Speed ${Number(speed).toFixed(2)} · ${moving ? "moving" : "still"} · ${visible.length} parts`;
    if (segment) {
      ui.motionLine.textContent = `Motion: ${segment.ms} ms, ${segment.reason} (at ${Number(segment.t).toFixed(1)} s)`;
    }
  } else {
    ui.speedLine.textContent = `Speed ${Number(speed).toFixed(2)}`;
  }
  if (state.page === "first_run") refreshChecklist();
});

engine.on("match", ({ gesture, ms }) => {
  state.pendingMatch = gesture;
  setLast(`Last: ${gesture} (${Math.round(ms)} ms)`);
});

engine.on("action_result", ({ ok, message = "", gesture = "" }) => {
  const matched = state.pendingMatch === gesture;
  state.pendingMatch = null;
  if (!ok) setLast(gesture ? `${gesture}: ${message}` : message, true);
  else if (!matched) setLast(`Last: ${gesture}, ${message || "ok"}`);
});

engine.on("training_prompt", (data) => dialog && dialog.onPrompt(data));
engine.on("training_progress", (data) => dialog && dialog.onProgress(data));
engine.on("training_sample", (data) => dialog && dialog.onSample(data));
engine.on("training_review", (data) => dialog && dialog.onReview(data));
engine.on("training_result", (data) => dialog && dialog.onResult(data));
engine.on("gestures_changed", ({ gestures }) => setGestures(gestures || []));
engine.on("armed_changed", ({ armed }) => { state.armed = armed; renderConnection(); });
engine.on("settings_changed", ({ settings }) => { state.settings = settings; syncSettings(); });
engine.on("api_key", (info) => { state.apiKey = info; renderKey(); refreshChecklist(); });
engine.on("chrome_status", (status) => { state.chrome = status; renderChrome(); refreshChecklist(); });
engine.on("idle_videos", ({ clips }) => { state.idle = clips || []; renderClips(); refreshChecklist(); });
engine.on("cameras", ({ devices }) => { state.cameras = devices || []; renderCameras(); });

// start -------------------------------------------------------------------------

buildShell();
buildMain();
buildFirstRun();
buildSettings();
renderConnection();
engine.connect(true);
