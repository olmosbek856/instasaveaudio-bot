import asyncio
import html as _html
import json
import logging
import os
import shutil
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

from config import BOT_TOKEN, MAX_FILE_SIZE_BYTES, TEMP_DIR
from downloader import (
    _get_instaloader,
    cleanup,
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill in your token.")

bot = Bot(
    token=BOT_TOKEN,
    session=AiohttpSession(timeout=300),
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()

_URL_CACHE_MAX = 500
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
    try:
        with open(_LANGS_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in langs.items()}, f)
    except Exception:
        pass


async def _save_langs_async(langs: dict[int, str]) -> None:
    """Off-loop disk write so a slow disk doesn't stall message dispatch."""
    loop = asyncio.get_running_loop()
    # Snapshot the dict to avoid mutation during the write thread's lifetime.
    await loop.run_in_executor(None, _save_langs, dict(langs))

_user_langs: dict[int, str] = _load_langs()


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
) -> None:
    if os.path.getsize(file_path) > MAX_FILE_SIZE_BYTES:
        await message.answer(get_message(lang_code, "too_large"))
        return

    ext = Path(file_path).suffix.lower()
    file = FSInputFile(file_path)

    if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        await message.answer_video(video=file, caption=caption, reply_markup=reply_markup)
    elif ext in (".jpg", ".jpeg", ".png", ".webp"):
        await message.answer_photo(photo=file, caption=caption, reply_markup=reply_markup)
    else:
        await message.answer_document(document=file, caption=caption, reply_markup=reply_markup)


