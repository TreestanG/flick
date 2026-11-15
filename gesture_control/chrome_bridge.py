"""The local WebSocket server the Chrome extension connects to (file 04).

Separate from the UI's socket (`server.py`): its own port, its own path
(`/chrome`), its own token and its own fixed list of messages. A UI token
does not work here, and the other way round.

Pairing. The extension cannot read files, so it gets the token once, over
the socket: after the user presses Connect Chrome in settings, an extension
that has no valid token yet is given one for the next two minutes. Outside
that window only an extension that already holds the token is let in. Only
`chrome-extension://` origins are accepted at all, so a web page cannot
connect: a page cannot fake its Origin.

Engine -> extension:  {"type": "call", "id": 7, "name": "close_tab", "params": {}}
Extension -> engine:  {"type": "result", "id": 7, "ok": true, "error": ""}
"""

from __future__ import annotations

import asyncio
import hmac
import itertools
import json
import os
import secrets
import threading
import time

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from . import store

PORT = 38917                # fixed: chrome_extension/background.js connects to it
PAIR_S = 120.0
HELLO_S = 2.0
CALL_S = 3.0

# The only calls the extension is ever asked to make.
CALLS = frozenset({"close_tab", "new_tab", "reopen_tab", "next_tab", "prev_tab",
                   "close_window", "minimize_window", "open_url"})


def token_path():
    return store.config_dir() / "chrome_token"


def load_token() -> str:
    """The Chrome token, made on first use and kept in the config folder."""
    path = token_path()
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(token)
    return token


class ChromeBridge:
    def __init__(self, on_status=None, port: int = PORT):
        """`on_status(connected, message)` is called from the bridge's thread."""
        self.port = port
        self.token = load_token()
        self._on_status = on_status or (lambda connected, message: None)
        self._ws = None                         # the one connected extension
        self._pair_until = 0.0
        self._waiting: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error = ""
        self._thread = threading.Thread(target=self._serve, name="chrome-bridge", daemon=True)

    # lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def start(self) -> None:
        if not self._thread.is_alive() and not self._ready.is_set():
            self._thread.start()
            self._ready.wait(5)
        self._report()

    def stop(self) -> None:
        if self._loop is not None and self._stopping is not None:
            try:
                self._loop.call_soon_threadsafe(self._stopping.set)
            except RuntimeError:
                pass
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def pair(self, seconds: float = PAIR_S) -> None:
        """The user pressed Connect Chrome: let a new extension in for a while."""
        self._pair_until = time.monotonic() + seconds
        self._report()

    def _report(self) -> None:
        if self._error:
            self._on_status(False, self._error)
        elif self.connected:
            self._on_status(True, "Connected")
        elif time.monotonic() < self._pair_until:
            self._on_status(False, "Waiting for the Chrome extension to pair…")
        else:
            self._on_status(False, "Not connected. Install the extension, then press Retry.")

    def _serve(self) -> None:
        try:
            asyncio.run(self._main())
        except OSError:
            self._error = f"Port {self.port} is in use, so Chrome cannot connect."
        except Exception as error:              # noqa: BLE001 - shown as the status
            self._error = f"The Chrome connection failed: {error}"
        finally:
            self._ws = None
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        async with serve(self._handle, "127.0.0.1", self.port) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stopping.wait()

    # one extension -----------------------------------------------------

    async def _handle(self, ws) -> None:
        origin = ws.request.headers.get("Origin", "")
        if ws.request.path != "/chrome" or not origin.startswith("chrome-extension://"):
            await ws.close(1008, "")
            return
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), HELLO_S))
            token = hello.get("token") if hello.get("type") == "hello" else None
        except Exception:
            await ws.close(1008, "")
            return
        known = isinstance(token, str) and hmac.compare_digest(token.encode(),
                                                               self.token.encode())
        if not known:
            if time.monotonic() >= self._pair_until:
                await ws.send(json.dumps({"type": "denied"}))
                await ws.close(1008, "")
                return
            self._pair_until = 0.0              # one extension per press of the button
            await ws.send(json.dumps({"type": "paired", "token": self.token}))
        else:
            await ws.send(json.dumps({"type": "welcome"}))

        old, self._ws = self._ws, ws
        if old is not None:
            await old.close(1000, "replaced")
        self._report()
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(message, dict) and message.get("type") == "result":
                    waiting = self._waiting.pop(message.get("id"), None)
                    if waiting is not None and not waiting.done():
                        waiting.set_result(message)
        except ConnectionClosed:
            pass
        finally:
            if self._ws is ws:
                self._ws = None
                self._report()

    # calls -------------------------------------------------------------

    async def _call(self, name: str, params: dict, timeout: float) -> dict:
        ws = self._ws
        if ws is None:
            raise RuntimeError("Chrome is not connected")
        call_id = next(self._ids)
        waiting = self._waiting[call_id] = self._loop.create_future()
        try:
            await ws.send(json.dumps({"type": "call", "id": call_id, "name": name,
                                      "params": params}))
            return await asyncio.wait_for(waiting, timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("Chrome did not answer") from None
        except ConnectionClosed:
            raise RuntimeError("Chrome is not connected") from None
        finally:
            self._waiting.pop(call_id, None)

    def call(self, name: str, params: dict | None = None, timeout: float = CALL_S) -> None:
        """Ask the extension to make one of the fixed calls. Blocks until it
        answers; raises RuntimeError with a message for the user if it fails.
        Not for the bridge's own thread."""
        if name not in CALLS:
            raise RuntimeError(f"Not a Chrome call: {name}")
        if self._loop is None or not self.connected:
            raise RuntimeError("Chrome is not connected")
        future = asyncio.run_coroutine_threadsafe(self._call(name, params or {}, timeout),
                                                  self._loop)
        try:
            result = future.result(timeout + 1)
        except TimeoutError:
            future.cancel()
            raise RuntimeError("Chrome did not answer") from None
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Chrome could not do that"))
