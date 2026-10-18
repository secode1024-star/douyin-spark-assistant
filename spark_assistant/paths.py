"""Separate immutable application resources from per-user private state."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def resource_path(name: str = "") -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / name


def data_directory() -> Path:
    override = os.environ.get("SPARK_ASSISTANT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "SparkAssistant"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/SparkAssistant"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "SparkAssistant"


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profiles/default"

    @property
    def resource_dir(self) -> Path:
        return resource_path()

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(exist_ok=True)
        if os.name != "nt":
            self.data_dir.chmod(0o700)


def configure_browser() -> None:
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        # Chromium is copied intact after PyInstaller builds the outer .app. It
        # deliberately bypasses PyInstaller's Frameworks/Resources relocation.
        app_browsers = Path(sys.executable).resolve().parent.parent / "Resources/browsers"
        if app_browsers.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(app_browsers)
            return
    bundled = resource_path("browsers")
    development = resource_path(".local/browsers")
    if bundled.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
    elif development.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(development))
