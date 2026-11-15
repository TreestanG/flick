"""API keys for the model that writes gesture functions: Fireworks or Anthropic.
Each is read from its environment variable if set, otherwise kept in the OS
keychain. Never written to config files."""

from __future__ import annotations

import os

SERVICE = "gesture_control"
# provider -> (name in the keychain, environment variable, name shown to the user)
PROVIDERS = {
    "fireworks": ("fireworks_api_key", "FIREWORKS_API_KEY", "Fireworks"),
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY", "Anthropic"),
}
ENV_VAR = PROVIDERS["fireworks"][1]
ACCOUNT = PROVIDERS["fireworks"][0]


def provider_for(writer: str) -> str:
    """Which key a "who writes gesture functions" setting needs."""
    return "anthropic" if writer == "claude" else "fireworks"


def env_var(provider: str = "fireworks") -> str:
    return PROVIDERS[provider][1]


def _keyring():
    import keyring
    return keyring


def from_env(provider: str = "fireworks") -> bool:
    return bool(os.environ.get(env_var(provider), "").strip())


def get_key(provider: str = "fireworks") -> str | None:
    """The environment variable wins over the keychain when both are set."""
    if from_env(provider):
        return os.environ[env_var(provider)].strip()
    try:
        return _keyring().get_password(SERVICE, PROVIDERS[provider][0])
    except Exception:
        return None


def has_key(provider: str = "fireworks") -> bool:
    return bool(get_key(provider))


def set_key(value: str, provider: str = "fireworks") -> str | None:
    """Store the key. Returns an error message, or None on success."""
    try:
        _keyring().set_password(SERVICE, PROVIDERS[provider][0], value)
        return None
    except Exception as exc:
        return f"Could not save to the keychain: {exc}"


def delete_key(provider: str = "fireworks") -> str | None:
    try:
        _keyring().delete_password(SERVICE, PROVIDERS[provider][0])
        return None
    except Exception as exc:
        return f"Could not remove from the keychain: {exc}"


def masked(value: str | None) -> str:
    """A fixed-length mask that shows only the last four characters."""
    if not value:
        return ""
    return "•" * 12 + value[-4:]
