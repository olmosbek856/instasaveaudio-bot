import html as _html_lib
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
    """Suppresses yt-dlp console output but records the last error message
    so callers can include it in their exception text. The single attribute
    `last_error` is the most recent string passed to `.error()`.
    """
    def __init__(self) -> None:
        self.last_error: str = ""

    def debug(self, msg: str) -> None: pass
    def info(self, msg: str) -> None: pass
    def warning(self, msg: str) -> None: pass

    def error(self, msg: str) -> None:
        self.last_error = msg

_COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")


class CookieExpiredError(Exception):
    """Instagram cookies are expired/invalid — extraction cannot proceed until rotated."""
    pass


# Substrings that, when present in an Instagram-related yt-dlp / instaloader
# error message, indicate the failure is an auth issue (not a transient
# network blip). Matching is case-insensitive.
_IG_AUTH_FAIL_HINTS: tuple[str, ...] = (
    "login required",
    "login_required",
    "loginrequired",
    "checkpoint",
    "rate-limit",
    "rate limit reached",
    "401",
    "403",
    "must be logged in",
    "login to view",
    "private account",
    "this account is private",
    "this content is not available",
    "is empty",  # instaloader sometimes raises BadResponseException("Fetching ... is empty")
)


def _looks_like_ig_auth_failure(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(hint in msg for hint in _IG_AUTH_FAIL_HINTS)


def _cookiefile_for(url: str) -> str | None:
    """Return cookies.txt path if usable for this URL, else None.

    Instagram and YouTube both reject many anonymous requests from datacenter
    IPs — passing cookies to yt-dlp is the difference between a 401/403 and a
    successful extraction. yt-dlp ignores domain-mismatched cookies silently,
    so a jar containing only Instagram cookies is safe to pass for YouTube.
    """
    if not os.path.isfile(_COOKIES_FILE):
        return None
    try:
        if os.path.getsize(_COOKIES_FILE) < 50:
            return None
    except OSError:
        return None
    url_l = url.lower()
    if "instagram.com" in url_l or "youtube.com" in url_l or "youtu.be" in url_l:
        return _COOKIES_FILE
    return None

# Late-2025 / 2026 working set:
#   tv_simply — TV web client, DRM-free (replacement for `tv` after yt-dlp #12563)
#   mweb      — mobile web, lightweight, no PO-token gate
#   web       — last-resort fallback; benefits from cookies.txt when present
_YT_PLAYER_CLIENTS = ["tv_simply", "mweb", "web"]


def _base_opts() -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "logger": _SilentLogger(),
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
    }
    return opts


def _extra_opts_for(url: str) -> dict:
    """Per-URL yt-dlp option overrides. See `_YT_PLAYER_CLIENTS` for the picks."""
    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        return {"extractor_args": {"youtube": {"player_client": _YT_PLAYER_CLIENTS}}}
    return {}


# ── Platform registry ──────────────────────────────────────────────────────
# Each entry: regex matching a URL on that platform.
# Order matters when a URL could match multiple platforms (none currently overlap).

PLATFORMS: dict[str, re.Pattern[str]] = {
    "instagram": re.compile(
        r"https?://(www\.)?instagram\.com/(p|reel|tv|stories)/[\w\-/]+",
        re.IGNORECASE,
    ),
    "youtube": re.compile(
        r"https?://(www\.)?(m\.)?(youtube\.com/(watch|shorts/|playlist)|youtu\.be/)",
        re.IGNORECASE,
    ),
    "tiktok": re.compile(
        r"https?://(www\.|vm\.|vt\.|m\.)?tiktok\.com/",
        re.IGNORECASE,
    ),
    "snapchat": re.compile(
        r"https?://(www\.)?(snapchat\.com/(@[\w.-]+/)?(spotlight|add|t|p|s|discover)/|story\.snapchat\.com/)",
        re.IGNORECASE,
    ),
    "likee": re.compile(
        r"https?://(www\.|l\.|m\.)?(likee\.video/|likee\.com/)",
        re.IGNORECASE,
    ),
    "pinterest": re.compile(
        r"https?://(www\.|[a-z]{2}\.)?(pinterest\.[a-z.]+/pin/|pin\.it/)",
        re.IGNORECASE,
    ),
    "threads": re.compile(
        r"https?://(www\.)?threads\.(net|com)/@?[\w./-]+",
        re.IGNORECASE,
    ),
}

