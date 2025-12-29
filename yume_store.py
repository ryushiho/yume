"""yume_store.py

Phase0 foundation: small data-access helpers on top of yume_db.

We keep the API tiny and explicit, so later features (weather, time-capsules,
stamps, etc.) can build on it without sprinkling SQL everywhere.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from yume_db import execute, fetchone


# =========================
# Generic bot config
# =========================


def get_config(key: str, default: str | None = None) -> str | None:
    row = fetchone("SELECT value FROM bot_config WHERE key=?;", (str(key),))
    if not row:
        return default
    v = str(row.get("value") or "")
    return v if v != "" else default


def set_config(key: str, value: str) -> None:
    now = int(time.time())
    execute(
        """
        INSERT INTO bot_config(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
        """,
        (str(key), str(value), now),
    )


def ensure_user_settings(user_id: int) -> None:
    now = int(time.time())
    execute(
        """
        INSERT INTO user_settings(user_id, dm_opt_in, noise_opt_in, stamps, last_stamp_at, created_at, updated_at)
        VALUES(?, 1, 1, 0, 0, ?, ?)
        ON CONFLICT(user_id) DO NOTHING;
        """,
        (int(user_id), now, now),
    )


def get_user_settings(user_id: int) -> Dict[str, Any]:
    ensure_user_settings(user_id)
    row = fetchone(
        "SELECT user_id, dm_opt_in, noise_opt_in, stamps, last_stamp_at, created_at, updated_at FROM user_settings WHERE user_id=?;",
        (int(user_id),),
    )
    return row or {
        "user_id": int(user_id),
        "dm_opt_in": 1,
        "noise_opt_in": 1,
        "stamps": 0,
        "last_stamp_at": 0,
        "created_at": 0,
        "updated_at": 0,
    }


def set_user_opt_in(user_id: int, *, dm_opt_in: Optional[bool] = None, noise_opt_in: Optional[bool] = None) -> None:
    ensure_user_settings(user_id)
    now = int(time.time())
    if dm_opt_in is not None:
        execute(
            "UPDATE user_settings SET dm_opt_in=?, updated_at=? WHERE user_id=?;",
            (1 if dm_opt_in else 0, now, int(user_id)),
        )
    if noise_opt_in is not None:
        execute(
            "UPDATE user_settings SET noise_opt_in=?, updated_at=? WHERE user_id=?;",
            (1 if noise_opt_in else 0, now, int(user_id)),
        )


def get_world_state() -> Dict[str, Any]:
    row = fetchone(
        "SELECT weather, weather_changed_at, weather_next_change_at, updated_at FROM world_state WHERE id=1;"
    )
    # init_db() should ensure the row exists, but be defensive.
    if not row:
        return {
            "weather": "clear",
            "weather_changed_at": 0,
            "weather_next_change_at": 0,
            "updated_at": 0,
        }
    return row


def set_world_weather(weather: str, *, changed_at: Optional[int] = None, next_change_at: Optional[int] = None) -> None:
    now = int(time.time())
    changed = int(changed_at or now)
    next_at = int(next_change_at or (now + 6 * 3600))
    execute(
        """
        UPDATE world_state
        SET weather=?, weather_changed_at=?, weather_next_change_at=?, updated_at=?
        WHERE id=1;
        """,
        (str(weather), changed, next_at, now),
    )


# =========================
# Phase3: Daily rules (교칙)
# =========================


def get_daily_rule(date_ymd: str) -> Optional[Dict[str, Any]]:
    return fetchone(
        """
        SELECT date, rule_no, rule_text, created_at, posted_channel_id, posted_at, attempts, last_error
        FROM daily_rules
        WHERE date=?;
        """,
        (str(date_ymd),),
    )


def ensure_daily_rule_row(date_ymd: str) -> Dict[str, Any]:
    """Ensure a row exists for the given date, assigning the next rule number.

    This is a small "claim" step to prevent duplicates after restarts.
    """
    now = int(time.time())

    # If already exists, return it.
    row = get_daily_rule(date_ymd)
    if row:
        return row

    # Create a new one with rule_no = max + 1.
    max_row = fetchone("SELECT COALESCE(MAX(rule_no), 141) AS mx FROM daily_rules;")
    mx = int((max_row or {}).get("mx") or 141)
    next_no = mx + 1

    execute(
        """
        INSERT INTO daily_rules(date, rule_no, rule_text, created_at, posted_channel_id, posted_at, attempts, last_error)
        VALUES(?, ?, '', ?, NULL, NULL, 0, NULL)
        ON CONFLICT(date) DO NOTHING;
        """,
        (str(date_ymd), int(next_no), now),
    )

    row = get_daily_rule(date_ymd)
    return row or {
        "date": str(date_ymd),
        "rule_no": int(next_no),
        "rule_text": "",
        "created_at": now,
        "posted_channel_id": None,
        "posted_at": None,
        "attempts": 0,
        "last_error": None,
    }


def update_daily_rule_text(date_ymd: str, rule_text: str) -> None:
    execute(
        "UPDATE daily_rules SET rule_text=? WHERE date=?;",
        (str(rule_text), str(date_ymd)),
    )


def mark_daily_rule_posted(date_ymd: str, *, channel_id: int) -> None:
    now = int(time.time())
    execute(
        "UPDATE daily_rules SET posted_channel_id=?, posted_at=?, last_error=NULL WHERE date=?;",
        (int(channel_id), now, str(date_ymd)),
    )


def bump_daily_rule_attempt(date_ymd: str, *, error: str) -> None:
    execute(
        """
        UPDATE daily_rules
        SET attempts = COALESCE(attempts, 0) + 1,
            last_error = ?
        WHERE date=?;
        """,
        (str(error)[:800], str(date_ymd)),
    )


def add_rule_suggestion(user_id: int, guild_id: Optional[int], content: str) -> None:
    now = int(time.time())
    execute(
        """
        INSERT INTO rule_suggestions(user_id, guild_id, content, created_at)
        VALUES(?, ?, ?, ?);
        """,
        (int(user_id), int(guild_id) if guild_id is not None else None, str(content)[:600], now),
    )


def save_rule_suggestion(user_id: int, guild_id: Optional[int], content: str) -> None:
    """Backward-compatible alias.

    Phase3 초기 구현에서 함수명이 add_rule_suggestion으로 들어갔는데,
    Cog(rule_maker)에서는 save_rule_suggestion을 import하도록 작성되어 있었어.
    기존 배포본/코그 모두 깨지지 않게 alias로 유지한다.
    """

    add_rule_suggestion(user_id=user_id, guild_id=guild_id, content=content)


# =========================
# Phase4: Survival cooking (상상 급식표)
# =========================


def get_daily_meal(date_ymd: str) -> Optional[Dict[str, Any]]:
    return fetchone(
        """
        SELECT date, meal_text, created_at, last_requested_at
        FROM daily_meals
        WHERE date=?;
        """,
        (str(date_ymd),),
    )


def upsert_daily_meal(date_ymd: str, meal_text: str) -> None:
    now = int(time.time())
    execute(
        """
        INSERT INTO daily_meals(date, meal_text, created_at, last_requested_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
          meal_text=excluded.meal_text,
          last_requested_at=excluded.last_requested_at;
        """,
        (str(date_ymd), str(meal_text)[:1800], now, now),
    )


def get_recent_rule_suggestions(limit: int = 5) -> list[Dict[str, Any]]:
    # Avoid importing fetchall at top-level to keep API tiny.
    from yume_db import fetchall

    lim = int(limit)
    if lim <= 0:
        lim = 5
    if lim > 20:
        lim = 20
    return fetchall(
        """
        SELECT id, user_id, guild_id, content, created_at
        FROM rule_suggestions
        ORDER BY id DESC
        LIMIT ?;
        """,
        (lim,),
    )
