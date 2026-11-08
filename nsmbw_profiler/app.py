"""The window: pick the phone and the NSMBW version, press Start, get a zip."""
from __future__ import annotations

import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__, adb as adbmod, paths, releases
from .capture import Capture, Options, PACKAGE

LATEST = "Latest release (looked up on github.com at Start)"
INSTALLED = "Installed version (don't install anything)"
APK_FILE = "APK file on this computer..."
TRACE_CHOICES = {  # label: Options.trace
    "Detailed (biggest zip, ~20 MB a level)": "detailed",
    "Light (~4 MB a level)": "light",
    "Off": "off",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"NSMBW Profiler {__version__}")
        self.geometry("780x720")
        self.minsize(680, 600)
        self.events: queue.Queue = queue.Queue()
        self.adb: adbmod.Adb | None = None
        self.devices: list[adbmod.Device] = []
        self.release_list: list[releases.Release] = []
        self.apk_file: Path | None = None
        self.capture: Capture | None = None
        self.worker: threading.Thread | None = None
        self.zip_path: Path | None = None
        self.closing = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._pump)
        self._find_adb()

    def report_callback_exception(self, kind, error, tb) -> None:
        """tkinter's hook for an error in a button or other callback: by default it prints to stderr, which the
        packaged app doesn't have, so the button just seemed to do nothing. Shown and logged instead."""
        text = "".join(traceback.format_exception(kind, error, tb))
        log_file = paths.data_dir() / "error.log"
        try:
            with open(log_file, "a", encoding="utf-8") as file:
                file.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} NSMBW Profiler {__version__}\n{text}\n")
        except OSError:
            pass
        self._on_log(text)
        try:
            messagebox.showerror("NSMBW Profiler", f"Something went wrong: {error}\n\nDetails are in {log_file}. "
                                 "Please send that file along.", parent=self)
        except Exception:  # noqa: BLE001 - a broken dialog must not hide the error again
            self.attention.configure(text=f"Something went wrong: {error} (details in {log_file})")

    # ---- layout ---------------------------------------------------------------------------------------

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 4}
        style = ttk.Style(self)
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Done.TLabel", font=("Segoe UI", 30, "bold"), foreground="#1a8f3c")
        style.configure("Big.TButton", font=("Segoe UI", 13, "bold"), padding=(18, 8))
        style.configure("Attention.TLabel", foreground="#b35c00", font=("Segoe UI", 10, "bold"))

        top = self.top_frame = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="NSMBW Profiler", style="Title.TLabel").pack(anchor="w")
        ttk.Label(top, text="Plays each world's level 1 of NSMBW (the recompilation) on an Android phone and records "
                            "how it runs: CPU, GPU, frame times, temperatures, the game's own profiler.",
                  wraplength=740).pack(anchor="w")

        self.adb_frame = ttk.Frame(self)
        self.adb_label = ttk.Label(self.adb_frame, text="", wraplength=430)
        self.adb_label.pack(side="left")
        self.adb_button = ttk.Button(self.adb_frame, text="Download adb from Google", command=self._download_adb)
        self.adb_button.pack(side="left", padx=8)
        terms = ttk.Label(self.adb_frame, text="(Google's terms)", foreground="#2060c0", cursor="hand2")
        terms.pack(side="left")
        terms.bind("<Button-1>", lambda e: webbrowser.open(adbmod.PLATFORM_TOOLS_TERMS))

        phone = ttk.LabelFrame(self, text="Phone")
        phone.pack(fill="x", **pad)
        self.device_box = ttk.Combobox(phone, state="readonly", width=60)
        self.device_box.pack(side="left", padx=8, pady=6)
        self.device_box.bind("<<ComboboxSelected>>", lambda e: self._device_chosen())
        ttk.Button(phone, text="Refresh", command=self._refresh_devices).pack(side="left")
        self.device_note = ttk.Label(phone, text="")
        self.device_note.pack(side="left", padx=8)

        version = ttk.LabelFrame(self, text="NSMBW version to test")
        version.pack(fill="x", **pad)
        self.version_box = ttk.Combobox(version, state="readonly", width=60, values=[LATEST, INSTALLED, APK_FILE])
        self.version_box.set(LATEST)
        self.version_box.pack(side="left", padx=8, pady=6)
        self.version_box.bind("<<ComboboxSelected>>", lambda e: self._version_chosen())
        self.older_button = ttk.Button(version, text="Older versions", command=self._load_releases)
        self.older_button.pack(side="left")
        self.installed_note = ttk.Label(version, text="")
        self.installed_note.pack(side="left", padx=8)

        levels = ttk.LabelFrame(self, text="Levels")
        levels.pack(fill="x", **pad)
        row = ttk.Frame(levels)
        row.pack(fill="x", padx=8, pady=4)
        self.world_vars = []
        for world in range(1, 10):
            var = tk.BooleanVar(value=True)
            self.world_vars.append(var)
            ttk.Checkbutton(row, text=f"{world}-1", variable=var).pack(side="left", padx=3)
        row2 = ttk.Frame(levels)
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Label(row2, text="Seconds of play per level:").pack(side="left")
        self.seconds = tk.IntVar(value=60)
        ttk.Spinbox(row2, from_=30, to=180, increment=10, width=5, textvariable=self.seconds).pack(side="left", padx=6)
        ttk.Label(row2, text="System trace:").pack(side="left", padx=(24, 4))
        self.trace = ttk.Combobox(row2, state="readonly", width=34, values=list(TRACE_CHOICES))
        self.trace.set(next(iter(TRACE_CHOICES)))
        self.trace.pack(side="left")

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", **pad)
        self.start_button = ttk.Button(buttons, text="Start", style="Big.TButton", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=10)
        ttk.Label(buttons, text="Keep the phone unlocked and plugged in, and don't touch it while it runs.",
                  wraplength=430).pack(side="left", padx=10)

        progress = ttk.Frame(self)
        progress.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(progress, maximum=1.0)
        self.progress.pack(fill="x")
        self.step_label = ttk.Label(progress, text="")
        self.step_label.pack(anchor="w")
        self.attention = ttk.Label(progress, text="", style="Attention.TLabel")
        self.attention.pack(anchor="w")

        self.done_frame = ttk.Frame(self)
        self.done_label = ttk.Label(self.done_frame, text="DONE!", style="Done.TLabel")
        self.done_label.pack(side="left", padx=4)
        ttk.Button(self.done_frame, text="Save .zip...", style="Big.TButton", command=self._save_zip).pack(side="left", padx=12)
        ttk.Button(self.done_frame, text="Show in folder", command=self._open_folder).pack(side="left")
        self.done_note = ttk.Label(self.done_frame, text="", wraplength=300)
        self.done_note.pack(side="left", padx=10)

        self.log = ScrolledText(self, height=12, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, **pad)

    # ---- events from worker threads -------------------------------------------------------------------

    def _post(self, kind: str, payload=None) -> None:
        self.events.put((kind, payload))

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                getattr(self, f"_on_{kind}")(payload)
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _on_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_step(self, payload) -> None:
        text, fraction = payload
        self.step_label.configure(text=text)
        if fraction is not None:
            self.progress["value"] = fraction

    def _on_attention(self, text: str) -> None:
        self.attention.configure(text=text)
        if text:
            self.bell()

    def _on_error(self, text: str) -> None:
        messagebox.showwarning("NSMBW Profiler", text, parent=self)

    def _on_done(self, payload) -> None:
        zip_path, status = payload
        self.zip_path = Path(zip_path)
        complete = status == "complete"
        self.done_label.configure(text="DONE!" if complete else "Stopped")
        self.done_note.configure(text="" if complete else f"Partial capture ({status}). The zip has what was recorded.")
        self.done_frame.pack(fill="x", padx=12, pady=6, before=self.log)
        self.progress["value"] = 1.0
        self.step_label.configure(text=f"Saved to {zip_path}")
        self.attention.configure(text="")

    def _on_finished(self, _payload) -> None:
        self.worker = None
        self.capture = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if self.closing:
            self.destroy()

    def _on_adb(self, path) -> None:
        if path and Path(path).is_file():
            self.adb = adbmod.Adb(Path(path))
            self.adb_frame.pack_forget()
            self._refresh_devices()
        else:
            self.adb_label.configure(text="adb (Android's debugging tool) isn't on this computer. The app can download "
                                          "Google's official platform-tools (about 7 MB) for you:")
            self.adb_frame.pack(fill="x", padx=12, pady=4, after=self.top_frame)

    def _on_devices(self, devices) -> None:
        if isinstance(devices, str):
            self.device_note.configure(text=devices)
            return
        current = self.device_box.get()
        self.devices = devices
        self.device_box["values"] = [d.label for d in devices]
        if devices:
            labels = [d.label for d in devices]
            self.device_box.set(current if current in labels else labels[0])
            self.device_note.configure(text="")
            self._device_chosen()
        else:
            self.device_box.set("")
            self.device_note.configure(text="No phone found: plug it in with USB debugging on, then Refresh.")

    def _on_releases(self, data) -> None:
        self.older_button.configure(state="normal")
        if isinstance(data, str):
            self.installed_note.configure(text="")
            messagebox.showinfo("NSMBW Profiler", f"Couldn't get the list of versions from github.com ({data}).\n\n"
                                "'Latest release' still works: it's looked up when you press Start.", parent=self)
            return
        self.release_list = data
        current = self.version_box.get()
        others = [v for v in self.version_box["values"] if v != LATEST and v not in {r.label for r in data}]
        self.version_box["values"] = [LATEST] + [r.label for r in data] + others
        self.version_box.set(current)
        self.older_button.configure(text=f"{len(data)} versions listed")

    def _on_installed(self, text: str) -> None:
        self.installed_note.configure(text=text)

    # ---- actions --------------------------------------------------------------------------------------

    def _background(self, work, kind: str) -> None:
        def run():
            try:
                self._post(kind, work())
            except Exception as error:  # noqa: BLE001
                self._post(kind, str(error))
        threading.Thread(target=run, daemon=True).start()

    def _find_adb(self) -> None:
        self._background(lambda: str(adbmod.find_adb() or ""), "adb")

    def _download_adb(self) -> None:
        self.adb_button.configure(state="disabled")
        self.step_label.configure(text="Downloading platform-tools from Google...")

        def work():
            try:
                return str(adbmod.download_platform_tools(
                    lambda f: self._post("step", (f"Downloading platform-tools: {f * 100:.0f}%", f))))
            except Exception as error:  # noqa: BLE001
                self._post("error", f"Downloading adb failed: {error}")
                self._post("adb_retry", None)
                return ""
        self._background(work, "adb")

    def _on_adb_retry(self, _payload) -> None:
        self.adb_button.configure(state="normal")

    def _refresh_devices(self) -> None:
        if self.adb:
            self._background(self.adb.devices, "devices")

    def _load_releases(self) -> None:
        """Only when asked: the default, Latest, needs nothing until Start."""
        self.older_button.configure(state="disabled")
        self._background(releases.list_releases, "releases")

    def _selected_device(self) -> adbmod.Device | None:
        labels = [d.label for d in self.devices]
        label = self.device_box.get()
        return self.devices[labels.index(label)] if label in labels else None

    def _device_chosen(self) -> None:
        device = self._selected_device()
        if not device or device.state != "device" or not self.adb:
            self.installed_note.configure(text="")
            return
        phone = adbmod.Phone(self.adb, device.serial)

        def work():
            text = phone.sh(f"dumpsys package {PACKAGE} | grep -m1 versionName", 20).strip()
            return f"On the phone: {text.split('=', 1)[1]}" if "=" in text else "NSMBW isn't installed on this phone"
        self._background(work, "installed")

    def _version_chosen(self) -> None:
        if self.version_box.get() == APK_FILE:
            chosen = filedialog.askopenfilename(parent=self, title="NSMBW APK", filetypes=[("Android app", "*.apk")])
            if chosen:
                self.apk_file = Path(chosen)
                values = list(self.version_box["values"])
                label = f"File: {self.apk_file.name}"
                values = [v for v in values if not v.startswith("File: ")]
                values.insert(len(values) - 2, label)
                self.version_box["values"] = values
                self.version_box.set(label)
            else:
                self.version_box.set(LATEST)

    def _read_options(self) -> Options | None:
        # Not "_options": tkinter calls the window's _options() inside every dialog (messagebox, file picker).
        # A method of that name here broke all of them in 1.0.0, so Start did nothing.
        device = self._selected_device()
        if not self.adb or not device:
            messagebox.showinfo("NSMBW Profiler", "Pick a phone first.", parent=self)
            return None
        if device.state != "device":
            messagebox.showinfo("NSMBW Profiler", "That phone isn't ready: " + device.label, parent=self)
            return None
        worlds = [i + 1 for i, var in enumerate(self.world_vars) if var.get()]
        if not worlds:
            messagebox.showinfo("NSMBW Profiler", "Pick at least one level.", parent=self)
            return None
        try:
            seconds = max(30, min(180, int(self.seconds.get())))
        except (tk.TclError, ValueError):
            seconds = 60
        choice = self.version_box.get()
        options = Options(serial=device.serial, worlds=worlds, play_seconds=seconds,
                          trace=TRACE_CHOICES.get(self.trace.get(), "detailed"))
        if choice == LATEST:
            options.version = "latest"
        elif choice == INSTALLED:
            options.version = "installed"
        elif choice.startswith("File: ") and self.apk_file:
            options.version, options.apk_file = "file", self.apk_file
        else:
            release = next((r for r in self.release_list if r.label == choice), None)
            if release is None:
                messagebox.showinfo("NSMBW Profiler", "Pick a version.", parent=self)
                return None
            options.version, options.release = "release", release
        return options

    def _start(self) -> None:
        options = self._read_options()
        if options is None:
            return
        minutes = round(2 + len(options.worlds) * (options.play_seconds + 75) / 60)
        if not messagebox.askokcancel(
                "NSMBW Profiler",
                f"This takes about {minutes} minutes. NSMBW will be closed if it's open, then started and closed "
                f"{len(options.worlds) + 1} times.\n\nBefore you start:\n"
                "- Keep the phone unlocked and plugged in, and don't touch it until it says DONE!\n"
                "- Last time you played, you saved on the world map with Mario standing on a level (not inside a "
                "level, not on a Toad house or cannon).\n\n"
                "Your save is copied to this computer first, the levels are only played part-way, and the game is "
                "closed the moment Mario leaves a level, before it could save. At the end the tool checks the "
                "save files are unchanged.", parent=self):
            return
        self.done_frame.pack_forget()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.progress["value"] = 0
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.capture = Capture(self.adb, options, self._post)

        def run():
            try:
                self.capture.run()
            finally:
                self._post("finished", None)
        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        if self.capture:
            self.capture.cancel.set()
            self.stop_button.configure(state="disabled")
            self.step_label.configure(text="Stopping: closing the game and cleaning up...")

    def _save_zip(self) -> None:
        if not self.zip_path or not self.zip_path.exists():
            return
        target = filedialog.asksaveasfilename(parent=self, title="Save the capture", defaultextension=".zip",
                                              initialfile=self.zip_path.name, filetypes=[("Zip", "*.zip")])
        if target and Path(target).resolve() != self.zip_path.resolve():
            shutil.copyfile(self.zip_path, target)
            self.step_label.configure(text=f"Saved to {target}")

    def _open_folder(self) -> None:
        if not self.zip_path:
            return
        folder = str(self.zip_path.parent)
        if platform.system() == "Windows":
            subprocess.run(["explorer", "/select,", str(self.zip_path)])
        elif platform.system() == "Darwin":
            subprocess.run(["open", folder])
        else:
            subprocess.run(["xdg-open", folder])

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("NSMBW Profiler", "A run is going. Stop it and quit? (The game is closed and "
                                                         "the phone cleaned up first.)", parent=self):
                return
            self.closing = True
            self._stop()
            return
        self.destroy()


def main() -> None:
    if sys.stderr is None:  # the packaged (windowed) app has no console: keep its output in a log file
        log = open(paths.data_dir() / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    App().mainloop()
