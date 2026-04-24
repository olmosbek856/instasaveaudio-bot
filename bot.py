import asyncio
import logging
import os
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from config import BOT_TOKEN, MAX_FILE_SIZE_BYTES, TEMP_DIR
from downloader import cleanup, download_audio, download_media, is_instagram_url
from messages import get_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill in your token.")

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

_url_cache: dict[str, str] = {}


def _lang(user: User) -> str:
    return user.language_code or "uz"


async def _send_media(
    message: Message,
    file_path: str,
    caption: str = "",
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if os.path.getsize(file_path) > MAX_FILE_SIZE_BYTES:
        await message.answer(get_message(_lang(message.from_user), "too_large"))
        return

    ext = Path(file_path).suffix.lower()
    file = FSInputFile(file_path)

    if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        await message.answer_video(video=file, caption=caption, reply_markup=reply_markup)
    elif ext in (".jpg", ".jpeg", ".png", ".webp"):
        await message.answer_photo(photo=file, caption=caption, reply_markup=reply_markup)
    else:
        await message.answer_document(document=file, caption=caption, reply_markup=reply_markup)


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(get_message(_lang(message.from_user), "start"))


@dp.message(F.text)
async def url_handler(message: Message) -> None:
    url = message.text.strip()
    lang = _lang(message.from_user)

    if not is_instagram_url(url):
        await message.answer(get_message(lang, "invalid_url"))
        return

    status_msg = await message.answer(get_message(lang, "downloading"))

    file_paths: list[str] = []
    try:
        file_paths = await download_media(url)

        url_key = str(uuid.uuid4())[:8]
        _url_cache[url_key] = url

        audio_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=get_message(lang, "audio_btn"),
                callback_data=f"audio:{url_key}",
            )
        ]])

        for i, file_path in enumerate(file_paths):
            kb = audio_keyboard if i == 0 else None
            caption = get_message(lang, "done_video") if i == 0 else ""
            await _send_media(message, file_path, caption=caption, reply_markup=kb)

    except Exception as exc:
        logging.error("Download failed for %s: %s", url, exc)
        await message.answer(get_message(lang, "error"))
    finally:
        await status_msg.delete()
        if file_paths:
            cleanup(file_paths[0])


@dp.callback_query(F.data.startswith("audio:"))
async def audio_callback(callback: CallbackQuery) -> None:
    url_key = callback.data[len("audio:"):]
    url = _url_cache.get(url_key, "")
    if not url:
        await callback.answer("Havola eskirgan. Qaytadan yuboring.")
        return

    user_lang = _lang(callback.from_user)
    await callback.answer()

    status_msg = await callback.message.answer(get_message(user_lang, "downloading"))

    file_path: str | None = None
    try:
        file_path = await download_audio(url)
        audio_file = FSInputFile(file_path)
        await callback.message.answer_audio(
            audio=audio_file,
            caption=get_message(user_lang, "done_audio"),
        )
    except Exception as exc:
        logging.error("Audio download failed for %s: %s", url, exc)
        await callback.message.answer(get_message(user_lang, "error"))
    finally:
        await status_msg.delete()
        if file_path:
            cleanup(file_path)


async def main() -> None:
    os.makedirs(TEMP_DIR, exist_ok=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