_SHORTCODE_PATTERN = re.compile(r"/(p|reel|tv)/([A-Za-z0-9_-]+)")

ContentType = Literal[
    "reel", "post", "story",
    "youtube", "tiktok",
    "snapchat", "likee", "pinterest", "threads",
]


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


def _reload_instaloader_cookies() -> None:
    """Drop the singleton so the next caller rebuilds it with a fresh cookie jar.

    Called after the admin uploads a new cookies.txt via /upload_cookies. In-flight
    requests finish with the old session; the next request pays one constructor
    cost (~50ms). Attribute writes are atomic in CPython, so no lock is needed.
    """
    global _instaloader_instance
    _instaloader_instance = None


# ── Concurrency limiters ────────────────────────────────────────────────────
# Lazy init so they're created inside the running event loop.

_extract_sem: asyncio.Semaphore | None = None
_download_sem: asyncio.Semaphore | None = None

def _get_extract_sem() -> asyncio.Semaphore:
    global _extract_sem
    if _extract_sem is None:
        _extract_sem = asyncio.Semaphore(24)  # max 24 parallel info extractions
    return _extract_sem

def _get_download_sem() -> asyncio.Semaphore:
    global _download_sem
    if _download_sem is None:
        _download_sem = asyncio.Semaphore(12)  # max 12 parallel file downloads
    return _download_sem


# Hard timeouts wrap blocking yt-dlp work so a hung extractor / slow CDN can't
# pin a semaphore slot forever. Tuned generously: YouTube with multi-client
# fallback (tv→ios→web) plus 3 retries can legitimately spend 60-80s on a
# slow extraction, and full audio+ffmpeg can spend several minutes.
_EXTRACT_TIMEOUT_SEC = 120.0
_DOWNLOAD_TIMEOUT_SEC = 600.0


# ── CDN result cache ────────────────────────────────────────────────────────
# (url, height) → (fetched_at_monotonic, meta, cdn_items)
# Different heights produce different CDN URLs, so they cache independently.

_RESULT_CACHE: dict[tuple[str, int | None], tuple[float, dict, list]] = {}
_RESULT_CACHE_TTL = 1800  # 30 minutes
_RESULT_CACHE_MAX = 2000  # evict oldest when full

# In-flight de-dupe: when two callers ask for the same (url, height) before the
# first extraction finishes, the second awaits the first's future instead of
# spawning a parallel yt-dlp run. Catches viral links and double-clicks.
_inflight: dict[tuple[str, int | None], asyncio.Future] = {}


def is_supported_url(url: str) -> bool:
    return any(p.search(url) for p in PLATFORMS.values())


# Backwards-compatible alias — older code/tests still call is_instagram_url
# despite it always covering all supported platforms.
def is_instagram_url(url: str) -> bool:
    return is_supported_url(url)


def detect_platform(url: str) -> str | None:
    for name, pattern in PLATFORMS.items():
        if pattern.search(url):
            return name
    return None


def detect_content_type(url: str) -> ContentType:
    lower = url.lower()
    if "tiktok.com" in lower:
        return "tiktok"
    if "youtube.com" in lower or "youtu.be" in lower:
        return "youtube"
    if "snapchat.com" in lower:
        return "snapchat"
    if "likee.video" in lower or "likee.com" in lower:
        return "likee"
    if "pinterest." in lower or "pin.it" in lower:
        return "pinterest"
    if "threads.net" in lower or "threads.com" in lower:
        return "threads"
    if "/stories/" in lower:
        return "story"
    if "/reel/" in lower:
        return "reel"
    return "post"


def _format_for(content_type: str, height: int | None, url: str) -> str:
    """yt-dlp format string for a given platform + height preference."""
    if height and height > 0:
        return (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={height}][ext=mp4]/best[height<={height}]/best"
        )
    if content_type == "youtube":
        return "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]"
    if content_type in ("reel", "post", "story") and "instagram.com" in url.lower():
        # Cookies bilan yt-dlp HLS formatni tanlab qo'yadi (yuzlab segment = sekin).
        # To'g'ridan HTTP MP4 ni majburlaymiz.
        return "best[protocol^=http][ext=mp4]/best[protocol^=http]/best[ext=mp4]/best"
    return "best"


