"""The planner (file 04): the user's sentence -> one action from the catalog.

The model sees only the sentence and the catalog. It picks from the catalog
when something there fits; when nothing does, it is told to write a short
script that does what was asked. Its reply is checked by `actions.check`,
with one retry. A script it writes is only a proposal: the user sees it on
the result page and it is saved, like any script, only when they save the
gesture. Without a model (no key, an error, two bad replies) a small set of
keyword rules is used instead, and if nothing fits there the result is
`actions.NONE` and the user is pointed to the custom script option.
"""

from __future__ import annotations

import json
import re

from . import actions
from .model_client import ModelError

_URL = re.compile(r"\b((?:https?://|www\.)\S+|[a-z0-9-]+\.(?:com|org|net|io|dev|app|co|ai|edu|gov)\b\S*)",
                  re.I)
_KEYS = re.compile(r"\b((?:ctrl|control|alt|shift|win|windows|cmd|command|option)"
                   r"(?:\s*\+\s*[a-z0-9]+)+)", re.I)


def _builtin(action_id: str, **params) -> dict:
    return {"kind": "builtin", "id": action_id, "params": params}


def guess(sentence: str) -> dict:
    """Keyword rules: the stand-in when no model is available."""
    text = " " + re.sub(r"\s+", " ", sentence.lower()) + " "
    has = lambda *words: all(word in text for word in words)     # noqa: E731
    chrome = has("chrome") or has("browser")
    try:
        keys = _KEYS.search(text)
        if keys:
            return actions.validate(_builtin("os.hotkey", keys=keys.group(1)))
        url = _URL.search(text)
        if url and (has("open") or has("go to") or has("visit")):
            return actions.validate(_builtin("chrome.open_url", url=url.group(1).rstrip(".,")))
    except ValueError:
        pass
    if has("tab"):
        if has("reopen") or has("restore") or has("undo"):
            return _builtin("chrome.reopen_tab")
        if has("new tab") or has("open a tab") or has("open tab"):
            return _builtin("chrome.new_tab")
        if has("next") or has("right"):
            return _builtin("chrome.next_tab")
        if has("previous") or has("prev") or has("left") or has("back"):
            return _builtin("chrome.prev_tab")
        if has("close"):
            return _builtin("chrome.close_tab")
    if has("minimi"):
        return _builtin("chrome.minimize_window" if chrome else "os.minimize_window")
    if has("close", "window") or has("close", "app"):
        return _builtin("chrome.close_window" if chrome else "os.close_window")
    if has("switch", "window") or has("switch", "app") or has("alt tab") or has("alt-tab") \
            or has("previous window") or has("last window"):
        return _builtin("os.switch_window")
    if has("desktop"):
        return _builtin("os.show_desktop")
    if has("mute") or has("silence"):
        return _builtin("os.mute")
    if has("volume up") or has("louder") or has("raise", "volume") or has("increase", "volume"):
        return _builtin("os.volume_up")
    if has("volume down") or has("quieter") or has("lower", "volume") or has("decrease", "volume"):
        return _builtin("os.volume_down")
    if has("pause") or has(" play") or has("resume"):
        return _builtin("os.play_pause")
    return dict(actions.NONE)


def catalog_text() -> str:
    lines = []
    for action_id, entry in actions.CATALOG.items():
        params = f'  params: {{"{entry.param}": {entry.hint}}}' if entry.param else ""
        lines.append(f"{action_id}: {entry.text}.{params}")
    return "\n".join(lines)


SYSTEM = """You turn one request into one action for a desktop app that runs it when the user makes a hand gesture. The computer runs {system}.

Catalog (id: what it does. params if it needs any):
{catalog}

If an action in the catalog does what is asked, reply with JSON only, no other text:
{{"id": "<an id from the catalog>", "params": {{}}}}

If nothing in the catalog does what is asked, write a script that does it and
reply with JSON only, no other text:
{{"kind": "script", "language": "python" or "shell", "code": "<the whole script>"}}

Rules:
- Use only ids from the catalog. Include params only for actions that list them.
- If the request is about tabs or a web page, prefer the chrome.* actions.
- Prefer the catalog. Write a script only when no catalog action does what is
  asked; do not pick something merely similar.
- A script runs once each time the gesture is made, with no input and no
  window, and must finish in a few seconds. "shell" is {shell}. "python" may
  use only the standard library. Keep it short and do only what was asked:
  nothing that deletes files, changes settings or uses the network unless the
  request asks for exactly that.
- If the request is empty or cannot be done by either, reply {{"id": "none"}}."""


def parse(reply: str) -> dict:
    """The action in a model's reply, or ValueError saying what is wrong."""
    text = re.sub(r"<think>.*?</think>", "", reply or "", flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The reply held no JSON object.")
    try:
        raw = json.loads(text[start:end + 1])
    except ValueError:
        raise ValueError("The reply was not valid JSON.") from None
    return actions.check(raw)        # a catalog action, or a script with its text in "code"


def plan(sentence: str, client=None) -> dict:
    """The action for a sentence. Always returns an action in the stored
    format, or a script the model wrote, still carrying its text in "code";
    `actions.NONE` when nothing fits and there is no model to write one."""
    sentence = sentence.strip()
    if client is not None and sentence:
        system = "Windows" if actions.WIN else "macOS" if actions.MAC else "Linux"
        shell = "PowerShell" if actions.WIN else "sh"
        messages = [{"role": "system", "content": SYSTEM.format(
                        catalog=catalog_text(), system=system, shell=shell)},
                    {"role": "user", "content": f"Request: {sentence}"}]
        try:
            for _ in range(2):                  # one retry, then the keyword rules
                reply = client.complete(messages)
                try:
                    return parse(reply)
                except ValueError as problem:
                    messages += [{"role": "assistant", "content": reply},
                                 {"role": "user", "content": f"{problem} Reply with the JSON "
                                                             "object only."}]
        except ModelError:
            pass
    return guess(sentence)
