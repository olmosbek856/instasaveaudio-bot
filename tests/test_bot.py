"""Production-readiness tests for bot.py: rate limiting, caches, language persistence."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from aiogram.exceptions import TelegramBadRequest
except ImportError:  # aiogram < 3
    class TelegramBadRequest(Exception):  # type: ignore[no-redef]
        pass


@pytest.fixture
def fresh_bot(monkeypatch, tmp_path):
    """Re-import bot with isolated lang file and clean rate-limit/cache state."""
    monkeypatch.setenv("BOT_TOKEN", "123456789:dummy-token-for-pytest")
    import importlib
    import bot as bot_module
    importlib.reload(bot_module)
    bot_module._LANGS_FILE = str(tmp_path / "user_langs.json")
    bot_module._user_langs = {}
    bot_module._rate_store.clear()
    bot_module._url_cache.clear()
    bot_module._meta_cache.clear()
    return bot_module


# --- Rate limiting ---

def test_rate_limit_allows_first_3(fresh_bot):
    user_id = 100
    assert fresh_bot._is_rate_limited("url", user_id) is False
    assert fresh_bot._is_rate_limited("url", user_id) is False
    assert fresh_bot._is_rate_limited("url", user_id) is False


def test_rate_limit_blocks_4th(fresh_bot):
    user_id = 101
    for _ in range(3):
        fresh_bot._is_rate_limited("url", user_id)
    assert fresh_bot._is_rate_limited("url", user_id) is True


def test_rate_limit_per_user_isolated(fresh_bot):
    for _ in range(3):
        fresh_bot._is_rate_limited("url", 200)
    assert fresh_bot._is_rate_limited("url", 200) is True
    assert fresh_bot._is_rate_limited("url", 201) is False


def test_rate_limit_buckets_isolated(fresh_bot):
    """url and cb buckets shouldn't share a counter."""
    user_id = 250
    for _ in range(3):
        fresh_bot._is_rate_limited("url", user_id)
    # url bucket exhausted, but cb has its own (looser) budget.
    assert fresh_bot._is_rate_limited("url", user_id) is True
    assert fresh_bot._is_rate_limited("cb", user_id) is False


def test_rate_limit_cb_bucket_has_higher_limit(fresh_bot):
    user_id = 260
    # cb bucket allows 15 — 4th call must still pass.
    for _ in range(4):
        assert fresh_bot._is_rate_limited("cb", user_id) is False


def test_rate_limit_resets_after_window(fresh_bot, monkeypatch):
    user_id = 300
    base = 1000.0
    times = [base, base, base]
    idx = {"i": 0}
    def fake_monotonic():
        v = times[min(idx["i"], len(times) - 1)]
        idx["i"] += 1
        return v
    monkeypatch.setattr(fresh_bot.time, "monotonic", fake_monotonic)
    for _ in range(3):
        fresh_bot._is_rate_limited("url", user_id)
    times.append(base + 31)  # past 30s window
    assert fresh_bot._is_rate_limited("url", user_id) is False


# --- Language detection / persistence ---

def test_lang_default_uzbek_for_none(fresh_bot):
    assert fresh_bot._lang(None) == "uz"


def test_lang_uses_saved_preference(fresh_bot):
    fresh_bot._user_langs[42] = "ru"

    class FakeUser:
        id = 42
        language_code = "en"

    assert fresh_bot._lang(FakeUser()) == "ru"


def test_lang_falls_back_to_locale(fresh_bot):
    class RuUser:
        id = 999
        language_code = "ru-RU"

    class EnUser:
        id = 998
        language_code = "en-US"

    class XxUser:
        id = 997
        language_code = "xx"

    assert fresh_bot._lang(RuUser()) == "ru"
    assert fresh_bot._lang(EnUser()) == "en"
    assert fresh_bot._lang(XxUser()) == "uz"


def test_lang_save_load_roundtrip(fresh_bot):
    fresh_bot._save_langs({1: "ru", 2: "en", 3: "uz"})
    loaded = fresh_bot._load_langs()
    assert loaded == {1: "ru", 2: "en", 3: "uz"}


