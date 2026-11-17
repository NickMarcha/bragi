"""Download links for the Bragi apps in the dashboard (app/downloads.py)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import downloads
from app.main import app


def release(tag, *assets, draft=False, prerelease=False):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease,
            "assets": [{"name": a, "browser_download_url": f"https://example.test/{tag}/{a}"} for a in assets]}


RELEASES = [
    release("android-v0.1.15", "BragiAndroid.apk", draft=True),
    release("client-v0.1.22", "BragiClient-win-Setup.exe", "BragiClient.AppImage"),
    release("android-v0.1.14", "BragiAndroid.apk", "BragiAndroid.apk.sha256"),
    release("bragi-v0.3.10", "bragi_0.3.10_arm64.deb"),
    release("android-v0.1.13", "BragiAndroid.apk"),
]


def test_the_newest_published_release_of_each_app():
    android = downloads.newest(RELEASES, "android-v")
    assert android["version"] == "0.1.14"  # the draft is skipped
    assert android["files"]["BragiAndroid.apk"] == "https://example.test/android-v0.1.14/BragiAndroid.apk"
    assert downloads.newest(RELEASES, "client-v")["version"] == "0.1.22"
    assert downloads.newest(RELEASES, "nothing-v") is None


def test_links_before_and_after_a_lookup():
    before = downloads.links({})
    assert before["windows"].endswith("/releases/latest/download/BragiClient-win-Setup.exe")
    assert before["linux"].endswith("/releases/latest/download/BragiClient.AppImage")
    assert before["android"].endswith("/releases?q=android-v&expanded=true")
    assert before["android_version"] is None and before["client_version"] is None

    after = downloads.links({"client": downloads.newest(RELEASES, "client-v"),
                             "android": downloads.newest(RELEASES, "android-v")})
    assert after["android"] == "https://example.test/android-v0.1.14/BragiAndroid.apk"
    assert after["android_version"] == "0.1.14" and after["client_version"] == "0.1.22"


def test_the_dashboard_shows_the_links(monkeypatch, session):
    monkeypatch.setattr(downloads, "_latest", {"android": downloads.newest(RELEASES, "android-v")})
    page = TestClient(app).get("/").text
    assert "Get the Bragi apps" in page
    assert "BragiClient-win-Setup.exe" in page and "BragiClient.AppImage" in page
    assert "https://example.test/android-v0.1.14/BragiAndroid.apk" in page
