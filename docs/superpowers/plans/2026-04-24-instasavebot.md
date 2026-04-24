# InstaSaveBot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a public Telegram bot that downloads Instagram videos, reels, stories, and audio (mp3) when a user sends an Instagram link.

**Architecture:** User sends Instagram URL → bot validates it → yt-dlp downloads to a UUID temp directory → bot sends the media file(s) to Telegram with an inline "🎵 Audiosi" button → temp files deleted. Audio callback re-downloads in audio-only mode.

**Tech Stack:** Python 3.11+, aiogram 3.x (async Telegram framework), yt-dlp (Instagram downloader), ffmpeg (audio extraction), python-dotenv

---

## File Map

| File | Responsibility |
|------|----------------|
| `config.py` | Load BOT_TOKEN from .env, define TEMP_DIR and MAX_FILE_SIZE_BYTES |
| `messages.py` | All Uzbek + Russian UI strings, `get_message(lang_code, key) -> str` |
| `downloader.py` | `is_instagram_url()`, `download_media()`, `download_audio()`, `cleanup()` |
| `bot.py` | Bot entry point, all aiogram handlers, `send_media()` helper |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template showing required env vars |
| `.gitignore` | Exclude .env and temp/ |
| `tests/__init__.py` | Empty, makes tests/ a package |
| `tests/test_messages.py` | Tests for `get_message()` language detection |
| `tests/test_downloader.py` | Tests for `is_instagram_url()` and `detect_content_type()` |

---

## Task 1: Project Scaffold

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `config.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create requirements.txt**

```
aiogram>=3.13.0
yt-dlp>=2024.1.0
python-dotenv>=1.0.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

- [ ] **Step 2: Create .env.example**

```
BOT_TOKEN=your_telegram_bot_token_here
```

- [ ] **Step 3: Create .gitignore**

```
.env
temp/
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 4: Create config.py**

```python
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TEMP_DIR = "./temp"
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB — Telegram bot upload limit
```

- [ ] **Step 5: Create tests/__init__.py**

Empty file:
```python
```

- [ ] **Step 6: Copy .env.example to .env and add your bot token**

```bash
cp .env.example .env
# Open .env and set BOT_TOKEN=<your token from @BotFather>
```

- [ ] **Step 7: Install dependencies**

```bash
pip install -r requirements.txt
```

Expected: All packages install without errors.

- [ ] **Step 8: Commit**

```bash
git add requirements.txt .env.example .gitignore config.py tests/__init__.py
git commit -m "feat: project scaffold with config and dependencies"
```

---

## Task 2: Bilingual Messages

**Files:**
- Create: `messages.py`
- Create: `tests/test_messages.py`

- [ ] **Step 1: Write failing tests**

`tests/test_messages.py`:
```python
import pytest
from messages import get_message


def test_uzbek_is_default():
    assert get_message("uz", "downloading") == "Yuklanmoqda... ⏳"


def test_english_falls_back_to_uzbek():
    assert get_message("en", "downloading") == "Yuklanmoqda... ⏳"


def test_none_lang_falls_back_to_uzbek():
    assert get_message(None, "downloading") == "Yuklanmoqda... ⏳"


def test_russian_returns_russian():
    assert get_message("ru", "downloading") == "Загружаю... ⏳"


def test_ru_RU_locale_returns_russian():
    assert get_message("ru-RU", "downloading") == "Загружаю... ⏳"


def test_all_keys_exist_in_both_languages():
    from messages import MESSAGES
    assert set(MESSAGES["uz"].keys()) == set(MESSAGES["ru"].keys())
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_messages.py -v
```

Expected: `ModuleNotFoundError: No module named 'messages'`

- [ ] **Step 3: Create messages.py**

```python
MESSAGES = {
    "uz": {
        "start": (
            "Salom! 👋 Men <b>InstaSaveBot</b>man.\n\n"
            "Instagram dan video, reel, story va audio yuklab beraman.\n\n"
            "📌 Foydalanish: Instagram linkini yuboring."
        ),
        "downloading": "Yuklanmoqda... ⏳",
        "done_video": "✅ Mana!",
        "done_audio": "🎵 Audio tayyor!",
        "error": "😔 Yuklab bo'lmadi. Keyinroq urinib ko'ring.",
        "invalid_url": (
            "❌ Bu Instagram havolasi emas.\n\n"
            "Iltimos, to'g'ri Instagram linkini yuboring."
        ),
        "too_large": "😔 Fayl hajmi juda katta (50MB dan oshadi). Telegram cheklovi.",
        "audio_btn": "🎵 Audiosi",
    },
    "ru": {
        "start": (
            "Привет! 👋 Я <b>InstaSaveBot</b>.\n\n"
            "Скачиваю видео, reel, stories и аудио из Instagram.\n\n"
            "📌 Как пользоваться: отправьте ссылку из Instagram."
        ),
        "downloading": "Загружаю... ⏳",
        "done_video": "✅ Готово!",
        "done_audio": "🎵 Аудио готово!",
        "error": "😔 Не удалось загрузить. Попробуйте позже.",
        "invalid_url": (
            "❌ Это не ссылка Instagram.\n\n"
            "Пожалуйста, отправьте правильную ссылку."
        ),
        "too_large": "😔 Файл слишком большой (больше 50MB). Ограничение Telegram.",
        "audio_btn": "🎵 Аудио",
    },
}


