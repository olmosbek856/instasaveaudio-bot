"""Production-readiness tests for bot.py: rate limiting, caches, language persistence."""
import json
import time
from pathlib import Path

import pytest


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
