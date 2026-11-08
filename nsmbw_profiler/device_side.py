"""What runs on the phone besides the game: a Perfetto system trace and a small sysfs sampler."""
from __future__ import annotations

import re
import time

PACKAGE = "com.wither.nsmbw"
TMP = "/data/local/tmp/nsmbw-prof"
TRACES = "/data/misc/perfetto-traces"

# What Perfetto can see without root: scheduling and clocks of every CPU, the GPU's clock and (where the
# driver offers them, Mali among them) GPU counters and render stages, SurfaceFlinger's frame timeline,
# memory, battery and power rails, thermal. "detailed" adds the graphics stack's atrace markers
# (SurfaceFlinger, the composer, the game's buffer queue): the most useful for GPU pacing, and most of
# the size (about 15-25 MB zipped per minute against 3-5 for "light"). Left out on purpose: cpu_idle and
# the view/input/audio/hal/binder categories, which made a 40 s level's trace 230 MB on a Pixel 3 XL.
_FTRACE_DETAILED = """  atrace_categories: "gfx"
  atrace_categories: "dalvik"
  atrace_apps: "com.wither.nsmbw"
  atrace_apps: "com.wither.nsmbw:game"
"""
_PERFETTO = """
buffers { size_kb: 65536 fill_policy: DISCARD }
buffers { size_kb: 8192 fill_policy: DISCARD }
data_sources { config { name: "linux.ftrace" target_buffer: 0 ftrace_config {
  ftrace_events: "sched/sched_switch"
  ftrace_events: "sched/sched_waking"
  ftrace_events: "sched/sched_wakeup_new"
  ftrace_events: "sched/sched_process_exit"
  ftrace_events: "sched/sched_process_free"
  ftrace_events: "task/task_newtask"
  ftrace_events: "task/task_rename"
  ftrace_events: "power/cpu_frequency"
  ftrace_events: "power/gpu_frequency"
  ftrace_events: "power/suspend_resume"
  ftrace_events: "thermal/thermal_temperature"
  ftrace_events: "thermal/cdev_update"
  ftrace_events: "gpu_mem/gpu_mem_total"
@ATRACE@  buffer_size_kb: 16384
  drain_period_ms: 250
  compact_sched { enabled: true }
} } }
data_sources { config { name: "linux.process_stats" target_buffer: 1 process_stats_config {
  scan_all_processes_on_start: true
  proc_stats_poll_ms: 1000
} } }
data_sources { config { name: "linux.sys_stats" target_buffer: 1 sys_stats_config {
  meminfo_period_ms: 1000
  vmstat_period_ms: 1000
  stat_period_ms: 500
  stat_counters: STAT_CPU_TIMES
  stat_counters: STAT_FORK_COUNT
} } }
data_sources { config { name: "android.power" target_buffer: 1 android_power_config {
  battery_poll_ms: 500
  battery_counters: BATTERY_COUNTER_CAPACITY_PERCENT
  battery_counters: BATTERY_COUNTER_CHARGE
  battery_counters: BATTERY_COUNTER_CURRENT
  battery_counters: BATTERY_COUNTER_VOLTAGE
  collect_power_rails: true
} } }
data_sources { config { name: "android.surfaceflinger.frametimeline" target_buffer: 0 } }
data_sources { config { name: "android.gpu.memory" target_buffer: 1 } }
data_sources { config { name: "gpu.counters" target_buffer: 0 gpu_counter_config { counter_period_ns: 1000000 } } }
data_sources { config { name: "gpu.renderstages" target_buffer: 0 } }
data_sources { config { name: "android.log" target_buffer: 1 android_log_config {
  log_ids: LID_DEFAULT
  log_ids: LID_SYSTEM
  log_ids: LID_CRASH
} } }
duration_ms: 900000
write_into_file: true
file_write_period_ms: 2000
max_file_size_bytes: 400000000
"""


def perfetto_config(detail: str) -> str:
    return _PERFETTO.replace("@ATRACE@", _FTRACE_DETAILED if detail == "detailed" else "")


# For an older Perfetto that rejects the config even with its unknown fields left out.
PERFETTO_MINIMAL = """
buffers { size_kb: 65536 fill_policy: DISCARD }
data_sources { config { name: "linux.ftrace" ftrace_config {
  ftrace_events: "sched/sched_switch"
  ftrace_events: "sched/sched_waking"
  ftrace_events: "power/cpu_frequency"
  ftrace_events: "power/gpu_frequency"
} } }
data_sources { config { name: "linux.process_stats" process_stats_config { scan_all_processes_on_start: true } } }
duration_ms: 900000
write_into_file: true
file_write_period_ms: 2000
max_file_size_bytes: 400000000
"""

