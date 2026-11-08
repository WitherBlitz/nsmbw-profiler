# NSMBW Profiler

A desktop app that records how the [NSMBW recompilation](https://github.com/WitherBlitz/NSMB-Wii-Recompilation)
runs on an Android phone. It installs the NSMBW version you pick, plays the first level of every world by
itself (1-1, 2-1 … 9-1) while the game's own profiler, a Perfetto system trace and a few samplers run, closes
the game, says **DONE!** and gives you a `.zip` to send to the developer.

It is made to be run by a player on their own phone without help.

## What you need

- A Windows PC (Linux and macOS can run it from source, see below; untested so far).
- The phone, its USB cable, and **USB debugging** turned on
  ([how](https://developer.android.com/studio/debug/dev-options#enable)). The first time, the phone asks to
  allow the computer: tap **Allow**.
- NSMBW already installed on the phone, with **your own game imported** (the app can't and doesn't ship any
  game files).
- Last time you played: save on the world map with Mario standing on a level (not inside a level, not on a
  Toad house or cannon).

adb (Android's debugging tool) is not included: its licence doesn't allow passing it on. If it isn't on your
PC already, the app offers to download Google's official platform-tools (about 7 MB) for you.

## Using it

1. Download `NSMBW-Profiler.exe` from the Releases page and open it.
2. Plug in the phone and pick it under **Phone**.
3. Under **NSMBW version to test**, keep *Latest release*, or press **Older versions** to pick one of the
   ten newest. The app installs it over the existing one with `adb install -r`, which keeps your saves,
   settings and game files. (Android can't go back to an older version than the one installed without
   deleting the game's data, so the app refuses that.) *Installed version* tests what's on the phone and
   needs no internet; *APK file…* installs a build from your PC.
4. Press **Start**, keep the phone unlocked and plugged in, and don't touch it. A full run (9 levels, 60 s
   each) takes about 25 minutes.
5. When it says **DONE!**, press **Save .zip…** and send that file.

## What it records

For each level (one game launch each):

- **The game's own profiler** (`NSMBW_PROFILE`): the game thread sampled about 1000 times a second with full
  call stacks (`profile-stacks.txt`, `profile-frames.txt`), from the boot through the first 25 s of play.
- **The game's log** (`console.log`): the GPU and driver, frame rate and frame time breakdown every 2 s
  (`NSMBW_LOG_FPS`), every slow frame with where its time went, scene changes.
- **A Perfetto system trace**: CPU scheduling and clocks of every core, GPU clock, Mali (or other) GPU counters
  and render stages when the driver offers them, SurfaceFlinger's frame timeline, the graphics stack's atrace
  markers, memory, battery and power rails, thermal. Open it at <https://ui.perfetto.dev>.
- **SurfaceFlinger timestats** (real present-to-present pacing), `dumpsys gfxinfo` and `meminfo`, the game's
  threads' CPU time, a mid-level screenshot.
- **A sampler** every 0.5 s: CPU cluster clocks, GPU clock and load (Samsung/Exynos, Mali and Adreno sysfs,
  whichever adb may read), thermal zones, cooling devices (throttling), battery; plus the thermal HAL every 5 s.

Once: the phone's hardware and software (getprop, CPU, GPU/Vulkan, display, memory, thermal and power state,
game mode), the game's version, APK SHA-256 and `libmain.so` build ID (to symbolize the profile against the
exact unstripped library), Android's log for the whole run. `summary.txt` sums it all up per level.

The **System trace** setting picks how much Perfetto records: *Detailed* (about 20 MB of zip per level),
*Light* (about 4 MB) or *Off*.

**Privacy:** the zip contains the phone's model, its serial number and build, the game's settings file, and
Android's system log for the run, which can include other apps' messages.

**Internet:** nothing is sent anywhere. The app only downloads, and only when it needs to: at Start, from
github.com, which release is the newest and its APK (an APK downloaded once is kept and reused); the list of
versions when you press *Older versions*; and adb from Google if you ask for it. It doesn't use GitHub's API,
and temporary errors (like a 504) are retried.

## How it plays, and why your save is safe

The game reads a few test settings (`debug.nsmbw.*` system properties) when it starts. The app sets them for
each launch and clears them again at the end:

- `debug.nsmbw.warp=W-1`: any level entered from the world map becomes world W's level 1.
- `debug.nsmbw.script…`: a scripted Wii Remote. It presses 2 through the title screen and file select (the
  last file you used) to the world map, enters the level Mario stands on (which the warp turns into W-1),
  then walks right in bursts, shuffles left and right and jumps.
- `debug.nsmbw.env`: the profiler, plus `debug.nsmbw.logfps` for the frame-time log.

NSMBW saves on its own when a level is cleared (the recompilation's autosave), and asks whether to save on
some world maps. So:

- Mario holds right for 20 s at most in a launch. Even at a speed he never reaches, that's under 4,800 px,
  and the shortest way to a flagpole in any of these levels is 5,952 px (1-1). He can't clear a level.
- The moment the game leaves the level for any reason (a lost life sends Mario to a world map, which after a
  warp can be another world's, and may ask to save), the app closes the game. A level that ends early is
  played again, up to 3 tries.
- If pressing 2 doesn't enter a level from where Mario stands, the app stops and asks you to move him, rather
  than move him itself (a cannon or a prompt could save).
- Your save files are copied to your PC before the run (`<zip name>-save-backup`, next to the zip; it is not
  inside the zip) and compared afterwards; the app and `summary.txt` say whether they changed.
- Nothing else on the phone is changed. The app's own files on the phone are deleted at the end.

If the app ever says your save changed, put the backup back with adb:

```
adb push <backup>/534d4e45/data/wiimj2d.sav /sdcard/Android/data/com.wither.nsmbw/files/NAND/title/00010000/534d4e45/data/
```

## Running from source (any OS)

Python 3.10 or newer with tkinter (included in the python.org installers; `sudo apt install python3-tk` on
Debian/Ubuntu):

```
python -m nsmbw_profiler             # the window
python -m nsmbw_profiler --cli --serial <adb serial> --version latest --worlds 1,2,9 --seconds 60
```

Captures go to `Documents/NSMBW Profiler`. Downloads (adb, APKs) are kept in `%LOCALAPPDATA%\NSMBW Profiler`
(Windows), `~/.local/share/nsmbw-profiler` (Linux) or `~/Library/Application Support/NSMBW Profiler` (macOS).

## Building the exe

```
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed --name NSMBW-Profiler run.py
```

The GitHub workflow in `.github/workflows/build.yml` does the same for Windows, Linux and macOS when a `v*`
tag is pushed, and attaches the builds to that release (an exe already attached by hand is kept).

## For the developer

- Symbolize a level's profile with the game repository's `projects/nsmbw/tools/profile-report.py`
  (`<level>/<run>/profile-stacks.txt --lib <unstripped libmain.so> --scene 005`), using the `libmain.so` whose
  GNU build ID matches `game-build.txt`.
- Runtime changes that would make this tool better (not made here):
  - `NSMBW_NAND_ROOT` (a scratch save folder for automated runs, like the LAN session's NAND override): runs
    could never touch a player's save, could clear levels and die freely, and would start from the same save
    on every phone.
  - `NSMBW_PROFILE` writing its stacks every few seconds instead of only at the end, so a launch closed after
    a lost life keeps its profile and needs no second try.
  - `<profileable android:shell="true"/>` in the manifest: Perfetto and simpleperf could then sample every
    thread's call stacks on a normal phone (the GX thread, audio, frame workers), not only the game thread.
  - The audio-tag log (`NSMBW_LOG_AUDIO_TAGS`) only runs while a volume slider is under 100%; with it always
    available the app could close the game the instant Mario touches a flagpole.

## Licence

MIT, see [LICENSE](LICENSE). NSMBW and its assets belong to Nintendo; this tool contains none of them.
