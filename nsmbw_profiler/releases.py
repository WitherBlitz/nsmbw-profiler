"""NSMBW releases straight from github.com (not GitHub's API, which can time out or rate-limit): which
version is the latest, the recent ones on request, and the APK, downloaded once and then kept."""
from __future__ import annotations

import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

from . import paths

# The repository renamed itself once already; github.com redirects old names, and the app lets this be changed.
DEFAULT_REPO = "WitherBlitz/NSMB-Wii-Recompilation"
SITE = "https://github.com"
HEADERS = {"User-Agent": "nsmbw-android-profiler"}


class NoApk(IOError):
    """The release has no Android build."""


@dataclass
class Release:
    tag: str
    published: str = ""
    title: str = ""
    latest: bool = False

    @property
    def apk_name(self) -> str:
        # Every release with an Android build names it NSMBW-<version>-android-arm64.apk.
        return f"NSMBW-{self.tag.lstrip('v')}-android-arm64.apk"

    def apk_url(self, repo: str) -> str:
        return f"{SITE}/{repo}/releases/download/{urllib.parse.quote(self.tag)}/{self.apk_name}"

    @property
    def label(self) -> str:
        return f"{self.tag}  {self.published[:10]}" + ("  (newest)" if self.latest else "")


def _open(url: str, method: str = "GET", timeout: float = 30, tries: int = 4):
    """urlopen with a few tries for what goes away by itself: a 5xx (a 504 timeout), a dropped connection."""
    for attempt in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, method=method, headers=HEADERS), timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == tries - 1:
                raise
        time.sleep(2 + 3 * attempt)


def latest(repo: str = DEFAULT_REPO) -> Release:
    """The release GitHub marks Latest: github.com/<repo>/releases/latest redirects to its tag."""
    with _open(f"{SITE}/{repo}/releases/latest", method="HEAD") as response:
        final = response.geturl()
    m = re.search(r"/releases/tag/([^/?#]+)$", final)
    if not m:
        raise IOError(f"{repo} has no release yet")
    return Release(urllib.parse.unquote(m.group(1)), latest=True)


def list_releases(repo: str = DEFAULT_REPO) -> list[Release]:
    """The recent releases (github.com's release feed: the newest ten), newest first."""
    with _open(f"{SITE}/{repo}/releases.atom") as response:
        root = ElementTree.fromstring(response.read())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    found = []
    for entry in root.findall("a:entry", ns):
        link = entry.find("a:link", ns)
        m = re.search(r"/releases/tag/([^/?#]+)$", link.get("href", "") if link is not None else "")
        if m:
            found.append(Release(urllib.parse.unquote(m.group(1)), entry.findtext("a:updated", "", ns),
                                 entry.findtext("a:title", "", ns)))
    if found:
        found[0].latest = True
    return found


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cached_apk(release: Release) -> Path | None:
    path = paths.apk_cache() / release.apk_name
    return path if path.is_file() else None


def download_apk(release: Release, repo: str = DEFAULT_REPO, progress=None, cancelled=lambda: False) -> Path:
    """The release's APK. One downloaded before is used as it is: nothing is fetched again."""
    if (kept := cached_apk(release)) is not None:
        return kept
    target = paths.apk_cache() / release.apk_name
    partial = target.with_suffix(".part")
    try:
        response = _open(release.apk_url(repo), timeout=60)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise NoApk(f"{release.tag} has no Android build (no {release.apk_name} on its release page)") from error
        raise
    with response, open(partial, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
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
    if total and done != total:
        partial.unlink(missing_ok=True)
        raise IOError(f"the download of {release.apk_name} stopped part-way ({done} of {total} bytes); try again")
    partial.replace(target)
    return target
