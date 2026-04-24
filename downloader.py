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