async def extract_info_full(url: str, height: int | None = None) -> tuple[dict, list[tuple[str, str]]]:
    """Return (metadata_dict, cdn_items_list). Cached + concurrency-limited + in-flight de-duped."""
    cache_key = (url, height)
    # Fast path: cache hit (no lock needed — asyncio is single-threaded)
    entry = _RESULT_CACHE.get(cache_key)
    if entry:
        ts, meta, cdn = entry
        if time.monotonic() - ts < _RESULT_CACHE_TTL:
            return meta, cdn

    # If another coroutine is already extracting this exact (url, height),
    # piggyback on its future instead of acquiring a semaphore slot ourselves.
    pending = _inflight.get(cache_key)
    if pending is not None:
        return await pending

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _inflight[cache_key] = fut
    try:
        async with _get_extract_sem():
            # Re-check after acquiring semaphore — a sibling may have populated the cache
            entry = _RESULT_CACHE.get(cache_key)
            if entry:
                ts, meta, cdn = entry
                if time.monotonic() - ts < _RESULT_CACHE_TTL:
                    if not fut.done():
                        fut.set_result((meta, cdn))
                    return meta, cdn

            try:
                meta, cdn = await asyncio.wait_for(
                    _do_extract_info(url, height=height),
                    timeout=_EXTRACT_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError as e:
                err = TimeoutError(f"extract_info timed out for {url}")
                if not fut.done():
                    fut.set_exception(err)
                logging.warning("extract_info: timeout (>%.0fs) for %s", _EXTRACT_TIMEOUT_SEC, url)
                raise err from e
            except BaseException as e:
                if not fut.done():
                    fut.set_exception(e)
                    # We propagate via `raise` below; mark the future's exception
                    # as retrieved so asyncio doesn't warn when there are no
                    # concurrent awaiters piggybacking on this future.
                    fut.exception()
                raise

            if cdn:
                if len(_RESULT_CACHE) >= _RESULT_CACHE_MAX:
                    _RESULT_CACHE.pop(next(iter(_RESULT_CACHE)))
                _RESULT_CACHE[cache_key] = (time.monotonic(), meta, cdn)

            if not fut.done():
                fut.set_result((meta, cdn))
            return meta, cdn
    finally:
        _inflight.pop(cache_key, None)


async def _do_extract_info(url: str, height: int | None = None) -> tuple[dict, list[tuple[str, str]]]:
    """Inner extraction — no cache, no semaphore."""
    content_type = detect_content_type(url)
    loop = asyncio.get_running_loop()

    # Threads: yt-dlp covers only some video posts and almost no image posts.
    # The og: scraper handles both reliably for the common case (single media).
    if content_type == "threads":
        meta, cdn = await fetch_threads_media(url)
        if cdn:
            return meta, cdn
        # fall through to yt-dlp as a last-resort attempt

    # Fast path: instaloader for Instagram posts AND reels (~2-4s vs yt-dlp ~8-12s).
    # Skipped when the user requested a specific height — instaloader returns whatever
    # IG provides, so respecting `height` requires the yt-dlp path.
    if (
        height is None
        and content_type in ("post", "reel")
        and "instagram.com" in url
    ):
        m = _SHORTCODE_PATTERN.search(url)
        if m:
            shortcode = m.group(2)
            try:
                future = loop.run_in_executor(None, _instaloader_fetch, shortcode)
                meta, cdn_items = await asyncio.wait_for(future, timeout=4.0)
                # Only use instaloader CDN for image-only posts — Telegram can access image
                # CDN URLs directly. For video URLs instaloader returns auth-scoped links
                # that Telegram's servers cannot fetch; yt-dlp returns publicly signed URLs.
                if cdn_items and all(e in ("jpg", "jpeg", "png", "webp") for _, e in cdn_items):
                    return meta, cdn_items
            except asyncio.TimeoutError:
                pass  # exceeded 4s budget — fall through to yt-dlp
            except Exception as e:
                # Auth failure (login required / 401 / checkpoint) — bail to a typed
                # error so the caller can show "cookies expired" instead of "error".
                if _looks_like_ig_auth_failure(e):
                    raise CookieExpiredError(f"instaloader: {e}") from e
                # Anything else (transient network, parser hiccup) → fall through
                # to yt-dlp which has its own retries.

    fmt = _format_for(content_type, height, url)
    ydl_opts = {**_base_opts(), **_extra_opts_for(url), "format": fmt}
    cookiefile = _cookiefile_for(url)
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    def _extract() -> tuple[dict, list[tuple[str, str]]]:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            if "instagram.com" in url.lower() and _looks_like_ig_auth_failure(e):
                raise CookieExpiredError(f"yt-dlp extract: {e}") from e
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
            "duration":  int(first.get("duration") or 0),
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


async def fetch_instagram_meta(url: str) -> dict:
    """Metadata-only fetch via instaloader (no CDN URLs, no yt-dlp)."""
    m = _SHORTCODE_PATTERN.search(url)
    if not m:
        return {}
    shortcode = m.group(2)
    loop = asyncio.get_running_loop()
    try:
        meta, _ = await asyncio.wait_for(
            loop.run_in_executor(None, _instaloader_fetch, shortcode),
            timeout=8.0,
        )
        return meta
    except Exception:
        return {}


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
        "duration": int(getattr(post, "video_duration", 0) or 0),
    }
    return metadata, result


