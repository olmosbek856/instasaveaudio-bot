import http.cookiejar
import logging
import os
import re
import time
import uuid
import asyncio
import shutil
from pathlib import Path
from typing import Literal

import yt_dlp

from config import TEMP_DIR


class _SilentLogger:
    def debug(self, msg: str) -> None: pass
    def info(self, msg: str) -> None: pass
    def warning(self, msg: str) -> None: pass
    def error(self, msg: str) -> None: pass

_COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

def _base_opts() -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "logger": _SilentLogger(),
        "socket_timeout": 10,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 0,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    }
    if os.path.isfile(_COOKIES_FILE):
        opts["cookiefile"] = _COOKIES_FILE
    return opts

_INSTAGRAM_PATTERN = re.compile(
    r"https?://(www\.)?instagram\.com/(p|reel|tv|stories)/[\w\-/]+",
    re.IGNORECASE,
)

_YOUTUBE_PATTERN = re.compile(
    r"https?://(www\.)?(youtube\.com/(watch|shorts/)|youtu\.be/)",
    re.IGNORECASE,
)

_TIKTOK_PATTERN = re.compile(
    r"https?://(www\.|vm\.)?tiktok\.com/",
    re.IGNORECASE,
)

_SHORTCODE_PATTERN = re.compile(r"/(p|reel|tv)/([A-Za-z0-9_-]+)")

# Module-level instaloader singleton — avoids re-creating session on every request
def _make_instaloader():
    import instaloader
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=1,
    )
    if os.path.isfile(_COOKIES_FILE):
        jar = http.cookiejar.MozillaCookieJar()
        try:
            jar.load(_COOKIES_FILE, ignore_discard=True, ignore_expires=True)
            L.context._session.cookies.update(jar)
        except Exception:
            pass
    return L

_instaloader_instance: "instaloader.Instaloader | None" = None  # type: ignore[name-defined]

def _get_instaloader():
    global _instaloader_instance
    if _instaloader_instance is None:
        _instaloader_instance = _make_instaloader()
    return _instaloader_instance


# ── Concurrency limiters ────────────────────────────────────────────────────
# Lazy init so they're created inside the running event loop.

_extract_sem: asyncio.Semaphore | None = None
_download_sem: asyncio.Semaphore | None = None

def _get_extract_sem() -> asyncio.Semaphore:
    global _extract_sem
    if _extract_sem is None:
        _extract_sem = asyncio.Semaphore(5)  # max 5 parallel info extractions
    return _extract_sem

def _get_download_sem() -> asyncio.Semaphore:
    global _download_sem
    if _download_sem is None:
        _download_sem = asyncio.Semaphore(3)  # max 3 parallel file downloads
    return _download_sem


# ── CDN result cache ────────────────────────────────────────────────────────
# url → (fetched_at_monotonic, meta, cdn_items)
# CDN links from Instagram/TikTok/YouTube are typically valid for hours.

_RESULT_CACHE: dict[str, tuple[float, dict, list]] = {}
_RESULT_CACHE_TTL = 300   # 5 minutes
_RESULT_CACHE_MAX = 300   # evict oldest when full


def is_instagram_url(url: str) -> bool:
    return bool(
        _INSTAGRAM_PATTERN.search(url)
        or _YOUTUBE_PATTERN.search(url)
        or _TIKTOK_PATTERN.search(url)
    )


def detect_content_type(url: str) -> Literal["reel", "post", "story", "youtube", "tiktok"]:
    lower = url.lower()
    if "tiktok.com" in lower:
        return "tiktok"
    if "youtube.com" in lower or "youtu.be" in lower:
        return "youtube"
    if "/stories/" in lower:
        return "story"
    if "/reel/" in lower:
        return "reel"
    return "post"


async def extract_info_full(url: str) -> tuple[dict, list[tuple[str, str]]]:
    """Return (metadata_dict, cdn_items_list). Cached + concurrency-limited."""
    # Fast path: cache hit (no lock needed — asyncio is single-threaded)
    entry = _RESULT_CACHE.get(url)
    if entry:
        ts, meta, cdn = entry
        if time.monotonic() - ts < _RESULT_CACHE_TTL:
            return meta, cdn

    async with _get_extract_sem():
        # Re-check after acquiring semaphore: a sibling may have fetched while we waited
        entry = _RESULT_CACHE.get(url)
        if entry:
            ts, meta, cdn = entry
            if time.monotonic() - ts < _RESULT_CACHE_TTL:
                return meta, cdn

        meta, cdn = await _do_extract_info(url)

        if cdn:
            if len(_RESULT_CACHE) >= _RESULT_CACHE_MAX:
                _RESULT_CACHE.pop(next(iter(_RESULT_CACHE)))
            _RESULT_CACHE[url] = (time.monotonic(), meta, cdn)

        return meta, cdn


