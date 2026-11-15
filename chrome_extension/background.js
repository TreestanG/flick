// Connects to the Gesture Control engine on this computer and runs a fixed
// list of chrome.tabs / chrome.windows calls for it (file 04). Nothing else:
// a message that is not one of the calls below is ignored.
//
// The token comes from the engine once, after the user presses Connect Chrome
// (Retry) in the app's settings, and is kept in chrome.storage.local.

const ENGINE = "ws://127.0.0.1:38917/chrome";   // PORT in gesture_control/chrome_bridge.py
const RETRY_MS = 3000;
const PING_MS = 20000;      // traffic at least every 30 s keeps the service worker alive

let socket = null;
let opening = false;

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("Chrome has no open tab");
  return tab;
}

async function step(by) {
  const tab = await activeTab();
  const tabs = await chrome.tabs.query({ windowId: tab.windowId });
  const next = tabs[(tab.index + by + tabs.length) % tabs.length];
  await chrome.tabs.update(next.id, { active: true });
}

async function frontWindow() {
  const win = await chrome.windows.getLastFocused();
  if (!win) throw new Error("Chrome has no open window");
  return win;
}

const CALLS = {
  close_tab: async () => chrome.tabs.remove((await activeTab()).id),
  new_tab: async () => chrome.tabs.create({}),
  reopen_tab: async () => chrome.sessions.restore(),
  next_tab: async () => step(1),
  prev_tab: async () => step(-1),
  close_window: async () => chrome.windows.remove((await frontWindow()).id),
  minimize_window: async () => chrome.windows.update((await frontWindow()).id, { state: "minimized" }),
  open_url: async ({ url }) => {
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) throw new Error("Not a web address");
    await chrome.tabs.create({ url });
  },
};

async function onMessage(ws, raw) {
  let message;
  try { message = JSON.parse(raw); } catch { return; }
  if (message.type === "paired" && typeof message.token === "string") {
    await chrome.storage.local.set({ token: message.token });
    return;
  }
  if (message.type !== "call") return;
  const reply = { type: "result", id: message.id, ok: true, error: "" };
  try {
    if (!Object.hasOwn(CALLS, message.name)) throw new Error("Not an allowed call");
    await CALLS[message.name](message.params || {});
  } catch (error) {
    reply.ok = false;
    reply.error = String((error && error.message) || error);
  }
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(reply));
}

async function connect() {
  if (opening || (socket && socket.readyState <= WebSocket.OPEN)) return;
  opening = true;
  try {
    const { token = null } = await chrome.storage.local.get("token");
    const ws = new WebSocket(ENGINE);
    socket = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: "hello", token }));
    ws.onmessage = (event) => { onMessage(ws, event.data); };
    ws.onclose = () => {
      if (socket === ws) socket = null;
      setTimeout(connect, RETRY_MS);          // the engine may not be running yet
    };
  } finally {
    opening = false;
  }
}

setInterval(() => {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "ping" }));
}, PING_MS);

// If Chrome stops the worker anyway, the alarm starts it again.
chrome.alarms.create("reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
