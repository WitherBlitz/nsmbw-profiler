"""The scripted Wii Remote that plays: NSMBW_INPUT_SCRIPT, set through debug.nsmbw.script/2/3.

The runtime reads "<ms>:<buttons>:<hold ms>[/<period>x<count>]" items, timed from the game's first input
read, for a remote held sideways: TWO jumps and confirms, ONE runs (and backs out of menus), the D-pad's
DOWN is screen right and UP is screen left. The script is fixed when the game starts, so a calibration
launch first measures how long this phone takes from Play to a course; every level launch then presses
TWO through the menus until then (the last presses enter the course from the world map, which
debug.nsmbw.warp turns into that world's level 1) and plays from there.

How it plays, and why it can't finish a course: the game's autosave writes the player's file right
after a course is cleared, so the bot must never reach a flagpole. Mario only walks (never runs: a lost
life sends him to the world map, where the run button opens the Items menu): right in bursts, then a
left/right shuffle, jumping all along. The whole script holds right for MAX_RIGHT_HOLD_S seconds at most.
Even at 4 px a frame (more than his top running speed) and 60 frames a second, 20 s moves him 4,800 px,
and the shortest start-to-flagpole run of any X-1 course is 5,952 px (1-1; 2-1 6,176; 9-1 6,864; the
rest are longer or need pipes the bot never presses into). The tool also closes the game the moment the
log shows it leaving the course for any reason: a lost life goes back to the world map, which after a
warp can be another world's, and NSMBW may ask there whether to save (pressing 2 would say yes).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

PROP_CHARS = 91  # a system property holds 91 characters; the game joins script, script2 and script3
SCRIPT_PROPS = ("debug.nsmbw.script", "debug.nsmbw.script2", "debug.nsmbw.script3")

MENU_START_MS = 3000
MENU_PERIOD_MS = 2000
MAX_RIGHT_HOLD_S = 20
WALK_BURST_MS, WALK_PERIOD_MS, WALK_BURSTS = 3000, 4000, 4  # 12 s of walking right first
SHUFFLE_PERIOD_MS, SHUFFLE_LEFT_MS, SHUFFLE_RIGHT_MS = 4000, 1200, 600  # then left, a step right, ...
JUMP_HOLD_MS, JUMP_PERIOD_MS = 350, 1100


@dataclass
class Press:
    start: int
    buttons: str
    hold: int
    period: int = 0
    count: int = 1

    def text(self) -> str:
        repeat = f"/{self.period}x{self.count}" if self.count > 1 else ""
        return f"{self.start}:{self.buttons}:{self.hold}{repeat}"

    def last_start(self) -> int:
        return self.start + (self.count - 1) * self.period


def serialize(presses: list[Press]) -> str:
    return ",".join(p.text() for p in presses if p.count > 0)


def trim(presses: list[Press], until_ms: int) -> list[Press]:
    """Only the presses that start before until_ms."""
    kept = []
    for press in presses:
        if press.start >= until_ms:
            continue
        count = press.count if press.count <= 1 else min(press.count, (until_ms - 1 - press.start) // press.period + 1)
        kept.append(replace(press, count=count))
    return kept


def menu_presses(until_ms: int) -> list[Press]:
    count = max(1, (until_ms - MENU_START_MS) // MENU_PERIOD_MS + 1)
    return [Press(MENU_START_MS, "TWO", 150, MENU_PERIOD_MS, min(count, 1000))]


def play_presses(start_ms: int, play_s: float, right_budget_ms: int = MAX_RIGHT_HOLD_S * 1000) -> list[Press]:
    """Walk right, then shuffle left and right, jumping all along; at most right_budget_ms holding right.
    Once the budget is spent he only jumps (on the world map that re-enters the course he stands on)."""
    tail_ms = int((play_s + 15) * 1000)
    presses = [Press(start_ms, "TWO", JUMP_HOLD_MS, JUMP_PERIOD_MS, min(1000, tail_ms // JUMP_PERIOD_MS + 1))]
    bursts = min(WALK_BURSTS, max(1, math.ceil(play_s * 1000 / WALK_PERIOD_MS)), right_budget_ms // WALK_BURST_MS)
    if bursts > 0:
        presses.append(Press(start_ms, "DOWN", WALK_BURST_MS, WALK_PERIOD_MS, bursts))
    budget = right_budget_ms - bursts * WALK_BURST_MS
    shuffle_start = start_ms + bursts * WALK_PERIOD_MS
    shuffles = min((start_ms + tail_ms - shuffle_start) // SHUFFLE_PERIOD_MS, budget // SHUFFLE_RIGHT_MS, 1000)
    if shuffles > 0:
        presses.append(Press(shuffle_start, "UP", SHUFFLE_LEFT_MS, SHUFFLE_PERIOD_MS, shuffles))
        presses.append(Press(shuffle_start + 2000, "DOWN", SHUFFLE_RIGHT_MS, SHUFFLE_PERIOD_MS, shuffles))
    return presses


def right_hold_seconds(presses: list[Press]) -> float:
    return sum(p.hold * p.count for p in presses if "DOWN" in p.buttons.split("+")) / 1000


def split_props(script: str) -> list[str]:
    if len(script) > PROP_CHARS * len(SCRIPT_PROPS):
        raise ValueError(f"input script too long ({len(script)} characters)")
    return [script[i:i + PROP_CHARS] for i in range(0, PROP_CHARS * len(SCRIPT_PROPS), PROP_CHARS)]
