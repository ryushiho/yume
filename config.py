import os
from dotenv import load_dotenv

# ------------------------------
# 🌐 환경 변수 로드 (.env / yumebot.env)
# ------------------------------
# 이 파일(config.py)이 들어있는 폴더 = 프로젝트 루트(yumebot)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

ENV_FILE = os.path.join(PROJECT_ROOT, "yumebot.env")
load_dotenv(ENV_FILE)

# ------------------------------
# 📌 Bot Token / API Keys
# ------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

# ------------------------------
# 📦 데이터 디렉토리 경로 설정
# ------------------------------
# => C:\Users\henna\Downloads\yumebot\data 처럼 동작
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

DICT_DIR = os.path.join(DATA_DIR, "dictionary")
USER_STATE_DIR = os.path.join(DATA_DIR, "user_state")
SYSTEM_DIR = os.path.join(DATA_DIR, "system")
STORAGE_DIR = os.path.join(DATA_DIR, "storage")

# ------------------------------
# 📂 개별 파일 경로 정의
# ------------------------------

# 한국어 단어 기반 게임 데이터 (Blue War)
WORDS_FILE = os.path.join(DICT_DIR, "blue_archive_words.txt")

# 블루전 기록 저장
BLUE_RECORDS_FILE = os.path.join(STORAGE_DIR, "blue_records.json")

# 유메 AI 감정/캐릭터 상태 저장 (앞으로 사용할 예정)
PERSONALITY_FILE = os.path.join(SYSTEM_DIR, "yume_personality.json")

# ------------------------------
# 🎮 블루전 / 게임 관련 설정
# ------------------------------

# 턴 제한 시간 (초)
TURN_TIMEOUT: float = 90.0

# 게임 복기용 채널
REVIEW_CHANNEL_ID: int = 1438871186330222662

# 게임 결과 알리기 채널
RESULT_CHANNEL_ID: int = 1438806750990962789

# 블루전 랭킹 채널
RANK_CHANNEL_ID: int = 1438804301916016723


# ------------------------------
# 📌 실행환경 테스트 (존재하지 않는 폴더 자동 생성)
# ------------------------------
def ensure_directories():
    """유메가 실행될 때 필요한 폴더가 존재하지 않으면 자동 생성."""
    dirs = [
        DATA_DIR,
        DICT_DIR,
        USER_STATE_DIR,
        SYSTEM_DIR,
        STORAGE_DIR,
    ]

    for d in dirs:
        if not os.path.exists(d):
            os.makedirs(d)


ensure_directories()
