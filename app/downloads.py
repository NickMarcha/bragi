"""Where to get the Bragi apps, for the dashboard's download links.

The desktop tray app (client-v* releases) is the repository's "latest"
release, so its files have fixed /releases/latest/download/ links. The
Android app (android-v* releases) is published with --latest=false, so its
newest release is looked up through GitHub's API, hourly. Until a lookup
works (no internet, rate limit) the Android link is the release list
filtered to the app.

BRAGI_RELEASES_REPO points the links at a fork ("owner/name").
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request

logger = logging.getLogger("bragi.downloads")

REPO = os.environ.get("BRAGI_RELEASES_REPO", "NickMarcha/bragi")
_REFRESH_SECONDS = 3600
_RETRY_SECONDS = 300

# The newest release of each app, as {"tag": ..., "version": ..., "files": {name: url}}.
_latest: dict[str, dict] = {}


def newest(releases: list[dict], prefix: str) -> dict | None:
    """The first published release whose tag starts with prefix (GitHub
    lists newest first)."""
    for release in releases:
        tag = release.get("tag_name") or ""
        if tag.startswith(prefix) and not release.get("draft") and not release.get("prerelease"):
            return {"tag": tag, "version": tag[len(prefix):],
                    "files": {a["name"]: a["browser_download_url"] for a in release.get("assets") or []
                              if a.get("name") and a.get("browser_download_url")}}
    return None


def links(latest: dict[str, dict] | None = None) -> dict:
    """The dashboard's download links."""
    latest = _latest if latest is None else latest
    base = f"https://github.com/{REPO}/releases"
    client, android = latest.get("client"), latest.get("android")
    return {
        "windows": f"{base}/latest/download/BragiClient-win-Setup.exe",
        "linux": f"{base}/latest/download/BragiClient.AppImage",
        "client_version": client["version"] if client else None,
        "android": (android or {}).get("files", {}).get("BragiAndroid.apk") or f"{base}?q=android-v&expanded=true",
        "android_version": android["version"] if android else None,
        "releases": base,
    }


def _fetch() -> list[dict]:
    request = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases?per_page=50",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "bragi"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


async def watch() -> None:
    """Keeps the newest releases known, hourly."""
    while True:
        wait = _REFRESH_SECONDS
        try:
            releases = await asyncio.to_thread(_fetch)
            for app, prefix in (("client", "client-v"), ("android", "android-v")):
                if found := newest(releases, prefix):
                    _latest[app] = found
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("downloads: couldn't look up the newest releases: %s", exc)
            wait = _RETRY_SECONDS
        await asyncio.sleep(wait)
