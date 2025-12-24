# records_core.py
import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, List

from config import BLUE_RECORDS_FILE

RECORDS_FILE = BLUE_RECORDS_FILE


def _empty_schema() -> Dict[str, Any]:
    return {
        "meta": {
            "schema": 2,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        "users": {},
        "matches": [],
    }


def ensure_records() -> None:
    """전적 파일/디렉터리 존재 보장. 없으면 기본 스키마로 생성."""
    os.makedirs(os.path.dirname(RECORDS_FILE), exist_ok=True)
    if not os.path.exists(RECORDS_FILE):
        with open(RECORDS_FILE, "w", encoding="utf-8") as f:
            json.dump(_empty_schema(), f, ensure_ascii=False, indent=2)


def _load_raw() -> Any:
    if not os.path.exists(RECORDS_FILE):
        return None
    try:
        with open(RECORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None
    except OSError:
        return None


def _migrate_if_legacy(data: Any) -> Dict[str, Any]:
    """
    레거시 포맷 지원:
      { "discord_id": {"win": 1, "loss": 2, "name": "xxx"}, ... }
    -> 신규 포맷:
      {"users": {id: {"wins":..,"losses":..,"name":..}}, "matches":[...]}
    """
    if not isinstance(data, dict):
        return _empty_schema()

    # 이미 신규 포맷이면 그대로
    if "users" in data and "matches" in data and isinstance(data.get("users"), dict) and isinstance(data.get("matches"), list):
        if "meta" not in data or not isinstance(data.get("meta"), dict):
            data["meta"] = {"schema": 2, "updated_at": datetime.now(timezone.utc).isoformat()}
        return data

    # 레거시로 판단: 키들이 user_id처럼 보이고 값이 dict(win/loss) 형태
    users: Dict[str, Any] = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        win = v.get("win", 0)
        loss = v.get("loss", 0)
        name = v.get("name")
        try:
            win_i = int(win)
        except Exception:
            win_i = 0
        try:
            loss_i = int(loss)
        except Exception:
            loss_i = 0

        entry = {"wins": win_i, "losses": loss_i}
        if isinstance(name, str) and name.strip():
            entry["name"] = name.strip()
        users[str(k)] = entry

    migrated = _empty_schema()
    migrated["users"] = users
    return migrated


def load_records() -> Dict[str, Any]:
    """전적 로드 (신규 스키마로 반환)."""
    ensure_records()
    raw = _load_raw()
    data = _migrate_if_legacy(raw)

    # 최소 필드 보정
    if "users" not in data or not isinstance(data["users"], dict):
        data["users"] = {}
    if "matches" not in data or not isinstance(data["matches"], list):
        data["matches"] = []
    if "meta" not in data or not isinstance(data["meta"], dict):
        data["meta"] = {"schema": 2, "updated_at": datetime.now(timezone.utc).isoformat()}

    return data


def save_records(data: Dict[str, Any]) -> None:
    """전적 저장 (신규 스키마로 저장)."""
    if not isinstance(data, dict):
        data = _empty_schema()

    if "meta" not in data or not isinstance(data.get("meta"), dict):
        data["meta"] = {"schema": 2}
    data["meta"]["schema"] = 2
    data["meta"]["updated_at"] = datetime.now(timezone.utc).isoformat()

    if "users" not in data or not isinstance(data.get("users"), dict):
        data["users"] = {}
    if "matches" not in data or not isinstance(data.get("matches"), list):
        data["matches"] = []

    os.makedirs(os.path.dirname(RECORDS_FILE), exist_ok=True)
    with open(RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _touch_user(users: Dict[str, Any], user_id: str, name: str | None = None) -> Dict[str, Any]:
    u = users.get(user_id)
    if not isinstance(u, dict):
        u = {"wins": 0, "losses": 0}
        users[user_id] = u

    if "wins" not in u:
        u["wins"] = 0
    if "losses" not in u:
        u["losses"] = 0

    if name and isinstance(name, str) and name.strip():
        u["name"] = name.strip()
    return u


def add_match_record(
    records: Dict[str, Any],
    *,
    mode: str,
    winner_id: str,
    loser_id: str,
    winner_name: str,
    loser_name: str,
    win_gap: int | None,
    total_rounds: int,
    history: List[str],
) -> None:
    """
    blue_war.py가 요구하는 형태로:
    - users 승/패 누적
    - matches에 한 판 로그 저장
    """
    if "users" not in records or not isinstance(records.get("users"), dict):
        records["users"] = {}
    if "matches" not in records or not isinstance(records.get("matches"), list):
        records["matches"] = []

    users: Dict[str, Any] = records["users"]
    matches: List[Dict[str, Any]] = records["matches"]

    w = _touch_user(users, str(winner_id), winner_name)
    l = _touch_user(users, str(loser_id), loser_name)

    try:
        w["wins"] = int(w.get("wins", 0)) + 1
    except Exception:
        w["wins"] = 1

    try:
        l["losses"] = int(l.get("losses", 0)) + 1
    except Exception:
        l["losses"] = 1

    matches.append(
        {
            "mode": mode,
            "winner_id": str(winner_id),
            "loser_id": str(loser_id),
            "winner_name": winner_name,
            "loser_name": loser_name,
            "win_gap": win_gap,
            "total_rounds": int(total_rounds) if total_rounds is not None else 0,
            "history": list(history) if history else [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


def calc_rankings(records: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    단순 랭킹:
    wins desc, losses asc
    """
    users = records.get("users", {})
    if not isinstance(users, dict):
        return []

    rows: List[Dict[str, Any]] = []
    for uid, u in users.items():
        if not isinstance(u, dict):
            continue
        name = u.get("name") or str(uid)
        try:
            wins = int(u.get("wins", 0))
        except Exception:
            wins = 0
        try:
            losses = int(u.get("losses", 0))
        except Exception:
            losses = 0
        rows.append({"id": str(uid), "name": str(name), "wins": wins, "losses": losses})

    rows.sort(key=lambda r: (-r["wins"], r["losses"], r["name"]))
    return rows
