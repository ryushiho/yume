import os
from dotenv import load_dotenv

# =========================
# Minimal config (core Yume bot)
# - Music / BlueWar are removed in this build.
# - Keep only shared paths and DB location.
# =========================

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

ENV_FILE = os.path.join(PROJECT_ROOT, "yumebot.env")
load_dotenv(ENV_FILE)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SYSTEM_DIR = os.path.join(DATA_DIR, "system")
STORAGE_DIR = os.path.join(DATA_DIR, "storage")

# Phase0+: persistent SQLite DB (world state / settings / scheduled jobs)
YUME_DB_FILE = os.path.join(STORAGE_DIR, "yume_bot.db")


def ensure_directories():
    """유메가 실행될 때 필요한 폴더가 존재하지 않으면 자동 생성."""
    for d in [DATA_DIR, SYSTEM_DIR, STORAGE_DIR]:
        if not os.path.exists(d):
            os.makedirs(d, exist_ok=True)


ensure_directories()
