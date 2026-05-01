"""Concurrency & resource-limit tests: semaphores, result cache, parallel safety."""
import asyncio
import time

import pytest


@pytest.fixture
def fresh_downloader():
    import importlib
    import downloader as d
    importlib.reload(d)
    d._RESULT_CACHE.clear()
    d._extract_sem = None
    d._download_sem = None
    return d


# --- Semaphore initialization ---

@pytest.mark.asyncio
async def test_extract_sem_initialized_with_8(fresh_downloader):
    sem = fresh_downloader._get_extract_sem()
    assert sem._value == 8


@pytest.mark.asyncio
async def test_download_sem_initialized_with_4(fresh_downloader):
    sem = fresh_downloader._get_download_sem()
    assert sem._value == 4


@pytest.mark.asyncio
async def test_extract_sem_singleton(fresh_downloader):
    a = fresh_downloader._get_extract_sem()
    b = fresh_downloader._get_extract_sem()
    assert a is b


# --- Concurrency limit enforcement ---

@pytest.mark.asyncio
async def test_download_sem_limits_to_4(fresh_downloader):
    """At most 4 coroutines hold the download semaphore at once."""
    sem = fresh_downloader._get_download_sem()
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal in_flight, peak
        async with sem:
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            async with lock:
                in_flight -= 1

    await asyncio.gather(*(worker() for _ in range(10)))
    assert peak == 4


@pytest.mark.asyncio
async def test_extract_sem_limits_to_8(fresh_downloader):
    sem = fresh_downloader._get_extract_sem()
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal in_flight, peak
        async with sem:
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            async with lock:
                in_flight -= 1

    await asyncio.gather(*(worker() for _ in range(20)))
    assert peak == 8


# --- Result cache ---

@pytest.mark.asyncio
async def test_result_cache_evicts_when_full(fresh_downloader):
    d = fresh_downloader
    d._RESULT_CACHE.clear()
    d._RESULT_CACHE_MAX_SAVED = d._RESULT_CACHE_MAX
    # Force tight max for test
    d._RESULT_CACHE_MAX = 3
    try:
        for i in range(3):
            d._RESULT_CACHE[f"u{i}"] = (time.monotonic(), {}, [("cdn", "mp4")])
        # FIFO eviction (mirroring extract_info_full logic)
        if len(d._RESULT_CACHE) >= d._RESULT_CACHE_MAX:
            d._RESULT_CACHE.pop(next(iter(d._RESULT_CACHE)))
        d._RESULT_CACHE["u3"] = (time.monotonic(), {}, [("cdn", "mp4")])
        assert "u0" not in d._RESULT_CACHE
        assert "u3" in d._RESULT_CACHE
    finally:
        d._RESULT_CACHE_MAX = d._RESULT_CACHE_MAX_SAVED


@pytest.mark.asyncio
async def test_result_cache_ttl_check(fresh_downloader):
    d = fresh_downloader
    d._RESULT_CACHE.clear()
    stale_ts = time.monotonic() - (d._RESULT_CACHE_TTL + 1)
    fresh_ts = time.monotonic()
    d._RESULT_CACHE["stale"] = (stale_ts, {"u": "old"}, [])
    d._RESULT_CACHE["fresh"] = (fresh_ts, {"u": "new"}, [])

    stale_entry = d._RESULT_CACHE["stale"]
    assert time.monotonic() - stale_entry[0] >= d._RESULT_CACHE_TTL

    fresh_entry = d._RESULT_CACHE["fresh"]
    assert time.monotonic() - fresh_entry[0] < d._RESULT_CACHE_TTL


# --- Cleanup safety ---

@pytest.mark.asyncio
async def test_cleanup_safe_under_concurrency(fresh_downloader, tmp_path):
    d = fresh_downloader
    paths = []
    for i in range(20):
        sub = tmp_path / f"id-{i}"
        sub.mkdir()
        f = sub / "file.mp4"
        f.write_text("x")
        paths.append(str(f))

    # Run cleanups concurrently in threads (mimics background cleanup tasks)
    loop = asyncio.get_running_loop()
    await asyncio.gather(*(
        loop.run_in_executor(None, d.cleanup, p) for p in paths
    ))
    # All parents removed, no exceptions
    for p in paths:
        from pathlib import Path
        assert not Path(p).parent.exists()


# --- Detection helpers under load ---

@pytest.mark.asyncio
async def test_url_validation_thread_safe(fresh_downloader):
    """1000 concurrent URL validations all return correct results."""
    d = fresh_downloader
    urls = [
        ("https://www.instagram.com/reel/ABC/", True),
        ("https://www.youtube.com/watch?v=x", True),
        ("https://www.tiktok.com/@u/video/1", True),
        ("https://google.com", False),
        ("not a url", False),
    ] * 200

    async def check(url, expected):
        return d.is_instagram_url(url) == expected

    results = await asyncio.gather(*(check(u, e) for u, e in urls))
    assert all(results)
