"""One capture: install the chosen build, learn this phone's boot timing, play each world's level 1 with
every profiler running, close the game, put it all in a zip."""
from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import threading
import time
import traceback
import xml.etree.ElementTree as ElementTree
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import apkinfo, bot, paths, releases, report
from .adb import Adb, AdbError, Phone
from .device_side import PACKAGE, SAMPLER, TMP, Perfetto
from .logwatch import GameLog, Tail

FILES = f"/sdcard/Android/data/{PACKAGE}/files"
LOGS = f"{FILES}/Logs"
SAVES = f"{FILES}/NAND/title/00010000"  # 534d4e45 (SMNE), 534d4e50 (SMNP), 534d4e4a (SMNJ)
OUR_PROPS = ("debug.nsmbw.warp", *bot.SCRIPT_PROPS, "debug.nsmbw.logfps", "debug.nsmbw.env")
PLAY_WORDS = {"Play", "Jugar", "Jouer", "Gioca", "Spielen", "Jogar"}
BASE_TAB, NEWER_TAB = "New Super Mario Bros. Wii", "Newer Super Mario Bros. Wii"
STAGE_LEAD_MS = 4000  # play starts this long after the course started in the calibration launch
MAP_WAIT_S = 10  # pressing 2 on the world map enters a course within a few presses, or never
PROFILE_PLAY_S = 25  # seconds of play inside the game's own profile window
ATTEMPTS = 3  # tries per level to get one with the game's profile


class Cancelled(Exception):
    pass


class RunError(Exception):
    """Stops the run, with a message for the player."""


@dataclass
class Options:
    serial: str
    version: str = "latest"  # "latest", "release" (the one below), "installed" or "file"
    release: releases.Release | None = None
    repo: str = releases.DEFAULT_REPO
    apk_file: Path | None = None
    worlds: list[int] = field(default_factory=lambda: list(range(1, 10)))
    play_seconds: int = 60
    trace: str = "detailed"  # Perfetto: "detailed", "light" or "off"


@dataclass
class Calibration:
    stage_ms: int
    menu: list[bot.Press]
    map_ms: int | None
    movie: bool


