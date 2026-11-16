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
  complement_ms: ["Quiet time for complementary gestures (ms)", true],
  negatives_per_positive: ["Negatives per positive sample", true],
};
const DETECTION_DEFAULTS = {
  start_speed: 1.2, stop_speed: 0.6, quiet_ms: 150, min_dur_ms: 250, max_dur_ms: 2500,
  cooldown_ms: 800, complement_ms: 1500, negatives_per_positive: 3,
};
const CHECKLIST = [   // key, text, settings section, optional
  ["camera", "Allow camera access", "camera", false],
  ["idle", "Record an idle video (needed for training)", "idle", false],
  ["key", "Add a Claude Code token", "model", false],
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
  apiKey: { present: false, masked: "", from_env: false, env_var: "CLAUDE_CODE_OAUTH_TOKEN" },
  cameraOk: true,
  gotStatus: false,
  page: "",
  filter: "",
  pendingMatch: null,
  testing: false,         // test mode: every gesture is watched, no action runs
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
  const params = action.params || {};
  if (!action.id || action.id === "none") return ["No action yet", "Choose one with Action"];
  if (action.id === "os.hotkey" && params.keys) return [`Press ${params.keys}`, "Runs through the operating system"];
  if (action.id === "os.open" && params.path) return [`Open ${params.path}`, "Runs through the operating system"];
  const words = String(action.id).split(".").pop().replaceAll("_", " ");
  const first = (words.slice(0, 1).toUpperCase() + words.slice(1)) + (params.url ? `: ${params.url}` : "");
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
function openDialog(title, { wide = false, onClose = null, locked = null } = {}) {
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
  // A click outside or Escape dismisses a dialog, unless it says it is locked:
  // a training run must only end through its own Cancel button.
  entry.dismiss = () => { if (!locked || !locked()) entry.close(); };
  overlay.addEventListener("mousedown", (event) => { if (event.target === overlay) entry.dismiss(); });
  $("dialogs").append(overlay);
  dialogs.push(entry);
  return entry;
}

function confirmDialog(title, message, confirmText, onConfirm, keepText = "Cancel") {
  const d = openDialog(title);
  d.body.append(
    h("p", { text: message }),
    h("div", { class: "buttons" },
      button(keepText, () => d.close()),
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
      else if (dialogs.length) dialogs[dialogs.length - 1].dismiss();
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
  if (ui.testButton) ui.testButton.disabled = !engine.connected;
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
      h("div", { class: "page-actions" },
        ui.testButton = button("Test mode", () => setTesting(!state.testing), "tall"),
        button("+  New command", () => newCommand(), "primary tall"))),
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

/** Ask for preview frames only while a camera picture can be seen: the camera
 *  card, or the recording view of a training run. That one always has the
 *  tracked parts drawn on it, whatever the setting says. */
function updatePreview() {
  const recording = !!(dialog && dialog.camera());
  const wanted = engine.connected && (state.page === "main" || recording) && document.visibilityState === "visible";
  const points = wanted && recording;
  if (wanted === state.previewOn && points === state.previewPoints) return;
  state.previewOn = wanted;
  state.previewPoints = points;
  if (engine.connected) engine.send("set_preview", { on: wanted, points }).catch(() => {});
}

/** Put one preview frame in an image, and let go of the one before it. */
function showFrame(image, blob) {
  const old = image.dataset.url;
  image.dataset.url = URL.createObjectURL(blob);
  image.src = image.dataset.url;
  image.hidden = false;
  if (old) URL.revokeObjectURL(old);
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

/** Turn test mode on or off. While it is on the engine keeps recognising
 *  every gesture but fires no action, so the window stays in front; whichever
 *  gesture triggers, its card goes green for a moment. */
function setTesting(on) {
  if (on === state.testing) return;
  state.testing = on;
  command("set_testing", { on });
  renderTesting();
}

/** The Test mode button and the cards follow state.testing. */
function renderTesting() {
  if (ui.testButton) {
    ui.testButton.textContent = state.testing ? "Stop testing" : "Test mode";
    ui.testButton.classList.toggle("active", state.testing);
    ui.testButton.disabled = !engine.connected;
  }
  renderGestures();
}

/** A gesture triggered in test mode: its card is green for a moment. */
function flashCard(id) {
  const card = [...ui.cards.children].find((el) => el.dataset.id === id);
  if (!card) return;
  for (const other of ui.cards.querySelectorAll(".card.hit")) endFlash(other);
  card.classList.add("hit");
  card.querySelector(".badge").textContent = "Triggered";
  clearTimeout(ui.hitTimer);
  ui.hitTimer = setTimeout(() => endFlash(card), 1200);
}

function endFlash(card) {
  card.classList.remove("hit");
  if (state.testing) card.querySelector(".badge").textContent = "Watching";
}

function gestureCard(gesture) {
  const kind = actionType(gesture);
  const [iconName, tint] = ACTION_ICONS[kind];
  const [first, second] = describeAction(gesture);
  const isNew = state.newIds.has(gesture.id);
  const testing = state.testing;
  const badge = testing ? h("span", { class: "badge accent", text: "Watching" })
    : h("span", { class: isNew ? "badge warn" : "badge", text: isNew ? "New" : kind });
  return h("article", { class: testing ? "card testing" : "card", "data-id": gesture.id },
    h("div", { class: "card-head" },
      h("span", { class: `icon-circle ${tint}` }, icon(iconName)),
      h("h2", { text: gesture.name, title: gesture.name }),
      badge),
    h("div", { class: "inset" },
      h("ul", { class: "lines" }, h("li", { text: first }), h("li", { text: second }))),
    h("div", { class: "card-foot" },
      button("Re-train", () => dataDashboard(gesture), "quiet"),
      button("Action", () => actionDialog({
        gestureId: gesture.id, name: gesture.name, action: gesture.action,
        onSave: (action) => command("set_action", { gesture_id: gesture.id, action }),
      }), "quiet"),
      button("Delete", () => confirmDialog("Delete gesture",
        `Delete "${gesture.name}"? This cannot be undone.`, "Delete",
        () => command("delete_gesture", { gesture_id: gesture.id })), "danger")));
}

// the action editor (file 04) ---------------------------------------------------
// One dialog for a gesture's action: a built-in one from the engine's catalog,
// or the user's own script with its working folder. The engine checks every
// action (check_action) and says in plain words what it will do; a script, a
// hotkey or an app to open is shown once more for the user to confirm.

/** The OS folder picker, through Tauri. Null in a plain browser or on Cancel. */
async function pickFolder(current) {
  const tauri = window.__TAURI__;
  if (!tauri) return null;
  const picked = await tauri.core.invoke("plugin:dialog|open", {
    options: { directory: true, multiple: false, title: "Working folder", defaultPath: current || undefined },
  });
  return typeof picked === "string" ? picked : null;
}

/** Show what a checked action will do and ask before it is saved. */
function confirmAction(reply, onConfirm) {
  const script = reply.action.kind === "script";
  const d = openDialog(script ? "Confirm script" : "Confirm action", { wide: script });
  d.body.append(...(script
    ? [h("label", { text: "When this gesture runs, this runs" }), h("pre", { text: reply.command }),
      h("label", { text: "In this folder" }), h("p", { text: reply.working_dir }),
      h("label", { text: "Script" }), h("pre", { text: reply.action.code }),
      h("p", { class: "small", text: "It runs with your own permissions, so it can do anything you can do." })]
    : [h("p", { text: `${reply.summary}.` }),
      h("p", { class: "small", text: "This happens every time the gesture is recognised." })]),
  h("div", { class: "buttons" },
    button("Back", () => d.close()),
    button("Save", () => { d.close(); onConfirm(); }, "primary")));
}

/** Edit an action. `gestureId` is null for a gesture still in training.
 *  `onSave(action, summary)` gets the checked action once the user agreed. */
async function actionDialog({ gestureId = null, name, action = {}, onSave }) {
  let info;
  try {
    info = await engine.send("action_info", { gesture_id: gestureId });
  } catch (error) {
    setLast(error.message, true);
    return;
  }
  const d = openDialog(`${name}: action`, { wide: true, onClose() { ui.runNow = null; } });
  const error = h("div", { class: "error" });
  let kind = action.kind === "script" ? "script" : "builtin";

  // built-in
  const entries = info.catalog;
  const pick = h("select", { class: "field", id: "action-pick" },
    entries.map((entry) => h("option", { value: entry.id, text: entry.text })));
  if (entries.some((entry) => entry.id === action.id)) pick.value = action.id;
  const paramLabel = h("label", { for: "action-param" });
  const param = h("input", { class: "field", id: "action-param", autocomplete: "off", spellcheck: false });
  const current = () => entries.find((entry) => entry.id === pick.value) || {};
  const syncParam = (keep) => {
    const entry = current();
    paramLabel.hidden = param.hidden = !entry.param;
    paramLabel.textContent = entry.hint ? entry.hint.slice(0, 1).toUpperCase() + entry.hint.slice(1) : "";
    param.value = keep && entry.param ? String((action.params || {})[entry.param] || "") : "";
  };
  pick.addEventListener("change", () => syncParam(false));
  syncParam(action.kind !== "script");
  const builtinPane = h("div", {},
    h("label", { for: "action-pick", text: "What should happen?" }), pick, paramLabel, param,
    h("p", { class: "small", text: "Chrome actions use the Chrome extension, and the matching shortcut when it is not connected." }));

  // script
  let language = action.kind === "script" && action.language !== "python" ? "shell" : "python";
  const languages = {};
  const languageRow = h("div", { class: "segmented" },
    [["python", "Python"], ["shell", info.shell_name]].map(([key, text]) => {
      languages[key] = h("button", {
        type: "button", text, class: key === language ? "on" : "",
        onclick: () => { language = key; for (const [k, el] of Object.entries(languages)) el.classList.toggle("on", k === key); },
      });
      return languages[key];
    }));
  const code = h("textarea", {
    class: "field code", id: "action-code", spellcheck: false, value: action.code || info.code,
    placeholder: "Paste your script here",
  });
  const folder = h("input", {
    class: "field grow", id: "action-folder", autocomplete: "off", spellcheck: false,
    value: action.kind === "script" ? action.working_dir || "" : "",
    placeholder: info.default_dir || "A new folder the app makes for this gesture",
  });
  const where = h("p", { class: "small" });
  const syncWhere = () => {
    where.textContent = `When this gesture runs, this script runs in ${folder.value.trim() || folder.placeholder}. You can change this folder.`;
  };
  folder.addEventListener("input", syncWhere);
  syncWhere();
  const choose = button("Choose folder", async () => {
    try {
      const picked = await pickFolder(folder.value.trim());
      if (picked) { folder.value = picked; syncWhere(); }
    } catch (failure) { error.textContent = String(failure.message || failure); }
  });
  choose.hidden = !window.__TAURI__;
  const timeout = h("input", {
    class: "field num", id: "action-timeout", inputMode: "decimal",
    value: String(action.kind === "script" ? action.timeout_s || 30 : 30),
  });
  const skip = toggle(action.kind === "script" ? action.skip_if_running !== false : true, () => {});
  const ran = h("span", { class: "small grow" });
  const runNow = button("Run now", () => {
    error.textContent = "";
    ran.textContent = "Running…";
    ran.classList.remove("error");
    runNow.disabled = true;
    ui.runNow = ({ ok, message }) => {
      runNow.disabled = false;
      ran.textContent = ok ? "Ran without errors." : message;
      ran.classList.toggle("error", !ok);
      engine.send("action_info", { gesture_id: gestureId }).then((fresh) => { info.log = fresh.log; }, () => {});
    };
    engine.send("run_script", { action: collect(), gesture_id: gestureId }).catch((failure) => {
      ui.runNow({ ok: false, message: failure.message });
    });
  });
  const viewLog = button("View log", () => codeDialog(`${name}: script log`, info.log || "Nothing has run yet."), "link");
  const scriptPane = h("div", {},
    h("label", { text: "Language" }), languageRow,
    h("label", { for: "action-code", text: "Script" }), code,
    h("label", { for: "action-folder", text: "Working folder" }),
    h("div", { class: "row" }, folder, choose), where,
    h("div", { class: "row" },
      h("label", { for: "action-timeout", text: "Stop it after (seconds)" }), timeout,
      h("span", { class: "grow" }),
      h("label", { text: "Skip if the last run is still going" }), skip),
    h("div", { class: "row" }, runNow, ran, viewLog));

  function collect() {
    if (kind === "builtin") {
      const entry = current();
      return { kind: "builtin", id: entry.id, params: entry.param ? { [entry.param]: param.value.trim() } : {} };
    }
    return {
      kind: "script", language, code: code.value, working_dir: folder.value.trim(),
      timeout_s: Number(timeout.value) || 0, skip_if_running: skip.input.checked,
    };
  }

  const kinds = {};
  const setKind = (name) => {
    kind = name;
    builtinPane.hidden = name !== "builtin";
    scriptPane.hidden = name !== "script";
    for (const [key, el] of Object.entries(kinds)) el.classList.toggle("on", key === name);
  };
  const kindRow = h("div", { class: "segmented" },
    [["builtin", "Built-in action"], ["script", "Custom script"]].map(([key, text]) => {
      kinds[key] = h("button", { type: "button", text, onclick: () => setKind(key) });
      return kinds[key];
    }));

  const save = button("Save", async () => {
    error.textContent = "";
    let reply;
    try {
      reply = await engine.send("check_action", { action: collect(), gesture_id: gestureId });
    } catch (failure) {
      error.textContent = failure.message;
      return;
    }
    const finish = () => { d.close(); onSave(reply.action, reply.summary); };
    if (reply.confirm) confirmAction(reply, finish);
    else finish();
  }, "primary");
  if (info.hardcoded) {
    save.disabled = true;
  }
  d.body.append(kindRow, builtinPane, scriptPane, error,
    h("div", { class: "buttons" }, button("Cancel", () => d.close()), save));
  setKind(kind);
}

// the data dashboard: what a saved gesture was trained on --------------------------
// Re-train opens it. The description, the notes about how the gesture should
// work and the saved samples can be edited here; adding data starts a training
// run that begins from what is left.

const FINGERS = ["thumb_tip", "index_tip", "middle_tip", "ring_tip", "pinky_tip"];

/** One saved sample, looping at its own speed. No video is kept of a saved
 *  sample, so this draws the tracked points: each hand as lines from the palm,
 *  and the path its palm has taken so far. Zoomed to where the motion is. */
function motionPlayer(sample) {
  const frames = (sample.frames || []).filter((f) => Object.keys(f.points).length);
  if (!frames.length) {
    return { el: h("div", { class: "clip-empty", text: "Nothing was tracked" }), stop() {} };
  }
  const W = 320, H = 240, PAD = 0.14, HOLD_S = 0.8;
  const canvas = h("canvas", { width: W, height: H });
  const ctx = canvas.getContext("2d");
  const fill = h("div");
  const box = h("div", { class: "clip-video" }, canvas, h("div", { class: "clip-bar" }, fill));
  let x0 = 1, x1 = 0, y0 = 1, y1 = 0;
  for (const frame of frames) {
    for (const [x, y] of Object.values(frame.points)) {
      x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
    }
  }
  const aspect = sample.aspect || 4 / 3;
  const scale = Math.min(W * (1 - 2 * PAD) / Math.max((x1 - x0) * aspect, 0.08),
    H * (1 - 2 * PAD) / Math.max(y1 - y0, 0.08));
  const flip = state.settings.mirror ? -1 : 1;
  const place = ([x, y]) => [W / 2 + flip * (x - (x0 + x1) / 2) * aspect * scale,
    H / 2 + (y - (y0 + y1) / 2) * scale];
  const line = (points, color, width) => {
    if (points.length < 2) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.stroke();
  };
  const dot = ([x, y], color, radius) => {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, 2 * Math.PI);
    ctx.fill();
  };
  const length = frames[frames.length - 1].t;
  const draw = (at) => {
    const css = getComputedStyle(document.documentElement);
    const accent = css.getPropertyValue("--accent").trim() || "#3B82F6";
    const muted = css.getPropertyValue("--text-muted").trim() || "#888";
    ctx.clearRect(0, 0, W, H);
    ctx.lineJoin = ctx.lineCap = "round";
    const frame = frames[at];
    for (const side of ["left", "right"]) {
      const trail = frames.slice(0, at + 1).map((f) => f.points[`${side}_palm_center`])
        .filter(Boolean).map(place);
      ctx.globalAlpha = 0.35;
      line(trail, accent, 2);
      ctx.globalAlpha = 1;
      const palm = frame.points[`${side}_palm_center`];
      const wrist = frame.points[`${side}_wrist`];
      if (palm && wrist) line([place(wrist), place(palm)], muted, 3);
      for (const finger of FINGERS) {
        const tip = frame.points[`${side}_${finger}`];
        if (!tip) continue;
        if (palm) line([place(palm), place(tip)], muted, 2);
        dot(place(tip), accent, finger === "index_tip" ? 5 : 3.5);
      }
      if (palm) dot(place(palm), muted, 4);
    }
    for (const [name, point] of Object.entries(frame.points)) {
      if (HEAD_PARTS.has(name)) dot(place(point), muted, 2.5);
    }
  };
  const started = performance.now();
  const next = () => {
    const elapsed = ((performance.now() - started) / 1000) % (length + HOLD_S);
    const held = elapsed >= length;
    let at = frames.length - 1;
    if (!held) at = Math.max(0, frames.findIndex((f) => f.t > elapsed) - 1);
    box.classList.toggle("held", held);
    draw(at);
    fill.style.width = `${held ? 100 : (length ? (elapsed / length) * 100 : 100)}%`;
  };
  next();
  const timer = setInterval(next, 40);
  return { el: box, stop: () => clearInterval(timer) };
}

async function dataDashboard(gesture) {
  if (dialog) return;
  setTesting(false);
  let data;
  try {
    data = await engine.send("gesture_data", { gesture_id: gesture.id });
  } catch (failure) {
    setLast(failure.message, true);
    return;
  }
  const removed = new Set();
  const players = [];
  const d = openDialog(`${gesture.name}: training data`, {
    onClose() { players.forEach((p) => p.stop()); },
  });
  d.box.classList.add("review");
  const error = h("div", { class: "error" });
  const motion = h("textarea", {
    class: "field", id: "data-motion", rows: 2, spellcheck: false, value: data.motion,
    placeholder: "Swipe my right index finger to the left",
  });
  const notes = h("textarea", {
    class: "field", id: "data-notes", rows: 3, spellcheck: false, value: data.notes,
    placeholder: "Anything the description leaves out. For example: only the index finger is " +
      "out and the others stay curled; it should not fire when I just move my hand to the mouse.",
  });
  // The gestures that complement this one: when one of a pair triggers, the
  // other cannot for the next few seconds (the hand coming back from a swipe).
  const before = new Set(gesture.complements || []);
  const paired = new Set(before);
  const others = state.gestures.filter((g) => g.id !== gesture.id);
  const pairs = h("div", { class: "checks" }, others.map((other) =>
    h("label", { class: "check" },
      h("input", {
        type: "checkbox", checked: paired.has(other.id),
        onchange: (event) => {
          if (event.target.checked) paired.add(other.id); else paired.delete(other.id);
        },
      }), other.name)));
  const summary = h("p", { class: "small" });
  const count = data.samples.length;
  const lengths = data.samples.map((s) => s.seconds).sort((a, b) => a - b);
  const typical = count ? lengths[Math.floor(count / 2)] : 0;
  const sync = () => {
    const kept = count - removed.size;
    summary.textContent = !count ? "This gesture has no saved samples."
      : `${kept} of ${count} saved samples kept  ·  typical length ${typical.toFixed(2)} s. ` +
        "Each is drawn from the tracked hand points; no video is kept. Remove any that are " +
        "not the gesture.";
  };
  const cards = data.samples.map((sample, index) => {
    const p = motionPlayer(sample);
    players.push(p);
    const card = h("div", { class: "clip" });
    const flip = button("Remove", () => {
      if (removed.has(index)) removed.delete(index); else removed.add(index);
      card.classList.toggle("removed", removed.has(index));
      flip.textContent = removed.has(index) ? "Keep" : "Remove";
      sync();
    }, "small-btn");
    card.append(p.el, h("div", { class: "clip-foot" },
      h("span", { text: `${index + 1}  ·  ${sample.seconds.toFixed(2)} s` }), flip));
    return card;
  });
  sync();

  /** What the user changed, as an update_gesture command; null if nothing. */
  const changes = () => {
    const out = { gesture_id: gesture.id };
    const text = motion.value.trim();
    if (text && text !== data.motion) out.motion = text;
    if (notes.value.trim() !== data.notes) out.notes = notes.value.trim();
    if (removed.size) { out.remove = [...removed]; out.count = count; }
    if (paired.size !== before.size || [...paired].some((id) => !before.has(id))) {
      out.complements = [...paired];
    }
    return Object.keys(out).length > 1 ? out : null;
  };
  const save = async () => {
    const changed = changes();
    if (changed) await engine.send("update_gesture", changed);
  };
  const run = (after) => async () => {
    error.textContent = "";
    try {
      await save();
    } catch (failure) {
      error.textContent = failure.message;
      return;
    }
    d.close();
    after();
  };
  // A wide dialog: what the gesture is on the left, its saved samples on the right.
  d.body.append(
    h("div", { class: "review-cols" },
      h("div", {},
        h("label", { for: "data-motion", text: "Motion description" }), motion,
        h("label", { for: "data-notes", text: "How it should work (extra notes for the function writer)" }), notes,
        h("label", { text: "Complementary gestures" }),
        h("p", { class: "small", text: others.length
          ? "When this gesture triggers, the ones ticked here cannot trigger for the next " +
            `${((state.settings.detection || {}).complement_ms || DETECTION_DEFAULTS.complement_ms) / 1000} ` +
            "seconds, and the other way round (the time is in Settings). Use it for pairs where the hand coming back " +
            "from one looks like the other, such as swipe left and swipe right."
          : "There are no other gestures yet to pair this one with." }),
        others.length ? pairs : null),
      h("div", {},
        h("label", { text: "Saved samples" }), summary,
        count ? h("div", { class: "clips-grid data-grid" }, cards) : null)),
    h("p", { class: "small", text: "Saving keeps the function the gesture uses now. The description, " +
      "notes and samples above are what the next training starts from; Add data and re-train " +
      "saves them and starts it." }),
    data.code ? button("View code", () => codeDialog(`${gesture.name}: detect`, data.code), "link") : null,
    error,
    h("div", { class: "buttons" },
      button("Cancel", () => d.close()),
      button("Save changes", run(() => {})),
      button("Add data and re-train", run(() => newCommand({
        ...gesture, motion: motion.value.trim() || gesture.motion,
      })), "primary")));
}

// new command dialog: a short form that becomes the training view -------------

const RECORDING = {
  collecting: "Record samples",
  negatives: "Record motions that are not the gesture",
  redoing: "Redo samples that were not the gesture",
  testing: "Record test samples",
};

function newCommand(gesture = null) {
  if (dialog) return;
  setTesting(false);            // training takes every motion
  const values = { name: gesture ? gesture.name : "", action: "", motion: "" };
  let training = false;
  let round = 1;
  let phase = "collecting";
  let view = "form";          // form, record, review or result
  let clips = [];             // the video of each sample in the current batch
  let players = [];           // the videos now playing on the review page
  let els = {};
  const heading = gesture ? "Re-train" : "New command";
  const stopPlayers = () => {
    players.forEach((p) => p.stop());
    players = [];
  };

  // The timeline beside the training view: everything this run has done, in
  // order, with the entry now in progress last. It stays while the view on its
  // left changes between recording, review and result.
  let log = [];
  const timeline = h("ol", { class: "timeline" });
  const askedEl = h("span", { class: "small", text: "" });
  const side = h("aside", { class: "side" },
    h("div", { class: "side-head" }, h("span", { text: "Progress" }), askedEl), timeline);
  const last = () => log[log.length - 1] || null;
  const waited = (item) => clock(((item.ended || performance.now()) - item.started) / 1000);
  /** Close the entry in progress: it keeps how long it took. */
  function finish() {
    const item = last();
    if (!item || item.ended) return;
    item.ended = performance.now();
    item.el.className = "done";
    item.time.textContent = waited(item);
    if (item.detail.textContent === WORKING) item.detail.textContent = "";
  }
  /** Add an entry and make it the one in progress. */
  function entry(kind, title, detail = "") {
    finish();
    const item = {
      kind, started: performance.now(), ended: 0,
      title: h("span", { class: "t-title", text: title }),
      time: h("span", { class: "t-time small", text: "0:00" }),
      detail: h("div", { class: "t-detail small", text: detail }),
    };
    item.el = h("li", { class: "active" }, h("span", { class: "t-dot" }),
      h("div", { class: "t-body" }, h("div", { class: "t-head" }, item.title, item.time), item.detail));
    timeline.append(item.el);
    log.push(item);
    timeline.scrollTop = timeline.scrollHeight;
    return item;
  }
  const WORKING = "Working…";
  // Once a second: the running entry's clock, and the same wait on the left
  // while the agent works on a step, so a wait is not blank.
  const tick = setInterval(() => {
    const item = last();
    if (!item || item.ended) return;
    item.time.textContent = waited(item);
    if (item.kind === "step" && view === "record" && els.count) {
      els.count.textContent = `Step ${item.step} · ${waited(item)}`;
    }
  }, 1000);
  /** A run begins: an empty timeline and the recording view. */
  function begin() {
    training = true; round = 1; phase = "collecting"; clips = []; els = {};
    log = [];
    timeline.replaceChildren();
    askedEl.textContent = "";
    entry("setup", "Getting ready");
    showTraining();
  }

  const d = openDialog(heading, {
    locked: () => training,
    onClose() {
      stopPlayers();
      clearInterval(tick);
      if (training) engine.send("cancel_training").catch(() => {});   // nothing is saved on cancel
      training = false;
      dialog = null;
      dropCamera();
      updatePreview();
    },
  });
  /** Let go of the camera picture of the recording view. */
  function dropCamera() {
    if (els.camera && els.camera.dataset.url) URL.revokeObjectURL(els.camera.dataset.url);
    els.camera = null;
  }
  /** Switch what the dialog shows. The form is a small dialog; everything
   *  after it is the large one, with the timeline on its right. */
  function swap(name, title, ...children) {
    stopPlayers();
    view = name;
    d.title.textContent = title;
    const session = name !== "form";
    d.box.classList.toggle("session", session);
    if (session) d.body.replaceChildren(h("div", { class: "session" }, h("div", { class: `stage ${name}` }, children), side));
    else d.body.replaceChildren(...children.filter(Boolean));
    updatePreview();            // the camera is asked for only while the recording view shows it
  }
  const buttons = (...items) => h("div", { class: "buttons" }, items);
  /** Cancel asks first once a run is under way: one click must not throw
   *  away the samples and the agent's work. */
  function cancel() {
    if (!training) { d.close(); return; }
    const ready = view === "result";
    confirmDialog(ready ? "Discard this gesture?" : "Cancel training?",
      ready ? "The gesture is ready but not saved. Discarding it throws away the samples and the function written for it."
        : "Everything in this run is lost: the samples you recorded and the work done on them so far. Nothing is saved.",
      ready ? "Discard gesture" : "Cancel training", () => d.close(), "Keep going");
  }

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
      }).then(begin, (failure) => { error.textContent = failure.message; });
    }

    swap("form", heading, ...rows, error,
      buttons(button("Cancel", () => d.close()), button("Start", start, "primary")));
    fields.name.focus();
  }

  const phaseText = () => `${phase.slice(0, 1).toUpperCase() + phase.slice(1)}  ·  round ${round}`;

  /** The recording view. While samples are taken (`camera`) it is the camera
   *  with what is tracked drawn on it, the prompt and the countdown over it
   *  and the progress bar of this batch under it. While the app or the agent
   *  works (starting, sending, writing the function) there is nothing to
   *  watch, so it is only the text and the bar. */
  function showTraining(prompt = "Starting…", count = "Loading your recorded motions. Don't move yet: a countdown comes first.", camera = false) {
    dropCamera();
    els = {
      camera: camera ? h("img", { alt: "Camera", hidden: true, class: state.settings.mirror ? "mirror" : "" }) : null,
      glow: h("div", { class: "shot-glow" }),
      phase: h("div", { class: "phase", text: phaseText() }),
      prompt: h("div", { class: "prompt", text: prompt }),
      count: h("div", { class: "count", text: count }),
      fill: h("div"),
      // Only while a sample is being taken; onPrompt shows it and names it.
      restart: button("Restart", () => engine.send("restart_sample").catch(() => {})),
    };
    els.restart.hidden = true;
    if (!camera) {
      swap("record", heading, els.phase, els.prompt, els.count, h("div", { class: "bar" }, els.fill),
        buttons(button("Cancel", cancel), els.restart));
      return;
    }
    swap("record", heading,
      h("div", { class: "shot" },
        h("span", { class: "shot-empty", text: state.cameraOk ? "Starting the camera…" : "Camera error" }),
        els.camera, els.glow, els.phase,
        h("div", { class: "shot-text" }, els.prompt, els.count)),
      h("div", { class: "bar" }, els.fill),
      buttons(button("Cancel", cancel), els.restart));
    d.body.querySelector(".stage").classList.add("camera");
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
    const rate = Math.max(5, clip.fps || 15);
    // Between plays the last frame holds, dimmed, for 0.8 s and the bar under
    // the video resets, so the jump back to the start is not read as movement.
    const gap = Math.round(rate * 0.8);
    const fill = h("div");
    const box = h("div", { class: "clip-video" }, image, h("div", { class: "clip-bar" }, fill));
    let at = 0;
    const next = () => {
      const held = at >= clip.frames.length;
      box.classList.toggle("held", held);
      if (!held) image.src = `data:image/jpeg;base64,${clip.frames[at]}`;
      fill.style.width = `${held ? 100 : ((at + 1) / clip.frames.length) * 100}%`;
      at = (at + 1) % (clip.frames.length + gap);
    };
    next();
    const timer = setInterval(next, 1000 / rate);
    return { el: box, stop: () => clearInterval(timer) };
  }

  /** The review page: every sample of the batch as a video. Nothing goes to
   *  the agent until Submit; any sample, or all, can be recorded again. */
  function showReview({ count, phase: batch, more = false }) {
    const negative = batch === "negatives";
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
    swap("review", negative ? "Check your not-the-gesture motions" : "Check your samples",
      h("div", { class: "phase", text: phaseText() }),
      h("p", { text: negative
        ? "These are motions that should NOT trigger the gesture: near misses and things " +
          "you do by habit. Re-record any that is actually the gesture, then submit."
        : "Watch each video. It should show the whole motion, from start to finish. " +
          "Re-record any that went wrong, then submit." }),
      h("div", { class: "clips-grid" }, cards), note,
      buttons(button("Cancel", cancel),
        button("Re-record all", () => send("rerecord_sample", { index: null })),
        // More than was asked for, if the user wants to give the agent more.
        more ? button("Record one more", () => send("add_sample")) : null,
        button(`Submit ${count} samples`, () => {
          send("submit_samples");
          const item = last();
          if (item && item.kind === "review") item.detail.textContent = `Submitted ${count} samples`;
          finish();
          clips = [];
          showTraining("One moment", "Sending your samples…");
        }, "primary")));
    players = next;
  }

  dialog = {
    close: () => d.close(),
    /** The image the camera goes to, while the recording view is showing. */
    camera: () => (view === "record" && els.camera) || null,
    /** The engine went away: its training is gone with it. */
    drop() { training = false; d.close(); },
    onSample({ index, fps, frames }) {
      if (training) clips[index] = { fps, frames };
    },
    onReview(data) {
      if (!training) return;
      setPhase(data.phase);
      const title = data.phase === "negatives" ? "Check your not-the-gesture motions" : "Check your samples";
      const item = last();
      if (item && item.kind.startsWith("record:") && !item.ended) item.detail.textContent = `${data.count} recorded`;
      const detail = `${data.count} to review`;
      if (item && item.kind === "review" && !item.ended) item.detail.textContent = detail;
      else entry("review", title, detail);
      showReview(data);
    },
    onProgress(data) {
      if (!training) return;
      round = data.round;
      // Nothing is recorded while the function is written: no camera then.
      if (view !== "record" || (data.phase === "revising" && els.camera)) showTraining();
      setPhase(data.phase);
      if (data.phase === "revising") {
        els.prompt.textContent = "Writing the gesture function";
        els.prompt.classList.remove("wait");
        els.restart.hidden = true;
        // Each step is one question to the model. Its note says what the step
        // before it led to, so that goes on the earlier entry.
        const item = last();
        if (!data.step) {
          if (!item || item.kind !== "step" || item.ended) els.count.textContent = "Writing the gesture function…";
          return;
        }
        if (item && item.kind === "step" && item.step === data.step) return;
        const before = log.findLast((e) => e.kind === "step");
        if (before && data.note) before.detail.textContent = data.note;
        entry("step", `Step ${data.step}`, WORKING).step = data.step;
        els.count.textContent = `Step ${data.step} · 0:00`;
      }
    },
    onPrompt({ phase, count, index, asked, budget, ready = 0, back = false, sample: redo = null, of = null, which = null }) {
      if (!training) return;
      // Back from the review or from the agent's work, to record: with the camera.
      if (view !== "record" || !els.camera) showTraining("", "", true);
      setPhase(phase);
      // One timeline entry for each batch of recording. Samples being redone
      // are named by their number, counted from the first one recorded.
      const kind = `record:${phase}`;
      let item = last();
      const title = which && which.length
        ? `Redo sample${which.length > 1 ? "s" : ""} ${which.join(", ")}`
        : RECORDING[phase] || "Record samples";
      if (!item || item.kind !== kind || item.ended) item = entry(kind, title);
      item.detail.textContent = `${Math.min(index, count)} of ${count} recorded`;
      // "negatives": the user performs motions that are NOT the gesture.
      const negative = phase === "negatives";
      const sample = phase === "redoing"
        ? (redo
          ? `Redo your sample ${redo}${of ? ` of ${of}` : ""} (${Math.min(index + 1, count)} of ${count} to redo): it was not the gesture`
          : `Redo ${Math.min(index + 1, count)} of ${count}: an earlier sample was not the gesture`)
        : negative ? `Not the gesture: ${Math.min(index + 1, count)} of ${count}`
          : `Sample ${Math.min(index + 1, count)} of ${count}`;
      if (ready) {                        // the countdown: 1, 2, then go
        els.prompt.textContent = `Get ready…  ${ready}`;
        els.count.textContent = negative
          ? `${sample}. When it says go, make a motion that should NOT trigger it.`
          : `${sample}. Start the motion when it says go.`;
      } else {
        els.prompt.textContent = negative ? "Go: a motion that is NOT the gesture"
          : "Go: perform the motion";
        els.count.textContent = sample;
      }
      els.prompt.classList.toggle("wait", !!ready);
      // The countdown closes in: a green edge that grows with each number and
      // is a firm ring on go. After the last sample of a batch there is none.
      els.glow.dataset.step = index >= count ? "" : ready ? String(ready) : "go";
      // During the countdown it redoes the sample just recorded, if there is
      // one in this batch; once recording has begun it takes this one again.
      els.restart.hidden = index >= count;
      els.restart.textContent = ready && back ? "Redo last sample" : "Restart";
      els.fill.style.width = `${count ? (index / count) * 100 : 0}%`;
      askedEl.textContent = `${asked} of ${budget} samples asked`;
    },
    onResult({ passed, summary = "", message = "", parts = [], code = "", action = null, retry = true }) {
      if (!training) return;
      entry("result", passed ? "Gesture ready" : "Not passed", passed ? "Waiting for you to save it" : "");
      if (!passed) {
        finish();
        training = false;               // nothing is waiting in the engine
        swap("result", "Not passed", h("p", { text: message || FAILED_TEXT }),
          buttons(button("Close", () => d.close()),
            retry ? button("Try again", showForm, "primary") : null));
        return;
      }
      // `training` stays set: closing without Save tells the engine to drop the draft.
      // The planned action, in plain words. The user confirms it by saving,
      // or changes it first: another built-in one, or their own script.
      const block = (title, text) => [h("label", { text: title }), h("p", { text })];
      let chosen = null;
      const planned = h("span", { class: "grow", text: summary });
      const change = button("Change", () => actionDialog({
        gestureId: gesture ? gesture.id : null, name: values.name, action: chosen || action || {},
        onSave: (picked, text) => { chosen = picked; planned.textContent = text; },
      }), "small-btn");
      swap("result", "Gesture ready",
        h("label", { text: "Action" }), h("div", { class: "row" }, planned, change),
        ...block("Uses", parts.map((p) => p.replaceAll("_", " ")).join(", ") || "–"),
        ...(message ? block("Note", message) : []),      // e.g. the motion differs from its description
        // A script the planner wrote is only a proposal: it can be read here,
        // and saving the gesture is what accepts it.
        action && action.kind === "script" && action.code
          ? button("View the action's script", () => codeDialog(`${values.name}: action script`,
            (chosen && chosen.code) || action.code), "link") : null,
        button("View code", () => codeDialog(`${values.name}: detect`, code), "link"),
        buttons(button("Cancel", cancel),
          button("Save", () => {
            training = false;
            command("save_gesture", chosen ? { action: chosen } : {});
            d.close();
          }, "primary")));
    },
  };
  if (gesture) {
    // Re-train only adds samples: same name, action and description, no form.
    engine.send("start_training", {
      name: gesture.name, action_text: "", motion_text: gesture.motion || "", gesture_id: gesture.id,
    }).then(begin,
      (failure) => swap("result", heading, h("p", { class: "error", text: failure.message }),
        buttons(button("Close", () => d.close()))));
    showTraining("One moment", "Starting from the samples this gesture already has…");
  } else {
    showForm();
  }
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
    class: "field", placeholder: "claude-opus-5-5", style: "width:260px",
    autocomplete: "off", spellcheck: false,
    onchange: () => saveSettings({ model_name: s.model.value.trim() }),
  });

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
        h("div", { class: "setting" },
          h("div", { class: "text" }, h("span", { text: "Claude Code token" }), s.keyHint), s.keyControls),
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
  s.keyHint.textContent = "Claude Code writes gesture functions in a Docker container the engine starts. "
    + "Run claude setup-token in a terminal and paste the token it prints. "
    + `Read from ${key.env_var} if set, otherwise stored in the OS keychain.`;
  const fail = (error) => { s.keyError.textContent = error.message; };
  if (key.present && key.from_env) {
    s.keyControls.replaceChildren(h("span", { class: "muted", text: `${key.masked}  from ${key.env_var}` }));
  } else if (key.present) {
    s.keyControls.replaceChildren(h("span", { class: "muted", text: key.masked }),
      button("Remove", () => { s.keyError.textContent = ""; engine.send("remove_api_key").catch(fail); }));
  } else {
    // The key goes to the engine once and is not kept here.
    const input = h("input", { class: "field", type: "password", placeholder: "Paste token", style: "width:180px", autocomplete: "off" });
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
  state.previewOn = state.previewPoints = false;   // a new connection starts with the preview off
  state.testing = false;              // and with test mode off
  setGestures(snapshot.gestures || []);
  renderTesting();
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
  state.previewOn = state.previewPoints = false;
  state.testing = false;
  renderTesting();
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
  if (!state.cameraOk) return;
  const recording = dialog && dialog.camera();
  if (recording) {                    // the training view is in front of the camera card
    showFrame(recording, blob);
    return;
  }
  if (state.page !== "main") return;
  showFrame(ui.previewImage, blob);
  ui.previewText.textContent = "";
});