async def _send_video_content(
    message: Message,
    url: str,
    user_lang: str,
    height: int | None = None,
    prefetched_cdn: list[tuple[str, str]] | None = None,
    caption: str = "",
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Download and send video/photo content. Tries CDN fast path first, falls back to disk."""
    if not caption:
        caption = get_message(user_lang, "attribution")
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
                        await message.answer_photo(photo=cdn_url, caption=caption, reply_markup=reply_markup)
                    else:
                        await message.answer_video(video=cdn_url, caption=caption, reply_markup=reply_markup)
                    return
                except TelegramBadRequest:
                    # Telegram can't fetch Instagram CDN — download directly via aiohttp
                    try:
                        fp = await download_cdn_url(cdn_url, ext)
                        file_paths = [fp]
                        await _send_media(message, fp, lang_code=user_lang, caption=caption, reply_markup=reply_markup)
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
                    await message.answer_media_group(media=media_list)
                    if reply_markup:
                        await message.answer(caption, reply_markup=reply_markup)
                    return
                except TelegramBadRequest:
                    # Download each CDN URL directly via aiohttp
                    try:
                        fps = []
                        for cdn_u, cdn_e in cdn_items:
                            fps.append(await download_cdn_url(cdn_u, cdn_e))
                        file_paths = fps
                        media_list = []
                        for i, fp in enumerate(fps):
                            e = Path(fp).suffix.lower().lstrip(".")
                            cap = caption if i == 0 else ""
                            f = FSInputFile(fp)
                            if e in ("jpg", "jpeg", "png", "webp"):
                                media_list.append(InputMediaPhoto(media=f, caption=cap))
                            else:
                                media_list.append(InputMediaVideo(media=f, caption=cap))
                        await message.answer_media_group(media=media_list)
                        if reply_markup:
                            await message.answer(caption, reply_markup=reply_markup)
                        return
                    except Exception:
                        pass  # Last resort: full yt-dlp re-download

        file_paths = await download_media(url, height=height)
        if len(file_paths) == 1:
            await _send_media(message, file_paths[0], lang_code=user_lang, caption=caption, reply_markup=reply_markup)
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
            await message.answer_media_group(media=media_list)
            if reply_markup:
                await message.answer(caption, reply_markup=reply_markup)
    finally:
        if file_paths:
            cleanup(file_paths[0])


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


@dp.callback_query(F.data.startswith("lang:"))
async def lang_callback(callback: CallbackQuery) -> None:
    chosen = callback.data[len("lang:"):]
    if chosen not in ("uz", "ru", "en"):
        await callback.answer()
        return
    _user_langs[callback.from_user.id] = chosen
    await _save_langs_async(_user_langs)
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
        if thumbnail_url:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(thumbnail_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            thumb_data = await resp.read()
                            thumb_input = BufferedInputFile(thumb_data, filename="thumb.jpg")
            except Exception:
                pass
        await callback.message.answer_audio(
            audio=audio_file,
            title=audio_title or None,
            performer=audio_performer or None,
            thumbnail=thumb_input,
            caption=get_message(user_lang, "attribution"),
        )
        if strip_markup_on_success:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass  # message gone or already edited — nothing to do
    except Exception as exc:
        logging.error("Audio download failed for %s: %s", url, exc)
        await callback.message.answer(get_message(user_lang, "error"))
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass
        if file_path:
            cleanup(file_path)


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
        await callback.answer()
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
    except Exception:
        logging.exception("Quality download failed for %s @ %sp", url, height)
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
        for i, key in enumerate(page_keys, start=1)
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

    # Non-URL text → music search (private chat only; groups stay quiet)
    if not _is_url_text(text):
        if message.chat.type == "private" and len(text) >= 2:
            await _handle_music_search(message, text, lang)
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
            # IG video: instaloader returns auth-scoped URLs and yt-dlp HLS works fine
            # at download-time. Skip the CDN extraction and just grab metadata.
            meta = await fetch_instagram_meta(url)
            cdn_items: list[tuple[str, str]] = []
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
            # Picker shows title only — no "downloaded via" attribution since
            # nothing has been downloaded yet at this point.
            picker_text = (
                f"📹 <b>{_html.escape(uploader)}</b>\n\n{get_message(lang, 'choose_quality')}"
                if uploader
                else get_message(lang, "choose_quality")
            )
            await message.answer(
                picker_text,
                reply_markup=_quality_keyboard(url_key, lang),
            )
    except Exception:
        logging.exception("Media handling failed for %s", url)
        await message.answer(get_message(lang, "error"))
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
        if thumbnail_url:
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


@dp.message(F.voice)
async def voice_handler(message: Message) -> None:
    if message.chat.type != "private":
        return
    if _shazam_rate_limited(message):
        await message.answer(get_message(_lang(message.from_user), "rate_limit"))
        return
    src = await _download_telegram_file(message.voice.file_id, ".ogg")
    try:
        await _handle_recognition(message, src)
    finally:
        cleanup(src)


@dp.message(F.audio)
async def audio_handler(message: Message) -> None:
    if message.chat.type != "private":
        return
    if _shazam_rate_limited(message):
        await message.answer(get_message(_lang(message.from_user), "rate_limit"))
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
    if message.chat.type != "private":
        return
    if _shazam_rate_limited(message):
        await message.answer(get_message(_lang(message.from_user), "rate_limit"))
        return
    src = await _download_telegram_file(message.video.file_id, ".mp4")
    try:
        await _handle_recognition(message, src)
    finally:
        cleanup(src)


@dp.message(F.video_note)
async def video_note_handler(message: Message) -> None:
    if message.chat.type != "private":
        return
    if _shazam_rate_limited(message):
        await message.answer(get_message(_lang(message.from_user), "rate_limit"))
        return
    src = await _download_telegram_file(message.video_note.file_id, ".mp4")
    try:
        await _handle_recognition(message, src)
    finally:
        cleanup(src)


async def main() -> None:
    global _BOT_USERNAME
    # Railway / hosted-deploy path: cookies.txt is a secret, so we read it from
    # the INSTAGRAM_COOKIES_TXT env var and write it to disk before any
    # instaloader/yt-dlp call. Local Docker compose users keep using the bind mount.
    cookies_env = os.getenv("INSTAGRAM_COOKIES_TXT")
    if cookies_env:
        cookies_path = os.path.join(os.path.dirname(__file__), "cookies.txt")
        try:
            with open(cookies_path, "w", encoding="utf-8") as f:
                f.write(cookies_env)
            logging.info("Wrote INSTAGRAM_COOKIES_TXT → %s (%d bytes)", cookies_path, len(cookies_env))
        except Exception:
            logging.exception("Failed to write cookies file from INSTAGRAM_COOKIES_TXT")
    if os.path.isdir(TEMP_DIR):
        for entry in Path(TEMP_DIR).iterdir():
            shutil.rmtree(entry, ignore_errors=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    cookies_path_check = os.path.join(os.path.dirname(__file__), "cookies.txt")
    if os.path.isfile(cookies_path_check) and os.path.getsize(cookies_path_check) >= 50:
        logging.info("cookies.txt present (%d bytes) — Instagram authenticated", os.path.getsize(cookies_path_check))
    else:
        logging.warning("cookies.txt missing or empty — Instagram reels/stories will likely fail (set INSTAGRAM_COOKIES_TXT)")
    if not shutil.which("ffmpeg"):
        logging.warning("ffmpeg not found — audio download will fail")
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
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _get_instaloader)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