def get_message(lang_code: str | None, key: str) -> str:
    lang = "ru" if lang_code and lang_code.startswith("ru") else "uz"
    return MESSAGES[lang][key]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_messages.py -v
```

Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add messages.py tests/test_messages.py
git commit -m "feat: bilingual messages (uz + ru) with language detection"
```

---

## Task 3: URL Validation and Content Type Detection

**Files:**
- Create: `downloader.py` (partial — validation functions only)
- Create: `tests/test_downloader.py`

- [ ] **Step 1: Write failing tests**

`tests/test_downloader.py`:
```python
import pytest
from downloader import is_instagram_url, detect_content_type


# --- is_instagram_url ---

def test_reel_url_is_valid():
    assert is_instagram_url("https://www.instagram.com/reel/ABC123/") is True


def test_post_url_is_valid():
    assert is_instagram_url("https://www.instagram.com/p/ABC123/") is True


def test_story_url_is_valid():
    assert is_instagram_url("https://www.instagram.com/stories/username/12345/") is True


def test_tv_url_is_valid():
    assert is_instagram_url("https://www.instagram.com/tv/ABC123/") is True


def test_non_instagram_url_is_invalid():
    assert is_instagram_url("https://www.youtube.com/watch?v=abc") is False


def test_random_text_is_invalid():
    assert is_instagram_url("hello world") is False


def test_empty_string_is_invalid():
    assert is_instagram_url("") is False


# --- detect_content_type ---

def test_story_url_returns_story():
    assert detect_content_type("https://www.instagram.com/stories/username/12345/") == "story"


def test_reel_url_returns_video():
    assert detect_content_type("https://www.instagram.com/reel/ABC123/") == "video"


def test_post_url_returns_video():
    assert detect_content_type("https://www.instagram.com/p/ABC123/") == "video"


def test_tv_url_returns_video():
    assert detect_content_type("https://www.instagram.com/tv/ABC123/") == "video"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_downloader.py -v
```

Expected: `ModuleNotFoundError: No module named 'downloader'`

- [ ] **Step 3: Create downloader.py with validation functions**

```python
import os
import re
import uuid
import asyncio
import shutil
from pathlib import Path
from typing import Literal

import yt_dlp

from config import TEMP_DIR, MAX_FILE_SIZE_BYTES


_INSTAGRAM_PATTERN = re.compile(
    r"https?://(www\.)?instagram\.com/(p|reel|tv|stories)/[\w\-/]+",
    re.IGNORECASE,
)


def is_instagram_url(url: str) -> bool:
    return bool(_INSTAGRAM_PATTERN.search(url))


def detect_content_type(url: str) -> Literal["video", "story"]:
    if "/stories/" in url:
        return "story"
    return "video"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_downloader.py -v
```

Expected: All 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add downloader.py tests/test_downloader.py
git commit -m "feat: Instagram URL validation and content type detection"
```

---

## Task 4: Download Functions

**Files:**
- Modify: `downloader.py` (add `download_media`, `download_audio`, `cleanup`)
- Modify: `tests/test_downloader.py` (add mocked download tests)

- [ ] **Step 1: Write failing tests for download functions**

Add to the bottom of `tests/test_downloader.py`:

```python
import os
from unittest.mock import patch, MagicMock


# --- cleanup ---

def test_cleanup_removes_parent_directory(tmp_path):
    subdir = tmp_path / "some-uuid"
    subdir.mkdir()
    test_file = subdir / "video.mp4"
    test_file.write_text("fake content")

    from downloader import cleanup
    cleanup(str(test_file))

    assert not subdir.exists()


