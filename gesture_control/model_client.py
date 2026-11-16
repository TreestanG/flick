"""Model access (file 03). One small interface, one backend: Claude Code in a
local Docker container (`claude_code.py`). The user's sentences and body-part
numbers go to it, never images."""

from __future__ import annotations

from typing import Protocol

from . import keychain


class ModelError(Exception):
    """A message that can be shown to the user as it is."""


class ModelClient(Protocol):
    def complete(self, messages: list[dict]) -> str:
        """messages: [{"role": "system" | "user" | "assistant", "content": str}]."""


def default_client(model: str = ""):
    """Claude Code in the engine's Docker container, signed in with the token
    from `claude setup-token`."""
    from .claude_code import ClaudeCodeClient
    key = keychain.get_key()
    if not key:
        raise ModelError("Run `claude setup-token` in a terminal and paste the token in "
                         "settings before training a gesture.")
    return ClaudeCodeClient(key, model)