def test_lang_load_returns_empty_when_missing(fresh_bot, tmp_path):
    fresh_bot._LANGS_FILE = str(tmp_path / "does_not_exist.json")
    assert fresh_bot._load_langs() == {}


def test_lang_load_handles_corrupt_file(fresh_bot, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json {{{")
    fresh_bot._LANGS_FILE = str(bad)
    assert fresh_bot._load_langs() == {}


# --- URL cache eviction (FIFO) ---

def test_url_cache_evicts_oldest_when_full(fresh_bot):
    fresh_bot._URL_CACHE_MAX = 3
    fresh_bot._url_cache.clear()
    for i in range(3):
        fresh_bot._url_cache[f"k{i}"] = f"url{i}"
    # Simulate the eviction code in url_handler
    if len(fresh_bot._url_cache) >= fresh_bot._URL_CACHE_MAX:
        fresh_bot._url_cache.pop(next(iter(fresh_bot._url_cache)))
    fresh_bot._url_cache["k3"] = "url3"
    assert "k0" not in fresh_bot._url_cache
    assert "k3" in fresh_bot._url_cache
    assert len(fresh_bot._url_cache) == 3


def test_meta_cache_independent_of_url_cache(fresh_bot):
    fresh_bot._url_cache["a"] = "url_a"
    fresh_bot._meta_cache["a"] = {"uploader": "x"}
    assert fresh_bot._url_cache["a"] == "url_a"
    assert fresh_bot._meta_cache["a"]["uploader"] == "x"


# --- Quality keyboard ---

def test_quality_keyboard_has_five_buttons(fresh_bot):
    kb = fresh_bot._quality_keyboard("KEY123", "uz")
    flat = [b for row in kb.inline_keyboard for b in row]
    assert len(flat) == 5


def test_quality_keyboard_carries_url_key_in_callback_data(fresh_bot):
    kb = fresh_bot._quality_keyboard("KEY123", "uz")
    flat = [b for row in kb.inline_keyboard for b in row]
    assert all("KEY123" in b.callback_data for b in flat)


def test_quality_keyboard_includes_audio_option(fresh_bot):
    kb = fresh_bot._quality_keyboard("KEY", "en")
    flat = [b for row in kb.inline_keyboard for b in row]
    assert any(b.callback_data == "dl:audio:KEY" for b in flat)


def test_quality_keyboard_uses_dl_prefix_for_heights(fresh_bot):
    kb = fresh_bot._quality_keyboard("KEY", "en")
    flat = [b for row in kb.inline_keyboard for b in row]
    height_callbacks = {b.callback_data for b in flat if b.callback_data != "dl:audio:KEY"}
    assert height_callbacks == {"dl:480:KEY", "dl:720:KEY", "dl:1080:KEY", "dl:2160:KEY"}


# --- URL pre-check (cheap text classifier) ---

def test_is_url_text_accepts_https(fresh_bot):
    assert fresh_bot._is_url_text("https://example.com/x") is True


def test_is_url_text_accepts_http(fresh_bot):
    assert fresh_bot._is_url_text("http://example.com/x") is True


def test_is_url_text_rejects_plain_chat(fresh_bot):
    assert fresh_bot._is_url_text("hello") is False
    assert fresh_bot._is_url_text("salom how are you") is False


def test_is_url_text_strips_whitespace(fresh_bot):
    assert fresh_bot._is_url_text("   https://x.com   ") is True


# --- file_id cache (the speed-up feature) ---

def _make_aiogram_bad_request():
    """TelegramBadRequest's signature varies across aiogram versions — build one
    via __new__ so tests don't depend on the constructor."""
    exc = TelegramBadRequest.__new__(TelegramBadRequest)
    Exception.__init__(exc, "stale file_id")
    return exc


def test_try_video_cache_hit_returns_false_on_miss(fresh_bot, monkeypatch):
    """No cached entry → return False so caller falls through to fresh download."""
    async def go():
        monkeypatch.setattr(fresh_bot.db, "media_cache_get", AsyncMock(return_value=None))
        msg = MagicMock()
        result = await fresh_bot._try_video_cache_hit(msg, "yt:abc:video:720", "cap", None)
        assert result is False
        # No telegram call attempted on miss.
        msg.answer_video.assert_not_called()
    asyncio.run(go())


def test_try_video_cache_hit_replays_video(fresh_bot, monkeypatch):
    """Cache hit on a 'video' kind → calls answer_video with cached file_id."""
    async def go():
        cached = {
            "kind": "video",
            "file_id": "AgADcached_video_id",
            "extra": None,
        }
        monkeypatch.setattr(fresh_bot.db, "media_cache_get", AsyncMock(return_value=cached))
        touch_mock = AsyncMock()
        monkeypatch.setattr(fresh_bot.db, "media_cache_touch", touch_mock)
        msg = MagicMock()
        msg.answer_video = AsyncMock(return_value=MagicMock())
        result = await fresh_bot._try_video_cache_hit(msg, "yt:abc:video:720", "cap", None)
        assert result is True
        msg.answer_video.assert_awaited_once()
        kwargs = msg.answer_video.await_args.kwargs
        assert kwargs["video"] == "AgADcached_video_id"
        assert kwargs["caption"] == "cap"
        touch_mock.assert_awaited_once_with("yt:abc:video:720")
    asyncio.run(go())


def test_try_video_cache_hit_replays_photo(fresh_bot, monkeypatch):
    async def go():
        cached = {"kind": "photo", "file_id": "AgADphoto", "extra": None}
        monkeypatch.setattr(fresh_bot.db, "media_cache_get", AsyncMock(return_value=cached))
        monkeypatch.setattr(fresh_bot.db, "media_cache_touch", AsyncMock())
        msg = MagicMock()
        msg.answer_photo = AsyncMock(return_value=MagicMock())
        result = await fresh_bot._try_video_cache_hit(msg, "ig:abc:video:default", "c", None)
        assert result is True
        msg.answer_photo.assert_awaited_once()
        assert msg.answer_photo.await_args.kwargs["photo"] == "AgADphoto"
    asyncio.run(go())


def test_try_video_cache_hit_replays_media_group(fresh_bot, monkeypatch):
    async def go():
        items = [
            {"type": "video", "file_id": "fid_v1"},
            {"type": "photo", "file_id": "fid_p1"},
        ]
        cached = {
            "kind": "media_group", "file_id": "fid_v1", "extra": json.dumps(items),
        }
        monkeypatch.setattr(fresh_bot.db, "media_cache_get", AsyncMock(return_value=cached))
        monkeypatch.setattr(fresh_bot.db, "media_cache_touch", AsyncMock())
        msg = MagicMock()
        msg.answer_media_group = AsyncMock(return_value=[MagicMock(), MagicMock()])
        result = await fresh_bot._try_video_cache_hit(msg, "ig:carousel:video:720", "c", None)
        assert result is True
        msg.answer_media_group.assert_awaited_once()
        media = msg.answer_media_group.await_args.kwargs["media"]
        assert len(media) == 2
    asyncio.run(go())


def test_try_video_cache_hit_drops_stale_entry(fresh_bot, monkeypatch):
    """Stale file_id → TelegramBadRequest → cache row deleted, returns False."""
    async def go():
        cached = {"kind": "video", "file_id": "stale_fid", "extra": None}
        monkeypatch.setattr(fresh_bot.db, "media_cache_get", AsyncMock(return_value=cached))
        delete_mock = AsyncMock()
        monkeypatch.setattr(fresh_bot.db, "media_cache_delete", delete_mock)
        msg = MagicMock()
        msg.answer_video = AsyncMock(side_effect=_make_aiogram_bad_request())
        result = await fresh_bot._try_video_cache_hit(msg, "yt:abc:video:720", "cap", None)
        assert result is False
        delete_mock.assert_awaited_once_with("yt:abc:video:720")
    asyncio.run(go())


def test_persist_media_cache_video(fresh_bot, monkeypatch):
    """A successful answer_video reply gets its file_id persisted."""
    async def go():
        put_mock = AsyncMock()
        monkeypatch.setattr(fresh_bot.db, "media_cache_put", put_mock)
        sent = SimpleNamespace(
            video=SimpleNamespace(file_id="AgADfid", file_size=1234567, duration=42),
            photo=None, audio=None,
        )
        await fresh_bot._persist_media_cache("yt:abc:video:720", sent)
        put_mock.assert_awaited_once()
        args, kwargs = put_mock.await_args.args, put_mock.await_args.kwargs
        assert args[0] == "yt:abc:video:720"
        assert args[1] == "AgADfid"
        assert args[2] == "video"
        assert kwargs["duration"] == 42
        assert kwargs["file_size"] == 1234567
    asyncio.run(go())


def test_persist_media_cache_audio_keeps_metadata(fresh_bot, monkeypatch):
    """Audio cache must store title/performer for replay metadata."""
    async def go():
        put_mock = AsyncMock()
        monkeypatch.setattr(fresh_bot.db, "media_cache_put", put_mock)
        sent = SimpleNamespace(
            audio=SimpleNamespace(
                file_id="AgADaudio", file_size=4_000_000, duration=180,
                title="Despacito", performer="Luis Fonsi",
            ),
            video=None, photo=None,
        )
        await fresh_bot._persist_media_cache("yt:abc:audio:default", sent)
        put_mock.assert_awaited_once()
        kwargs = put_mock.await_args.kwargs
        assert kwargs["title"] == "Despacito"
        assert kwargs["uploader"] == "Luis Fonsi"
        assert kwargs["duration"] == 180
    asyncio.run(go())


def test_persist_media_cache_media_group(fresh_bot, monkeypatch):
    """List of Messages → stores all file_ids in extra as JSON, kind='media_group'."""
    async def go():
        put_mock = AsyncMock()
        monkeypatch.setattr(fresh_bot.db, "media_cache_put", put_mock)
        sent = [
            SimpleNamespace(video=SimpleNamespace(file_id="fid_v1"), photo=None),
            SimpleNamespace(photo=[SimpleNamespace(file_id="fid_p1")], video=None),
        ]
        await fresh_bot._persist_media_cache("ig:abc:video:720", sent)
        put_mock.assert_awaited_once()
        args, kwargs = put_mock.await_args.args, put_mock.await_args.kwargs
        assert args[2] == "media_group"
        items = json.loads(kwargs["extra"])
        assert items == [
            {"type": "video", "file_id": "fid_v1"},
            {"type": "photo", "file_id": "fid_p1"},
        ]
    asyncio.run(go())


def test_persist_media_cache_swallows_errors(fresh_bot, monkeypatch):
    """db put failure must NOT propagate — caching is best-effort."""
    async def go():
        monkeypatch.setattr(
            fresh_bot.db, "media_cache_put",
            AsyncMock(side_effect=RuntimeError("db down")),
        )
        sent = SimpleNamespace(
            video=SimpleNamespace(file_id="fid", file_size=1, duration=1),
            photo=None, audio=None,
        )
        # Must not raise.
        await fresh_bot._persist_media_cache("yt:abc:video:720", sent)
    asyncio.run(go())


def test_persist_media_cache_skips_when_key_or_sent_missing(fresh_bot, monkeypatch):
    """Empty cache_key or empty `sent` → don't even call put."""
    async def go():
        put_mock = AsyncMock()
        monkeypatch.setattr(fresh_bot.db, "media_cache_put", put_mock)
        await fresh_bot._persist_media_cache("", MagicMock())
        await fresh_bot._persist_media_cache("yt:abc:video:720", None)
        put_mock.assert_not_called()
    asyncio.run(go())