class Capture:
    def __init__(self, adb: Adb, options: Options, ui=lambda kind, payload: None):
        self.adb = adb
        self.o = options
        self.ui = ui
        self.phone = Phone(adb, options.serial)
        self.cancel = threading.Event()
        self.stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.work = paths.data_dir() / "work" / self.stamp
        self.out = self.work / f"NSMBW-profile-{self.stamp}"
        self.saved_props: dict[str, str] = {}
        self.restore_newer_tab = False
        self.timestats_on = False
        self.perfetto: Perfetto | None = None
        self.sampler_on = False
        self.logcat: subprocess.Popen | None = None
        self.logcat_file = None
        self.info: dict = {"levels": []}
        self.log_lines: list[str] = []
        self.save_hashes: dict[str, str] = {}
        self.save_backup: Path | None = None

    # ---- plumbing -------------------------------------------------------------------------------------

    def log(self, text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {text}"
        self.log_lines.append(line)
        self.ui("log", line)

    def step(self, text: str, fraction: float) -> None:
        self.ui("step", (text, fraction))
        self.log(text)

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while True:
            self.check()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.2, left))

    def save(self, relative: str, text: str | bytes) -> Path:
        path = self.out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8", errors="replace")
        return path

    def sh_save(self, relative: str, command: str, timeout: float = 60) -> str:
        try:
            text = self.phone.sh(command, timeout)
        except AdbError as error:
            text = f"(failed: {error})"
        self.save(relative, text)
        return text

    # ---- the run --------------------------------------------------------------------------------------

    def run(self) -> Path | None:
        status = "complete"
        try:
            self.out.mkdir(parents=True, exist_ok=True)
            self._run()
        except Cancelled:
            status = "stopped before the end"
            self.log("Stopped.")
        except RunError as error:
            status = f"stopped: {error}"
            self.log(f"Stopped: {error}")
            self.ui("error", str(error))
        except Exception as error:  # noqa: BLE001 - anything else still gets cleaned up and zipped
            status = f"failed: {error}"
            self.log("Unexpected error:\n" + traceback.format_exc())
            self.ui("error", f"Unexpected error: {error}")
        finally:
            self._cleanup()
        try:
            return self._finish(status)
        except Exception:  # noqa: BLE001
            self.log("Writing the zip failed:\n" + traceback.format_exc())
            self.ui("error", "Writing the zip failed; the files are in " + str(self.out))
            return None

    def _run(self) -> None:
        self.step("Checking the phone", 0.01)
        self._preflight()
        self.step("Getting the game version ready", 0.03)
        self._install()
        self.step("Recording the phone's hardware and settings", 0.08)
        self._device_info()
        self._start_logcat()
        self.step("Learning how long this phone takes to reach a level (one quick launch)", 0.1)
        calibration = self._calibrate()
        worlds = self.o.worlds
        for i, world in enumerate(worlds):
            self.step(f"Playing {world}-1 ({i + 1} of {len(worlds)})", 0.15 + 0.8 * i / len(worlds))
            self._level(world, calibration, i + 1)
        self.step("Closing the game", 0.96)

    # ---- before playing -------------------------------------------------------------------------------

    def _preflight(self) -> None:
        state = next((d.state for d in self.adb.devices() if d.serial == self.o.serial), None)
        if state is None:
            raise RunError("The phone isn't connected any more. Plug it back in and press Start again.")
        if state == "unauthorized":
            raise RunError("The phone hasn't allowed this computer yet: unlock it and tap Allow on the USB debugging prompt.")
        if state != "device":
            raise RunError(f"The phone is {state}. Reconnect the cable and try again.")
        model = self.phone.getprop("ro.product.model")
        sdk = int(self.phone.getprop("ro.build.version.sdk") or 0)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or "phone"
        renamed = self.work / f"NSMBW-profile-{safe}-{self.stamp}"
        self.out.rename(renamed)
        self.out = renamed
        self.info["device"] = {"model": model, "serial": self.o.serial, "sdk": sdk}
        self.log(f"Phone: {self.phone.getprop('ro.product.manufacturer')} {model}, Android "
                 f"{self.phone.getprop('ro.build.version.release')} (SDK {sdk})")
        if sdk and sdk < 28:
            raise RunError("NSMBW needs Android 9 or newer.")
        if "package:" not in self.phone.sh(f"pm path {PACKAGE}", 20):
            raise RunError("NSMBW isn't installed on this phone. Install it, open it and import your game, then run this again.")
        listing = self.phone.sh(f"ls {FILES}/game 2>&1 | head -5", 20)
        if "No such file" in listing or not listing.strip():
            raise RunError("NSMBW has no game imported yet: open NSMBW, import your game (its Help button explains how), then run this again.")
        if "Permission denied" in listing:
            self.log("Note: this phone doesn't let adb look into the game's folder; logs may be missing.")
        self._back_up_save()
        for prop in OUR_PROPS:
            self.saved_props[prop] = self.phone.getprop(prop)
        left_over = {k: v for k, v in self.saved_props.items() if v}
        if left_over:
            self.log(f"These test properties were already set and will be put back afterwards: {left_over}")
        if self.phone.sh(f"pidof {PACKAGE}:game", 10).strip():
            self.log("NSMBW was open: it will be closed now.")
        self.phone.sh(f"am force-stop {PACKAGE}", 20)
        self.phone.sh(f"rm -rf {TMP}; mkdir -p {TMP}", 20)

    def _save_hashes(self) -> dict[str, str]:
        text = self.phone.sh(f"for f in {SAVES}/534d4e*/data/*; do [ -f \"$f\" ] && sha256sum \"$f\"; done 2>/dev/null", 60)
        pairs = (line.split(None, 1) for line in text.splitlines())
        return {path.strip(): digest for digest, path in (p for p in pairs if len(p) == 2)}

    def _back_up_save(self) -> None:
        """A copy of the game's save files on this computer (only read from the phone), and their hashes, so
        the end of the run can say whether they changed."""
        self.save_hashes = self._save_hashes()
        self.save_backup = paths.captures_dir() / f"{self.out.name}-save-backup"
        for remote in self.save_hashes:
            self.phone.pull(remote, self.save_backup / remote.split("/title/", 1)[-1])
        if self.save_hashes:
            self.log(f"Backed up your save ({len(self.save_hashes)} files) to {self.save_backup}")
        else:
            self.log("No NSMBW save files found to back up.")

    def _check_save(self) -> None:
        if not self.save_hashes:
            return
        after = self._save_hashes()
        unchanged = after == self.save_hashes
        self.info["save_unchanged"] = unchanged
        if unchanged:
            self.log("Your save files are exactly as they were before the run.")
        else:
            changed = sorted(set(after.items()) ^ set(self.save_hashes.items()))
            self.log(f"WARNING: the save files changed during the run: {changed}. The copy from before the run is in "
                     f"{self.save_backup}")
            self.ui("error", f"The game's save files changed during the run. A copy from before it is in {self.save_backup}")

    def _installed(self) -> dict:
        text = self.phone.sh(f"dumpsys package {PACKAGE}", 30)
        name = re.search(r"versionName=(\S+)", text)
        code = re.search(r"versionCode=(\d+)", text)
        path = self.phone.sh(f"pm path {PACKAGE}", 20).strip().splitlines()
        apk = next((p[len("package:"):] for p in path if p.endswith("base.apk")), path[0][len("package:"):] if path else "")
        return {"versionName": name.group(1) if name else "", "versionCode": code.group(1) if code else "",
                "apk_path": apk}

    def _remote_sha256(self, path: str) -> str:
        return (self.phone.sh(f"sha256sum '{path}'", 120).split() or [""])[0]

    def _install(self) -> None:
        before = self._installed()
        self.log(f"Installed now: NSMBW {before['versionName']} (code {before['versionCode']})")
        build = {"installed_before": before["versionName"], "source": self.o.version}
        local_apk = None
        if self.o.version in ("latest", "release"):
            try:
                if self.o.version == "latest":
                    self.log("Asking github.com which release is the newest")
                    release = releases.latest(self.o.repo)
                else:
                    release = self.o.release
                build["release"] = release.tag
                if releases.cached_apk(release):
                    self.log(f"Using {release.apk_name}, downloaded before")
                else:
                    self.log(f"Downloading {release.apk_name} from github.com (it's kept for next time)")
                local_apk = releases.download_apk(
                    release, self.o.repo,
                    lambda f: self.ui("step", (f"Downloading {release.tag}: {f * 100:.0f}%", 0.03 + 0.03 * f)),
                    self.cancel.is_set)
            except releases.NoApk as error:
                raise RunError(f"{error}. Pick another version.") from error
            except InterruptedError as error:
                raise Cancelled() from error
            except (OSError, ValueError) as error:
                raise RunError(f"Couldn't get the NSMBW release from github.com ({error}). Check the internet "
                               "connection and press Start again, or pick 'Installed version' to test what's on the "
                               "phone.") from error
        elif self.o.version == "file":
            local_apk = self.o.apk_file
            build["apk_file"] = local_apk.name
        if local_apk is not None:
            sha = releases.sha256_file(local_apk)
            build["apk_sha256"] = sha
            if before["apk_path"] and self._remote_sha256(before["apk_path"]) == sha:
                self.log("That exact APK is already installed; not reinstalling.")
            else:
                self.step("Installing it (your saves, settings and game files are kept). "
                          "If the phone asks whether to allow or scan the app, allow it.", 0.06)
                result = self.adb.run(["install", "-r", str(local_apk)], self.o.serial, 900)
                text = (result.stdout + result.stderr).decode(errors="replace")
                self.log(text.strip()[-500:])
                if "Success" not in text:
                    if "VERSION_DOWNGRADE" in text:
                        raise RunError(f"This phone has a newer NSMBW ({before['versionName']}) than the one picked. Android only "
                                       "goes back to an older version by uninstalling, which deletes the game's data, so pick "
                                       f"{before['versionName']} or newer (or 'Installed version').")
                    if "UPDATE_INCOMPATIBLE" in text or "SIGNATURES" in text:
                        raise RunError("The installed NSMBW was signed by someone else, so this build can't update it "
                                       "without uninstalling (which deletes the game's data).")
                    raise RunError("Installing failed: " + text.strip()[-300:])
            try:
                build["libmain_build_id"] = apkinfo.build_id(apkinfo.LocalFile(local_apk))
            except Exception as error:  # noqa: BLE001
                build["libmain_build_id"] = f"(unknown: {error})"
        after = self._installed()
        build.update(versionName=after["versionName"], versionCode=after["versionCode"])
        if "apk_sha256" not in build:
            build["apk_sha256"] = self._remote_sha256(after["apk_path"])
        if "libmain_build_id" not in build:
            try:
                build["libmain_build_id"] = apkinfo.build_id(apkinfo.RemoteFile(self.phone, after["apk_path"]))
            except Exception as error:  # noqa: BLE001
                build["libmain_build_id"] = f"(unknown: {error})"
        self.info["build"] = build
        self.log(f"Testing NSMBW {build['versionName']} (code {build['versionCode']}), libmain.so build ID "
                 f"{build['libmain_build_id']}")
        self.save("game-build.txt", "\n".join(f"{k}: {v}" for k, v in build.items()) + "\n")

    def _device_info(self) -> None:
        commands = {
            "getprop.txt": "getprop",
            "uname.txt": "uname -a; cat /proc/version",
            "cpuinfo.txt": "cat /proc/cpuinfo",
            "meminfo.txt": "cat /proc/meminfo",
            "cpufreq.txt": "for p in /sys/devices/system/cpu/cpufreq/policy*; do echo \"== $p\"; for f in related_cpus "
                           "cpuinfo_min_freq cpuinfo_max_freq scaling_min_freq scaling_max_freq scaling_governor "
                           "scaling_available_frequencies; do echo \"$f: $(cat $p/$f 2>/dev/null)\"; done; done",
            "gpu-sysfs.txt": "for f in /sys/kernel/gpu/* /sys/class/misc/mali0/device/* /sys/class/kgsl/kgsl-3d0/*; do "
                             "[ -f \"$f\" ] && [ -r \"$f\" ] && echo \"$f: $(head -c 400 \"$f\" 2>/dev/null | tr '\\n' ' ')\"; "
                             "done 2>/dev/null",
            "thermal-zones.txt": "for z in /sys/class/thermal/thermal_zone* /sys/class/thermal/cooling_device*; do "
                                 "echo \"$z $(cat $z/type 2>/dev/null) $(cat $z/temp 2>/dev/null)$(cat $z/cur_state "
                                 "2>/dev/null)\"; done",
            "vulkan.json": "cmd gpu vkjson",
            "dumpsys-gpu.txt": "dumpsys gpu",
            "dumpsys-surfaceflinger.txt": "dumpsys SurfaceFlinger",
            "dumpsys-display.txt": "dumpsys display",
            "dumpsys-thermalservice-start.txt": "dumpsys thermalservice",
            "dumpsys-battery-start.txt": "dumpsys battery",
            "dumpsys-power.txt": "dumpsys power",
            "dumpsys-game.txt": "dumpsys game",
            "dumpsys-package.txt": f"dumpsys package {PACKAGE}",
            "services.txt": "dumpsys -l",
            "screen.txt": "wm size; wm density",
            "clk_tck.txt": "getconf CLK_TCK",
            "game-files.txt": f"ls -la {FILES} {FILES}/game 2>&1",
            "game-Config.toml": f"cat {FILES}/Config.toml",
        }
        for name, command in commands.items():
            self.check()
            self.sh_save(f"device/{name}", command, 90)
        props = (self.out / "device" / "getprop.txt").read_text(encoding="utf-8", errors="replace")

        def prop(name: str) -> str:
            m = re.search(r"^\[" + re.escape(name) + r"\]: \[(.*)\]$", props, re.M)
            return m.group(1) if m else ""

        self.info["device"].update(
            manufacturer=prop("ro.product.manufacturer"), android=prop("ro.build.version.release"),
            soc=" ".join(x for x in (prop("ro.soc.manufacturer"), prop("ro.soc.model")) if x),
            platform=prop("ro.board.platform") or prop("ro.hardware"), fingerprint=prop("ro.build.fingerprint"))
        (self.work / "sampler.sh").write_bytes(SAMPLER.encode())
        self.phone.push(self.work / "sampler.sh", f"{TMP}/sampler.sh")

    def _start_logcat(self) -> None:
        self.logcat_file = open(self.out / "logcat.txt", "wb")
        self.logcat = self.adb.popen(["logcat", "-v", "threadtime", "-T", "1"], self.o.serial, self.logcat_file)

    # ---- launching the game ---------------------------------------------------------------------------

    def _ui_nodes(self) -> list[dict]:
        text = self.phone.sh(f"uiautomator dump {TMP}/ui.xml >/dev/null 2>&1; cat {TMP}/ui.xml", 40)
        start = text.find("<?xml")
        if start < 0:
            return []
        try:
            root = ElementTree.fromstring(text[start:].strip())
        except ElementTree.ParseError:
            return []
        nodes = []
        for node in root.iter("node"):
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                nodes.append({"text": node.get("text", ""), "x": (x1 + x2) // 2, "y": (y1 + y2) // 2,
                              "area": (x2 - x1) * (y2 - y1), "pkg": node.get("package", "")})
        return nodes

    def _tap(self, node: dict) -> None:
        self.phone.sh(f"input tap {node['x']} {node['y']}", 15)

    def _resumed(self) -> str:
        text = self.phone.sh("dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity'", 20)
        return text.strip()

    def _launch(self) -> tuple[str, str]:
        """Starts the game from the start menu's Play; returns its run's Logs folder and its pid."""
        self.phone.sh(f"am force-stop {PACKAGE}", 20)
        self.phone.sh("input keyevent KEYCODE_WAKEUP", 10)
        started = int(self.phone.sh("date +%s", 10).strip() or 0)
        self.phone.sh(f"am start -W -n {PACKAGE}/.LauncherActivity --ez showStartMenu true", 40)
        play = None
        asked_unlock = False
        for _ in range(240):
            self.check()
            resumed = self._resumed()
            if resumed and "LauncherActivity" not in resumed:
                if not asked_unlock:
                    asked_unlock = True
                    self.ui("attention", "Unlock the phone, please: the run continues by itself once it's unlocked.")
                    self.log("Waiting for the phone to be unlocked")
                self.sleep(1)
                self.phone.sh(f"am start -n {PACKAGE}/.LauncherActivity --ez showStartMenu true", 30)
                self.sleep(1)
                continue
            nodes = self._ui_nodes()
            subtitle_newer = any(NEWER_TAB in n["text"] and n["text"] != NEWER_TAB for n in nodes)
            base_tab = next((n for n in nodes if n["text"] == BASE_TAB), None)
            if subtitle_newer and base_tab:
                self.log("The start menu is on the Newer tab: picking New Super Mario Bros. Wii for the run "
                         "(the Newer tab is picked again at the end).")
                self.restore_newer_tab = True
                self._tap(base_tab)
                self.sleep(1.5)
                continue
            plays = [n for n in nodes if n["text"].strip() in PLAY_WORDS and n["pkg"] == PACKAGE]
            if plays:
                play = max(plays, key=lambda n: n["area"])
                break
            self.sleep(0.5)
        if asked_unlock:
            self.ui("attention", "")
        if play is None:
            self._screenshot("launch-failed.png")
            raise RunError("Couldn't find the Play button on NSMBW's start menu (a screenshot is in the zip).")
        self._tap(play)
        for _ in range(90):
            self.check()
            runs = []
            for name in self.phone.sh(f"ls {LOGS} 2>/dev/null", 15).split():
                m = re.match(r".+_(\d+)_pid(\d+)$", name)
                if m and int(m.group(1)) >= started - 3:
                    runs.append((int(m.group(1)), name, m.group(2)))
            if runs:
                _, name, pid = max(runs)
                return f"{LOGS}/{name}", pid
            self.sleep(0.5)
        self._screenshot("launch-failed.png")
        raise RunError("The game didn't start after tapping Play (a screenshot is in the zip).")

    def _set_props(self, warp: str, script: str, env: str) -> None:
        values = dict(zip(bot.SCRIPT_PROPS, bot.split_props(script)))
        values.update({"debug.nsmbw.warp": warp, "debug.nsmbw.logfps": "1", "debug.nsmbw.env": env})
        if len(env) > bot.PROP_CHARS:
            raise ValueError("debug.nsmbw.env too long")
        for name, value in values.items():
            self.phone.setprop(name, value)

    def _alive(self, pid: str) -> bool:
        return bool(self.phone.sh(f"[ -d /proc/{pid} ] && echo yes", 10).strip())

    def _screenshot(self, relative: str) -> None:
        try:
            data = self.phone.sh_bytes("screencap -p", 30)
            if data.startswith(b"\x89PNG"):
                self.save(relative, data)
        except AdbError:
            pass

    # ---- calibration ----------------------------------------------------------------------------------

    def _calibrate(self) -> Calibration:
        """One launch pressing only 2 until the warped 1-1 starts: how long this phone takes to get there."""
        name = "00-calibration"
        presses = bot.menu_presses(170_000)
        script = bot.serialize(presses)
        self._set_props("1-1", script, "NSMBW_LOG_AUDIO_TAGS=1")
        self.save(f"{name}/script.txt", script + "\n")
        remote, pid = self._launch()
        launched = time.time()
        tail = Tail(self.phone, f"{remote}/console.log")
        game = GameLog()
        last_alive = launched
        try:
            while True:
                self.check()
                game.feed(tail.lines())
                now = time.time()
                if game.guard:
                    self.phone.sh(f"am force-stop {PACKAGE}", 20)
                    if game.resumed_course:
                        raise RunError("Your save starts inside a level (it was saved part-way through one). Open NSMBW, "
                                       "finish or leave that level so you're on the world map, save (+ menu, Save), close "
                                       "the game and run this again.")
                    if game.movie:
                        raise RunError("The last save slot used in NSMBW is empty (pressing 2 started a new game). Open "
                                       "NSMBW, play your own file until you're on its world map, then run this again.")
                    raise RunError(f"Stopped: {game.guard}.")
                if game.stage_at is not None and game.script_ms(game.stage_at) is not None:
                    stage_ms = game.script_ms(game.stage_at)
                    map_ms = game.script_ms(game.map_seen_at) if game.map_seen_at else None
                    self.log(f"This phone reaches the world map {map_ms / 1000:.0f} s and a level {stage_ms / 1000:.0f} s "
                             "after the game starts reading input." if map_ms else
                             f"This phone reaches a level {stage_ms / 1000:.0f} s after the game starts reading input.")
                    return Calibration(stage_ms, bot.trim(presses, stage_ms), map_ms, game.movie)
                if game.map_seen_at and not game.crsin_seen_at and now - game.map_seen_at > MAP_WAIT_S:
                    # 2 doesn't enter anything where Mario stands. Moving him there could fire a cannon or reach a
                    # prompt that saves, so the player does it.
                    self._screenshot(f"{name}/map.png")
                    self.phone.sh(f"am force-stop {PACKAGE}", 20)
                    raise RunError("Mario isn't standing on a level on the world map (a screenshot is in the zip). Open "
                                   "NSMBW, walk him onto any level (not a Toad house or cannon), save (+ menu, Save), "
                                   "close the game and run this again.")
                if now - launched > 200:
                    self._screenshot(f"{name}/stuck.png")
                    raise RunError("The game didn't get from its title screen to a level within 3 minutes (a screenshot "
                                   "is in the zip). Is the right save file the last one used?")
                if now - last_alive > 5:
                    last_alive = now
                    if not self._alive(pid):
                        raise RunError("The game closed by itself while starting (its log is in the zip).")
                self.sleep(0.3)
        finally:
            self.phone.sh(f"am force-stop {PACKAGE}", 20)
            self.phone.pull(remote, self.out / name)

    # ---- one level ------------------------------------------------------------------------------------

    def _level(self, world: int, calibration: Calibration, index: int) -> None:
        """Plays W-1; a try that ends early (a lost life closes the game at once) is played again, up to
        ATTEMPTS tries, until one has the game's own profile."""
        for attempt in range(1, ATTEMPTS + 1):
            result = self._level_try(world, calibration, index, attempt)
            if result["profile_samples"] or result["error"] == "didn't reach the level":
                return
            if attempt < ATTEMPTS:
                self.log(f"{world}-1 ended before the game's profile was written: playing it again "
                         f"(try {attempt + 1} of {ATTEMPTS}).")

    def _level_try(self, world: int, calibration: Calibration, index: int, attempt: int) -> dict:
        name = f"{index:02d}-level-{world}-1" + (f"-try{attempt}" if attempt > 1 else "")
        W = self.o.play_seconds
        play_start = calibration.stage_ms + STAGE_LEAD_MS
        menu = bot.trim(calibration.menu, play_start - 1000)
        budget = int((bot.MAX_RIGHT_HOLD_S - bot.right_hold_seconds(menu)) * 1000)
        presses = menu + bot.play_presses(play_start, W, budget)
        assert bot.right_hold_seconds(presses) <= bot.MAX_RIGHT_HOLD_S
        script = bot.serialize(presses)
        # The game writes its profile only when its window ends, and a lost life closes the game at once, so
        # the window covers the boot and the first PROFILE_PLAY_S seconds of play, not all of it.
        profile_len = int(play_start / 1000 + min(W, PROFILE_PLAY_S))
        env = f"NSMBW_PROFILE=0:{profile_len};NSMBW_LOG_AUDIO_TAGS=1"
        result = {"level": f"{world}-1", "folder": name, "attempt": attempt, "entered": False, "guard": "",
                  "error": "", "profile_samples": None, "play_seconds_planned": W}
        self.info["levels"].append(result)
        self.save(f"{name}/script.txt", f"debug.nsmbw.warp={world}-1\ndebug.nsmbw.env={env}\n"
                  f"NSMBW_INPUT_SCRIPT={script}\n")
        self.sh_save(f"{name}/battery-start.txt", "dumpsys battery")
        self.sh_save(f"{name}/thermal-start.txt", "dumpsys thermalservice")
        self._set_props(f"{world}-1", script, env)
        self._start_sampler(name)
        if self.o.trace != "off":
            self.perfetto = Perfetto(self.phone, self.log)
            if self.perfetto.start(name, self.o.trace):
                self.save(f"{name}/perfetto-config.txt", self.perfetto.config_used)
            else:
                self.perfetto = None
        remote, pid = "", ""
        try:
            remote, pid = self._launch()
            launched = time.time()
            result["pid"] = pid
            tail = Tail(self.phone, f"{remote}/console.log")
            game = GameLog()
            in_course = mid_done = False
            last_alive = launched
            while True:
                self.check()
                game.feed(tail.lines())
                now = time.time()
                if game.guard:
                    # Out of the course: close the game before a world map can ask to save (or it autosaves).
                    self.phone.sh(f"am force-stop {PACKAGE}", 20)
                    result["guard"] = game.guard
                    self.log(f"Closed the game at once: {game.guard}.")
                    break
                if game.stage_at is not None and not in_course:
                    in_course = True
                    result["entered"] = game.warp == f"{world}-1"
                    self.log(f"In {game.warp or '?'}: playing for {W} s")
                    self._course_started(name, pid)
                if in_course and not mid_done and now - game.stage_at > min(20, W / 2):
                    mid_done = True
                    self._mid_level(name, pid)
                if game.stacks is not None and result["profile_samples"] is None:
                    result["profile_samples"] = game.stacks
                    self.log(f"The game's profile is written ({game.stacks} samples)")
                if in_course and now - game.stage_at >= W and (game.stacks is not None or now - game.stage_at > W + 30):
                    break
                if not in_course and now - launched > play_start / 1000 + 90:
                    result["error"] = "didn't reach the level"
                    self._screenshot(f"{name}/stuck.png")
                    self.log(f"{world}-1: the level didn't start in time (screenshot saved); going on.")
                    break
                if now - launched > play_start / 1000 + W + 150:
                    result["error"] = "timed out"
                    break
                if now - last_alive > 5:
                    last_alive = now
                    if not self._alive(pid):
                        result["error"] = "the game closed by itself (crash?)"
                        self.log(f"{world}-1: the game closed by itself; its logs are kept.")
                        break
                self.sleep(0.3)
            if in_course and game.stage_at is not None:
                result["play_seconds"] = round(time.time() - game.stage_at, 1)
            if not result["guard"] and pid and self._alive(pid):
                self.save(f"{name}/threads-end.txt", self._threads(pid))
                self.sh_save(f"{name}/gfxinfo.txt", f"dumpsys gfxinfo {pid} framestats")
        finally:
            if self.timestats_on:
                self.sh_save(f"{name}/surfaceflinger-timestats.txt", "dumpsys SurfaceFlinger --timestats -dump")
                self.phone.sh("dumpsys SurfaceFlinger --timestats -disable -clear", 20)
                self.timestats_on = False
            self.phone.sh(f"am force-stop {PACKAGE}", 20)
            if self.perfetto:
                self.step(f"Saving {world}-1's system trace", None)
                self.perfetto.stop()
                if not self.perfetto.pull(self.out / name / "perfetto-trace.pftrace"):
                    self.log("The Perfetto trace couldn't be copied off the phone.")
                self.perfetto = None
            self._stop_sampler(name)
            if remote:
                self.phone.pull(remote, self.out / name)
            self.sh_save(f"{name}/battery-end.txt", "dumpsys battery")
            self.sh_save(f"{name}/thermal-end.txt", "dumpsys thermalservice")
        return result

    def _threads(self, pid: str) -> str:
        return self.phone.sh(f"for t in /proc/{pid}/task/*; do read -r l < $t/stat && echo \"$l\"; done 2>/dev/null", 30)

    def _course_started(self, name: str, pid: str) -> None:
        self.save(f"{name}/threads-start.txt", self._threads(pid))
        self.phone.sh("dumpsys SurfaceFlinger --timestats -clear -enable", 20)
        self.timestats_on = True
        self.phone.sh(f"dumpsys gfxinfo {pid} reset", 20)

    def _mid_level(self, name: str, pid: str) -> None:
        self._screenshot(f"{name}/screenshot.png")
        self.sh_save(f"{name}/meminfo.txt", f"dumpsys meminfo {pid}")
        self.sh_save(f"{name}/top-threads.txt", f"top -H -b -n 1 -p {pid}")

    def _start_sampler(self, name: str) -> None:
        self.phone.sh(f"rm -f {TMP}/stop {TMP}/sampler.tsv {TMP}/labels.txt {TMP}/hal.txt", 10)
        self.phone.sh(f"nohup sh {TMP}/sampler.sh {TMP}/sampler.tsv 0.5 {TMP}/stop {TMP}/labels.txt {TMP}/hal.txt "
                      ">/dev/null 2>&1 </dev/null &", 15)
        self.sampler_on = True

    def _stop_sampler(self, name: str) -> None:
        if not self.sampler_on:
            return
        self.phone.sh(f"touch {TMP}/stop", 10)
        time.sleep(1.2)
        self.sampler_on = False
        self.phone.pull(f"{TMP}/sampler.tsv", self.out / name / "sampler.tsv")
        self.phone.pull(f"{TMP}/labels.txt", self.out / name / "sampler-labels.txt")
        self.phone.pull(f"{TMP}/hal.txt", self.out / name / "thermal-hal.txt")

    # ---- afterwards -----------------------------------------------------------------------------------

    def _cleanup(self) -> None:
        """Always runs: the game closed, every test property back as it was, nothing of ours left on the phone."""
        steps = [
            ("closing the game", lambda: self.phone.sh(f"am force-stop {PACKAGE}", 20)),
            ("stopping the system trace", lambda: self.perfetto and self.perfetto.stop()),
            ("stopping SurfaceFlinger's frame stats",
             lambda: self.timestats_on and self.phone.sh("dumpsys SurfaceFlinger --timestats -disable -clear", 20)),
            ("stopping the sampler", lambda: self.phone.sh(f"touch {TMP}/stop", 10)),
            ("putting the test properties back", self._restore_props),
            ("putting the start menu back on the Newer tab", self._restore_tab),
            ("removing the tool's files from the phone",
             lambda: self.phone.sh(f"sleep 1; rm -rf {TMP} /data/misc/perfetto-traces/nsmbw-prof-*", 20)),
            ("checking the save files", self._check_save),
        ]
        for label, action in steps:
            try:
                action()
            except Exception as error:  # noqa: BLE001
                self.log(f"While {label}: {error}")
        if self.logcat:
            self.logcat.terminate()
            try:
                self.logcat.wait(5)
            except subprocess.TimeoutExpired:
                self.logcat.kill()
        if self.logcat_file:
            self.logcat_file.close()

    def _restore_props(self) -> None:
        for prop in OUR_PROPS:
            self.phone.setprop(prop, self.saved_props.get(prop, ""))
        left = {p: self.phone.getprop(p) for p in OUR_PROPS}
        wrong = {p: v for p, v in left.items() if v != self.saved_props.get(p, "")}
        self.log("Test properties restored." if not wrong else f"Properties not restored: {wrong}")

    def _restore_tab(self) -> None:
        if not self.restore_newer_tab:
            return
        self.phone.sh(f"am start -W -n {PACKAGE}/.LauncherActivity --ez showStartMenu true", 40)
        time.sleep(2)
        tab = next((n for n in self._ui_nodes() if n["text"] == NEWER_TAB), None)
        if tab:
            self._tap(tab)
            time.sleep(1)
            self.log("Start menu back on the Newer tab.")
        self.phone.sh("input keyevent KEYCODE_HOME", 10)
        self.phone.sh(f"am force-stop {PACKAGE}", 20)

    def _finish(self, status: str) -> Path:
        self.ui("step", ("Writing the summary and the zip", 0.98))
        self.info["status"] = status
        self.info["options"] = {"worlds": self.o.worlds, "play_seconds": self.o.play_seconds,
                                "trace": self.o.trace, "version": self.o.version}
        try:
            report.write(self.out, self.info)
        except Exception:  # noqa: BLE001
            self.log("Writing the summary failed:\n" + traceback.format_exc())
        self.save("run.log", "\n".join(self.log_lines) + "\n")
        zip_path = paths.captures_dir() / f"{self.out.name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(self.out.rglob("*")):
                if path.is_file():
                    method = zipfile.ZIP_STORED if path.suffix == ".png" else zipfile.ZIP_DEFLATED
                    archive.write(path, f"{self.out.name}/{path.relative_to(self.out).as_posix()}", method)
        shutil.rmtree(self.work, ignore_errors=True)
        self.ui("done", (str(zip_path), status))
        return zip_path
