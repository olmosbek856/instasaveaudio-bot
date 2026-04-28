import os
import sys
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456789:dummy-token-for-pytest")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
