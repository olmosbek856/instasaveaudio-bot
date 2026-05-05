import asyncio
import html as _html
import json
import logging
import os
import shutil
import signal
import time
import uuid
from collections import defaultdict
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    User,
)

import db
from config import (
    ADMIN_USER_IDS,
    BOT_TOKEN,
    DAILY_QUOTA,
    DATA_DIR,
    HEALTH_FILE,
    LOG_LEVEL,
    MAX_FILE_SIZE_BYTES,
    SENTRY_DSN,
    TEMP_DIR,
)
import downloader
from downloader import (
    CookieExpiredError,
    _get_instaloader,
    _is_safe_public_url,
    cleanup,
    content_cache_key,
    detect_content_type,
    download_audio,
    download_cdn_url,
    download_media,
    extract_direct_urls,
    extract_info_full,
    fetch_instagram_meta,
    is_supported_url,
    search_and_download_audio,
    search_music,
)
from messages import get_message
import recognizer


class _DefaultsFilter(logging.Filter):
    """Inject `rid` and `uid` defaults so the formatter never KeyErrors when
    a log call doesn't pass them via `extra=`."""
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "rid"):
            record.rid = "-"
        if not hasattr(record, "uid"):
            record.uid = "-"
        return True


_root_handler = logging.StreamHandler()
_root_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] rid=%(rid)s uid=%(uid)s %(message)s",
))
_root_handler.addFilter(_DefaultsFilter())
logging.basicConfig(level=LOG_LEVEL, handlers=[_root_handler], force=True)


# Optional Sentry integration. Skipped silently if SENTRY_DSN is empty
# or if sentry-sdk is not installed (the dep is optional in requirements.txt).
if SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=0.0,
            send_default_pii=False,
            max_breadcrumbs=20,
        )
        logging.info("Sentry enabled")
    except ImportError:
        logging.warning("SENTRY_DSN set but sentry-sdk not installed; skipping init")
    except Exception:
        logging.exception("Sentry init failed; continuing without it")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill in your token.")

bot = Bot(
    token=BOT_TOKEN,
    session=AiohttpSession(timeout=300),
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()

_URL_CACHE_MAX = 10000
_url_cache: dict[str, str] = {}
_meta_cache: dict[str, dict] = {}  # url_key → {title, uploader, thumbnail}

# Bot username, populated at startup from get_me(). Used by the
# "Add to group" button so it works regardless of which token is deployed.
_BOT_USERNAME: str = "instasaveaudio_bot"  # fallback if get_me() hasn't run

# Music search pagination state
_SEARCH_CACHE_MAX = 100
_SEARCH_RESULTS_PER_PAGE = 10
_SEARCH_TOTAL_LIMIT = 50  # ~5 pages
_search_cache: dict[str, dict] = {}  # search_id → {query, results, url_keys}

# Per-bucket (window_seconds, max_requests). URL submissions are tight because
# each one triggers a yt-dlp/instaloader call. Callbacks are a free click on a
# cached state, so the limit is looser. Shazam files are mid-weight (ffmpeg +
# Shazam call + YT search/download).
_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "url":    (30, 3),
    "cb":     (30, 15),
    "shazam": (60, 5),
}
_rate_store: dict[tuple[str, int], list[float]] = defaultdict(list)

# Heights offered to the user. yt-dlp falls back to "best" when a specific
# height isn't available, so users always get something even on low-res sources.
QUALITY_HEIGHTS = (480, 720, 1080, 2160)

def _is_rate_limited(bucket: str, user_id: int) -> bool:
    window, limit = _RATE_LIMITS[bucket]
    now = time.monotonic()
    key = (bucket, user_id)
    _rate_store[key] = [t for t in _rate_store[key] if now - t < window]
    if len(_rate_store[key]) >= limit:
        return True
    _rate_store[key].append(now)
    return False

_LANGS_FILE = os.path.join(os.path.dirname(__file__), "user_langs.json")

def _load_langs() -> dict[int, str]:
    try:
        with open(_LANGS_FILE, encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        return {}

def _save_langs(langs: dict[int, str]) -> None:
    # Atomic write — partial json.dump on crash/disk-full would otherwise
    # leave the file empty and silently wipe every user's saved language.
    tmp = _LANGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in langs.items()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _LANGS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def _save_langs_async(langs: dict[int, str]) -> None:
    """Off-loop disk write so a slow disk doesn't stall message dispatch."""
    loop = asyncio.get_running_loop()
    # Snapshot the dict to avoid mutation during the write thread's lifetime.
    await loop.run_in_executor(None, _save_langs, dict(langs))

_user_langs: dict[int, str] = _load_langs()


# Throttle admin DM alerts so a flood of identical errors doesn't spam.
# Keyed by error_kind → monotonic timestamp of last alert.
_admin_alert_throttle: dict[str, float] = {}
_ADMIN_ALERT_INTERVAL_SEC = 3600.0  # at most one DM per error_kind per hour

async def _check_user_allowed(user_id: int) -> tuple[bool, str | None]:
    """Returns (allowed, reason). reason ∈ {'banned', 'quota', None}.
    Admins bypass both checks. Logs are emitted by the caller after sending feedback."""
    if user_id in ADMIN_USER_IDS:
        return True, None
    try:
        if await db.is_banned(user_id):
            return False, "banned"
        if DAILY_QUOTA > 0 and await db.daily_count(user_id) >= DAILY_QUOTA:
            return False, "quota"
    except Exception:
        # DB hiccup must not break the bot — fail open. The next request
        # will retry; the noise floor is logged for ops.
        logging.exception("daily-quota / ban check failed for uid=%d", user_id)
    return True, None


async def _alert_admins(error_kind: str, detail: str = "") -> None:
    """Best-effort DM to ADMIN_USER_IDS, throttled per error_kind."""
    if not ADMIN_USER_IDS:
        return
    now = time.monotonic()
    if now - _admin_alert_throttle.get(error_kind, 0.0) < _ADMIN_ALERT_INTERVAL_SEC:
        return
    _admin_alert_throttle[error_kind] = now
    text = f"⚠️ <b>{_html.escape(error_kind)}</b>"
    if detail:
        text += f"\n<code>{_html.escape(detail[:200])}</code>"
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logging.exception("alert_admins: failed to DM %d", admin_id)


def _lang(user: User | None) -> str:
    if user is None:
        return "uz"
    if user.id in _user_langs:
        return _user_langs[user.id]
    lc = user.language_code or ""
    if lc.startswith("ru"):
        return "ru"
    if lc.startswith("en"):
        return "en"
    return "uz"


def _lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇿  O'zbekcha", callback_data="lang:uz")],
        [InlineKeyboardButton(text="🇷🇺  Русский",   callback_data="lang:ru")],
        [InlineKeyboardButton(text="🇬🇧  English",   callback_data="lang:en")],
    ])