async def extract_direct_urls(url: str, height: int | None = None) -> list[tuple[str, str]]:
    """Return (cdn_url, ext) pairs without downloading. Fast path (~2-5s).

    Delegates to extract_info_full so we share its 5-minute result cache,
    extract-semaphore, and platform-specific fallbacks (Threads og: scraper,
    instaloader image-post handler). Without this, clicking the same quality
    button twice within the cache window costs two full yt-dlp extractions.
    """
    _, cdn_items = await extract_info_full(url, height=height)
    return cdn_items


async def download_media(url: str, height: int | None = None) -> list[str]:
    """Download all media from URL. Returns list of temp file paths."""
    async with _get_download_sem():
        content_type = detect_content_type(url)

        # Threads: scrape og: tags + stream the CDN URL directly. yt-dlp's
        # Threads extractor is unreliable, especially for photos.
        if content_type == "threads":
            _, cdn_items = await fetch_threads_media(url)
            if cdn_items:
                paths: list[str] = []
                for cdn_url, ext in cdn_items:
                    paths.append(await _stream_cdn_to_file(cdn_url, ext))
                if paths:
                    return paths

        output_dir = os.path.join(TEMP_DIR, str(uuid.uuid4()))
        os.makedirs(output_dir, exist_ok=True)

        fmt = _format_for(content_type, height, url)

        ydl_opts = {
            **_base_opts(),
            **_extra_opts_for(url),
            "format": fmt,
            "outtmpl": os.path.join(output_dir, "%(autonumber)03d_%(title)s.%(ext)s"),
            "concurrent_fragment_downloads": 20,
        }
        cookiefile = _cookiefile_for(url)
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile

        loop = asyncio.get_running_loop()

        def _download() -> None:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                if "instagram.com" in url.lower() and _looks_like_ig_auth_failure(e):
                    raise CookieExpiredError(f"yt-dlp download: {e}") from e
                raise

        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _download),
                timeout=_DOWNLOAD_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as e:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise TimeoutError(f"yt-dlp download timed out for {url}") from e

        files = sorted(Path(output_dir).iterdir())
        if not files:
            raise FileNotFoundError("yt-dlp produced no files")

        return [str(f) for f in files]


async def search_and_download_audio(query: str) -> tuple[str, dict] | None:
    """Single yt-dlp call: ytsearch + download top match's audio (no MP3 reencode).

    Used by the Shazam-recognition flow where speed matters more than the
    forced MP3 codec — Telegram plays m4a/aac with proper title/performer
    metadata just fine, and skipping the FFmpeg conversion saves ~10-20s
    on a typical 4-minute track.
    """
    async with _get_download_sem():
        output_dir = os.path.join(TEMP_DIR, str(uuid.uuid4()))
        os.makedirs(output_dir, exist_ok=True)
        ydl_opts = {
            **_base_opts(),
            "extractor_args": {"youtube": {"player_client": _YT_PLAYER_CLIENTS}},
            "format": "bestaudio[ext=m4a]/bestaudio[acodec=aac]/bestaudio",
            "outtmpl": os.path.join(output_dir, "audio.%(ext)s"),
            "default_search": "ytsearch",
            "noplaylist": True,
            "playlist_items": "1",
            "concurrent_fragment_downloads": 10,
        }
        loop = asyncio.get_running_loop()
        captured: dict = {}

        def _run() -> None:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"ytsearch1:{query}")
                if info and info.get("entries"):
                    captured["info"] = info["entries"][0] or {}
            except Exception:
                logging.exception("search_and_download_audio failed for %r", query)

        await loop.run_in_executor(None, _run)

        files = sorted(Path(output_dir).glob("audio.*"))
        if not files:
            return None
        return str(files[0]), captured.get("info", {})


