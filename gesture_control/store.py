"""Settings on disk. Gestures and recorded data join this folder in file 03."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_DIR = "gesture_control"

# Starting values from files 02 and 03. All need tuning on a real camera.
DETECTION_DEFAULTS = {
    "start_speed": 1.2,
    "stop_speed": 0.6,
    "quiet_ms": 150,
    "min_dur_ms": 250,
    "max_dur_ms": 2500,
    "cooldown_ms": 800,
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


@dataclass
class Settings:
    camera_device: str = ""
    mirror: bool = True
    show_points: bool = True
    model_name: str = ""
    start_on_login: bool = False
    run_in_background: bool = False
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


def save_settings(settings: Settings) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)
