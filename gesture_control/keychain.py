"""The Fireworks API key: read from the FIREWORKS_API_KEY environment variable
if set, otherwise kept in the OS keychain. Never written to config files."""

from __future__ import annotations

import os

ENV_VAR = "FIREWORKS_API_KEY"
SERVICE = "gesture_control"
ACCOUNT = "fireworks_api_key"


def _keyring():
    import keyring
    return keyring


def from_env() -> bool:
    return bool(os.environ.get(ENV_VAR, "").strip())


def get_key() -> str | None:
    """The environment variable wins over the keychain when both are set."""
    if from_env():
        return os.environ[ENV_VAR].strip()
    try:
        return _keyring().get_password(SERVICE, ACCOUNT)
    except Exception:
        return None


def has_key() -> bool:
    return bool(get_key())


def set_key(value: str) -> str | None:
    """Store the key. Returns an error message, or None on success."""
    try:
        _keyring().set_password(SERVICE, ACCOUNT, value)
        return None
    except Exception as exc:
        return f"Could not save to the keychain: {exc}"


def delete_key() -> str | None:
    try:
        _keyring().delete_password(SERVICE, ACCOUNT)
        return None
    except Exception as exc:
        return f"Could not remove from the keychain: {exc}"


def masked(value: str | None) -> str:
    """A fixed-length mask that shows only the last four characters."""
    if not value:
        return ""
    return "•" * 12 + value[-4:]
