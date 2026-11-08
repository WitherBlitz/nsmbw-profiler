"""Following the game's console.log on the phone while it runs, and what its lines mean."""
from __future__ import annotations

import re
import time

SCENES = {0x0: "BOOT", 0x1: "AUTO_SELECT", 0x2: "SELECT", 0x3: "WORLD_MAP", 0x4: "WORLD_9_DEMO", 0x5: "STAGE",
          0x6: "RESTART_CRSIN", 0x7: "CRSIN", 0x8: "MOVIE", 0x9: "GAMEOVER", 0xA: "GAME_SETUP"}
WORLD_MAP, STAGE, RESTART_CRSIN, CRSIN, MOVIE, GAMEOVER, GAME_SETUP = 0x3, 0x5, 0x6, 0x7, 0x8, 0x9, 0xA

# Sound IDs (the game's SOUND_e) of a course's goal: the flag, Mario/Luigi/Toads grabbing the pole, the
# fireworks. Logged with NSMBW_LOG_AUDIO_TAGS (0.4.1 and later) seconds before the course ends.
GOAL_SOUNDS = {176, 832, 903, 974, 1044, 592, 2016}

RE_SCENE = re.compile(r"scene -> 0x([0-9A-Fa-f]+) (\S*) \(frame (\d+)\)")
RE_WARP = re.compile(r"warp: course (\d+)-(\d+)")
RE_SCRIPT = re.compile(r"scripted input at (\d+) ms")
RE_SOUND = re.compile(r"voice \d+: sound (\d+) on player")
RE_LEFT = re.compile(r"autosave: course left \(exit mode (-?\d+)\)")
RE_STACKS = re.compile(r"profile: (\d+) samples; stacks written")


class Tail:
    """New bytes of a growing file on the phone, read with dd from where the last read stopped."""

    def __init__(self, phone, path: str):
        self.phone = phone
        self.path = path
        self.offset = 0
        self.partial = ""

    def lines(self) -> list[str]:
        block = 4096
        first = self.offset // block
        data = self.phone.sh_bytes(f"dd if='{self.path}' bs={block} skip={first} 2>/dev/null", 30)
        data = data[self.offset - first * block:]
        if not data:
            return []
        self.offset += len(data)
        text = self.partial + data.decode("utf-8", errors="replace")
        parts = text.split("\n")
        self.partial = parts.pop()
        return parts


class GameLog:
    """What a launch has done so far, from its console.log lines."""

    def __init__(self):
        self.scenes: list[tuple[float, int]] = []  # (host time, scene)
        self.warp = ""
        self.script_offset = None  # host time minus script time, in s (its smallest seen = least latency)
        self.setup_seen = False
        self.map_seen_at = None
        self.crsin_seen_at = None
        self.stage_at = None  # host time the warped course started
        self.resumed_course = False
        self.goal_sound = None
        self.exit_goal = False
        self.autosaved = False
        self.left_course = None  # the scene the game went to from the course (a lost life: the world map)
        self.movie = False
        self.stacks = None
        self.suspends = []
        self.lines = 0

    def feed(self, lines: list[str], now: float | None = None) -> None:
        now = time.time() if now is None else now
        for line in lines:
            self.lines += 1
            if m := RE_SCRIPT.search(line):
                offset = now - int(m.group(1)) / 1000
                self.script_offset = offset if self.script_offset is None else min(self.script_offset, offset)
            elif m := RE_SCENE.search(line):
                scene = int(m.group(1), 16)
                self.scenes.append((now, scene))
                self._scene(scene, now)
            elif m := RE_WARP.search(line):
                self.warp = f"{m.group(1)}-{m.group(2)}"
            elif m := RE_SOUND.search(line):
                if int(m.group(1)) in GOAL_SOUNDS and self.stage_at is not None and self.goal_sound is None:
                    self.goal_sound = int(m.group(1))
            elif m := RE_LEFT.search(line):
                if m.group(1) == "0" and self.stage_at is not None:
                    self.exit_goal = True
            elif "autosave: progress saved" in line:
                self.autosaved = True
            elif m := RE_STACKS.search(line):
                self.stacks = int(m.group(1))
            elif "suspend:" in line:
                self.suspends.append(line.strip())

    def _scene(self, scene: int, now: float) -> None:
        if self.stage_at is not None:
            # Out of the course for any reason (a lost life goes back to the world map, which can be another
            # world's after a warp and may then ask to save): the run stops this launch at once.
            if scene not in (STAGE, CRSIN, RESTART_CRSIN) and self.left_course is None:
                self.left_course = scene
            return
        if scene == GAME_SETUP:
            self.setup_seen = True
        elif scene == MOVIE and self.setup_seen:
            self.movie = True
        elif scene == WORLD_MAP and self.setup_seen and self.map_seen_at is None:
            self.map_seen_at = now
        elif scene in (CRSIN, RESTART_CRSIN) and self.setup_seen:
            if self.map_seen_at is None:
                self.resumed_course = True  # the file boots straight into a course: not ours
            elif self.crsin_seen_at is None:
                self.crsin_seen_at = now
        elif scene == STAGE and self.crsin_seen_at is not None:
            if self.warp:
                self.stage_at = now
            else:
                self.resumed_course = True

    def script_ms(self, host_time: float) -> int | None:
        return None if self.script_offset is None else int((host_time - self.script_offset) * 1000)

    @property
    def guard(self) -> str:
        """Why the game must be stopped right now (before anything is saved), or ""."""
        if self.goal_sound is not None:
            return f"the flagpole was reached (sound {self.goal_sound})"
        if self.exit_goal:
            return "the course was cleared"
        if self.left_course is not None:
            if self.left_course == WORLD_MAP:
                return "Mario lost a life"
            return f"the game left the course for {SCENES.get(self.left_course, hex(self.left_course))}"
        if self.resumed_course:
            return "the save file resumes inside a course"
        if self.movie:
            return "the save slot used is empty (a new game's opening movie started)"
        return ""
