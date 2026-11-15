"""Model access (file 03). One small interface, one backend: open models on
Fireworks through its OpenAI-compatible API. Only text goes out: the user's
sentences and body-part numbers, never images."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Protocol

from . import keychain

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


CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_MAX_TOKENS = 16000
# The app tests every answer on the user's samples, so a wrong first try is
# cheap and a slow one is not: keep the model's deliberation short.
CLAUDE_EFFORT = "low"
CLAUDE_TIMEOUT_S = 180.0


class ClaudeClient:
    """Claude through the Anthropic API, with the official SDK."""

    def __init__(self, api_key: str, model: str = "", timeout: float = CLAUDE_TIMEOUT_S):
        import anthropic
        self.anthropic = anthropic
        model = model.strip()
        self.model = model if model.startswith("claude-") else CLAUDE_MODEL
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)
        # What Claude sent back for each of its turns, whole: its thinking
        # blocks have to be returned unchanged on later requests.
        self.turns: list = []

    def _history(self, messages: list[dict]) -> tuple[str, list]:
        """The trainer keeps plain text. Split off the system prompt and put
        Claude's own turns back as the blocks it produced."""
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        history, turn = [], 0
        for message in messages:
            if message["role"] == "system":
                continue
            content = message["content"]
            if message["role"] == "assistant":
                if turn < len(self.turns) and self.turns[turn][0] == content:
                    content = self.turns[turn][1]
                turn += 1
            history.append({"role": message["role"], "content": content})
        return system, history

    def complete(self, messages: list[dict]) -> str:
        anthropic = self.anthropic
        system, history = self._history(messages)
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],   # the same every call
                messages=history,
                output_config={"effort": CLAUDE_EFFORT},
                # If a safety check declines the request, the API retries it on
                # another Claude model inside the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError:
            raise ModelError("Anthropic rejected the API key. Check it in settings.") from None
        except anthropic.PermissionDeniedError:
            raise ModelError("This Anthropic API key may not use that model.") from None
        except anthropic.NotFoundError:
            raise ModelError(f"Anthropic does not know the model {self.model}. "
                             "Check the model name in settings.") from None
        except anthropic.RateLimitError:
            raise ModelError("Anthropic is rate limiting this key. Try again in a "
                             "minute.") from None
        except anthropic.BadRequestError as error:
            raise ModelError(f"Anthropic refused the request: {error.message}") from None
        except anthropic.APIStatusError as error:
            raise ModelError(f"Anthropic returned an error ({error.status_code}).") from None
        except anthropic.APIConnectionError:
            raise ModelError("Could not reach Anthropic. Check the connection.") from None
        if response.stop_reason == "refusal":
            raise ModelError("Claude declined to answer this request.")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise ModelError("Claude ran out of room before answering. Try again."
                             if response.stop_reason == "max_tokens"
                             else "Claude sent an empty answer. Try again.")
        self.turns.append((text, response.content))
        return text


def claude_client(model: str = "") -> ClaudeClient:
    key = keychain.get_key("anthropic")
    if not key:
        raise ModelError("Add an Anthropic API key in settings before training a gesture.")
    return ClaudeClient(key, model)


def default_client(model: str = "") -> FireworksClient:
    key = keychain.get_key()
    if not key:
        raise ModelError("Add a Fireworks API key in settings before training a gesture.")
    return FireworksClient(key, model)
