import os
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

ENV_FILE = os.path.join(PROJECT_ROOT, "yumebot.env")
load_dotenv(ENV_FILE)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

DATA_DIR = os.path.join(PROJECT_ROOT, "data")

DICT_DIR = os.path.join(DATA_DIR, "dictionary")
USER_STATE_DIR = os.path.join(DATA_DIR, "user_state")
SYSTEM_DIR = os.path.join(DATA_DIR, "system")
STORAGE_DIR = os.path.join(DATA_DIR, "storage")

CACHE_DIR = os.path.join(DATA_DIR, "cache")
BLUEWAR_WORDLIST_CACHE_DIR = os.path.join(CACHE_DIR, "bluewar_wordlists")
BLUEWAR_CACHED_WORDS_FILE = os.path.join(BLUEWAR_WORDLIST_CACHE_DIR, "blue_archive_words.txt")
BLUEWAR_CACHED_SUGGESTION_FILE = os.path.join(BLUEWAR_WORDLIST_CACHE_DIR, "suggestion.txt")
BLUEWAR_WORDLIST_META_FILE = os.path.join(BLUEWAR_WORDLIST_CACHE_DIR, "meta.json")


WORDS_FILE = os.path.join(DICT_DIR, "blue_archive_words.txt")

BLUE_RECORDS_FILE = os.path.join(STORAGE_DIR, "blue_records.json")

# Phase0: bot persistent SQLite DB (shared foundation for world state / settings / scheduled jobs)
YUME_DB_FILE = os.path.join(STORAGE_DIR, "yume_bot.db")

PERSONALITY_FILE = os.path.join(SYSTEM_DIR, "yume_personality.json")


TURN_TIMEOUT: float = 90.0

REVIEW_CHANNEL_ID: int = 1438871186330222662

RESULT_CHANNEL_ID: int = 1438806750990962789

RANK_CHANNEL_ID: int = 1438804301916016723


def ensure_directories():
    """유메가 실행될 때 필요한 폴더가 존재하지 않으면 자동 생성."""
    dirs = [
        DATA_DIR,
        DICT_DIR,
        USER_STATE_DIR,
        SYSTEM_DIR,
        STORAGE_DIR,
        CACHE_DIR,
        BLUEWAR_WORDLIST_CACHE_DIR,
    ]

    for d in dirs:
        if not os.path.exists(d):
            os.makedirs(d, exist_ok=True)


ensure_directories()
