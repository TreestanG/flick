"""Settings and gestures on disk: settings.json, gestures.json and
gestures/<id>.py in the config folder. Recorded samples are in dataset.py."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_DIR = "gesture_control"

# Starting values from files 02 and 03. All need tuning on a real camera.
DETECTION_DEFAULTS = {
    "start_speed": 0.5,
    "stop_speed": 0.25,
    "quiet_ms": 150,
    "min_dur_ms": 250,
    "max_dur_ms": 2500,
    "cooldown_ms": 800,
    "complement_ms": 1500,      # after a gesture fires, its complements stay quiet this long
    "negatives_per_positive": 3,
}


def config_dir() -> Path:
    override = os.environ.get("GESTURE_CONTROL_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_DIR


def runtime_dir() -> Path:
    """Where the engine and the UI leave notes for each other (`engine.json`,
    `ui.json`, `engine.log`): a per-user temporary folder. Not the config
    folder, because some Python installs (the Microsoft Store one) quietly
    redirect files written under AppData to a private place the UI cannot see.
    `app/src-tauri/src/lib.rs` works out the same folder."""
    override = os.environ.get("GESTURE_CONTROL_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("TEMP") or tempfile.gettempdir()) / APP_DIR
    shared = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR")
    if shared:
        return Path(shared) / APP_DIR
    return Path("/tmp") / f"{APP_DIR}-{os.environ.get('USER', 'user')}"


@dataclass
class Settings:
    camera_device: str = ""
    mirror: bool = True
    show_points: bool = True
    model_name: str = ""
    start_on_login: bool = False
    run_in_background: bool = True     # off: the engine quits when the last window closes
    first_run_done: bool = False
    detection: dict = field(default_factory=lambda: dict(DETECTION_DEFAULTS))

    def to_dict(self) -> dict:
        return asdict(self)


def settings_path() -> Path:
    return config_dir() / "settings.json"


def load_settings() -> Settings:
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    known = {f.name for f in fields(Settings)}
    settings = Settings(**{k: v for k, v in raw.items() if k in known and k != "detection"})
    if isinstance(raw.get("detection"), dict):
        settings.detection.update(
            {k: v for k, v in raw["detection"].items() if k in DETECTION_DEFAULTS})
    return settings


def update_settings(settings: Settings, changes: dict) -> None:
    """Merge changed keys sent by a UI window (file 01) into `settings`.
    Unknown keys and values of the wrong kind are ignored, so a window can
    never write something the engine cannot load again."""
    for item in fields(Settings):
        if item.name not in changes or item.name == "detection":
            continue
        value = changes[item.name]
        if isinstance(value, bool if item.type == "bool" else str):
            setattr(settings, item.name, value)
    detection = changes.get("detection")
    if isinstance(detection, dict):
        for key, default in DETECTION_DEFAULTS.items():
            value = detection.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                settings.detection[key] = type(default)(value)


def save_settings(settings: Settings) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)


# gestures (file 03) -----------------------------------------------------

def gestures_path() -> Path:
    return config_dir() / "gestures.json"


def code_path(gesture_id: str) -> Path:
    return config_dir() / "gestures" / f"{gesture_id}.py"


def data_dir() -> Path:
    return config_dir() / "data"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_gestures() -> list[dict]:
    """The saved gestures: id, name, motion, parts, action (file 04's format)."""
    try:
        raw = json.loads(gestures_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    return [g for g in raw if isinstance(g, dict) and g.get("id") and g.get("name")]


def save_gestures(gestures: list[dict]) -> None:
    _write(gestures_path(), json.dumps(gestures, indent=2))


def load_code(gesture_id: str) -> str | None:
    try:
        return code_path(gesture_id).read_text(encoding="utf-8")
    except OSError:
        return None


def save_code(gesture_id: str, code: str) -> None:
    _write(code_path(gesture_id), code)


def delete_code(gesture_id: str) -> None:
    try:
        code_path(gesture_id).unlink()
    except OSError:
        pass
