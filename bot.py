import asyncio
import logging
import os
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


async def main() -> None:
    os.makedirs(TEMP_DIR, exist_ok=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