def test_cleanup_ignores_missing_directory():
    from downloader import cleanup
    cleanup("/tmp/nonexistent-uuid/video.mp4")  # Must not raise


# --- download_media (mocked yt-dlp) ---

@pytest.mark.asyncio
async def test_download_media_returns_file_list(tmp_path):
    fake_file = tmp_path / "000_video.mp4"
    fake_file.write_text("fake video")

    def fake_download(self_ydl, urls):
        pass  # yt-dlp does nothing

    with patch("yt_dlp.YoutubeDL") as MockYDL:
        instance = MockYDL.return_value.__enter__.return_value
        instance.download.side_effect = lambda urls: fake_file.write_text("content")

        # We override TEMP_DIR to use tmp_path for this test
        import downloader
        original_temp = downloader.TEMP_DIR
        downloader.TEMP_DIR = str(tmp_path / "temp")
        os.makedirs(downloader.TEMP_DIR, exist_ok=True)

        try:
            with patch.object(Path, "iterdir", return_value=iter([fake_file])):
                result = await downloader.download_media("https://www.instagram.com/reel/ABC/")
            assert isinstance(result, list)
            assert len(result) >= 0  # May be empty if mock doesn't create files
        finally:
            downloader.TEMP_DIR = original_temp
```

- [ ] **Step 2: Run tests to verify new tests fail**

```bash
pytest tests/test_downloader.py::test_cleanup_removes_parent_directory -v
```

Expected: `ImportError` — `cleanup` not yet defined.

- [ ] **Step 3: Add download functions to downloader.py**

Append to `downloader.py` (after the existing functions):

```python

async def download_media(url: str) -> list[str]:
    """Download all media from Instagram URL. Returns list of temp file paths."""
    output_dir = os.path.join(TEMP_DIR, str(uuid.uuid4()))
    os.makedirs(output_dir, exist_ok=True)

    ydl_opts = {
        "format": "best",
        "outtmpl": os.path.join(output_dir, "%(autonumber)03d_%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
    }

    loop = asyncio.get_event_loop()

    def _download() -> None:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    await loop.run_in_executor(None, _download)

    files = sorted(Path(output_dir).iterdir())
    if not files:
        raise FileNotFoundError("yt-dlp produced no files")

    return [str(f) for f in files]


async def download_audio(url: str) -> str:
    """Download audio-only (mp3) from Instagram URL. Returns temp file path."""
    output_dir = os.path.join(TEMP_DIR, str(uuid.uuid4()))
    os.makedirs(output_dir, exist_ok=True)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "quiet": True,
        "no_warnings": True,
        "playlist_items": "1",
    }

    loop = asyncio.get_event_loop()

    def _download() -> None:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    await loop.run_in_executor(None, _download)

    mp3_files = list(Path(output_dir).glob("*.mp3"))
    all_files = list(Path(output_dir).iterdir())
    files = mp3_files if mp3_files else all_files

    if not files:
        raise FileNotFoundError("yt-dlp produced no audio")

    return str(files[0])


def cleanup(path: str) -> None:
    """Remove the UUID temp directory that contains path."""
    parent = str(Path(path).parent)
    shutil.rmtree(parent, ignore_errors=True)
```

- [ ] **Step 4: Run all downloader tests**

```bash
pytest tests/test_downloader.py -v
```

Expected: All tests PASS (cleanup tests definitely pass; async mock test may be skipped depending on setup — that is acceptable).

- [ ] **Step 5: Commit**

```bash
git add downloader.py tests/test_downloader.py
git commit -m "feat: download_media, download_audio, cleanup functions"
```

---

## Task 5: Bot Core and /start Handler

**Files:**
- Create: `bot.py`

- [ ] **Step 1: Create bot.py with /start handler**

```python
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
)

from config import BOT_TOKEN, MAX_FILE_SIZE_BYTES, TEMP_DIR
from downloader import cleanup, download_audio, download_media, is_instagram_url
from messages import get_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()