async def _do_extract_info(url: str) -> tuple[dict, list[tuple[str, str]]]:
    """Inner extraction — no cache, no semaphore."""
    content_type = detect_content_type(url)
    loop = asyncio.get_running_loop()

    # Fast path: instaloader for Instagram posts AND reels (~2-4s vs yt-dlp ~8-12s)
    if content_type in ("post", "reel") and "instagram.com" in url:
        m = _SHORTCODE_PATTERN.search(url)
        if m:
            shortcode = m.group(2)
            try:
                future = loop.run_in_executor(None, _instaloader_fetch, shortcode)
                meta, cdn_items = await asyncio.wait_for(future, timeout=4.0)
                if cdn_items:
                    return meta, cdn_items
            except Exception:
                pass  # timed out or 403 — fall through to yt-dlp

    fmt = (
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]"
        if content_type == "youtube"
        else "best"
    )
    ydl_opts = {**_base_opts(), "format": fmt}

    def _extract() -> tuple[dict, list[tuple[str, str]]]:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            logging.exception("yt-dlp extract_info failed for %s", url)
            return {}, []
        if not info:
            return {}, []
        entries = [e for e in (info.get("entries") or [info]) if e]
        first = entries[0] if entries else None
        if not first:
            return {}, []
        metadata = {
            "title":     first.get("title") or first.get("description") or "",
            "uploader":  first.get("uploader") or first.get("channel") or "",
            "thumbnail": first.get("thumbnail") or "",
        }
        cdn_items: list[tuple[str, str]] = []
        for entry in entries:
            if not entry:
                continue
            direct = entry.get("url") or entry.get("manifest_url")
            ext = (entry.get("ext") or "").lower()
            if direct:
                cdn_items.append((direct, ext))
        return metadata, cdn_items

    return await loop.run_in_executor(None, _extract)


async def extract_metadata(url: str) -> dict:
    """Return title, uploader, thumbnail without downloading."""
    ydl_opts = {**_base_opts(), "format": "best"}
    loop = asyncio.get_running_loop()

    def _extract() -> dict:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return {}
        entries = info.get("entries")
        item = entries[0] if entries else info
        return {
            "title": item.get("title") or item.get("description") or "",
            "uploader": item.get("uploader") or item.get("channel") or "",
            "thumbnail": item.get("thumbnail") or "",
        }

    return await loop.run_in_executor(None, _extract)


def _instaloader_fetch(shortcode: str) -> tuple[dict, list[tuple[str, str]]]:
    """Get CDN URLs and metadata via instaloader (posts, reels, carousels)."""
    import instaloader
    L = _get_instaloader()
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    result: list[tuple[str, str]] = []
    thumbnail = ""
    if post.typename == "GraphSidecar":
        nodes = list(post.get_sidecar_nodes())
        for node in nodes:
            if node.is_video:
                result.append((node.video_url, "mp4"))
            else:
                result.append((node.display_url, "jpg"))
        thumbnail = nodes[0].display_url if nodes else ""
    elif post.is_video:
        result.append((post.video_url, "mp4"))
        thumbnail = post.url
    else:
        result.append((post.url, "jpg"))
        thumbnail = post.url
    metadata = {
        "title": "",
        "uploader": post.owner_username or "",
        "thumbnail": thumbnail,
    }
    return metadata, result


async def extract_direct_urls(url: str) -> list[tuple[str, str]]:
    """Return (cdn_url, ext) pairs without downloading. Fast path (~2-5s)."""
    content_type = detect_content_type(url)
    if content_type == "youtube":
        fmt = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]"
    else:
        fmt = "best"

    ydl_opts = {
        **_base_opts(),
        "format": fmt,
    }
    loop = asyncio.get_running_loop()

    def _extract() -> list[tuple[str, str]]:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            return []
        if not info:
            return []
        entries = info.get("entries") or [info]
        result = []
        for entry in entries:
            if not entry:
                continue
            direct = entry.get("url") or entry.get("manifest_url")
            ext = (entry.get("ext") or "").lower()
            if direct:
                result.append((direct, ext))
        return result

    items = await loop.run_in_executor(None, _extract)
    if items:
        return items

    # Fallback: instaloader handles image posts and carousels (Instagram only)
    m = _SHORTCODE_PATTERN.search(url)
    if m and "instagram.com" in url:
        shortcode = m.group(2)
        try:
            _, cdn_items = await loop.run_in_executor(None, _instaloader_fetch, shortcode)
            return cdn_items
        except Exception:
            pass

    return []


async def download_media(url: str) -> list[str]:
    """Download all media from URL. Returns list of temp file paths."""
    async with _get_download_sem():
        output_dir = os.path.join(TEMP_DIR, str(uuid.uuid4()))
        os.makedirs(output_dir, exist_ok=True)

        content_type = detect_content_type(url)
        if content_type == "youtube":
            fmt = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]"
        else:
            fmt = "best"

        ydl_opts = {
            **_base_opts(),
            "format": fmt,
            "outtmpl": os.path.join(output_dir, "%(autonumber)03d_%(title)s.%(ext)s"),
        }

        loop = asyncio.get_running_loop()

        def _download() -> None:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

        await loop.run_in_executor(None, _download)

        files = sorted(Path(output_dir).iterdir())
        if not files:
            raise FileNotFoundError("yt-dlp produced no files")

        return [str(f) for f in files]


async def download_audio(url: str) -> str:
    """Download audio-only (mp3) from URL. Returns temp file path."""
    async with _get_download_sem():
        output_dir = os.path.join(TEMP_DIR, str(uuid.uuid4()))
        os.makedirs(output_dir, exist_ok=True)

        ydl_opts = {
            **_base_opts(),
            "format": "bestaudio/best",
            "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "playlist_items": "1",
        }

        loop = asyncio.get_running_loop()

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


async def download_cdn_url(cdn_url: str, ext: str) -> str:
    """Stream-download a direct CDN URL to a temp file via aiohttp (no yt-dlp re-extraction)."""
    import aiohttp
    async with _get_download_sem():
        output_dir = os.path.join(TEMP_DIR, str(uuid.uuid4()))
        os.makedirs(output_dir, exist_ok=True)
        file_path = os.path.join(output_dir, f"media.{ext or 'mp4'}")
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(cdn_url, timeout=timeout) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"CDN download failed: HTTP {resp.status}")
                with open(file_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
        return file_path


def cleanup(path: str) -> None:
    """Remove the UUID temp directory that contains path."""
    parent = str(Path(path).parent)
    shutil.rmtree(parent, ignore_errors=True)
