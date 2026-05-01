"""SQLite-backed persistence for users, language prefs, request log, and quotas.

Single shared connection (WAL mode, synchronous=NORMAL) — safe with our async
single-threaded event loop. All blocking calls are dispatched to the default
executor so the loop never stalls on disk I/O.

Schema is created idempotently on import_init(). One-shot migration from the
legacy `user_langs.json` runs on first boot and renames the file so it never
runs twice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable

_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "bot.db")
_LEGACY_LANGS_FILE = os.path.join(os.path.dirname(__file__), "user_langs.json")

_conn: sqlite3.Connection | None = None
_init_done = False


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(
            _DB_PATH,
            check_same_thread=False,
            isolation_level=None,  # autocommit; we use explicit transactions where needed
        )
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.row_factory = sqlite3.Row
    return _conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    lang         TEXT NOT NULL DEFAULT 'uz',
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    is_banned    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    platform     TEXT,
    success      INTEGER NOT NULL,
    duration_ms  INTEGER,
    error_kind   TEXT,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_user_day ON requests(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_requests_created ON requests(created_at);
"""


def init_sync() -> None:
    """Create schema and migrate legacy JSON. Safe to call multiple times."""
    global _init_done
    if _init_done:
        return
    conn = _get_conn()
    conn.executescript(_SCHEMA)
    _migrate_legacy_langs(conn)
    _init_done = True


def _migrate_legacy_langs(conn: sqlite3.Connection) -> None:
    """One-shot import of user_langs.json into users table, then rename file."""
    if not os.path.isfile(_LEGACY_LANGS_FILE):
        return
    try:
        with open(_LEGACY_LANGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logging.exception("legacy user_langs.json unreadable — skipping migration")
        return
    if not isinstance(data, dict) or not data:
        # Empty or junk — still rename so we don't keep retrying.
        _rename_legacy()
        return

    now = int(time.time())
    rows: list[tuple[int, str, int, int]] = []
    for k, v in data.items():
        try:
            uid = int(k)
        except (TypeError, ValueError):
            continue
        if v not in ("uz", "ru", "en"):
            continue
        rows.append((uid, v, now, now))
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO users(user_id, lang, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        logging.info("Migrated %d users from user_langs.json into SQLite", len(rows))
    _rename_legacy()


def _rename_legacy() -> None:
    target = _LEGACY_LANGS_FILE + ".migrated"
    try:
        os.replace(_LEGACY_LANGS_FILE, target)
    except Exception:
        logging.exception("Failed to rename legacy user_langs.json")


# ── Sync primitives (used inside run_in_executor) ──────────────────────────

def _get_lang_sync(user_id: int) -> str | None:
    row = _get_conn().execute(
        "SELECT lang FROM users WHERE user_id = ?", (user_id,),
    ).fetchone()
    return row["lang"] if row else None


def _set_lang_sync(user_id: int, lang: str) -> None:
    if lang not in ("uz", "ru", "en"):
        return
    now = int(time.time())
    _get_conn().execute(
        "INSERT INTO users(user_id, lang, first_seen, last_seen) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang, last_seen = excluded.last_seen",
        (user_id, lang, now, now),
    )


def _touch_user_sync(user_id: int, default_lang: str) -> None:
    now = int(time.time())
    _get_conn().execute(
        "INSERT INTO users(user_id, lang, first_seen, last_seen) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET last_seen = excluded.last_seen",
        (user_id, default_lang, now, now),
    )


def _is_banned_sync(user_id: int) -> bool:
    row = _get_conn().execute(
        "SELECT is_banned FROM users WHERE user_id = ?", (user_id,),
    ).fetchone()
    return bool(row and row["is_banned"])


def _set_banned_sync(user_id: int, banned: bool) -> None:
    now = int(time.time())
    _get_conn().execute(
        "INSERT INTO users(user_id, lang, first_seen, last_seen, is_banned) "
        "VALUES (?, 'uz', ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET is_banned = excluded.is_banned",
        (user_id, now, now, 1 if banned else 0),
    )


def _log_request_sync(
    user_id: int,
    kind: str,
    *,
    platform: str | None = None,
    success: bool = True,
    duration_ms: int | None = None,
    error_kind: str | None = None,
) -> None:
    _get_conn().execute(
        "INSERT INTO requests(user_id, kind, platform, success, duration_ms, error_kind, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, kind, platform, 1 if success else 0, duration_ms, error_kind, int(time.time())),
    )


def _daily_count_sync(user_id: int) -> int:
    cutoff = int(time.time()) - 86400
    row = _get_conn().execute(
        "SELECT COUNT(*) AS c FROM requests "
        "WHERE user_id = ? AND created_at >= ? AND kind != 'rate_limited'",
        (user_id, cutoff),
    ).fetchone()
    return int(row["c"]) if row else 0


def _all_langs_sync() -> dict[int, str]:
    rows = _get_conn().execute("SELECT user_id, lang FROM users").fetchall()
    return {int(r["user_id"]): r["lang"] for r in rows}


def _stats_summary_sync() -> dict:
    """Lightweight admin summary (used by /admin in future, harmless to leave)."""
    conn = _get_conn()
    today = int(time.time()) - 86400
    total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    active_24h = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE last_seen >= ?", (today,),
    ).fetchone()["c"]
    req_24h = conn.execute(
        "SELECT COUNT(*) AS c, SUM(success) AS ok FROM requests WHERE created_at >= ?",
        (today,),
    ).fetchone()
    return {
        "total_users": int(total_users),
        "active_24h": int(active_24h),
        "requests_24h": int(req_24h["c"] or 0),
        "successes_24h": int(req_24h["ok"] or 0),
    }


# ── Async wrappers ─────────────────────────────────────────────────────────

async def _run(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def get_lang(user_id: int) -> str | None:
    return await _run(_get_lang_sync, user_id)


async def set_lang(user_id: int, lang: str) -> None:
    await _run(_set_lang_sync, user_id, lang)


async def touch_user(user_id: int, default_lang: str = "uz") -> None:
    await _run(_touch_user_sync, user_id, default_lang)


async def is_banned(user_id: int) -> bool:
    return await _run(_is_banned_sync, user_id)


async def set_banned(user_id: int, banned: bool) -> None:
    await _run(_set_banned_sync, user_id, banned)


async def log_request(
    user_id: int,
    kind: str,
    *,
    platform: str | None = None,
    success: bool = True,
    duration_ms: int | None = None,
    error_kind: str | None = None,
) -> None:
    await _run(
        _log_request_sync,
        user_id, kind,
        platform=platform, success=success,
        duration_ms=duration_ms, error_kind=error_kind,
    )


async def daily_count(user_id: int) -> int:
    return await _run(_daily_count_sync, user_id)


async def all_langs() -> dict[int, str]:
    return await _run(_all_langs_sync)


async def stats_summary() -> dict:
    return await _run(_stats_summary_sync)


def close() -> None:
    """Close the shared connection. Called from graceful shutdown."""
    global _conn, _init_done
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            logging.exception("db.close: connection close failed")
        _conn = None
    _init_done = False


# Test hook — lets test_db.py rebind the DB path before init_sync().
def _set_db_path_for_tests(path: str) -> None:
    global _DB_PATH, _conn, _init_done
    close()
    _DB_PATH = path
    _init_done = False
