import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TEMP_DIR = "./temp"
DATA_DIR = "./data"
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB — Telegram bot upload limit

# Optional: error reporting (Sentry). When unset, sentry_sdk init is skipped.
SENTRY_DSN = os.getenv("SENTRY_DSN") or ""

# Optional: structured log level. Accepts standard logging level names.
LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()

# Per-user daily request quota across all kinds (URL, search, shazam, audio).
# 200 leaves a comfortable margin for normal users while killing scrapers.
DAILY_QUOTA = int(os.getenv("DAILY_QUOTA") or "200")

# Comma-separated Telegram user IDs that bypass the daily quota and receive
# admin-alert DMs (cookie expiry, repeated failures, etc.).
def _parse_admin_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if piece.isdigit() or (piece.startswith("-") and piece[1:].isdigit()):
            try:
                out.add(int(piece))
            except ValueError:
                pass
    return out

ADMIN_USER_IDS: set[int] = _parse_admin_ids(os.getenv("ADMIN_USER_IDS"))

# Healthcheck: bot writes mtime here every 30s; Docker HEALTHCHECK reads it.
HEALTH_FILE = os.path.join(DATA_DIR, "health")