def _start_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=get_message(lang, "btn_add_to_group"),
            url=f"https://t.me/{_BOT_USERNAME}?startgroup",
        )
    ]])


def _quality_keyboard(url_key: str, lang: str) -> InlineKeyboardMarkup:
    btn = lambda key, data: InlineKeyboardButton(text=get_message(lang, key), callback_data=data)
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("btn_quality_480",  f"dl:480:{url_key}"),  btn("btn_quality_720", f"dl:720:{url_key}")],
        [btn("btn_quality_1080", f"dl:1080:{url_key}"), btn("btn_quality_4k",  f"dl:2160:{url_key}")],
        [btn("btn_quality_audio", f"dl:audio:{url_key}")],
    ])


def _audio_keyboard(url_key: str, lang: str) -> InlineKeyboardMarkup:
    """Audio-extraction button shown under a delivered video."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=get_message(lang, "btn_audio"), callback_data=f"fa:{url_key}"),
    ]])


async def _send_media(
    message: Message,
    file_path: str,
    lang_code: str = "uz",
    caption: str = "",
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Returns the sent Message so callers can capture file_id for caching."""
    if os.path.getsize(file_path) > MAX_FILE_SIZE_BYTES:
        await message.answer(get_message(lang_code, "too_large"))
        return None

    ext = Path(file_path).suffix.lower()
    file = FSInputFile(file_path)

    if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        return await message.answer_video(video=file, caption=caption, reply_markup=reply_markup)
    elif ext in (".jpg", ".jpeg", ".png", ".webp"):
        return await message.answer_photo(photo=file, caption=caption, reply_markup=reply_markup)
    else:
        return await message.answer_document(document=file, caption=caption, reply_markup=reply_markup)


async def _persist_media_cache(
    cache_key: str,
    sent: Message | list[Message] | None,
) -> None:
    """Best-effort capture of file_id(s) from a successful send. Errors swallowed —
    caching must never break the user flow.

    For media groups we store the per-item list (file_id + type) in `extra` as
    JSON so a cache hit can rebuild the exact same group.
    """
    if not cache_key or not sent:
        return
    try:
        if isinstance(sent, list):
            items: list[dict] = []
            for m in sent:
                if getattr(m, "video", None):
                    items.append({"type": "video", "file_id": m.video.file_id})
                elif getattr(m, "photo", None):
                    items.append({"type": "photo", "file_id": m.photo[-1].file_id})
            if items:
                await db.media_cache_put(
                    cache_key, items[0]["file_id"], "media_group",
                    extra=json.dumps(items),
                )
            return
        if getattr(sent, "video", None):
            await db.media_cache_put(
                cache_key, sent.video.file_id, "video",
                file_size=getattr(sent.video, "file_size", None),
                duration=getattr(sent.video, "duration", None),
            )
        elif getattr(sent, "photo", None):
            await db.media_cache_put(
                cache_key, sent.photo[-1].file_id, "photo",
                file_size=getattr(sent.photo[-1], "file_size", None),
            )
        elif getattr(sent, "audio", None):
            await db.media_cache_put(
                cache_key, sent.audio.file_id, "audio",
                file_size=getattr(sent.audio, "file_size", None),
                duration=getattr(sent.audio, "duration", None),
                title=getattr(sent.audio, "title", None),
                uploader=getattr(sent.audio, "performer", None),
            )
    except Exception:
        logging.exception("media_cache persist failed for key=%s", cache_key)


