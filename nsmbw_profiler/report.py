"""summary.txt / summary.json and the README that go into the zip."""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path


RE_FPS = re.compile(r"fps ([\d.]+): game ([\d.]+) ms \(gx ([\d.]+) ms.*?([\d.]+) draws.*?end_frame ([\d.]+) ms, "
                    r"pace ([\d.]+) ms, worst frame ([\d.]+) ms")
RE_SLOW = re.compile(r"slow frame ([\d.]+) ms \(scene 0x([0-9A-Fa-f]+)\)")
RE_HITCH = re.compile(r"Presentation hitch: ([\d.]+) ms")
RE_SCENE = re.compile(r"scene -> 0x([0-9A-Fa-f]+)")
RE_PROFILE_ROW = re.compile(r"profile:\s+([\d.]+)%\s+(.+)$")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def game_log(folder: Path) -> dict:
    """The run's console.log: GPU, frame rates in the course, slow frames, the on-phone profile table."""
    logs = list(folder.glob("*/console.log"))
    if not logs:
        return {}
    text = _read(logs[0])
    out: dict = {}
    if m := re.search(r"API: (.+)\n\s*Device: (.+)\n\s*Driver: (.+)", text):
        out["graphics"] = {"api": m.group(1).strip(), "device": m.group(2).strip(), "driver": m.group(3).strip()}
    scene = None
    fps_rows, slow, hitches = [], [], []
    for line in text.splitlines():
        if m := RE_SCENE.search(line):
            scene = int(m.group(1), 16)
        elif m := RE_FPS.search(line):
            if scene == 0x5:
                fps_rows.append(tuple(float(g) for g in m.groups()))
        elif m := RE_SLOW.search(line):
            slow.append((float(m.group(1)), int(m.group(2), 16)))
        elif m := RE_HITCH.search(line):
            hitches.append(float(m.group(1)))
    if len(fps_rows) > 2:
        rows = fps_rows[1:]  # the first window holds the course's first frames
        out["course_fps"] = {
            "windows": len(rows),
            "fps_mean": round(statistics.mean(r[0] for r in rows), 1),
            "fps_min": round(min(r[0] for r in rows), 1),
            "game_ms_mean": round(statistics.mean(r[1] for r in rows), 2),
            "gx_ms_mean": round(statistics.mean(r[2] for r in rows), 2),
            "draws_mean": round(statistics.mean(r[3] for r in rows)),
            "end_frame_ms_mean": round(statistics.mean(r[4] for r in rows), 2),
            "pace_ms_mean": round(statistics.mean(r[5] for r in rows), 2),
            "worst_frame_ms": max(r[6] for r in rows),
        }
    out["lives_lost"] = text.count("autosave: course left (exit mode 1)")
    out["slow_frames_logged"] = len(slow)
    out["slow_frames_in_course"] = len([s for s in slow if s[1] == 0x5])
    out["worst_slow_frame_ms"] = max((s[0] for s in slow), default=0)
    out["presentation_hitches"] = len(hitches)
    table = []
    for line in text.splitlines():
        if "profile:" in line and (m := RE_PROFILE_ROW.search(line)):
            table.append((float(m.group(1)), m.group(2).strip()))
    out["profile_top"] = table[:15]
    return out


def sampler(folder: Path) -> dict:
    lines = _read(folder / "sampler.tsv").splitlines()
    if len(lines) < 3:
        return {}
    header = lines[0].split("\t")
    rows = [line.split("\t") for line in lines[1:] if line.count("\t") == len(header) - 1]
    labels = {}
    for line in _read(folder / "sampler-labels.txt").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            labels[parts[0]] = parts[1]
    out: dict = {"samples": len(rows)}

    def column(i):
        values = []
        for row in rows:
            try:
                values.append(float(row[i].split()[0]))
            except (ValueError, IndexError):
                pass
        return values

    for i, name in enumerate(header[1:], start=1):
        values = column(i)
        if not values:
            continue
        base = name.rsplit("/", 1)[0]
        if name.endswith("scaling_cur_freq"):
            key = f"cpu {base.rsplit('/', 1)[-1]} MHz"
            values = [v / 1000 for v in values]
        elif "thermal_zone" in name:
            key = f"temp {labels.get(base, base.rsplit('/', 1)[-1])} C"
            values = [v / 1000 if v > 1000 else v for v in values]
        elif "cooling_device" in name:
            key = f"cooling {labels.get(base, base.rsplit('/', 1)[-1])} state"
        elif "power_supply" in name:
            key = "battery " + name.rsplit("/", 1)[-1]
        else:
            key = "gpu " + name
        out[key] = {"mean": round(statistics.mean(values), 1), "min": round(min(values), 1), "max": round(max(values), 1)}
    return out