async def download_audio(url: str) -> str:
    """Download audio-only (mp3) from URL. Returns temp file path."""
    async with _get_download_sem():
        output_dir = os.path.join(TEMP_DIR, str(uuid.uuid4()))
        os.makedirs(output_dir, exist_ok=True)

        # Per-call logger so we can recover the last yt-dlp error message
        # if the download silently produces nothing.
        capture_logger = _SilentLogger()
        ydl_opts = {
            **_base_opts(),
            **_extra_opts_for(url),
            "logger": capture_logger,
            "format": "bestaudio/best",
            "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "playlist_items": "1",
            "concurrent_fragment_downloads": 10,
        }
        cookiefile = _cookiefile_for(url)
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile

        loop = asyncio.get_running_loop()

        def _download() -> None:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                if "instagram.com" in url.lower() and _looks_like_ig_auth_failure(e):
                    raise CookieExpiredError(f"yt-dlp audio: {e}") from e
                raise

        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _download),
                timeout=_DOWNLOAD_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as e:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise TimeoutError(f"yt-dlp audio download timed out for {url}") from e

        mp3_files = list(Path(output_dir).glob("*.mp3"))
        all_files = list(Path(output_dir).iterdir())
        files = mp3_files if mp3_files else all_files

        if not files:
            detail = capture_logger.last_error or "no formats / unknown reason"
            raise FileNotFoundError(f"yt-dlp produced no audio — {detail[:200]}")

        return str(files[0])


_META_TAG_RE = re.compile(r'<meta\b([^>]*?)/?>', re.IGNORECASE | re.DOTALL)
_META_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*["\']([^"\']*)["\']')


def _parse_og_tags(html: str) -> dict[str, str]:
    """Extract og:* meta tags via a two-pass parse — robust to attribute order
    and unrelated attributes between property and content."""
    tags: dict[str, str] = {}
    for tag_match in _META_TAG_RE.finditer(html):
        attrs_str = tag_match.group(1)
        attrs = dict(_META_ATTR_RE.findall(attrs_str))
        prop = (attrs.get("property") or attrs.get("name") or "").lower()
        if not prop.startswith("og:"):
            continue
        content = attrs.get("content", "")
        key = prop[3:]  # strip "og:"
        if not content or key in tags:
            continue
        # html.unescape handles named (&amp;) AND numeric (&#064;) entities.
        tags[key] = _html_lib.unescape(content)
    return tags


_JS_UNICODE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _decode_js_string(s: str) -> str:
    """Decode JavaScript string escapes — \\/, \\uXXXX — back to plain text."""
    s = s.replace("\\/", "/")
    s = _JS_UNICODE_RE.sub(lambda m: chr(int(m.group(1), 16)), s)
    return s


# Threads embeds video URLs in inline JS state — these patterns find them.
_THREADS_VIDEO_PATTERNS = (
    re.compile(r'"playable_url_quality_hd":"([^"\\]*(?:\\.[^"\\]*)*)"'),
    re.compile(r'"playable_url":"([^"\\]*(?:\\.[^"\\]*)*)"'),
    re.compile(r'"video_url":"([^"\\]*(?:\\.[^"\\]*)*)"'),
    re.compile(r'"video_versions":\s*\[\s*\{[^}]*?"url":"([^"\\]*(?:\\.[^"\\]*)*)"'),
)


def _extract_threads_video_from_json(html: str) -> str | None:
    """Threads doesn't expose og:video — the video URL is in inline JS state."""
    for pat in _THREADS_VIDEO_PATTERNS:
        m = pat.search(html)
        if m:
            url = _decode_js_string(m.group(1))
            if url.startswith("http"):
                return url
    return None


