"""Music recognition tests — Shazam mocked so the suite runs offline."""
import asyncio
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def stub_shazamio(monkeypatch):
    """Provide a fake shazamio module so recognizer imports cleanly without the
    real package or its native deps installed."""
    fake_module = types.ModuleType("shazamio")

    class FakeShazam:
        async def recognize(self, path):
            return {"track": {"title": "stub", "subtitle": "stub artist"}}

    fake_module.Shazam = FakeShazam
    monkeypatch.setitem(sys.modules, "shazamio", fake_module)
    yield fake_module


@pytest.fixture
def fresh_recognizer(monkeypatch, tmp_path, stub_shazamio):
    """Reload recognizer with isolated TEMP_DIR and clean module state."""
    import importlib
    import config
    monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path / "temp"))
    os.makedirs(config.TEMP_DIR, exist_ok=True)
    import recognizer as r
    importlib.reload(r)
    r._shazam = None
    r._shazam_sem = None
    return r


def test_make_workdir_creates_unique_dirs(fresh_recognizer):
    a = fresh_recognizer.make_workdir()
    b = fresh_recognizer.make_workdir()
    assert a != b
    assert os.path.isdir(a)
    assert os.path.isdir(b)


def test_cleanup_removes_workdir(fresh_recognizer):
    workdir = fresh_recognizer.make_workdir()
    Path(workdir, "junk.mp3").write_text("x")
    fresh_recognizer.cleanup(workdir)
    assert not os.path.exists(workdir)


def test_cleanup_safe_when_missing(fresh_recognizer):
    fresh_recognizer.cleanup("/nonexistent/path-xyz")  # must not raise


@pytest.mark.asyncio
async def test_recognize_returns_track_dict(fresh_recognizer):
    fake_track = {
        "title": "Hello",
        "subtitle": "Adele",
        "url": "https://shazam.com/track/123",
        "share": {"image": "https://img/cover.jpg"},
        "hub": {"actions": [
            {"type": "applemusicplay", "uri": "applemusic://albums/1"},
        ]},
    }

    class FakeShazam:
        async def recognize(self, path):
            return {"track": fake_track}

    fresh_recognizer._shazam = FakeShazam()
    result = await fresh_recognizer.recognize("/tmp/clip.mp3")
    assert result == {
        "title": "Hello",
        "artist": "Adele",
        "url": "https://shazam.com/track/123",
        "cover": "https://img/cover.jpg",
        "apple": "applemusic://albums/1",
    }


@pytest.mark.asyncio
async def test_recognize_returns_none_when_unidentified(fresh_recognizer):
    class FakeShazam:
        async def recognize(self, path):
            return {"track": None}

    fresh_recognizer._shazam = FakeShazam()
    assert await fresh_recognizer.recognize("/tmp/x.mp3") is None


@pytest.mark.asyncio
async def test_recognize_handles_exceptions(fresh_recognizer):
    class FakeShazam:
        async def recognize(self, path):
            raise RuntimeError("boom")

    fresh_recognizer._shazam = FakeShazam()
    assert await fresh_recognizer.recognize("/tmp/x.mp3") is None


@pytest.mark.asyncio
async def test_recognize_handles_timeout(fresh_recognizer, monkeypatch):
    class HangingShazam:
        async def recognize(self, path):
            await asyncio.sleep(60)

    fresh_recognizer._shazam = HangingShazam()
    monkeypatch.setattr(fresh_recognizer, "_RECOGNIZE_TIMEOUT", 0.05)
    assert await fresh_recognizer.recognize("/tmp/x.mp3") is None


@pytest.mark.asyncio
async def test_extract_audio_clip_invokes_ffmpeg(fresh_recognizer, tmp_path):
    """ffmpeg is invoked with -t (clip length), -vn (no video), and an mp3 codec."""
    captured: dict = {}

    class FakeProc:
        returncode = 0
        async def wait(self):
            # Simulate ffmpeg producing a non-empty file
            Path(captured["out"]).write_bytes(b"fake-mp3" * 16)

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        # The output is the last positional arg before the closing flags
        captured["out"] = args[-1]
        return FakeProc()

    monkeypatch_ctx = patch("asyncio.create_subprocess_exec", new=fake_exec)
    with monkeypatch_ctx:
        out_dir = str(tmp_path)
        result = await fresh_recognizer.extract_audio_clip("/fake/in.mp4", out_dir)

    assert result.endswith("clip.mp3")
    assert "-vn" in captured["args"]
    assert "-t" in captured["args"]
    assert "libmp3lame" in captured["args"]


@pytest.mark.asyncio
async def test_extract_audio_clip_raises_on_ffmpeg_failure(fresh_recognizer, tmp_path):
    class FakeProc:
        returncode = 1
        async def wait(self):
            return None

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    with patch("asyncio.create_subprocess_exec", new=fake_exec):
        with pytest.raises(RuntimeError):
            await fresh_recognizer.extract_audio_clip("/fake/in.mp4", str(tmp_path))


@pytest.mark.asyncio
async def test_shazam_semaphore_limits_to_2(fresh_recognizer):
    sem = fresh_recognizer._get_sem()
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
    assert peak == 2