async def _try_video_cache_hit(
    message: Message,
    cache_key: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> bool:
    """Replay a cached video/photo/media_group send. Returns True on hit, False on miss
    or stale entry (which is auto-deleted)."""
    try:
        cached = await db.media_cache_get(cache_key)
    except Exception:
        return False
    if not cached:
        return False
    try:
        kind = cached["kind"]
        if kind == "video":
            await message.answer_video(
                video=cached["file_id"], caption=caption, reply_markup=reply_markup,
            )
        elif kind == "photo":
            await message.answer_photo(
                photo=cached["file_id"], caption=caption, reply_markup=reply_markup,
            )
        elif kind == "media_group":
            items = json.loads(cached.get("extra") or "[]")
            if not items:
                raise RuntimeError("empty media_group cache")
            media_list: list = []
            for i, item in enumerate(items):
                cap = caption if i == 0 else ""
                if item["type"] == "photo":
                    media_list.append(InputMediaPhoto(media=item["file_id"], caption=cap))
                else:
                    media_list.append(InputMediaVideo(media=item["file_id"], caption=cap))
            await message.answer_media_group(media=media_list)
            if reply_markup:
                await message.answer(caption, reply_markup=reply_markup)
        else:
            return False
        try:
            await db.media_cache_touch(cache_key)
        except Exception:
            pass
        return True
    except TelegramBadRequest:
        # Stale file_id — drop and fall through to fresh download.
        try:
            await db.media_cache_delete(cache_key)
        except Exception:
            pass
        return False
    except Exception:
        logging.exception("cache hit replay failed for key=%s", cache_key)
        return False


async def _send_video_content(
    message: Message,
    url: str,
    user_lang: str,
    height: int | None = None,
    prefetched_cdn: list[tuple[str, str]] | None = None,
    caption: str = "",
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Download and send video/photo content. Tries cache → CDN → disk."""
    if not caption:
        caption = get_message(user_lang, "attribution")

    # Persistent file_id cache — instant replay if we've sent this before.
    cache_key = content_cache_key(url, "video", height)
    if cache_key and await _try_video_cache_hit(message, cache_key, caption, reply_markup):
        return

    file_paths: list[str] = []
    try:
        cdn_items = (
            prefetched_cdn
            if prefetched_cdn is not None
            else await extract_direct_urls(url, height=height)
        )
        if cdn_items:
            if len(cdn_items) == 1:
                cdn_url, ext = cdn_items[0]
                is_photo = ext in ("jpg", "jpeg", "png", "webp")
                try:
                    if is_photo:
                        sent = await message.answer_photo(photo=cdn_url, caption=caption, reply_markup=reply_markup)
                    else:
                        sent = await message.answer_video(video=cdn_url, caption=caption, reply_markup=reply_markup)
                    if cache_key:
                        asyncio.create_task(_persist_media_cache(cache_key, sent))
                    return
                except TelegramBadRequest:
                    # Telegram can't fetch Instagram CDN — download directly via aiohttp
                    try:
                        fp = await download_cdn_url(cdn_url, ext)
                        file_paths = [fp]
                        sent = await _send_media(message, fp, lang_code=user_lang, caption=caption, reply_markup=reply_markup)
                        if cache_key:
                            asyncio.create_task(_persist_media_cache(cache_key, sent))
                        return
                    except Exception:
                        pass  # Last resort: full yt-dlp re-download
            else:
                try:
                    media_list = []
                    for i, (cdn_url, ext) in enumerate(cdn_items):
                        is_photo = ext in ("jpg", "jpeg", "png", "webp")
                        cap = caption if i == 0 else ""
                        if is_photo:
                            media_list.append(InputMediaPhoto(media=cdn_url, caption=cap))
                        else:
                            media_list.append(InputMediaVideo(media=cdn_url, caption=cap))
                    sent = await message.answer_media_group(media=media_list)
                    if cache_key:
                        asyncio.create_task(_persist_media_cache(cache_key, sent))
                    if reply_markup:
                        await message.answer(caption, reply_markup=reply_markup)
                    return
                except TelegramBadRequest:
                    # Download each CDN URL directly via aiohttp. Append to
                    # file_paths as we go so a partial failure still gets
                    # cleaned up by the outer finally.
                    try:
                        for cdn_u, cdn_e in cdn_items:
                            file_paths.append(await download_cdn_url(cdn_u, cdn_e))
                        fps = file_paths
                        media_list = []
                        for i, fp in enumerate(fps):
                            e = Path(fp).suffix.lower().lstrip(".")
                            cap = caption if i == 0 else ""
                            f = FSInputFile(fp)
                            if e in ("jpg", "jpeg", "png", "webp"):
                                media_list.append(InputMediaPhoto(media=f, caption=cap))
                            else:
                                media_list.append(InputMediaVideo(media=f, caption=cap))
                        sent = await message.answer_media_group(media=media_list)
                        if cache_key:
                            asyncio.create_task(_persist_media_cache(cache_key, sent))
                        if reply_markup:
                            await message.answer(caption, reply_markup=reply_markup)
                        return
                    except Exception:
                        pass  # Last resort: full yt-dlp re-download

        file_paths = await download_media(url, height=height)
        if len(file_paths) == 1:
            sent = await _send_media(message, file_paths[0], lang_code=user_lang, caption=caption, reply_markup=reply_markup)
            if cache_key:
                asyncio.create_task(_persist_media_cache(cache_key, sent))
        else:
            media_list = []
            for i, fp in enumerate(file_paths):
                ext = Path(fp).suffix.lower().lstrip(".")
                is_photo = ext in ("jpg", "jpeg", "png", "webp")
                cap = caption if i == 0 else ""
                f = FSInputFile(fp)
                if is_photo:
                    media_list.append(InputMediaPhoto(media=f, caption=cap))
                else:
                    media_list.append(InputMediaVideo(media=f, caption=cap))
            sent = await message.answer_media_group(media=media_list)
            if cache_key:
                asyncio.create_task(_persist_media_cache(cache_key, sent))
            if reply_markup:
                await message.answer(caption, reply_markup=reply_markup)
    finally:
        # Each path may live in its own UUID temp dir (download_cdn_url creates
        # one per call), so wipe every distinct parent — not just the first.
        seen_parents: set[str] = set()
        for fp in file_paths:
            parent = str(Path(fp).parent)
            if parent in seen_parents:
                continue
            seen_parents.add(parent)
            cleanup(fp)


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    lang = _lang(message.from_user)
    await message.answer(get_message(lang, "choose_lang"), reply_markup=_lang_keyboard())


@dp.message(Command("lang"))
async def lang_handler(message: Message) -> None:
    lang = _lang(message.from_user)
    await message.answer(get_message(lang, "choose_lang"), reply_markup=_lang_keyboard())


@dp.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(get_message(_lang(message.from_user), "help"))


@dp.message(Command("stats"))
async def stats_handler(message: Message) -> None:
    """Admin-only usage summary. Silently ignored for non-admins."""
    if not message.from_user or message.from_user.id not in ADMIN_USER_IDS:
        return
    try:
        s = await db.stats_summary()
        # Per-platform breakdown for the last 24h, fetched directly so the
        # admin sees where traffic is concentrated.
        loop = asyncio.get_running_loop()
        def _platform_breakdown() -> list[tuple[str, int, int]]:
            cutoff = int(time.time()) - 86400
            rows = db._get_conn().execute(
                "SELECT COALESCE(platform,'?') AS p, COUNT(*) AS n, SUM(success) AS ok "
                "FROM requests WHERE created_at >= ? GROUP BY p ORDER BY n DESC",
                (cutoff,),
            ).fetchall()
            return [(r["p"], int(r["n"]), int(r["ok"] or 0)) for r in rows]
        breakdown = await loop.run_in_executor(None, _platform_breakdown)
    except Exception:
        logging.exception("stats_handler: db read failed")
        await message.answer("⚠️ Stats unavailable (DB error).")
        return

    lines = [
        "📊 <b>Bot statistikasi</b>",
        "",
        f"👥 Jami foydalanuvchilar: <b>{s['total_users']}</b>",
        f"🟢 24 soatda faol: <b>{s['active_24h']}</b>",
        f"📥 24 soatda so'rovlar: <b>{s['requests_24h']}</b> "
        f"(muvaffaqiyatli: {s['successes_24h']})",
    ]
    if breakdown:
        lines.append("")
        lines.append("<b>Platforma bo'yicha (24h):</b>")
        for platform, n, ok in breakdown:
            rate = f"{(ok / n * 100):.0f}%" if n else "—"
            lines.append(f"• <code>{_html.escape(platform)}</code> — {n} ({rate})")
    await message.answer("\n".join(lines))


@dp.callback_query(F.data.startswith("lang:"))
async def lang_callback(callback: CallbackQuery) -> None:
    chosen = callback.data[len("lang:"):]
    if chosen not in ("uz", "ru", "en"):
        await callback.answer()
        return
    user_id = callback.from_user.id
    _user_langs[user_id] = chosen
    try:
        await db.set_lang(user_id, chosen)
    except Exception:
        # SQLite hiccup must not block the user — the in-memory cache still
        # serves them this session; we fall back to JSON persistence.
        logging.exception("db.set_lang failed for uid=%d", user_id)
        try:
            await _save_langs_async(_user_langs)
        except Exception:
            logging.exception("JSON fallback save_langs also failed")
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(get_message(chosen, "start"), reply_markup=_start_keyboard(chosen))



async def _deliver_audio(
    callback: CallbackQuery,
    url_key: str,
    url: str,
    user_lang: str,
    *,
    strip_markup_on_success: bool = False,
) -> None:
    """Shared audio-extraction path used by 'fa:', 'sa:', and 'dl:audio:' callbacks.

    When `strip_markup_on_success` is True, the source message's reply markup
    is cleared after a successful send — used by the 'fa:' (Audio button under
    a video) callback so the user can't re-trigger the same expensive download
    by re-clicking. Search-result pickers ('sa:') keep their markup.
    """
    # Persistent file_id cache — instant replay if we've delivered this audio before.
    audio_cache_key = content_cache_key(url, "audio")
    if audio_cache_key:
        try:
            cached = await db.media_cache_get(audio_cache_key)
        except Exception:
            cached = None
        if cached and cached.get("kind") == "audio":
            try:
                await callback.message.answer_audio(
                    audio=cached["file_id"],
                    title=cached.get("title") or None,
                    performer=cached.get("uploader") or None,
                    caption=get_message(user_lang, "attribution"),
                )
                try:
                    await db.media_cache_touch(audio_cache_key)
                except Exception:
                    pass
                if strip_markup_on_success:
                    try:
                        await callback.message.edit_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                # Best-effort delete of the "downloading…" status (no-op if not yet posted).
                return
            except TelegramBadRequest:
                # Stale file_id — drop and fall through to fresh download.
                try:
                    await db.media_cache_delete(audio_cache_key)
                except Exception:
                    pass
            except Exception:
                logging.exception("audio cache hit replay failed for %s", audio_cache_key)

    status_msg = await callback.message.answer(get_message(user_lang, "downloading"))

    meta = _meta_cache.get(url_key, {})
    audio_title = meta.get("title") or ""
    audio_performer = meta.get("uploader") or ""
    thumbnail_url = meta.get("thumbnail") or ""

    file_path: str | None = None
    try:
        file_path = await download_audio(url)
        audio_file = FSInputFile(file_path)
        thumb_input = None
        if thumbnail_url and _is_safe_public_url(thumbnail_url):
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(thumbnail_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            thumb_data = await resp.read()
                            thumb_input = BufferedInputFile(thumb_data, filename="thumb.jpg")
            except Exception:
                pass
        sent = await callback.message.answer_audio(
            audio=audio_file,
            title=audio_title or None,
            performer=audio_performer or None,
            thumbnail=thumb_input,
            caption=get_message(user_lang, "attribution"),
        )
        if audio_cache_key:
            asyncio.create_task(_persist_media_cache(audio_cache_key, sent))
        delivered = True
    except CookieExpiredError as exc:
        logging.error("cookie_expired (audio): %s", exc)
        await callback.message.answer(get_message(user_lang, "cookies_expired"))
        asyncio.create_task(_alert_admins("ig_cookies_expired", str(exc)))
        delivered = False
    except Exception as exc:
        logging.error("Audio download failed for %s: %s", url, exc)
        asyncio.create_task(_alert_admins(
            f"dl_fail_audio_{detect_content_type(url) or 'unknown'}",
            f"{url}\n{str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__}",
        ))
        await callback.message.answer(get_message(user_lang, "error"))
        delivered = False
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass
        if file_path:
            cleanup(file_path)

    # Markup cleanup is best-effort and runs after the main flow — its failure
    # must never trigger the outer error message that contradicts a successful
    # send. Catch broadly: TelegramRetryAfter / TelegramNetworkError can both
    # surface here on a busy or flaky connection.
    if delivered and strip_markup_on_success:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("fa:"))
async def audio_callback(callback: CallbackQuery) -> None:
    """Legacy audio-extraction button shown under a delivered video."""
    url_key = callback.data[3:]
    url = _url_cache.get(url_key, "")
    if not url:
        await callback.answer(get_message(_lang(callback.from_user), "stale_url"))
        return
    if not callback.message:
        await callback.answer("Error: no message context.")
        return

    user_lang = _lang(callback.from_user)
    if callback.from_user and _is_rate_limited("cb", callback.from_user.id):
        await callback.answer(get_message(user_lang, "rate_limit"), show_alert=True)
        return
    await callback.answer()
    await _deliver_audio(callback, url_key, url, user_lang, strip_markup_on_success=True)


@dp.callback_query(F.data.startswith("sa:"))
async def search_audio_callback(callback: CallbackQuery) -> None:
    """Numeric pick from search results — deliver audio without deleting the picker."""
    url_key = callback.data[3:]
    url = _url_cache.get(url_key, "")
    if not url:
        await callback.answer(get_message(_lang(callback.from_user), "stale_url"))
        return
    if not callback.message:
        await callback.answer("Error: no message context.")
        return
    user_lang = _lang(callback.from_user)
    if callback.from_user and _is_rate_limited("cb", callback.from_user.id):
        await callback.answer(get_message(user_lang, "rate_limit"), show_alert=True)
        return
    await callback.answer()
    await _deliver_audio(callback, url_key, url, user_lang)


@dp.callback_query(F.data.startswith("sp:"))
async def search_page_callback(callback: CallbackQuery) -> None:
    """Pagination ⬅️/➡️ on a search results message."""
    payload = callback.data[3:]
    if payload == "noop":
        await callback.answer()
        return
    parts = payload.split(":", 1)
    if len(parts) != 2:
        await callback.answer()
        return
    search_id, page_str = parts
    if search_id not in _search_cache:
        await callback.answer(get_message(_lang(callback.from_user), "stale_url"))
        return
    try:
        page = int(page_str)
    except ValueError:
        await callback.answer()
        return

    await callback.answer()
    text, kb = _render_search_page(search_id, page=page, lang=_lang(callback.from_user))
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.startswith("sx:"))
async def search_close_callback(callback: CallbackQuery) -> None:
    """❌ — drop the cached search and remove the message."""
    search_id = callback.data[3:]
    _search_cache.pop(search_id, None)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("dl:"))
async def download_callback(callback: CallbackQuery) -> None:
    """Quality picker — `dl:{height_or_audio}:{url_key}`."""
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    _, quality, url_key = parts

    url = _url_cache.get(url_key, "")
    if not url:
        await callback.answer(get_message(_lang(callback.from_user), "stale_url"))
        return
    if not callback.message:
        await callback.answer("Error: no message context.")
        return

    user_lang = _lang(callback.from_user)
    if callback.from_user and _is_rate_limited("cb", callback.from_user.id):
        await callback.answer(get_message(user_lang, "rate_limit"), show_alert=True)
        return
    await callback.answer()

    # Delete the picker prompt entirely — the user has chosen, the message has no further use.
    try:
        await callback.message.delete()
    except Exception:
        pass

    if quality == "audio":
        await _deliver_audio(callback, url_key, url, user_lang)
        return

    try:
        height = int(quality)
    except ValueError:
        # callback.answer() was already called above — calling it again raises
        # TelegramBadRequest. Just bail.
        return

    content_type = detect_content_type(url)
    status_key = {
        "reel":      "downloading_reel",
        "post":      "downloading_post",
        "story":     "downloading_story",
        "youtube":   "downloading_youtube",
        "tiktok":    "downloading_tiktok",
        "snapchat":  "downloading_snapchat",
        "likee":     "downloading_likee",
        "pinterest": "downloading_pinterest",
        "threads":   "downloading_threads",
    }.get(content_type, "downloading")
    status_msg = await callback.message.answer(get_message(user_lang, status_key))

    meta = _meta_cache.get(url_key, {})
    uploader = meta.get("uploader") or ""
    attribution = get_message(user_lang, "attribution")
    caption = f"📹 <b>{_html.escape(uploader)}</b>\n\n{attribution}" if uploader else attribution
    audio_kb = _audio_keyboard(url_key, user_lang)

    try:
        await _send_video_content(
            callback.message, url, user_lang,
            height=height,
            prefetched_cdn=None,
            caption=caption,
            reply_markup=audio_kb,
        )
    except CookieExpiredError as e:
        logging.error("cookie_expired (quality): %s", e)
        await callback.message.answer(get_message(user_lang, "cookies_expired"))
        asyncio.create_task(_alert_admins("ig_cookies_expired", str(e)))
    except Exception as exc:
        logging.exception("Quality download failed for %s @ %sp", url, height)
        asyncio.create_task(_alert_admins(
            f"dl_fail_quality_{detect_content_type(url) or 'unknown'}",
            f"{url} @{height}p\n{str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__}",
        ))
        await callback.message.answer(get_message(user_lang, "error"))
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass


def _is_url_text(text: str) -> bool:
    """Cheap pre-check so plain chatter is routed to music search instead of URL flow."""
    t = text.strip()
    return t.startswith("http://") or t.startswith("https://")


def _format_duration(seconds) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _render_search_page(search_id: str, page: int, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """Build the message text + inline keyboard for one search-results page."""
    entry = _search_cache[search_id]
    query = entry["query"]
    results = entry["results"]
    url_keys = entry["url_keys"]

    total_pages = max(1, (len(results) + _SEARCH_RESULTS_PER_PAGE - 1) // _SEARCH_RESULTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * _SEARCH_RESULTS_PER_PAGE
    end = start + _SEARCH_RESULTS_PER_PAGE
    page_results = results[start:end]
    page_keys = url_keys[start:end]

    lines = [f"🔎 <code>{_html.escape(query)}</code>", ""]
    for offset, r in enumerate(page_results):
        global_num = start + offset + 1  # continuous across pages: 1..10, 11..20, ...
        title = _html.escape((r.get("title") or "").strip())
        dur = _format_duration(r.get("duration"))
        lines.append(
            f"<b>{global_num}.</b> {title} <b>{dur}</b>"
            if dur
            else f"<b>{global_num}.</b> {title}"
        )
    text = "\n".join(lines)

    # Numeric picker — two rows of five (rounded up to actual count).
    num_buttons = [
        InlineKeyboardButton(text=str(i), callback_data=f"sa:{key}")
        for i, key in enumerate(page_keys, start=start + 1)
    ]
    rows: list[list[InlineKeyboardButton]] = []
    if num_buttons:
        rows.append(num_buttons[:5])
        if len(num_buttons) > 5:
            rows.append(num_buttons[5:10])

    # Pagination row: ⬅️ ❌ ➡️
    prev_data = f"sp:{search_id}:{page-1}" if page > 0 else "sp:noop"
    next_data = f"sp:{search_id}:{page+1}" if page < total_pages - 1 else "sp:noop"
    rows.append([
        InlineKeyboardButton(text="⬅️", callback_data=prev_data),
        InlineKeyboardButton(text="❌", callback_data=f"sx:{search_id}"),
        InlineKeyboardButton(text="➡️", callback_data=next_data),
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _handle_music_search(message: Message, query: str, lang: str) -> None:
    """Type a song/artist name → numbered list of YouTube results with paging."""
    status_msg = await message.answer(get_message(lang, "searching"))
    try:
        results = await search_music(query, limit=_SEARCH_TOTAL_LIMIT)
    except Exception:
        logging.exception("Music search failed for %r", query)
        try: await status_msg.delete()
        except Exception: pass
        await message.answer(get_message(lang, "search_failed"))
        return

    try: await status_msg.delete()
    except Exception: pass

    if not results:
        await message.answer(get_message(lang, "search_no_results"))
        return

    # Pre-allocate url_keys for every result so the existing audio-download flow works.
    url_keys: list[str] = []
    for r in results:
        url_key = uuid.uuid4().hex
        if len(_url_cache) >= _URL_CACHE_MAX:
            _url_cache.pop(next(iter(_url_cache)))
        _url_cache[url_key] = r["url"]
        if len(_meta_cache) >= _URL_CACHE_MAX:
            _meta_cache.pop(next(iter(_meta_cache)))
        _meta_cache[url_key] = {
            "title":     r.get("title", ""),
            "uploader":  r.get("uploader", ""),
            "thumbnail": r.get("thumbnail", ""),
        }
        url_keys.append(url_key)

    search_id = uuid.uuid4().hex
    if len(_search_cache) >= _SEARCH_CACHE_MAX:
        _search_cache.pop(next(iter(_search_cache)))
    _search_cache[search_id] = {
        "query": query,
        "results": results,
        "url_keys": url_keys,
    }

    text, kb = _render_search_page(search_id, page=0, lang=lang)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@dp.message(F.text)
async def url_handler(message: Message) -> None:
    text = (message.text or "").strip()
    lang = _lang(message.from_user)

    if message.from_user and _is_rate_limited("url", message.from_user.id):
        await message.answer(get_message(lang, "rate_limit"))
        return

    if message.from_user:
        allowed, reason = await _check_user_allowed(message.from_user.id)
        if not allowed:
            await message.answer(get_message(lang, f"quota_{reason}" if reason == "banned" else "quota_exceeded"))
            return

    # Non-URL text → music search (private chat only; groups stay quiet)
    if not _is_url_text(text):
        if message.chat.type == "private" and len(text) >= 2:
            await _handle_music_search(message, text, lang)
            # Count toward daily quota — each search is a real yt-dlp call.
            if message.from_user:
                asyncio.create_task(db.log_request(
                    message.from_user.id, "search", success=True,
                ))
        return

    url = text

    if not is_supported_url(url):
        await message.answer(get_message(lang, "invalid_url"))
        return

    content_type = detect_content_type(url)
    url_key = uuid.uuid4().hex
    if len(_url_cache) >= _URL_CACHE_MAX:
        _url_cache.pop(next(iter(_url_cache)))
    _url_cache[url_key] = url

    status_msg = await message.answer(get_message(lang, "fetching_meta"))

    is_instagram_video = (
        content_type in ("reel", "story")
        and "instagram.com" in url.lower()
    )

    try:
        if is_instagram_video:
            # Stories aren't covered by extract_info_full's instaloader path
            # (only post/reel), so just grab metadata. Reels go through the
            # full path below to get CDN items at picker-time too.
            if content_type == "story":
                meta = await fetch_instagram_meta(url)
                cdn_items: list[tuple[str, str]] = []
            else:
                meta, cdn_items = await extract_info_full(url)
        else:
            meta, cdn_items = await extract_info_full(url)

        uploader = meta.get("uploader") or ""
        attribution = get_message(lang, "attribution")
        caption = f"📹 <b>{_html.escape(uploader)}</b>\n\n{attribution}" if uploader else attribution

        if len(_meta_cache) >= _URL_CACHE_MAX:
            _meta_cache.pop(next(iter(_meta_cache)))
        _meta_cache[url_key] = {
            "title": meta.get("title") or "",
            "uploader": uploader,
            "thumbnail": meta.get("thumbnail") or "",
            "duration": int(meta.get("duration") or 0),
        }

        is_images_only = bool(cdn_items) and all(
            ext in ("jpg", "jpeg", "png", "webp") for _, ext in cdn_items
        )

        if is_images_only:
            # Photos have no quality variants worth picking — just send them.
            await _send_video_content(
                message, url, lang,
                prefetched_cdn=cdn_items,
                caption=caption,
                reply_markup=None,
            )
        else:
            title = (meta.get("title") or "").strip()
            if title and uploader and title.lower() == uploader.strip().lower():
                title = ""
            if len(title) > 80:
                title = title[:77].rstrip() + "…"
            duration_s = _format_duration(meta.get("duration"))

            lines: list[str] = []
            if uploader:
                lines.append(f"📹 <b>{_html.escape(uploader)}</b>")
            if title and duration_s:
                lines.append(f"🎬 {_html.escape(title)}  ·  <b>{duration_s}</b>")
            elif title:
                lines.append(f"🎬 {_html.escape(title)}")
            elif duration_s:
                lines.append(f"⏱ <b>{duration_s}</b>")
            if lines:
                lines.append("")
            lines.append(get_message(lang, "choose_quality"))
            picker_text = "\n".join(lines)

            await message.answer(
                picker_text,
                reply_markup=_quality_keyboard(url_key, lang),
            )
        if message.from_user:
            asyncio.create_task(db.log_request(
                message.from_user.id, "url",
                platform=detect_content_type(url), success=True,
            ))
    except CookieExpiredError as e:
        logging.error("cookie_expired: %s", e)
        await message.answer(get_message(lang, "cookies_expired"))
        asyncio.create_task(_alert_admins("ig_cookies_expired", str(e)))
        if message.from_user:
            asyncio.create_task(db.log_request(
                message.from_user.id, "url",
                platform="instagram", success=False, error_kind="cookie_expired",
            ))
    except Exception as exc:
        logging.exception("Media handling failed for %s", url)
        asyncio.create_task(_alert_admins(
            f"dl_fail_media_{detect_content_type(url) or 'unknown'}",
            f"{url}\n{str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__}",
        ))
        await message.answer(get_message(lang, "error"))
        if message.from_user:
            asyncio.create_task(db.log_request(
                message.from_user.id, "url",
                platform=detect_content_type(url), success=False, error_kind="error",
            ))
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass


# ── Music recognition (Shazam) ─────────────────────────────────────────────

async def _handle_recognition(message: Message, source_path: str) -> None:
    user_lang = _lang(message.from_user)
    status_msg = await message.answer(get_message(user_lang, "recognizing"))
    workdir = recognizer.make_workdir()
    if message.from_user:
        asyncio.create_task(db.log_request(
            message.from_user.id, "shazam", success=True,
        ))
    try:
        clip = await recognizer.extract_audio_clip(source_path, workdir)
        track = await recognizer.recognize(clip)
        if not track:
            await message.answer(get_message(user_lang, "not_recognized"))
            return

        title_raw = (track.get("title") or "").strip()
        artist_raw = (track.get("artist") or "").strip()
        track_url = (track.get("url") or "").strip()

        if not (title_raw and artist_raw):
            # Partial match — surface whatever we got + Shazam link if any,
            # rather than silently saying "not recognized".
            shown_title = _html.escape(title_raw or "?")
            shown_artist = _html.escape(artist_raw or "?")
            if track_url:
                msg = get_message(user_lang, "recognized").format(
                    title=shown_title, artist=shown_artist, url=track_url,
                )
            else:
                msg = get_message(user_lang, "recognized_no_link").format(
                    title=shown_title, artist=shown_artist,
                )
            await message.answer(msg, disable_web_page_preview=False)
            return

        delivered = await _deliver_recognized_song(
            message, title_raw, artist_raw, user_lang,
        )
        if not delivered:
            # Shazam matched cleanly but YouTube didn't yield a downloadable
            # audio. Tell the user the title/artist instead of pretending we
            # didn't recognise anything.
            await message.answer(
                get_message(user_lang, "recognized_no_audio").format(
                    title=_html.escape(title_raw),
                    artist=_html.escape(artist_raw),
                ),
                disable_web_page_preview=True,
            )
    except Exception:
        logging.exception("Recognition failed for %s", source_path)
        await message.answer(get_message(user_lang, "not_recognized"))
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass
        recognizer.cleanup(workdir)


async def _deliver_recognized_song(
    message: Message, title: str, artist: str, user_lang: str,
) -> bool:
    """After a Shazam match, fetch the full audio from YouTube and send it.

    Uses search_and_download_audio (single yt-dlp call, no MP3 reencode) so
    delivery is ~3x faster than the explicit-MP3 path. Returns True on
    successful send so the caller can fall back to 'not recognized' otherwise.
    """
    query = f"{artist} {title}"
    try:
        await bot.send_chat_action(message.chat.id, "upload_voice")
    except Exception:
        pass

    file_path: str | None = None
    try:
        result = await search_and_download_audio(query)
        if not result:
            logging.info("Recognised-song: nothing found/downloadable for %r", query)
            return False
        file_path, info = result

        thumb_input = None
        thumbnail_url = (info.get("thumbnail") or "") if info else ""
        if thumbnail_url and _is_safe_public_url(thumbnail_url):
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        thumbnail_url, timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            thumb_data = await resp.read()
                            thumb_input = BufferedInputFile(thumb_data, filename="thumb.jpg")
            except Exception:
                pass

        await message.answer_audio(
            audio=FSInputFile(file_path),
            title=title,
            performer=artist,
            thumbnail=thumb_input,
            caption=get_message(user_lang, "attribution"),
        )
        return True
    except Exception:
        logging.exception("Recognised-song download failed for %r", query)
        return False
    finally:
        if file_path:
            cleanup(file_path)


async def _download_telegram_file(file_id: str, suffix: str) -> str:
    """Download a Telegram-hosted file into TEMP_DIR and return the local path."""
    out_dir = os.path.join(TEMP_DIR, f"shazam-src-{uuid.uuid4()}")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"src{suffix}")
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, destination=out_path)
    return out_path


def _shazam_rate_limited(message: Message) -> bool:
    """True when the user has hit the Shazam-recognition rate limit."""
    if not message.from_user:
        return False
    return _is_rate_limited("shazam", message.from_user.id)


async def _gate_shazam(message: Message) -> bool:
    """Apply rate-limit + ban + quota checks to a Shazam request.
    Returns True when the request may proceed; sends a localized rejection otherwise."""
    if message.chat.type != "private":
        return False
    lang = _lang(message.from_user)
    if _shazam_rate_limited(message):
        await message.answer(get_message(lang, "rate_limit"))
        return False
    if message.from_user:
        allowed, reason = await _check_user_allowed(message.from_user.id)
        if not allowed:
            await message.answer(get_message(lang, f"quota_{reason}" if reason == "banned" else "quota_exceeded"))
            return False
    return True


@dp.message(F.voice)
async def voice_handler(message: Message) -> None:
    if not await _gate_shazam(message):
        return
    src = await _download_telegram_file(message.voice.file_id, ".ogg")
    try:
        await _handle_recognition(message, src)
    finally:
        cleanup(src)


@dp.message(F.audio)
async def audio_handler(message: Message) -> None:
    if not await _gate_shazam(message):
        return
    suffix = ".mp3"
    name = message.audio.file_name or ""
    if name and "." in name:
        suffix = "." + name.rsplit(".", 1)[1].lower()
    src = await _download_telegram_file(message.audio.file_id, suffix)
    try:
        await _handle_recognition(message, src)
    finally:
        cleanup(src)


@dp.message(F.video)
async def video_handler(message: Message) -> None:
    if not await _gate_shazam(message):
        return
    src = await _download_telegram_file(message.video.file_id, ".mp4")
    try:
        await _handle_recognition(message, src)
    finally:
        cleanup(src)


@dp.message(F.video_note)
async def video_note_handler(message: Message) -> None:
    if not await _gate_shazam(message):
        return
    src = await _download_telegram_file(message.video_note.file_id, ".mp4")
    try:
        await _handle_recognition(message, src)
    finally:
        cleanup(src)


def _purge_stale_temp(max_age_seconds: int = 3600) -> None:
    """Remove TEMP_DIR entries older than max_age_seconds. Safe across restarts.

    Replaces the previous unconditional wipe so a Docker restart race can't
    yank files out from under an in-flight handler in another instance.
    """
    if not os.path.isdir(TEMP_DIR):
        return
    cutoff = time.time() - max_age_seconds
    for entry in Path(TEMP_DIR).iterdir():
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                try:
                    entry.unlink()
                except FileNotFoundError:
                    pass
        except Exception:
            logging.exception("purge_stale_temp: failed on %s", entry)


def _check_temp_disk_use() -> None:
    """Log a warning if temp/ exceeds ~500MB — early signal of cleanup leaks."""
    if not os.path.isdir(TEMP_DIR):
        return
    total = 0
    for root, _, files in os.walk(TEMP_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    mb = total / (1024 * 1024)
    if mb > 500:
        logging.warning("temp/ disk use is %.0f MB — investigate cleanup", mb)
    else:
        logging.info("temp/ disk use: %.0f MB", mb)


async def _healthcheck_loop(stop_event: asyncio.Event) -> None:
    """Touch HEALTH_FILE every 30s. Docker HEALTHCHECK reads its mtime."""
    Path(HEALTH_FILE).parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            Path(HEALTH_FILE).touch(exist_ok=True)
        except Exception:
            logging.exception("healthcheck: failed to touch %s", HEALTH_FILE)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30.0)
            return  # stop requested
        except asyncio.TimeoutError:
            continue


async def _rate_store_sweep_loop(stop_event: asyncio.Event) -> None:
    """Drop expired entries from _rate_store and purge stale temp/ every 5 minutes."""
    purge_counter = 0
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=300.0)
            return  # stop requested
        except asyncio.TimeoutError:
            pass
        now = time.monotonic()
        for k, ts_list in list(_rate_store.items()):
            bucket = k[0]
            window = _RATE_LIMITS.get(bucket, (60, 1))[0]
            kept = [t for t in ts_list if now - t < window]
            if kept:
                _rate_store[k] = kept
            else:
                _rate_store.pop(k, None)
        # Once an hour (every 12th sweep), purge stale temp dirs so any leaked
        # UUID dirs from failed cleanup paths don't accumulate over a long deploy.
        # Once a day (every 288th sweep), evict expired/over-cap media_cache rows.
        purge_counter += 1
        if purge_counter % 12 == 0:
            try:
                await asyncio.get_running_loop().run_in_executor(None, _purge_stale_temp, 3600)
            except Exception:
                logging.exception("rate_store_sweep: temp purge failed")
        if purge_counter >= 288:
            purge_counter = 0
            try:
                deleted = await db.media_cache_evict(max_rows=100_000, ttl_days=30)
                if deleted:
                    logging.info("media_cache_evict: removed %d stale rows", deleted)
            except Exception:
                logging.exception("rate_store_sweep: media_cache_evict failed")


async def main() -> None:
    global _BOT_USERNAME

    # ── Persistent state setup ─────────────────────────────────────────
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        db.init_sync()
        # Hydrate the in-memory lang cache from SQLite so _lang() stays fast/sync.
        from_db = await db.all_langs()
        _user_langs.update(from_db)
        logging.info("DB ready (%d users loaded)", len(from_db))
    except Exception:
        logging.exception("DB init failed; continuing with JSON fallback (lang persistence degraded)")

    # ── Cookies bootstrap (Railway-style env-var path) ────────────────
    # cookies.txt is a secret on hosted deploys, so we read it from
    # INSTAGRAM_COOKIES_TXT and write it to disk before any
    # instaloader/yt-dlp call. Local Docker compose users keep using the bind mount.
    cookies_env = os.getenv("INSTAGRAM_COOKIES_TXT")
    if cookies_env:
        cookies_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
        try:
            tmp = cookies_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(cookies_env)
            os.replace(tmp, cookies_path)  # atomic
            logging.info("Wrote INSTAGRAM_COOKIES_TXT → %s (%d bytes)", cookies_path, len(cookies_env))
        except Exception:
            logging.exception("Failed to write cookies file from INSTAGRAM_COOKIES_TXT")

    os.makedirs(TEMP_DIR, exist_ok=True)
    _purge_stale_temp()
    _check_temp_disk_use()

    cookies_path_check = os.path.join(os.path.dirname(__file__), "cookies.txt")
    if os.path.isfile(cookies_path_check) and os.path.getsize(cookies_path_check) >= 50:
        logging.info("cookies.txt present (%d bytes) — Instagram authenticated", os.path.getsize(cookies_path_check))
    else:
        logging.warning("cookies.txt missing or empty — Instagram reels/stories will likely fail (set INSTAGRAM_COOKIES_TXT)")
    if not shutil.which("ffmpeg"):
        logging.warning("ffmpeg not found — audio download will fail")
    try:
        import yt_dlp
        logging.info("yt-dlp version: %s", getattr(yt_dlp.version, "__version__", "?"))
    except Exception:
        logging.exception("Failed to read yt-dlp version")
    try:
        import shazamio  # noqa: F401
        logging.info("shazamio available — music recognition enabled")
    except ImportError as e:
        logging.warning(
            "shazamio not installed — music recognition will return 'not recognized' "
            "for every voice/audio/video. Install with `pip install shazamio` "
            "(Linux/Docker: works out of the box; Windows + Python 3.13: needs MSVC "
            "Build Tools for the Rust compile, or use Python 3.11). Detail: %s", e,
        )
    try:
        me = await bot.get_me()
        if me.username:
            _BOT_USERNAME = me.username
            logging.info("Bot username: @%s", _BOT_USERNAME)
    except Exception:
        logging.exception("Failed to fetch bot username via get_me()")

    if ADMIN_USER_IDS:
        logging.info("Admin user ids: %s", sorted(ADMIN_USER_IDS))
    if DAILY_QUOTA > 0:
        logging.info("Daily quota: %d req/user/24h", DAILY_QUOTA)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _get_instaloader)

    # ── Graceful shutdown wiring ───────────────────────────────────────
    stop_event = asyncio.Event()

    def _request_stop(*_: object) -> None:
        if not stop_event.is_set():
            logging.info("Shutdown signal received — draining…")
            stop_event.set()

    # add_signal_handler is Unix-only; Windows raises NotImplementedError.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            pass

    health_task = asyncio.create_task(_healthcheck_loop(stop_event))
    sweep_task = asyncio.create_task(_rate_store_sweep_loop(stop_event))

    polling_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False)
    )
    stop_waiter_task = asyncio.create_task(stop_event.wait())

    # Wait for either: the polling task to exit, or a shutdown signal.
    done, _pending = await asyncio.wait(
        {polling_task, stop_waiter_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    # ── Drain ───────────────────────────────────────────────────────────
    try:
        await dp.stop_polling()
    except Exception:
        logging.exception("dp.stop_polling failed")

    # Include stop_waiter_task so it doesn't leak when polling exits first.
    for t in (polling_task, health_task, sweep_task, stop_waiter_task):
        if not t.done():
            t.cancel()
    await asyncio.gather(
        polling_task, health_task, sweep_task, stop_waiter_task,
        return_exceptions=True,
    )

    try:
        await bot.session.close()
    except Exception:
        logging.exception("bot.session.close failed")
    db.close()
    logging.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