def thermal_hal(folder: Path) -> dict:
    """Highest temperature per sensor and the worst throttling status the thermal HAL reported."""
    highest: dict[str, float] = {}
    status = 0
    for line in _read(folder / "thermal-hal.txt").splitlines():
        if m := re.search(r"mValue=([-\d.]+), mType=\d+, mName=([^,]+)", line):
            highest[m.group(2)] = max(highest.get(m.group(2), -1e9), float(m.group(1)))
        elif m := re.search(r"Thermal Status: (\d+)", line):
            status = max(status, int(m.group(1)))
    top = sorted(highest.items(), key=lambda kv: -kv[1])[:6]
    return {"max_temps_c": top, "worst_thermal_status": status} if highest else {}


def threads(folder: Path, clk_tck: int) -> list[tuple[str, float]]:
    def parse(path: Path) -> dict:
        found = {}
        for line in _read(path).splitlines():
            if ")" not in line:
                continue
            head, rest = line.rsplit(")", 1)
            tid, _, comm = head.partition(" (")
            fields = rest.split()
            if len(fields) > 13:
                found[tid] = (comm, (int(fields[11]) + int(fields[12])) / clk_tck)
        return found

    start, end = parse(folder / "threads-start.txt"), parse(folder / "threads-end.txt")
    used = [(f"{comm} ({tid})", round(cpu - start.get(tid, (comm, 0))[1], 2)) for tid, (comm, cpu) in end.items()]
    return sorted(used, key=lambda x: -x[1])[:12]


def timestats(folder: Path) -> dict:
    text = _read(folder / "surfaceflinger-timestats.txt")
    best = None
    for block in re.split(r"\n(?=layerName)", text):
        if "nsmbw" not in block or "present2present" not in block:
            continue
        hist_text = block.split("present2present histogram is as below:", 1)[1].split("\n", 2)[1]
        hist = {int(k): int(v) for k, v in re.findall(r"(\d+)ms=(\d+)", hist_text)}
        total = sum(hist.values())
        if total and (best is None or total > best["frames"]):
            ordered = sorted(hist.items())

            def percentile(p):
                seen = 0
                for ms, count in ordered:
                    seen += count
                    if seen >= total * p:
                        return ms
                return ordered[-1][0]

            best = {"frames": total, "p50_ms": percentile(0.5), "p90_ms": percentile(0.9), "p99_ms": percentile(0.99),
                    "over_34ms": sum(c for ms, c in ordered if ms > 34)}
    return best or {}


