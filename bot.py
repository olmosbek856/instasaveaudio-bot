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
    download_media,
    extract_direct_urls,
    extract_info_full,
    is_instagram_url,
)
from messages import get_message

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

_RATE_WINDOW = 30
_RATE_MAX = 3
_rate_store: dict[int, list[float]] = defaultdict(list)

def _is_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    _rate_store[user_id] = [t for t in _rate_store[user_id] if now - t < _RATE_WINDOW]
    if len(_rate_store[user_id]) >= _RATE_MAX:
        return True
    _rate_store[user_id].append(now)
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
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
        InlineKeyboardButton(text="🇷🇺 Русский",   callback_data="lang:ru"),
        InlineKeyboardButton(text="🇬🇧 English",   callback_data="lang:en"),
    ]])


def _start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Guruhga qo'shish ⚡",
            url="https://t.me/insta_reelsave_bot?startgroup=true",
        )
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
    prefetched_cdn: list[tuple[str, str]] | None = None,
    caption: str = "",
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Download and send video/photo content. Tries CDN fast path first, falls back to disk."""
    if not caption:
        caption = get_message(user_lang, "attribution")
    file_paths: list[str] = []
    try:
        cdn_items = prefetched_cdn if prefetched_cdn is not None else await extract_direct_urls(url)
        if cdn_items:
            if len(cdn_items) == 1:
                cdn_url, ext = cdn_items[0]
                is_photo = ext in ("jpg", "jpeg", "png", "webp")
                if is_photo:
                    await message.answer_photo(photo=cdn_url, caption=caption, reply_markup=reply_markup)
                else:
                    await message.answer_video(video=cdn_url, caption=caption, reply_markup=reply_markup)
                return
            else:
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

        file_paths = await download_media(url)
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
    _save_langs(_user_langs)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(get_message(chosen, "start"), reply_markup=_start_keyboard())



@dp.callback_query(F.data.startswith("fa:"))
async def audio_callback(callback: CallbackQuery) -> None:
    url_key = callback.data[3:]
    url = _url_cache.get(url_key, "")
    if not url:
        await callback.answer(get_message(_lang(callback.from_user), "stale_url"))
        return

    if not callback.message:
        await callback.answer("Error: no message context.")
        return

    user_lang = _lang(callback.from_user)
    await callback.answer()

    status_msg = await callback.message.answer(get_message(user_lang, "downloading"))

    meta = _meta_cache.pop(url_key, {})
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
    except Exception as exc:
        logging.error("Audio download failed for %s: %s", url, exc)
        await callback.message.answer(get_message(user_lang, "error"))
    finally:
        await status_msg.delete()
        if file_path:
            cleanup(file_path)


@dp.message(F.text)
async def url_handler(message: Message) -> None:
    url = message.text.strip()
    lang = _lang(message.from_user)

    if message.from_user and _is_rate_limited(message.from_user.id):
        await message.answer(get_message(lang, "rate_limit"))
        return

    if not is_instagram_url(url):
        await message.answer(get_message(lang, "invalid_url"))
        return

    content_type = detect_content_type(url)
    status_key = {
        "reel":    "downloading_reel",
        "post":    "downloading_post",
        "story":   "downloading_story",
        "youtube": "downloading_youtube",
        "tiktok":  "downloading_tiktok",
    }.get(content_type, "downloading")

    url_key = uuid.uuid4().hex
    if len(_url_cache) >= _URL_CACHE_MAX:
        _url_cache.pop(next(iter(_url_cache)))
    _url_cache[url_key] = url

    status_msg = await message.answer(get_message(lang, status_key))
    meta, cdn_items = await extract_info_full(url)

    try:
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

        is_images_only = bool(cdn_items) and all(ext in ("jpg", "jpeg", "png", "webp") for _, ext in cdn_items)
        audio_keyboard = None
        if not is_images_only and content_type != "story":
            audio_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=get_message(lang, "btn_audio"), callback_data=f"fa:{url_key}"),
            ]])

        await _send_video_content(message, url, lang, prefetched_cdn=cdn_items, caption=caption, reply_markup=audio_keyboard)

    except Exception:
        logging.exception("Media fetch failed for %s", url)
        await message.answer(get_message(lang, "error"))
    finally:
        await status_msg.delete()


async def main() -> None:
    if os.path.isdir(TEMP_DIR):
        for entry in Path(TEMP_DIR).iterdir():
            shutil.rmtree(entry, ignore_errors=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    if not shutil.which("ffmpeg"):
        logging.warning("ffmpeg not found — audio download will fail")
    _cookies = os.path.join(os.path.dirname(__file__), "cookies.txt")
    if os.path.isfile(_cookies):
        logging.info("cookies.txt found — Instagram requests will use authenticated session")
    else:
        logging.warning("cookies.txt not found — Instagram may rate-limit anonymous requests")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _get_instaloader)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
