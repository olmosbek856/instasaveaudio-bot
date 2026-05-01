"""Resilience tests: cookie-expiry mapping, in-flight de-dupe, age-based temp purge."""
import asyncio
import os
import time
from pathlib import Path

import pytest


# --- CookieExpiredError detection ---

def test_looks_like_ig_auth_failure_recognises_common_messages():
    from downloader import _looks_like_ig_auth_failure

    assert _looks_like_ig_auth_failure(Exception("HTTP Error 401: Unauthorized"))
    assert _looks_like_ig_auth_failure(Exception("Login required to view this content"))
    assert _looks_like_ig_auth_failure(Exception("checkpoint required"))
    assert _looks_like_ig_auth_failure(Exception("Fetching Profile foo is empty"))


def test_looks_like_ig_auth_failure_ignores_transient_errors():
    from downloader import _looks_like_ig_auth_failure

    assert not _looks_like_ig_auth_failure(Exception("Connection reset by peer"))
    assert not _looks_like_ig_auth_failure(Exception("Read timed out"))
    assert not _looks_like_ig_auth_failure(Exception("DNS lookup failed"))


# --- In-flight de-dupe ---

def test_inflight_dedupe_collapses_concurrent_requests(monkeypatch):
    """Two concurrent extract_info_full calls for the same URL must call the
    underlying _do_extract_info exactly once."""
    import downloader

    downloader._RESULT_CACHE.clear()
    downloader._inflight.clear()
    call_count = {"n": 0}

    async def fake_do_extract(url, height=None):
        call_count["n"] += 1
        await asyncio.sleep(0.05)
        return ({"title": "x"}, [("https://cdn/x.mp4", "mp4")])

    monkeypatch.setattr(downloader, "_do_extract_info", fake_do_extract)

    async def go():
        return await asyncio.gather(
            downloader.extract_info_full("https://example.com/p/abc"),
            downloader.extract_info_full("https://example.com/p/abc"),
            downloader.extract_info_full("https://example.com/p/abc"),
        )

    results = asyncio.run(go())
    assert call_count["n"] == 1
    assert all(r[0]["title"] == "x" for r in results)
    downloader._RESULT_CACHE.clear()
    downloader._inflight.clear()


def test_inflight_releases_on_exception(monkeypatch):
    """If extraction fails, _inflight must drop the key so the next call retries."""
    import downloader

    downloader._RESULT_CACHE.clear()
    downloader._inflight.clear()

    async def fake_fail(url, height=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(downloader, "_do_extract_info", fake_fail)

    async def go():
        with pytest.raises(RuntimeError):
            await downloader.extract_info_full("https://example.com/p/fail")
        # In-flight key must be cleaned up
        assert ("https://example.com/p/fail", None) not in downloader._inflight

    asyncio.run(go())


# --- Age-based temp purge ---

def test_purge_stale_temp_keeps_fresh_files(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:dummy")
    import importlib
    import bot as bot_module
    importlib.reload(bot_module)
    monkeypatch.setattr(bot_module, "TEMP_DIR", str(tmp_path))

    fresh = tmp_path / "fresh_uuid"
    stale = tmp_path / "stale_uuid"
    fresh.mkdir()
    stale.mkdir()
    (fresh / "x.mp4").write_text("x")
    (stale / "y.mp4").write_text("y")

    # Backdate the stale dir by 2h
    old = time.time() - 7200
    os.utime(stale, (old, old))
    os.utime(stale / "y.mp4", (old, old))

    bot_module._purge_stale_temp(max_age_seconds=3600)
    assert fresh.exists()
    assert not stale.exists()


def test_purge_stale_temp_handles_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:dummy")
    import importlib
    import bot as bot_module
    importlib.reload(bot_module)
    monkeypatch.setattr(bot_module, "TEMP_DIR", str(tmp_path / "nonexistent"))
    # Must not raise
    bot_module._purge_stale_temp(max_age_seconds=60)
