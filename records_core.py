# records_core.py
import os
import json
from typing import Dict, Any

from config import BLUE_RECORDS_FILE

# 실제 전적 파일 경로
# 예: C:\Users\henna\Downloads\yumebot\data\storage\blue_records.json
RECORDS_FILE = BLUE_RECORDS_FILE


def load_records() -> Dict[str, Dict[str, Any]]:
    """JSON에서 전적 불러오기. win/loss/name 을 모두 읽어온다."""
    if not os.path.exists(RECORDS_FILE):
        return {}

    try:
        with open(RECORDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[WARN] 전적 파일 JSON 파싱 실패, 새로 시작할게: {e!r}")
        return {}
    except OSError as e:
        print(f"[WARN] 전적 파일 읽기 실패: {e!r}")
        return {}

    if not isinstance(data, dict):
        return {}

    result: Dict[str, Dict[str, Any]] = {}

    for key, value in data.items():
        if not isinstance(value, dict):
            continue

        win = int(value.get("win", 0))
        loss = int(value.get("loss", 0))

        entry: Dict[str, Any] = {"win": win, "loss": loss}

        name = value.get("name")
        if isinstance(name, str) and name.strip():
            entry["name"] = name.strip()

        # key 는 str 로 맞춰서 저장해 둔다. (BlueWarCog 쪽에서 int로 캐스팅)
        result[str(key)] = entry

    return result


def save_records(data: Dict[str, Dict[str, Any]]) -> None:
    """전적 딕셔너리를 JSON 으로 저장."""
    serializable: Dict[str, Dict[str, Any]] = {}

    for key, value in data.items():
        win = int(value.get("win", 0))
        loss = int(value.get("loss", 0))

        entry: Dict[str, Any] = {"win": win, "loss": loss}

        name = value.get("name")
        if isinstance(name, str) and name.strip():
            entry["name"] = name.strip()

        serializable[str(key)] = entry

    try:
        # 디렉터리가 없으면 먼저 만든다.
        os.makedirs(os.path.dirname(RECORDS_FILE), exist_ok=True)

        with open(RECORDS_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[WARN] 전적 파일 저장 실패: {e!r}")
