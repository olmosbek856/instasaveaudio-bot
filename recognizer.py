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
        from shazamio import Shazam
        _shazam = Shazam()
    return _shazam


def _get_sem() -> asyncio.Semaphore:
    global _shazam_sem
    if _shazam_sem is None:
        _shazam_sem = asyncio.Semaphore(2)
    return _shazam_sem


def make_workdir() -> str:
    """Create a fresh UUID-scoped temp directory for one recognition request."""
    out = os.path.join(TEMP_DIR, f"shazam-{uuid.uuid4()}")
    os.makedirs(out, exist_ok=True)
    return out


async def extract_audio_clip(input_path: str, out_dir: str) -> str:
    """Trim to first ~20s + downmix to 16kHz mono mp3 — enough for fingerprinting."""
    out = os.path.join(out_dir, "clip.mp3")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-i", input_path,
        "-t", str(_SHAZAM_CLIP_SECONDS),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-acodec", "libmp3lame",
        "-ab", "64k",
        "-y", out,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
        raise RuntimeError(f"ffmpeg failed (rc={proc.returncode}) on {input_path}")
    return out


async def recognize(audio_path: str) -> dict | None:
    """Return {title, artist, url, cover, apple} or None when unrecognised."""
    async with _get_sem():
        try:
            result = await asyncio.wait_for(
                _get_shazam().recognize(audio_path),
                timeout=_RECOGNIZE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logging.warning("Shazam timed out for %s", audio_path)
            return None
        except Exception:
            logging.exception("Shazam recognise failed for %s", audio_path)
            return None

    track = (result or {}).get("track")
    if not track:
        return None

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