def write(out: Path, info: dict) -> None:
    clk = _read(out / "device" / "clk_tck.txt").strip()
    clk_tck = int(clk) if clk.isdigit() else 100
    for level in info.get("levels", []):
        folder = out / level["folder"]
        level["game_log"] = game_log(folder)
        level["sampler"] = sampler(folder)
        level["thermal_hal"] = thermal_hal(folder)
        level["threads_cpu_s"] = threads(folder, clk_tck)
        level["frame_presents"] = timestats(folder)
        level["files"] = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    (out / "summary.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    (out / "summary.txt").write_text(summary_text(info), encoding="utf-8")
    (out / "README.txt").write_text(README, encoding="utf-8")


def summary_text(info: dict) -> str:
    d, b = info.get("device", {}), info.get("build", {})
    lines = ["NSMBW profiling run", "=" * 19, "",
             f"Result: {info.get('status', '?')}", "",
             f"Phone: {d.get('manufacturer', '')} {d.get('model', '')}, Android {d.get('android', '')} (SDK {d.get('sdk', '')})",
             f"SoC: {d.get('soc') or '?'} (platform {d.get('platform', '?')})",
             f"Build: {d.get('fingerprint', '')}",
             f"Game: NSMBW {b.get('versionName', '?')} (code {b.get('versionCode', '?')}), from {b.get('release') or b.get('apk_file') or 'the installed app'}",
             f"APK SHA-256: {b.get('apk_sha256', '?')}",
             f"libmain.so build ID: {b.get('libmain_build_id', '?')}",
             {True: "Save files: unchanged by the run (checked by hash).",
              False: "Save files: CHANGED during the run; a copy from before it was kept on the PC that ran it.",
              None: "Save files: not checked."}[info.get("save_unchanged")], ""]
    graphics = next((lv["game_log"].get("graphics") for lv in info.get("levels", []) if lv.get("game_log", {}).get("graphics")), None)
    if graphics:
        lines.append(f"GPU: {graphics['device']} via {graphics['api']}, driver {graphics['driver']}")
        lines.append("")
    for level in info.get("levels", []):
        g, s = level.get("game_log", {}), level.get("sampler", {})
        attempt = f", try {level['attempt']}" if level.get("attempt", 1) > 1 else ""
        lines.append(f"--- {level['level']}{attempt}  ({level['folder']})")
        state = "played" if level.get("entered") else "NOT entered"
        lines.append(f"  {state} for {level.get('play_seconds', 0)} s; game profile samples: "
                     f"{level.get('profile_samples') or 'none'}")
        if level.get("guard"):
            lines.append(f"  ended early, game closed at once: {level['guard']}")
        if level.get("error"):
            lines.append(f"  problem: {level['error']}")
        if fps := g.get("course_fps"):
            lines.append(f"  in the course: {fps['fps_mean']} fps (lowest 2 s window {fps['fps_min']}), game "
                         f"{fps['game_ms_mean']} ms/frame (GX {fps['gx_ms_mean']}), end_frame {fps['end_frame_ms_mean']} ms, "
                         f"pace {fps['pace_ms_mean']} ms, {fps['draws_mean']} draws, worst frame {fps['worst_frame_ms']} ms")
        lines.append(f"  slow frames logged: {g.get('slow_frames_logged', 0)} ({g.get('slow_frames_in_course', 0)} in the "
                     f"course, worst {g.get('worst_slow_frame_ms', 0)} ms); presentation hitches: {g.get('presentation_hitches', 0)}")
        if p := level.get("frame_presents"):
            lines.append(f"  SurfaceFlinger presents: {p['frames']}, present-to-present p50 {p['p50_ms']} ms, "
                         f"p90 {p['p90_ms']} ms, p99 {p['p99_ms']} ms, {p['over_34ms']} over 34 ms")
        cpu = [f"{k[4:]} {v['mean']:.0f}/{v['max']:.0f}" for k, v in s.items() if k.startswith("cpu ")]
        if cpu:
            lines.append("  CPU clusters MHz (mean/max): " + ", ".join(cpu))
        gpu = [f"{k[4:]} mean {v['mean']} max {v['max']}" for k, v in s.items() if k.startswith("gpu ")]
        if gpu:
            lines.append("  GPU: " + "; ".join(gpu))
        temps = sorted(((v["max"], k[5:-2]) for k, v in s.items() if k.startswith("temp ")), reverse=True)[:4]
        if temps:
            lines.append("  hottest zones (max C): " + ", ".join(f"{name} {t}" for t, name in temps))
        if hal := level.get("thermal_hal"):
            lines.append("  thermal HAL max C: " + ", ".join(f"{n} {t:.1f}" for n, t in hal["max_temps_c"]) +
                         f"; worst thermal status {hal['worst_thermal_status']} (0 none ... 6 shutdown)")
        cooling = [f"{k[8:-6]} {v['max']:.0f}" for k, v in s.items() if k.startswith("cooling ") and v["max"] > 0]
        if cooling:
            lines.append("  throttling (cooling device max state): " + ", ".join(cooling))
        if level.get("threads_cpu_s"):
            lines.append("  CPU seconds per game thread: " + ", ".join(f"{n} {c}" for n, c in level["threads_cpu_s"][:8]))
        if g.get("profile_top"):
            lines.append("  game thread profile, the game's own rough table (leaf function; libc.so is mostly "
                         "waiting; symbolize profile-stacks.txt for the real one):")
            lines += [f"    {pct:5.1f}%  {name}" for pct, name in g["profile_top"][:10]]
        lines.append("")
    return "\n".join(lines) + "\n"


README = """What's in this zip
==================

Made by NSMBW Profiler: it installed the chosen NSMBW build, started the game, warped into each world's
level 1 and played it with a scripted Wii Remote while every profiler below ran, then closed the game.

summary.txt / summary.json   The short version: phone, game build, and per level the frame rate, frame
                             times, slow frames, CPU/GPU clocks, temperatures, throttling, busiest threads.
run.log                      What the tool did, step by step.
game-build.txt               The game version, the APK's SHA-256 and libmain.so's GNU build ID. Symbolize
                             profile-stacks.txt only against the unstripped libmain.so with that build ID.
device/                      The phone: getprop, CPU and its clock steps, memory, GPU (dumpsys gpu, Vulkan
                             JSON, sysfs), display, thermal and battery at the start, power state, game mode,
                             the game's Config.toml and file list.
logcat.txt                   Android's log for the whole run.
00-calibration/              One launch that only went from the title screen to 1-1, timing this phone's
                             boot (its console.log has the GPU/driver lines and the boot's slow frames).
NN-level-W-1/                One launch per level:
  base_<time>_pid<pid>/      The game's own run folder: console.log (every slow frame with its breakdown,
                             fps every 2 s, scene changes, the GPU), profile-stacks.txt (the game thread
                             sampled ~1000x a second, one call stack per line, S<scene> first) and
                             profile-frames.txt (the frame each sample fell in).
  perfetto-trace.pftrace     System trace: CPU scheduling and clocks, GPU clock/counters/render stages
                             where the driver offers them, SurfaceFlinger frame timeline, memory, battery,
                             power rails, thermal. Open it at https://ui.perfetto.dev
  sampler.tsv                Every 0.5 s: CPU cluster clocks, GPU clock and load, every thermal zone,
                             cooling devices (throttling), battery: whichever of these the phone lets adb
                             read. sampler-labels.txt names the zones.
  thermal-hal.txt            Every 5 s: the thermal HAL's temperatures and throttling status.
  perfetto-config.txt        The Perfetto config this phone accepted.
  surfaceflinger-timestats.txt  Present-to-present histogram of the game's surface (real frame pacing).
  threads-start/end.txt      /proc stat of every game thread at the course's start and end (CPU time).
  top-threads.txt, meminfo.txt, gfxinfo.txt, screenshot.png (mid-level), battery-*/thermal-* (dumpsys)
  script.txt                 The warp and the scripted input used.

How the levels were played: the start menu's Play, then the scripted remote pressed 2 through the title
and file select to the world map; debug.nsmbw.warp turned the course entered into W-1. Mario then walked
right in bursts and shuffled left and right, jumping all along, holding right for 20 s at most. That
can't reach a flagpole (the shortest X-1 run to one is 5,952 px; 20 s covers under 4,800 even at a
speed Mario never reaches), so no course is cleared and the game never autosaves. The moment the game
leaves the course for any reason (a lost life sends Mario to a world map, which can ask to save), the
tool closes it, and plays that level again (up to 3 tries) if the game's own profile wasn't written
yet: the profile covers the boot and the first 25 s of play, the rest of the system data all of it.
Before the run the save files are copied to the PC (only read), and after it their hashes are compared:
summary.txt says whether they changed. Nothing else on the phone is changed: the debug.nsmbw.* test
properties are put back as they were, and the tool's files on the phone are deleted.

Symbolizing the game thread's profile (on a PC with the matching unstripped libmain.so):
  python projects/nsmbw/tools/profile-report.py <level>/<run>/profile-stacks.txt --lib libmain.so --scene 005
  (profile-report.py is in the game's repository; scene 005 is the course, 003 the world map)
"""