engine.on("tracker_status", ({ fps = 0, visible = [], speed = 0, camera_ok = true, moving, segment, loading = false, live = null }) => {
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
    if (live) {                // recognition is running: what it looked at last
      ui.motionLine.textContent = `Checking the last ${live.windows} stretches, ${live.ms} ms a look`;
    } else if (segment) {
      ui.motionLine.textContent = `Motion: ${segment.ms} ms, ${segment.reason} (at ${Number(segment.t).toFixed(1)} s)`;
    }
  } else {
    ui.speedLine.textContent = `Speed ${Number(speed).toFixed(2)}`;
  }
  if (state.page === "first_run") refreshChecklist();
});

engine.on("match", ({ gesture, ms, id = null, test = false }) => {
  if (test) {                           // test mode: shown, and no action ran
    if (state.testing) flashCard(id);
    setLast(`Test: ${gesture} triggered (${Math.round(ms)} ms)`);
    return;
  }
  state.pendingMatch = gesture;
  setLast(`Last: ${gesture} (${Math.round(ms)} ms)`);
});

engine.on("action_result", ({ ok, message = "", gesture = "", run_now = false }) => {
  if (run_now) {                        // the answer to Run now in the action editor
    if (ui.runNow) ui.runNow({ ok, message });
    return;
  }
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