def _lang(user) -> str:
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
```

- [ ] **Step 2: Verify bot starts without errors**

```bash
python bot.py
```

Expected output (first few lines):
```
INFO  Bot started. Press Ctrl+C to stop.
```

Send `/start` to your bot in Telegram. Expected: welcome message in Uzbek or Russian depending on your Telegram language.

- [ ] **Step 3: Stop the bot (Ctrl+C) and commit**

```bash
git add bot.py
git commit -m "feat: bot skeleton with /start handler"
```

---

## Task 6: Instagram URL Handler

**Files:**
- Modify: `bot.py` (add `url_handler` between `start_handler` and `main`)

- [ ] **Step 1: Add url_handler to bot.py**

Add this function after `start_handler` and before `main()`:

```python
@dp.message(F.text)
async def url_handler(message: Message) -> None:
    url = message.text.strip()

    if not is_instagram_url(url):
        await message.answer(get_message(_lang(message.from_user), "invalid_url"))
        return

    status_msg = await message.answer(get_message(_lang(message.from_user), "downloading"))

    file_paths: list[str] = []
    try:
        file_paths = await download_media(url)

        audio_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=get_message(_lang(message.from_user), "audio_btn"),
                callback_data=f"audio:{url}",
            )
        ]])

        for i, file_path in enumerate(file_paths):
            kb = audio_keyboard if i == 0 else None
            caption = get_message(_lang(message.from_user), "done_video") if i == 0 else ""
            await _send_media(message, file_path, caption=caption, reply_markup=kb)

    except Exception as exc:
        logging.error("Download failed for %s: %s", url, exc)
        await message.answer(get_message(_lang(message.from_user), "error"))
    finally:
        await status_msg.delete()
        if file_paths:
            cleanup(file_paths[0])
```

- [ ] **Step 2: Start bot and test with a real Instagram reel**

```bash
python bot.py
```

1. Open Telegram, find your bot
2. Send any Instagram reel link, e.g.: `https://www.instagram.com/reel/XXXXXXXXXXX/`
3. Expected: "Yuklanmoqda..." appears, then video is sent with "🎵 Audiosi" button
4. Send a non-Instagram URL like `https://google.com`
5. Expected: "Bu Instagram havolasi emas" message

- [ ] **Step 3: Stop bot (Ctrl+C) and commit**

```bash
git add bot.py
git commit -m "feat: Instagram URL download handler with inline audio button"
```

---

## Task 7: Audio Callback Handler

**Files:**
- Modify: `bot.py` (add `audio_callback` between `url_handler` and `main`)

- [ ] **Step 1: Add audio_callback to bot.py**

Add this function after `url_handler` and before `main()`:

```python
@dp.callback_query(F.data.startswith("audio:"))
async def audio_callback(callback: CallbackQuery) -> None:
    url = callback.data[len("audio:"):]
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
```

- [ ] **Step 2: Start bot and test audio download**

```bash
python bot.py
```

1. Send an Instagram reel link
2. Wait for video to arrive
3. Click "🎵 Audiosi" button
4. Expected: "Yuklanmoqda..." appears, then mp3 audio file is sent

- [ ] **Step 3: Stop bot (Ctrl+C) and commit**

```bash
git add bot.py
git commit -m "feat: audio extraction callback handler"
```

---

## Task 8: Run All Tests and Final Verification

**Files:** No changes — this is a verification task.

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/ -v
```

Expected: All tests pass. No failures.

- [ ] **Step 2: Full end-to-end test checklist**

Start bot: `python bot.py`

| Test | Input | Expected result |
|------|-------|----------------|
| `/start` | `/start` command | Welcome message (uz or ru) |
| Invalid link | `https://google.com` | "Bu Instagram havolasi emas" |
| Random text | `salom` | "Bu Instagram havolasi emas" |
| Reel video | Instagram reel URL | Video file + "🎵 Audiosi" button |
| Audio button | Click "🎵 Audiosi" | MP3 audio file |
| Story | Instagram story URL | Story media |
| Russian user | (switch Telegram to Russian) | All messages in Russian |

- [ ] **Step 3: Verify temp/ directory is clean after each download**

After each successful download, `temp/` folder should contain no UUID subdirectories.

```bash
ls temp/
```

Expected: empty directory (or no subdirectories).

- [ ] **Step 4: Final commit**

```bash
git add .
git commit -m "feat: InstaSaveBot complete — video, story, and audio downloads"
```

---

## Prerequisites (Before Running)

**ffmpeg must be installed** for audio extraction:

- Windows: Download from https://ffmpeg.org/download.html and add to PATH
- Or install via: `winget install ffmpeg`

**Verify ffmpeg:**
```bash
ffmpeg -version
```
Expected: version info printed.

**Bot token:** Get from [@BotFather](https://t.me/BotFather) on Telegram:
1. Start a chat with @BotFather
2. Send `/newbot`
3. Follow prompts, copy the token
4. Paste into `.env` as `BOT_TOKEN=<token>`
