import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TEMP_DIR = "./temp"
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB — Telegram bot upload limit
