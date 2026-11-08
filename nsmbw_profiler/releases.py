"""The NSMBW recompilation's GitHub releases: which ones carry an Android APK, and downloading one."""
from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import paths

# The repository renamed itself once already; GitHub redirects old names, and the app lets this be changed.
DEFAULT_REPO = "WitherBlitz/NSMB-Wii-Recompilation"
APK_SUFFIX = "-android-arm64.apk"


@dataclass
class Release:
    tag: str
    name: str
    published: str
    prerelease: bool
    latest: bool
    apk_name: str
    apk_url: str
    apk_size: int
    apk_sha256: str  # "" when GitHub has no digest for the asset

    @property
    def label(self) -> str:
        marks = [m for m, on in (("Latest", self.latest), ("pre-release", self.prerelease)) if on]
        extra = f"  ({', '.join(marks)})" if marks else ""
        return f"{self.tag}  {self.published[:10]}{extra}"


def _get_json(url: str):
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                  "User-Agent": "nsmbw-android-profiler"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def list_releases(repo: str = DEFAULT_REPO) -> list[Release]:
    data = _get_json(f"https://api.github.com/repos/{repo}/releases?per_page=100")
    try:
        latest_tag = _get_json(f"https://api.github.com/repos/{repo}/releases/latest").get("tag_name")
    except Exception:
        latest_tag = None
    releases = []
    for item in data:
        if item.get("draft"):
            continue
        apk = next((a for a in item.get("assets", []) if a["name"].endswith(APK_SUFFIX)), None)
        if apk is None:
            continue
        digest = apk.get("digest") or ""
        releases.append(Release(
            tag=item["tag_name"], name=item.get("name") or item["tag_name"],
            published=item.get("published_at") or "", prerelease=bool(item.get("prerelease")),
            latest=item["tag_name"] == latest_tag, apk_name=apk["name"],
            apk_url=apk["browser_download_url"], apk_size=int(apk.get("size") or 0),
            apk_sha256=digest.split(":", 1)[1] if digest.startswith("sha256:") else ""))
    if releases and not any(r.latest for r in releases):
        first = next((r for r in releases if not r.prerelease), releases[0])
        first.latest = True
    return releases


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download_apk(release: Release, progress=None, cancelled=lambda: False) -> Path:
    """The release's APK, from the cache when already downloaded and intact."""
    target = paths.apk_cache() / release.apk_name
    if target.is_file() and (not release.apk_sha256 or sha256_file(target) == release.apk_sha256):
        return target
    partial = target.with_suffix(".part")
    request = urllib.request.Request(release.apk_url, headers={"User-Agent": "nsmbw-android-profiler"})
    with urllib.request.urlopen(request, timeout=60) as response, open(partial, "wb") as out:
        total = int(response.headers.get("Content-Length") or release.apk_size or 0)
        done = 0
        while True:
            if cancelled():
                raise InterruptedError("cancelled")
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    if release.apk_sha256 and sha256_file(partial) != release.apk_sha256:
        partial.unlink(missing_ok=True)
        raise IOError(f"{release.apk_name} did not match GitHub's SHA-256; try again")
    partial.replace(target)
    return target
