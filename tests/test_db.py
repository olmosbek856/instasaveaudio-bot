"""SQLite persistence + JSON migration tests."""
import asyncio
import json
import time

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Isolated DB per test. Re-import db module to reset module state."""
    import importlib
    import db as db_module
    importlib.reload(db_module)
    db_path = tmp_path / "data" / "bot.db"
    legacy = tmp_path / "user_langs.json"
    monkeypatch.setattr(db_module, "_DB_PATH", str(db_path))
    monkeypatch.setattr(db_module, "_LEGACY_LANGS_FILE", str(legacy))
    db_module._conn = None
    db_module._init_done = False
    db_module.init_sync()
    yield db_module
    db_module.close()


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_init_creates_schema(fresh_db):
    conn = fresh_db._get_conn()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "users" in tables
    assert "requests" in tables
    assert "media_cache" in tables


def test_lang_roundtrip(fresh_db):
    async def go():
        await fresh_db.set_lang(42, "ru")
        assert await fresh_db.get_lang(42) == "ru"
        await fresh_db.set_lang(42, "en")
        assert await fresh_db.get_lang(42) == "en"
        assert await fresh_db.get_lang(999) is None
    asyncio.run(go())


def test_set_lang_rejects_invalid_code(fresh_db):
    async def go():
        await fresh_db.set_lang(7, "fr")  # not in {uz, ru, en}
        assert await fresh_db.get_lang(7) is None
    asyncio.run(go())


def test_log_request_and_daily_count(fresh_db):
    async def go():
        for _ in range(5):
            await fresh_db.log_request(100, "url", platform="instagram", success=True)
        assert await fresh_db.daily_count(100) == 5
        assert await fresh_db.daily_count(101) == 0
    asyncio.run(go())


def test_ban_check(fresh_db):
    async def go():
        assert await fresh_db.is_banned(50) is False
        await fresh_db.set_banned(50, True)
        assert await fresh_db.is_banned(50) is True
        await fresh_db.set_banned(50, False)
        assert await fresh_db.is_banned(50) is False
    asyncio.run(go())


def test_legacy_json_migration(tmp_path, monkeypatch):
    import importlib
    import db as db_module
    importlib.reload(db_module)

    legacy = tmp_path / "user_langs.json"
    legacy.write_text(json.dumps({"1": "ru", "2": "en", "3": "uz", "bogus": "ru", "4": "fr"}))

    db_path = tmp_path / "data" / "bot.db"
    monkeypatch.setattr(db_module, "_DB_PATH", str(db_path))
    monkeypatch.setattr(db_module, "_LEGACY_LANGS_FILE", str(legacy))
    db_module._conn = None
    db_module._init_done = False
    db_module.init_sync()

    async def go():
        return await db_module.all_langs()
    langs = asyncio.run(go())

    assert langs == {1: "ru", 2: "en", 3: "uz"}  # bogus key + invalid lang dropped
    # Legacy file renamed → migration is one-shot
    assert not legacy.exists()
    assert (tmp_path / "user_langs.json.migrated").exists()
    db_module.close()


def test_media_cache_put_and_get(fresh_db):
    async def go():
        assert await fresh_db.media_cache_get("yt:abc:audio") is None
        await fresh_db.media_cache_put(
            "yt:abc:audio", "AgADfileid", "audio",
            duration=240, title="Test Song", uploader="Test Artist",
        )
        row = await fresh_db.media_cache_get("yt:abc:audio")
        assert row is not None
        assert row["file_id"] == "AgADfileid"
        assert row["kind"] == "audio"
        assert row["duration"] == 240
        assert row["title"] == "Test Song"
        assert row["uploader"] == "Test Artist"
        assert row["hits"] == 0
    asyncio.run(go())


def test_media_cache_put_upsert(fresh_db):
    """Second put with same key must overwrite (not duplicate)."""
    async def go():
        await fresh_db.media_cache_put("yt:abc:video:720", "fid_v1", "video")
        await fresh_db.media_cache_put("yt:abc:video:720", "fid_v2", "video")
        row = await fresh_db.media_cache_get("yt:abc:video:720")
        assert row["file_id"] == "fid_v2"
        # Only one row in the table
        conn = fresh_db._get_conn()
        n = conn.execute("SELECT COUNT(*) AS c FROM media_cache").fetchone()["c"]
        assert n == 1
    asyncio.run(go())


def test_media_cache_touch_increments_hits(fresh_db):
    async def go():
        await fresh_db.media_cache_put("yt:abc:audio", "AgAD", "audio")
        await fresh_db.media_cache_touch("yt:abc:audio")
        await fresh_db.media_cache_touch("yt:abc:audio")
        row = await fresh_db.media_cache_get("yt:abc:audio")
        assert row["hits"] == 2
    asyncio.run(go())


def test_media_cache_delete(fresh_db):
    async def go():
        await fresh_db.media_cache_put("yt:abc:audio", "AgAD", "audio")
        await fresh_db.media_cache_delete("yt:abc:audio")
        assert await fresh_db.media_cache_get("yt:abc:audio") is None
        # Deleting a missing key is a no-op (no exception).
        await fresh_db.media_cache_delete("nonexistent")
    asyncio.run(go())


def test_media_cache_evict_by_ttl(fresh_db):
    async def go():
        await fresh_db.media_cache_put("fresh", "fid_fresh", "audio")
        await fresh_db.media_cache_put("stale", "fid_stale", "audio")
        # Backdate the stale entry's last_used_at to 60 days ago.
        ancient = int(time.time()) - 60 * 86400
        fresh_db._get_conn().execute(
            "UPDATE media_cache SET last_used_at = ? WHERE content_key = ?",
            (ancient, "stale"),
        )
        deleted = await fresh_db.media_cache_evict(max_rows=10_000, ttl_days=30)
        assert deleted == 1
        assert await fresh_db.media_cache_get("stale") is None
        assert await fresh_db.media_cache_get("fresh") is not None
    asyncio.run(go())


def test_media_cache_evict_by_max_rows(fresh_db):
    async def go():
        for i in range(10):
            await fresh_db.media_cache_put(f"key{i}", f"fid{i}", "audio")
        deleted = await fresh_db.media_cache_evict(max_rows=4, ttl_days=10_000)
        assert deleted == 6
        n = fresh_db._get_conn().execute(
            "SELECT COUNT(*) AS c FROM media_cache"
        ).fetchone()["c"]
        assert n == 4
    asyncio.run(go())


def test_media_cache_extra_field_roundtrip(fresh_db):
    """media_group caching stores per-item file_ids in `extra` as JSON."""
    async def go():
        items = [
            {"type": "video", "file_id": "fid_v1"},
            {"type": "photo", "file_id": "fid_p1"},
        ]
        await fresh_db.media_cache_put(
            "ig:abc:video:720", "fid_v1", "media_group",
            extra=json.dumps(items),
        )
        row = await fresh_db.media_cache_get("ig:abc:video:720")
        assert row["kind"] == "media_group"
        assert json.loads(row["extra"]) == items
    asyncio.run(go())


def test_migration_idempotent_no_legacy_file(tmp_path, monkeypatch):
    """Re-running init when no legacy file exists must not error."""
    import importlib
    import db as db_module
    importlib.reload(db_module)
    monkeypatch.setattr(db_module, "_DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(db_module, "_LEGACY_LANGS_FILE", str(tmp_path / "missing.json"))
    db_module._conn = None
    db_module._init_done = False
    db_module.init_sync()
    db_module.init_sync()  # second call must be a no-op
    db_module.close()