# User-Agents tried in order until og: tags are found. Threads/Meta sites
# whitelist their own crawler and Telegram's bot for og: rendering even when
# they would gate the SPA content from a generic browser request.
_THREADS_USER_AGENTS = (
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "TelegramBot (like TwitterBot)",
    "Twitterbot/1.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)


async def _fetch_html(url: str, user_agent: str, timeout_sec: float = 10.0) -> str:
    """Fetch URL HTML with a specific User-Agent. Returns "" on any failure."""
    import aiohttp
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    logging.info("Threads scrape: UA=%r → HTTP %d", user_agent[:30], resp.status)
                    return ""
                return await resp.text(errors="ignore")
    except Exception as e:
        logging.warning("Threads scrape: UA=%r → %s", user_agent[:30], e)
        return ""


def _meta_from_og(og: dict[str, str], image: str) -> dict:
    """Build the standard meta dict from parsed og: tags."""
    title = og.get("description") or og.get("title") or ""
    uploader = ""
    og_title = og.get("title") or ""
    handle_match = re.search(r"\(@([\w.]+)\)", og_title)
    if handle_match:
        uploader = handle_match.group(1)
    elif og_title:
        uploader = og_title.split(" on Threads")[0].strip()
    return {
        "title": title,
        "uploader": uploader,
        "thumbnail": image or "",
    }


async def fetch_threads_media(url: str) -> tuple[dict, list[tuple[str, str]]]:
    """Scrape Threads post HTML for video / image URLs.

    Threads (Meta) injects og: tags server-side for shareability, but og:video
    is NOT exposed — the video URL lives only in inline JS state. We try
    multiple User-Agents (FB crawler first, then Telegram, Twitter, browser)
    until we either find an og:video or pull a `playable_url`/`video_versions`
    URL out of the page's embedded JSON. og:image is always the fallback.
    """
    best_video: str | None = None
    best_image: str | None = None
    best_meta: dict = {}

    for ua in _THREADS_USER_AGENTS:
        html = await _fetch_html(url, ua)
        if not html:
            continue
        og = _parse_og_tags(html)

        video = (
            og.get("video")
            or og.get("video:secure_url")
            or og.get("video:url")
            or _extract_threads_video_from_json(html)
        )
        image = og.get("image") or og.get("image:secure_url") or og.get("image:url")

        if video and not best_video:
            best_video = video
        if image and not best_image:
            best_image = image
        if (video or image) and not best_meta:
            best_meta = _meta_from_og(og, image or "")

        logging.info(
            "Threads scrape: UA=%r → html=%d og=%s video=%s image=%s",
            ua[:30], len(html), list(og.keys()),
            "yes" if video else "no",
            "yes" if image else "no",
        )

        if best_video:
            break  # video is the best outcome — stop trying more UAs

    cdn_items: list[tuple[str, str]] = []
    if best_video:
        cdn_items.append((best_video, "mp4"))
    elif best_image:
        cdn_items.append((best_image, "jpg"))

    if not cdn_items:
        logging.warning("Threads scrape: no media found for %s", url)
    return best_meta, cdn_items


async def search_music(query: str, limit: int = 10) -> list[dict]:
    """YouTube search → list of {title, url, duration, uploader} dicts.

    Powers the "type a song or artist name and get a picker" flow for users
    who don't have a direct link. Uses yt-dlp's flat-extract mode so the
    response stays fast (~2-3s for 10 results) instead of fetching full
    metadata per result.
    """
    if not query or len(query.strip()) < 2:
        return []

    ydl_opts = {
        **_base_opts(),
        "extractor_args": {"youtube": {"player_client": _YT_PLAYER_CLIENTS}},
        "extract_flat": True,
        "skip_download": True,
        "default_search": "ytsearch",
    }
    loop = asyncio.get_running_loop()

    def _search() -> list[dict]:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        except Exception:
            logging.exception("yt-dlp search failed for %r", query)
            return []
        if not info:
            return []
        results: list[dict] = []
        for entry in info.get("entries") or []:
            if not entry:
                continue
            url = entry.get("url") or entry.get("webpage_url") or ""
            # Flat mode sometimes yields just a video ID — promote to full URL.
            if url and not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={url}"
            if not url:
                continue
            results.append({
                "title":    entry.get("title", "") or "",
                "url":      url,
                "duration": entry.get("duration") or 0,
                "uploader": entry.get("uploader") or entry.get("channel") or "",
                "thumbnail": entry.get("thumbnail") or "",
            })
        return results

    async with _get_extract_sem():
        return await loop.run_in_executor(None, _search)


async def _stream_cdn_to_file(cdn_url: str, ext: str) -> str:
    """Download a CDN URL to disk via aiohttp. No semaphore — caller manages concurrency."""
    import aiohttp
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


async def download_cdn_url(cdn_url: str, ext: str) -> str:
    """Stream-download a direct CDN URL to a temp file via aiohttp (no yt-dlp re-extraction)."""
    async with _get_download_sem():
        return await _stream_cdn_to_file(cdn_url, ext)


def cleanup(path: str) -> None:
    """Remove the UUID temp directory that contains path."""
    parent = str(Path(path).parent)
    shutil.rmtree(parent, ignore_errors=True)
