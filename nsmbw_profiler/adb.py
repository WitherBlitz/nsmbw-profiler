"""Finding adb (or fetching Google's platform-tools) and talking to devices."""
from __future__ import annotations

import io
import os
import platform
import shutil
import stat
import subprocess
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import paths

# Google's official platform-tools. Their licence does not allow redistributing adb, so the app
# downloads it from Google on first use when no adb is installed.
PLATFORM_TOOLS_URL = {
    "Windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "Linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    "Darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
}
PLATFORM_TOOLS_TERMS = "https://developer.android.com/studio/terms"

ADB_NAME = "adb.exe" if os.name == "nt" else "adb"
# No console window flashing up for every adb call in the windowed app.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class AdbError(RuntimeError):
    pass


def candidates() -> list[Path]:
    found = [paths.data_dir() / "platform-tools" / ADB_NAME]
    on_path = shutil.which("adb")
    if on_path:
        found.append(Path(on_path))
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            found.append(Path(os.environ[var]) / "platform-tools" / ADB_NAME)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        found.append(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk" / "platform-tools" / ADB_NAME)
    home = Path.home()
    found += [home / "Android" / "Sdk" / "platform-tools" / ADB_NAME,
              home / "Library" / "Android" / "sdk" / "platform-tools" / ADB_NAME]
    return found


def find_adb() -> Path | None:
    for path in candidates():
        if path.is_file():
            return path
    return None


def download_platform_tools(progress=None) -> Path:
    """Downloads Google's platform-tools for this OS into the app's data folder; returns adb."""
    url = PLATFORM_TOOLS_URL.get(platform.system())
    if url is None:
        raise AdbError(f"No platform-tools download for {platform.system()}")
    request = urllib.request.Request(url, headers={"User-Agent": "nsmbw-android-profiler"})
    data = io.BytesIO()
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            data.write(chunk)
            if progress and total:
                progress(data.tell() / total)
    target = paths.data_dir()
    with zipfile.ZipFile(data) as archive:
        archive.extractall(target)
    adb = target / "platform-tools" / ADB_NAME
    if os.name != "nt":
        for name in ("adb", "fastboot"):
            tool = target / "platform-tools" / name
            if tool.exists():
                tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if not adb.is_file():
        raise AdbError("platform-tools downloaded, but adb is missing from it")
    return adb


@dataclass
class Device:
    serial: str
    state: str
    model: str = ""

    @property
    def label(self) -> str:
        name = self.model.replace("_", " ") or "Android device"
        note = {"device": "", "unauthorized": "  (tap Allow on the phone)",
                "offline": "  (offline: reconnect the cable)"}.get(self.state, f"  ({self.state})")
        return f"{name}  [{self.serial}]{note}"


class Adb:
    def __init__(self, path: Path):
        self.path = str(path)

    def run(self, args: list[str], serial: str | None = None, timeout: float = 60,
            stdin: bytes | None = None) -> subprocess.CompletedProcess:
        command = [self.path] + (["-s", serial] if serial else []) + args
        try:
            return subprocess.run(command, input=stdin, capture_output=True, timeout=timeout,
                                  creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired as error:
            raise AdbError(f"adb {' '.join(args)[:80]} timed out after {timeout:.0f} s") from error

    def devices(self) -> list[Device]:
        result = self.run(["devices", "-l"], timeout=20)
        devices = []
        for line in result.stdout.decode(errors="replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            model = next((p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), "")
            devices.append(Device(parts[0], parts[1], model))
        return devices

    def popen(self, args: list[str], serial: str, stdout) -> subprocess.Popen:
        return subprocess.Popen([self.path, "-s", serial] + args, stdout=stdout, stderr=subprocess.STDOUT,
                                creationflags=_NO_WINDOW)


class Phone:
    """One device: shell helpers with the serial filled in."""

    def __init__(self, adb: Adb, serial: str):
        self.adb = adb
        self.serial = serial

    def sh(self, command: str, timeout: float = 60, check: bool = False) -> str:
        result = self.adb.run(["shell", command], self.serial, timeout)
        text = result.stdout.decode(errors="replace").replace("\r\n", "\n")
        if check and result.returncode != 0:
            raise AdbError(f"'{command[:60]}' failed: {text.strip()[:300]} {result.stderr.decode(errors='replace')[:200]}")
        return text

    def sh_bytes(self, command: str, timeout: float = 60) -> bytes:
        return self.adb.run(["exec-out", command], self.serial, timeout).stdout

    def sh_stdin(self, command: str, data: bytes, timeout: float = 60) -> subprocess.CompletedProcess:
        return self.adb.run(["shell", command], self.serial, timeout, stdin=data)

    def push(self, local: Path, remote: str, timeout: float = 120) -> None:
        result = self.adb.run(["push", str(local), remote], self.serial, timeout)
        if result.returncode != 0:
            raise AdbError(f"push to {remote} failed: {result.stdout.decode(errors='replace')[-300:]}")

    def pull(self, remote: str, local: Path, timeout: float = 600) -> bool:
        local.parent.mkdir(parents=True, exist_ok=True)
        result = self.adb.run(["pull", remote, str(local)], self.serial, timeout)
        return result.returncode == 0

    def getprop(self, name: str) -> str:
        return self.sh(f"getprop {name}", 15).strip()

    def setprop(self, name: str, value: str) -> None:
        if "'" in value:
            raise ValueError("property values may not contain single quotes")
        self.sh(f"setprop {name} '{value}'", 15)

