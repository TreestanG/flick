// The link to the engine (file 01): one WebSocket on 127.0.0.1.
// Text messages are JSON commands, replies and events; binary messages are
// preview frames, one JPEG each.

const VERSION = 1;
const RETRY_MS = 1000;

/** Port and token. In the Tauri app the Rust shell reads them from
 *  engine.json (and starts the engine if `start`). In a plain browser, for
 *  working on the UI, they come from the page address: #port=1234&token=abc */
async function engineInfo(start) {
  const tauri = window.__TAURI__;
  if (tauri) return tauri.core.invoke("engine_info", { start });
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get("port") && hash.get("token")) {
    return { port: Number(hash.get("port")), token: hash.get("token"), version: VERSION };
  }
  throw new Error("Open this page in the Gesture Control app.");
}

export class Engine {
  constructor() {
    this.connected = false;
    this.reason = "";
    this._ws = null;
    this._nextId = 1;
    this._waiting = new Map();     // id -> {resolve, reject}
    this._handlers = new Map();    // event name -> [fn]
    this._timer = null;
  }

  /** Listen for an engine event, or for "snapshot", "down" and "frame". */
  on(name, fn) {
    if (!this._handlers.has(name)) this._handlers.set(name, []);
    this._handlers.get(name).push(fn);
  }

  _emit(name, data) {
    for (const fn of this._handlers.get(name) || []) {
      try { fn(data); } catch (error) { console.error(error); }
    }
  }

  /** Send a command. Resolves with the reply's data, rejects with its error. */
  send(name, data = {}) {
    return new Promise((resolve, reject) => {
      if (!this._ws || this._ws.readyState !== WebSocket.OPEN) {
        reject(new Error("Engine not running"));
        return;
      }
      const id = this._nextId++;
      this._waiting.set(id, { resolve, reject });
      this._ws.send(JSON.stringify({ type: "command", id, name, data }));
    });
  }

  /** Find the engine and connect. `start` also starts it if it is not running;
   *  after a drop we only look, so quitting the engine from the tray sticks. */
  async connect(start = true) {
    clearTimeout(this._timer);
    let info;
    try {
      info = await engineInfo(start);
    } catch (error) {
      this._down(String(error && error.message ? error.message : error));
      return;
    }
    const ws = new WebSocket(`ws://127.0.0.1:${info.port}/ui`);
    ws.binaryType = "arraybuffer";
    this._ws = ws;
    let helloFailed = "";

    ws.onopen = () => {
      this.send("hello", { token: info.token, version: VERSION }).then(
        (snapshot) => {
          this.connected = true;
          this.reason = "";
          this._emit("snapshot", snapshot);
        },
        (error) => { helloFailed = error.message; },
      );
    };
    ws.onmessage = (event) => {
      if (typeof event.data !== "string") {
        this._emit("frame", new Blob([event.data], { type: "image/jpeg" }));
        return;
      }
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === "event") {
        this._emit(message.name, message.data || {});
      } else if (message.type === "reply") {
        const waiting = this._waiting.get(message.id);
        this._waiting.delete(message.id);
        if (!waiting) return;
        if (message.ok) waiting.resolve(message.data || {});
        else waiting.reject(new Error(message.error || "The engine refused."));
      }
    };
    ws.onclose = () => {
      if (this._ws !== ws) return;
      this._ws = null;
      for (const waiting of this._waiting.values()) waiting.reject(new Error("Engine not running"));
      this._waiting.clear();
      this._down(helloFailed || "The engine stopped or was quit.", !helloFailed);
    };
  }

  _down(reason, retry = true) {
    this.connected = false;
    this.reason = reason;
    this._emit("down", reason);
    clearTimeout(this._timer);
    if (retry) this._timer = setTimeout(() => this.connect(false), RETRY_MS);
  }
}
