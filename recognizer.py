"""Music recognition via ShazamIO. Accepts audio paths and returns track info."""
import asyncio
import logging
import os
import uuid
from pathlib import Path

from config import TEMP_DIR

_shazam = None
_shazam_sem: asyncio.Semaphore | None = None

# Shazam recognises 12s of audio reliably; we trim to 20s as a safety margin.
_SHAZAM_CLIP_SECONDS = 20
# Soft ceiling so a malformed file can't tie up an executor forever.
_RECOGNIZE_TIMEOUT = 25.0


def _get_shazam():
    global _shazam
    if _shazam is None:
        try:
            from shazamio import Shazam
        except ImportError as e:
            logging.error(
                "shazamio not installed — music recognition disabled. "
                "Install with: pip install shazamio (Python 3.13 + Windows requires "
                "Visual Studio Build Tools for the Rust shazamio-core compile; on Linux "
                "Docker the prebuilt wheels just work). Original error: %s", e,
            )
            raise
        _shazam = Shazam()
    return _shazam


def _get_sem() -> asyncio.Semaphore:
    global _shazam_sem
    if _shazam_sem is None:
        _shazam_sem = asyncio.Semaphore(3)
    return _shazam_sem


def make_workdir() -> str:
    """Create a fresh UUID-scoped temp directory for one recognition request."""
    out = os.path.join(TEMP_DIR, f"shazam-{uuid.uuid4()}")
    os.makedirs(out, exist_ok=True)
    return out


async def extract_audio_clip(input_path: str, out_dir: str) -> str:
    """Trim to first ~20s — preserves source quality (no downsample/downmix).

    Earlier we forced 16kHz mono 64kbps which mangled the fingerprint and made
    Shazam miss recognisable tracks. Now we just trim and re-encode at high
    VBR quality, keeping stereo and original sample rate.
    """
    out = os.path.join(out_dir, "clip.mp3")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-i", input_path,
        "-t", str(_SHAZAM_CLIP_SECONDS),
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",  # high-quality VBR (~190 kbps)
        "-y", out,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
        # Tail of stderr is the only diagnostic for "Shazam silently fails":
        # surface it instead of swallowing.
        tail = (err or b"").decode("utf-8", errors="replace")[-500:].strip()
        logging.error("ffmpeg failed (rc=%s) on %s: %s", proc.returncode, input_path, tail)
        raise RuntimeError(f"ffmpeg failed (rc={proc.returncode}) on {input_path}")
    return out


async def recognize(audio_path: str) -> dict | None:
    """Return {title, artist, url, cover, apple} or None when unrecognised."""
    file_size = os.path.getsize(audio_path) if os.path.isfile(audio_path) else 0
    logging.info("Shazam: recognising %s (%d bytes)", audio_path, file_size)
    async with _get_sem():
        try:
            result = await asyncio.wait_for(
                _get_shazam().recognize(audio_path),
                timeout=_RECOGNIZE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logging.warning("Shazam: timeout for %s", audio_path)
            return None
        except Exception:
            logging.exception("Shazam: recognise failed for %s", audio_path)
            return None

    track = (result or {}).get("track")
    if not track:
        matches = (result or {}).get("matches") or []
        logging.info(
            "Shazam: no track for %s (matches=%d, raw_keys=%s)",
            audio_path, len(matches), list((result or {}).keys()),
        )
        return None
    logging.info("Shazam: matched %r — %r", track.get("title"), track.get("subtitle"))

    apple = None
    for action_block in track.get("hub", {}).get("actions", []) or []:
        if action_block.get("type") == "applemusicplay":
            apple = action_block.get("uri")
            break

    return {
        "title":  track.get("title", "") or "",
        "artist": track.get("subtitle", "") or "",
        "url":    track.get("url", "") or "",
        "cover":  (track.get("share") or {}).get("image", "") or "",
        "apple":  apple,
    }


def cleanup(workdir: str) -> None:
    """Remove a workdir created by make_workdir(). Safe if missing."""
    import shutil
    shutil.rmtree(workdir, ignore_errors=True)
