"""Start on login, per platform. Nothing here runs until the user flips the switch."""

from __future__ import annotations

import os
import sys
from pathlib import Path

NAME = "GestureControl"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--background"]
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    if sys.platform == "win32" and windowless.exists():
        exe = windowless
    # Login items start in an arbitrary folder, so point Python at the package explicitly.
    root = Path(__file__).resolve().parent.parent
    code = f"import sys; sys.path.insert(0, {str(root)!r}); from gesture_control.__main__ import main; main()"
    return [str(exe), "-c", code, "--background"]


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.gesturecontrol.app.plist"


def _linux_desktop() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / "gesture-control.desktop"


def is_enabled() -> bool:
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, NAME)
            return True
        if sys.platform == "darwin":
            return _mac_plist().exists()
        return _linux_desktop().exists()
    except OSError:
        return False


def set_enabled(enabled: bool) -> str | None:
    """Turn start on login on or off. Returns an error message, or None."""
    try:
        if sys.platform == "win32":
            _set_windows(enabled)
        elif sys.platform == "darwin":
            _set_file(_mac_plist(), _plist_text() if enabled else None)
        else:
            _set_file(_linux_desktop(), _desktop_text() if enabled else None)
        return None
    except OSError as exc:
        return f"Could not change start on login: {exc}"


def _set_windows(enabled: bool) -> None:
    import subprocess
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(_command()))
        else:
            try:
                winreg.DeleteValue(key, NAME)
            except FileNotFoundError:
                pass


def _set_file(path: Path, text: str | None) -> None:
    if text is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _plist_text() -> str:
    args = "".join(f"    <string>{a}</string>\n" for a in _command())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        "  <key>Label</key><string>com.gesturecontrol.app</string>\n"
        f"  <key>ProgramArguments</key><array>\n{args}  </array>\n"
        "  <key>RunAtLoad</key><true/>\n"
        "</dict></plist>\n"
    )


def _desktop_text() -> str:
    exec_line = " ".join(f'"{a}"' for a in _command())
    return (
        "[Desktop Entry]\nType=Application\nName=Gesture Control\n"
        f"Exec={exec_line}\nX-GNOME-Autostart-enabled=true\n"
    )