# Every half second: each CPU cluster's clock, the GPU's clock and load (Samsung/Exynos, Mali, Adreno
# sysfs), every thermal zone, every cooling device's state (throttling) and the battery. Only files the
# shell user can read are sampled; the header names them, sampler-labels.txt says what each zone is.
# Every 5 s also the thermal HAL's temperatures and throttling status (dumpsys thermalservice), which
# phones that hide /sys/class/thermal from adb still report.
SAMPLER = r"""#!/system/bin/sh
out="$1"; period="$2"; stop="$3"; labels="$4"; hal="$5"
files=""
for f in /sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq \
         /sys/kernel/gpu/gpu_clock /sys/kernel/gpu/gpu_busy /sys/kernel/gpu/gpu_freq \
         /sys/class/misc/mali0/device/cur_freq /sys/class/misc/mali0/device/clock \
         /sys/class/misc/mali0/device/utilization /sys/class/misc/mali0/device/dvfs_utilization \
         /sys/devices/platform/*mali*/cur_freq /sys/devices/platform/*mali*/utilization \
         /sys/devices/platform/*mali*/clock /sys/devices/platform/*mali*/dvfs_utilization \
         /sys/class/devfreq/*mali*/cur_freq /sys/class/devfreq/*gpu*/cur_freq /sys/class/devfreq/*.g3d/cur_freq \
         /sys/class/kgsl/kgsl-3d0/gpuclk /sys/class/kgsl/kgsl-3d0/gpu_busy_percentage \
         /sys/class/thermal/thermal_zone*/temp /sys/class/thermal/cooling_device*/cur_state \
         /sys/class/power_supply/battery/capacity /sys/class/power_supply/battery/current_now \
         /sys/class/power_supply/battery/voltage_now /sys/class/power_supply/battery/temp; do
  [ -f "$f" ] || continue
  v=""
  read -r v 2>/dev/null < "$f" || continue
  [ -n "$v" ] && files="$files $f"
done
: > "$labels"
for z in /sys/class/thermal/thermal_zone* /sys/class/thermal/cooling_device*; do
  t=""; read -r t 2>/dev/null < "$z/type"; m=""; read -r m 2>/dev/null < "$z/max_state"
  echo "$z	$t	$m" >> "$labels"
done
line="uptime_s"
for f in $files; do line="$line	$f"; done
echo "$line" > "$out"
: > "$hal"
n=0
while [ ! -e "$stop" ]; do
  read -r up idle < /proc/uptime
  line="$up"
  for f in $files; do v=""; read -r v 2>/dev/null < "$f"; line="$line	$v"; done
  echo "$line" >> "$out"
  if [ $((n % 10)) -eq 0 ]; then
    echo "== $up" >> "$hal"
    dumpsys thermalservice 2>/dev/null | grep -E "Temperature\{|Thermal Status|Status:" >> "$hal"
  fi
  n=$((n + 1))
  sleep "$period"
done
"""


class Perfetto:
    def __init__(self, phone, log):
        self.phone = phone
        self.log = log
        self.pid = None
        self.remote = None
        self.config_used = ""

    def _try(self, lines: list[str], before: set[str]) -> tuple[str | None, str]:
        result = self.phone.sh_stdin(f"perfetto --background --txt -c - -o {self.remote}",
                                     ("\n".join(lines) + "\n").encode(), 30)
        time.sleep(1.0)
        started = sorted(set(self.phone.sh("pidof perfetto", 10).split()) - before, key=int)
        return (started[-1] if started else None), (result.stdout + result.stderr).decode(errors="replace")

    def start(self, name: str, detail: str) -> bool:
        """A background trace. A field this phone's Perfetto doesn't know (its error names the line) is left
        out and the config tried again; if it still won't start, the minimal config."""
        self.remote = f"{TRACES}/nsmbw-prof-{name}.pftrace"
        self.phone.sh(f"rm -f {self.remote}", 15)
        before = set(self.phone.sh("pidof perfetto", 10).split())
        lines = perfetto_config(detail).strip("\n").split("\n")
        output = ""
        for _ in range(8):
            pid, output = self._try(lines, before)
            if pid:
                self.pid = pid
                self.config_used = "\n".join(lines) + "\n"
                self.log(f"Perfetto trace started (pid {pid})")
                return True
            bad = re.search(r"^-:(\d+):\d+ error", output, re.M)
            if not bad or not 1 <= int(bad.group(1)) <= len(lines):
                break
            dropped = lines.pop(int(bad.group(1)) - 1).strip()
            self.log(f"This phone's Perfetto doesn't know '{dropped}': leaving it out")
        self.log(f"Perfetto didn't take the full config ({output.strip()[-300:]}); using the minimal one")
        pid, output = self._try(PERFETTO_MINIMAL.strip("\n").split("\n"), before)
        if pid:
            self.pid = pid
            self.config_used = PERFETTO_MINIMAL
            self.log(f"Perfetto trace started with the minimal config (pid {pid})")
            return True
        self.log(f"Perfetto did not start: {output.strip()[-300:]}")
        self.remote = None
        return False

    def stop(self) -> None:
        if not self.pid:
            return
        self.phone.sh(f"kill -TERM {self.pid}", 10)
        for _ in range(60):
            if not self.phone.sh(f"[ -d /proc/{self.pid} ] && echo running", 10).strip():
                break
            time.sleep(0.5)
        self.pid = None

    def pull(self, local) -> bool:
        if not self.remote:
            return False
        ok = self.phone.pull(self.remote, local)
        self.phone.sh(f"rm -f {self.remote}", 15)
        self.remote = None
        return ok
