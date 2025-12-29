"""yume_store.py

Phase0 foundation: small data-access helpers on top of yume_db.

We keep the API tiny and explicit, so later features (weather, time-capsules,
stamps, etc.) can build on it without sprinkling SQL everywhere.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from yume_db import execute, fetchone


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
