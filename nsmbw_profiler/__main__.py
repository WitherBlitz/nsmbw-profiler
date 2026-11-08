"""python -m nsmbw_profiler: the window, or with --cli a run without one (for scripts and SSH)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cli(argv: list[str]) -> int:
    from . import adb as adbmod, releases
    from .capture import Capture, Options

    parser = argparse.ArgumentParser(prog="nsmbw_profiler --cli")
    parser.add_argument("--serial", required=True, help="adb serial of the phone")
    parser.add_argument("--version", default="latest", help="a release tag, 'latest' or 'installed'")
    parser.add_argument("--apk", type=Path, help="install this APK file instead of a release")
    parser.add_argument("--worlds", default="1,2,3,4,5,6,7,8,9", help="worlds whose level 1 to play, e.g. 1,2,9")
    parser.add_argument("--seconds", type=int, default=60, help="seconds of play per level (30-180)")
    parser.add_argument("--trace", choices=("detailed", "light", "off"), default="detailed",
                        help="Perfetto system trace detail (detailed makes the biggest zip)")
    parser.add_argument("--repo", default=releases.DEFAULT_REPO)
    args = parser.parse_args(argv)

    path = adbmod.find_adb() or adbmod.download_platform_tools()
    options = Options(serial=args.serial, worlds=[int(w) for w in args.worlds.split(",") if w.strip()],
                      play_seconds=max(30, min(180, args.seconds)), trace=args.trace, repo=args.repo)
    if args.apk:
        options.version, options.apk_file = "file", args.apk
    elif args.version in ("installed", "latest"):
        options.version = args.version
    else:
        options.version, options.release = "release", releases.Release(args.version)

    def ui(kind, payload):
        if kind == "log":
            print(payload, flush=True)
        elif kind in ("error", "attention") and payload:
            print(f"!! {payload}", flush=True)
        elif kind == "done":
            print(f"ZIP {payload[0]} ({payload[1]})", flush=True)

    result = Capture(adbmod.Adb(path), options, ui).run()
    return 0 if result else 1


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        sys.exit(cli(sys.argv[2:]))
    from .app import main as gui
    gui()


if __name__ == "__main__":
    main()
