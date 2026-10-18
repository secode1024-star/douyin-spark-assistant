"""Opt-in login startup; no service, privileged helper or network access."""
from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

NAME = "SparkAssistant"
LABEL = "org.sparkassistant.desktop"


def _command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--tray"]
    executable = Path(sys.executable)
    if sys.platform == "win32" and executable.with_name("pythonw.exe").exists():
        executable = executable.with_name("pythonw.exe")
    return [str(executable), str(Path(__file__).resolve().parent.parent / "desktop_main.py"), "--tray"]


def _agent_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def is_enabled() -> bool:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                return winreg.QueryValueEx(key, NAME)[0] == subprocess.list2cmdline(_command())
        except OSError:
            return False
    if sys.platform == "darwin":
        return _agent_path().is_file()
    return False


def set_enabled(enabled: bool) -> None:
    if sys.platform == "win32":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            if enabled:
                winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(_command()))
            else:
                try:
                    winreg.DeleteValue(key, NAME)
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        path = _agent_path()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": _command(),
                                           "RunAtLoad": True, "ProcessType": "Interactive"}))
        else:
            path.unlink(missing_ok=True)
    elif enabled:
        raise RuntimeError("当前系统暂不支持自动配置登录启动。")
