"""Where the app keeps its downloads (adb, APKs) and its finished captures."""
from __future__ import annotations

import os
import platform
from pathlib import Path

APP_NAME = "NSMBW Profiler"


def data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        path = base / APP_NAME
    elif system == "Darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        path = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "nsmbw-profiler"
    path.mkdir(parents=True, exist_ok=True)
    return path


def apk_cache() -> Path:
    path = data_dir() / "apks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def captures_dir() -> Path:
    """Finished zips land here; the Save .zip button copies one wherever the player wants."""
    documents = Path.home() / "Documents"
    path = (documents if documents.is_dir() else Path.home()) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path
