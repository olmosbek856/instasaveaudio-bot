import os
import re
import uuid
import asyncio
import shutil
from pathlib import Path
from typing import Literal

import yt_dlp

from config import TEMP_DIR, MAX_FILE_SIZE_BYTES

# os, uuid, asyncio, shutil, Path, yt_dlp, TEMP_DIR, MAX_FILE_SIZE_BYTES used by download functions added in Task 4

_INSTAGRAM_PATTERN = re.compile(
    r"https?://(www\.)?instagram\.com/(p|reel|tv|stories)/[\w\-/]+",
    re.IGNORECASE,
)


def is_instagram_url(url: str) -> bool:
    return bool(_INSTAGRAM_PATTERN.search(url))


def detect_content_type(url: str) -> Literal["video", "story"]:
    """Return content type for a validated Instagram URL."""
    if "/stories/" in url:
        return "story"
    return "video"


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
