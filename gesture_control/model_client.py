"""Model access (file 03). One small interface, one backend: open models on
Fireworks through its OpenAI-compatible API. Only text goes out: the user's
sentences and body-part numbers, never images."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Protocol

from . import keychain, store

BASE_URL = "https://api.fireworks.ai/inference/v1"
# Top of Fireworks' own list for code generation when this was written (Nov 2026).
DEFAULT_MODEL = "accounts/fireworks/models/deepseek-v4p1-flash-1017"
TIMEOUT_S = 300.0
# These models think before they answer, and the thinking counts against the
# allowance. With 4096 the whole allowance went on thinking and the answer came
# back empty, so the allowance is large and a call may take a few minutes.
MAX_TOKENS = 48000


class ModelError(Exception):
    """A message that can be shown to the user as it is."""


class ModelClient(Protocol):
    def complete(self, messages: list[dict]) -> str:
        """messages: [{"role": "system" | "user" | "assistant", "content": str}]."""


class FireworksClient:
    def __init__(self, api_key: str, model: str = "", base_url: str = BASE_URL,
                 timeout: float = TIMEOUT_S):
        self.api_key = api_key
        self.model = model.strip() or DEFAULT_MODEL
        if "/" not in self.model:
            self.model = "accounts/fireworks/models/" + self.model
        elif not self.model.startswith("accounts/"):
            self.model = DEFAULT_MODEL      # another provider's name, not a Fireworks one
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(self, messages: list[dict]) -> str:
        """One reply. The timeout on the socket applies to each read, so a slow
        trickle of data could hold the call open forever; this gives the whole
        call a hard deadline instead."""
        box: dict = {}

        def ask() -> None:
            try:
                box["text"] = self._complete(messages)
            except BaseException as error:      # noqa: BLE001 - re-raised below
                box["error"] = error

        worker = threading.Thread(target=ask, name="model-call", daemon=True)
        worker.start()
        worker.join(self.timeout + 15)
        if worker.is_alive():
            raise ModelError("Fireworks took too long to reply. Try again.")
        if "error" in box:
            raise box["error"]
        return box["text"]

    def _complete(self, messages: list[dict]) -> str:
        body = json.dumps({"model": self.model, "messages": messages, "temperature": 0.2,
                           "max_tokens": MAX_TOKENS,
                           # The app tests every answer, so long deliberation buys little.
                           "reasoning_effort": "low"}).encode()
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=body, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.loads(response.read())
            choice = raw["choices"][0]
            text = choice["message"]["content"] or ""
            if not text.strip() and choice.get("finish_reason") == "length":
                raise ModelError("The model used its whole allowance thinking and never "
                                 "answered. Try again, or pick another model in settings.")
            return text
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise ModelError("Fireworks rejected the API key. Check it in settings.") from None
            if error.code == 404:
                raise ModelError(f"Fireworks does not know the model {self.model}. "
                                 "Check the model name in settings.") from None
            if error.code == 429:
                raise ModelError("Fireworks is rate limiting this key. Try again in a "
                                 "minute.") from None
            raise ModelError(f"Fireworks returned an error ({error.code}).") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ModelError("Could not reach Fireworks. Check the connection.") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("Fireworks sent a reply that could not be read.") from None


class ChatClient:
    """Hands each question to a Claude Code session instead of a hosted model.

    The question goes into `<runtime folder>/handoff/request.json` (the whole
    conversation so far, the newest message last). The session answers by
    writing `reply.json` next to it: {"run": ..., "step": ..., "text": "..."}
    with the same run and step. Nothing leaves the machine by this route
    except through that session. It only works while the session is open."""

    WAIT_S = 30 * 60.0
    POLL_S = 0.5

    def __init__(self, folder=None, wait: float = WAIT_S):
        self.folder = folder or handoff_dir()
        self.wait = wait
        self.run = time.strftime("%Y%m%d-%H%M%S")
        self.step = 0

    def _write(self, name: str, body: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / (name + ".tmp")
        tmp.write_text(json.dumps(body, indent=1), encoding="utf-8")
        os.replace(tmp, self.folder / name)

    def complete(self, messages: list[dict]) -> str:
        self.step += 1
        request, reply = self.folder / "request.json", self.folder / "reply.json"
        try:
            reply.unlink(missing_ok=True)
            self._write("request.json", {"run": self.run, "step": self.step,
                                         "asked_at": time.strftime("%H:%M:%S"),
                                         "messages": messages})
        except OSError as error:
            raise ModelError(f"Could not write the question for Claude Code: {error}") from None
        end = time.monotonic() + self.wait
        try:
            while time.monotonic() < end:
                time.sleep(self.POLL_S)
                try:
                    answer = json.loads(reply.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue                    # not there yet, or still being written
                if answer.get("run") == self.run and answer.get("step") == self.step \
                        and isinstance(answer.get("text"), str):
                    return answer["text"]
            raise ModelError("Claude Code did not answer. Is the session open and watching "
                             "for training questions?")
        finally:
            for path in (request, reply):       # nothing is left waiting for an answer
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def handoff_dir():
    return store.runtime_dir() / "handoff"


def default_client(model: str = "") -> FireworksClient:
    key = keychain.get_key()
    if not key:
        raise ModelError("Add a Fireworks API key in settings before training a gesture.")
    return FireworksClient(key, model)
