"""yume_store.py

Small, explicit data-access helpers on top of yume_db.

Phases
- Phase0: user_settings + world_state + bot_config
- Phase3: daily_rules + rule_suggestions
- Phase4: daily_meals
- Phase5: stamps reward/event logs

We keep this module intentionally tiny and boring:
- no ORM
- no globals
- functions are simple and grep-friendly
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, Optional

from yume_db import execute, fetchone, fetchall, transaction


def now_ts() -> int:
    """Return current unix timestamp (seconds)."""

    return int(time.time())


def get_con():
    """Write transaction context (alias).

    Some features need multi-step updates; we keep them atomic by reusing
    yume_db.transaction().
    """

    return transaction()


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


# =========================
# User settings
# =========================


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
    """Return user settings (and stamp fields) with safe defaults."""

    ensure_user_settings(user_id)

    row = fetchone(
        """
        SELECT
          user_id,
          dm_opt_in,
          noise_opt_in,
          stamps,
          last_stamp_at,
          COALESCE(stamps_opt_in, 1) AS stamps_opt_in,
          COALESCE(stamps_rewarded, 0) AS stamps_rewarded,
          COALESCE(stamp_title, '') AS stamp_title,
          COALESCE(last_reward_at, 0) AS last_reward_at,
          created_at,
          updated_at
        FROM user_settings
        WHERE user_id=?;
        """,
        (int(user_id),),
    )

    return row or {
        "user_id": int(user_id),
        "dm_opt_in": 1,
        "noise_opt_in": 1,
        "stamps": 0,
        "last_stamp_at": 0,
        "stamps_opt_in": 1,
        "stamps_rewarded": 0,
        "stamp_title": "",
        "last_reward_at": 0,
        "created_at": 0,
        "updated_at": 0,
    }


def set_user_opt_in(
    user_id: int,
    *,
    dm_opt_in: Optional[bool] = None,
    noise_opt_in: Optional[bool] = None,
    stamps_opt_in: Optional[bool] = None,
) -> None:
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

    if stamps_opt_in is not None:
        execute(
            "UPDATE user_settings SET stamps_opt_in=?, updated_at=? WHERE user_id=?;",
            (1 if stamps_opt_in else 0, now, int(user_id)),
        )


def set_stamp_state(
    user_id: int,
    *,
    stamps: Optional[int] = None,
    last_stamp_at: Optional[int] = None,
    stamps_rewarded: Optional[int] = None,
    stamp_title: Optional[str] = None,
    last_reward_at: Optional[int] = None,
) -> None:
    """Low-level setter for stamp fields."""

    ensure_user_settings(user_id)
    now = int(time.time())

    if stamps is not None:
        execute(
            "UPDATE user_settings SET stamps=?, updated_at=? WHERE user_id=?;",
            (int(stamps), now, int(user_id)),
        )

    if last_stamp_at is not None:
        execute(
            "UPDATE user_settings SET last_stamp_at=?, updated_at=? WHERE user_id=?;",
            (int(last_stamp_at), now, int(user_id)),
        )

    if stamps_rewarded is not None:
        execute(
            "UPDATE user_settings SET stamps_rewarded=?, updated_at=? WHERE user_id=?;",
            (int(stamps_rewarded), now, int(user_id)),
        )

    if stamp_title is not None:
        execute(
            "UPDATE user_settings SET stamp_title=?, updated_at=? WHERE user_id=?;",
            (str(stamp_title)[:80], now, int(user_id)),
        )

    if last_reward_at is not None:
        execute(
            "UPDATE user_settings SET last_reward_at=?, updated_at=? WHERE user_id=?;",
            (int(last_reward_at), now, int(user_id)),
        )


def add_stamp_event(
    *,
    user_id: int,
    guild_id: Optional[int],
    reason: str | None,
    delta: int,
    stamps_after: int,
    created_at: Optional[int] = None,
) -> None:
    now = int(created_at or time.time())
    execute(
        """
        INSERT INTO stamp_events(user_id, guild_id, reason, delta, stamps_after, created_at)
        VALUES(?, ?, ?, ?, ?, ?);
        """,
        (int(user_id), int(guild_id) if guild_id is not None else None, (str(reason)[:40] if reason else None), int(delta), int(stamps_after), int(now)),
    )


def add_stamp_reward(
    *,
    user_id: int,
    guild_id: Optional[int],
    milestone: int,
    title: str,
    letter: str,
    created_at: Optional[int] = None,
) -> None:
    now = int(created_at or time.time())
    execute(
        """
        INSERT INTO stamp_rewards(user_id, guild_id, milestone, title, letter, created_at)
        VALUES(?, ?, ?, ?, ?, ?);
        """,
        (int(user_id), int(guild_id) if guild_id is not None else None, int(milestone), str(title)[:80], str(letter)[:3000], int(now)),
    )


# =========================
# World state (virtual weather)
# =========================


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


def ensure_world_weather_rotated(*, now_ts: Optional[int] = None) -> Dict[str, Any]:
    """Return current world_state, rotating weather if it's past next_change_at.

    Design:
    - Rotation can be triggered lazily when features query the world state.
    - (Phase2) A background task may also call this to keep the world updated.

    Weather weights (Phase1):
    - clear: 55%
    - cloudy: 30%
    - sandstorm: 15%

    Returns the (possibly updated) state dict.
    """

    state = get_world_state()
    now = int(now_ts or time.time())

    weather = str(state.get("weather") or "clear")
    next_at = int(state.get("weather_next_change_at") or 0)
    changed_at = int(state.get("weather_changed_at") or 0)

    # If next_at is missing (older DB), initialize it.
    if next_at <= 0:
        next_at = now + 6 * 3600
        set_world_weather(weather, changed_at=changed_at or now, next_change_at=next_at)
        state = get_world_state()
        return state

    if now < next_at:
        return state

    def _roll_next_change_at(now_ts: int, w: str) -> int:
        """Phase2: weather duration is random (per weather).

        - clear: 4~8 hours
        - cloudy: 3~6 hours
        - sandstorm: 45~120 minutes
        """

        if w == "sandstorm":
            return int(now_ts + random.randint(45 * 60, 120 * 60))
        if w == "cloudy":
            return int(now_ts + random.randint(3 * 3600, 6 * 3600))
        return int(now_ts + random.randint(4 * 3600, 8 * 3600))

    # Roll a new weather.
    # - Avoid repeating the same weather back-to-back when possible.
    # - Avoid repeating sandstorm too often.
    def _roll(prev: str) -> str:
        if prev == "sandstorm":
            choices = ["clear", "cloudy", "sandstorm"]
            weights = [0.70, 0.28, 0.02]
        else:
            choices = ["clear", "cloudy", "sandstorm"]
            weights = [0.55, 0.30, 0.15]

        # Try a few times to avoid exact repeat.
        for _ in range(3):
            w = random.choices(choices, weights=weights, k=1)[0]
            if w != prev:
                return w
        return random.choices(choices, weights=weights, k=1)[0]

    new_weather = _roll(weather)
    new_next = _roll_next_change_at(now, new_weather)
    set_world_weather(new_weather, changed_at=now, next_change_at=new_next)
    return get_world_state()


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



# =========================
# Phase5: Stamps (참 잘했어요! 도장판)
# =========================


def set_stamps_opt_in(user_id: int, enabled: bool) -> None:
    set_user_opt_in(user_id, stamps_opt_in=bool(enabled))


def update_stamp_state(
    user_id: int,
    *,
    stamps: Optional[int] = None,
    last_stamp_at: Optional[int] = None,
    stamps_rewarded: Optional[int] = None,
    stamp_title: Optional[str] = None,
    last_reward_at: Optional[int] = None,
) -> None:
    """Update stamp-related fields. Any None values are left untouched."""

    ensure_user_settings(user_id)
    now = int(time.time())

    fields = []
    params = []
    if stamps is not None:
        fields.append("stamps=?")
        params.append(int(stamps))
    if last_stamp_at is not None:
        fields.append("last_stamp_at=?")
        params.append(int(last_stamp_at))
    if stamps_rewarded is not None:
        fields.append("stamps_rewarded=?")
        params.append(int(stamps_rewarded))
    if stamp_title is not None:
        fields.append("stamp_title=?")
        params.append(str(stamp_title)[:80])
    if last_reward_at is not None:
        fields.append("last_reward_at=?")
        params.append(int(last_reward_at))

    if not fields:
        return

    fields.append("updated_at=?")
    params.append(now)

    params.append(int(user_id))
    execute(
        f"UPDATE user_settings SET {', '.join(fields)} WHERE user_id=?;",
        tuple(params),
    )


def add_stamp_event(
    user_id: int,
    *,
    guild_id: Optional[int],
    reason: str,
    delta: int,
    stamps_after: int,
) -> None:
    now = int(time.time())
    execute(
        """
        INSERT INTO stamp_events(user_id, guild_id, reason, delta, stamps_after, created_at)
        VALUES(?, ?, ?, ?, ?, ?);
        """,
        (
            int(user_id),
            int(guild_id) if guild_id is not None else None,
            str(reason)[:80],
            int(delta),
            int(stamps_after),
            now,
        ),
    )


def add_stamp_reward(
    user_id: int,
    *,
    guild_id: Optional[int],
    milestone: int,
    title: str,
    letter_text: str,
) -> None:
    now = int(time.time())
    execute(
        """
        INSERT INTO stamp_rewards(user_id, guild_id, milestone, title, letter_text, created_at)
        VALUES(?, ?, ?, ?, ?, ?);
        """,
        (
            int(user_id),
            int(guild_id) if guild_id is not None else None,
            int(milestone),
            str(title)[:80],
            str(letter_text)[:2000],
            now,
        ),
    )


def get_latest_stamp_reward(user_id: int) -> Optional[Dict[str, Any]]:
    return fetchone(
        """
        SELECT id, user_id, guild_id, milestone, title, letter_text, created_at
        FROM stamp_rewards
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 1;
        """,
        (int(user_id),),
    )


def get_top_stamps(limit: int = 10) -> list[Dict[str, Any]]:
    """Return top users by stamps.

    Used by Phase6-1 web sync to show a tiny leaderboard.
    """

    lim = int(limit)
    if lim <= 0:
        lim = 10
    if lim > 50:
        lim = 50

    rows = fetchall(
        """
        SELECT user_id,
               COALESCE(stamps, 0) AS stamps,
               COALESCE(stamp_title, '') AS stamp_title,
               COALESCE(updated_at, 0) AS updated_at
        FROM user_settings
        ORDER BY COALESCE(stamps, 0) DESC, updated_at DESC
        LIMIT ?;
        """,
        (lim,),
    )
    return rows or []

# -----------------------------------------------------------------------------
# Abydos Mini-Game Economy (Phase6-2)
# -----------------------------------------------------------------------------

from decimal import Decimal, ROUND_CEILING
import datetime


KST = datetime.timezone(datetime.timedelta(hours=9))


ABY_DEFAULT_DEBT = 900_000_000  # 9억 크레딧
# NOTE: "0.35"는 0.35% (퍼센트) 의미로 사용한다. (즉, 0.0035)
#       9억 * 0.35% = 3,150,000/day 수준이라, "갚기 어려운" 서사에 맞는다.
ABY_DEFAULT_INTEREST_RATE = 0.0035  # 일일 이자율 (0.35%)


def _date_from_ymd(ymd: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(ymd)
    except Exception:
        # fallback: treat as today (caller should pass valid ymd)
        return datetime.date.today()


def _ymd_iter_exclusive(start_ymd: str, end_ymd: str):
    """Yield dates for (start_ymd, end_ymd] in ISO format."""
    start = _date_from_ymd(start_ymd)
    end = _date_from_ymd(end_ymd)
    d = start
    while d < end:
        d = d + datetime.timedelta(days=1)
        yield d.isoformat()


def ensure_user_economy(user_id: int) -> None:
    uid = int(user_id)
    now = now_ts()
    with transaction() as con:
        con.execute(
            """
            INSERT INTO aby_user_economy(user_id, credits, water, last_explore_ymd, created_at, updated_at)
            VALUES(?, 0, 0, '', ?, ?)
            ON CONFLICT(user_id) DO NOTHING;
            """,
            (uid, now, now),
        )


def get_user_economy(user_id: int) -> Dict[str, Any]:
    ensure_user_economy(user_id)
    row = fetchone(
        """
        SELECT user_id, credits, water, last_explore_ymd, created_at, updated_at
        FROM aby_user_economy
        WHERE user_id=?;
        """,
        (int(user_id),),
    )
    if not row:
        return {
            "user_id": int(user_id),
            "credits": 0,
            "water": 0,
            "last_explore_ymd": "",
            "created_at": now_ts(),
            "updated_at": now_ts(),
        }
    return row


def claim_daily_explore(user_id: int, date_ymd: str, delta_credits: int, delta_water: int = 0) -> Optional[Dict[str, Any]]:
    """Claim one daily explore reward (KST date).

    Returns updated economy row if successful, else None if already claimed today.
    """

    uid = int(user_id)
    # Phase6-2 Phase3: 탐사 중 "사고/조우"로 크레딧이 줄어드는 이벤트를 허용한다.
    # SQL에서 MAX(0, credits + delta)로 바닥을 막고, 로그에는 음수 델타도 그대로 남긴다.
    d_credits = int(delta_credits)
    d_water = int(delta_water)
    if d_water < 0:
        d_water = 0

    ensure_user_economy(uid)
    now = now_ts()

    with transaction() as con:
        row = con.execute(
            "SELECT credits, water, last_explore_ymd FROM aby_user_economy WHERE user_id=?;",
            (uid,),
        ).fetchone()
        last_ymd = (row[2] or "") if row else ""
        if last_ymd == date_ymd:
            return None

        con.execute(
            """
            UPDATE aby_user_economy
            SET credits = MAX(0, credits + ?),
                water   = MAX(0, water + ?),
                last_explore_ymd = ?,
                updated_at = ?
            WHERE user_id=?;
            """,
            (d_credits, d_water, date_ymd, now, uid),
        )

        con.execute(
            """
            INSERT INTO aby_economy_log(guild_id, user_id, kind, delta_credits, delta_water, delta_debt, memo, created_at)
            VALUES(NULL, ?, 'explore', ?, ?, 0, ?, ?);
            """,
            (uid, d_credits, d_water, f"{date_ymd}", now),
        )

    return get_user_economy(uid)


# ------------------------------
# Abydos Inventory / Buffs (Phase6-2 Phase3)
# ------------------------------


def get_user_inventory(user_id: int) -> Dict[str, int]:
    """Return {item_key: qty} for a user."""

    uid = int(user_id)
    rows = fetchall(
        """
        SELECT item_key, qty
        FROM aby_inventory
        WHERE user_id=? AND qty > 0
        ORDER BY item_key ASC;
        """,
        (uid,),
    )
    inv: Dict[str, int] = {}
    for r in rows:
        k = str(r.get("item_key") or "").strip()
        if not k:
            continue
        inv[k] = int(r.get("qty") or 0)
    return inv


def add_user_item(user_id: int, item_key: str, qty: int = 1) -> None:
    uid = int(user_id)
    key = str(item_key).strip().lower()
    q = int(qty)
    if not key or q <= 0:
        return

    now = now_ts()
    with transaction() as con:
        con.execute(
            """
            INSERT INTO aby_inventory(user_id, item_key, qty, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id, item_key) DO UPDATE SET
              qty = MAX(0, qty + excluded.qty),
              updated_at = excluded.updated_at;
            """,
            (uid, key, q, now),
        )


def consume_user_item(user_id: int, item_key: str, qty: int = 1) -> bool:
    """Consume items, returning True on success."""

    uid = int(user_id)
    key = str(item_key).strip().lower()
    q = int(qty)
    if not key or q <= 0:
        return False

    now = now_ts()
    with transaction() as con:
        row = con.execute(
            "SELECT qty FROM aby_inventory WHERE user_id=? AND item_key=?;",
            (uid, key),
        ).fetchone()
        cur = int(row[0]) if row else 0
        if cur < q:
            return False
        con.execute(
            "UPDATE aby_inventory SET qty = qty - ?, updated_at=? WHERE user_id=? AND item_key=?;",
            (q, now, uid, key),
        )
    return True


# ------------------------------
# Phase6-2 Phase4: Craft / Sell helpers (atomic)
# ------------------------------


def get_user_item_qty(user_id: int, item_key: str) -> int:
    """Return current qty for (user_id, item_key)."""

    uid = int(user_id)
    key = str(item_key).strip().lower()
    if not key:
        return 0
    row = fetchone(
        "SELECT qty FROM aby_inventory WHERE user_id=? AND item_key=?;",
        (uid, key),
    )
    return int((row or {}).get('qty') or 0)


def craft_user_items(
    user_id: int,
    *,
    cost_credits: int,
    req_items: dict[str, int],
    out_items: dict[str, int],
    memo: str = '',
) -> dict:
    """Atomic craft: spend credits + consume req_items + grant out_items.

    Returns:
      {ok: bool, reason?: str, credits_after?: int, missing?: list[(key, need, have)]}

    Notes:
    - This never partially consumes items.
    - It logs to aby_economy_log(kind='craft') with delta_credits negative.
    """

    uid = int(user_id)
    cost = max(0, int(cost_credits))
    ensure_user_economy(uid)

    # normalize dicts
    req = {str(k).strip().lower(): int(v) for k, v in (req_items or {}).items() if str(k).strip() and int(v) > 0}
    out = {str(k).strip().lower(): int(v) for k, v in (out_items or {}).items() if str(k).strip() and int(v) > 0}

    now = now_ts()

    with transaction() as con:
        u = con.execute("SELECT credits FROM aby_user_economy WHERE user_id=?;", (uid,)).fetchone()
        credits = int(u[0]) if u else 0
        if credits < cost:
            return {"ok": False, "reason": "credits", "credits": credits, "need": cost}

        missing = []
        for k, need in req.items():
            row = con.execute(
                "SELECT qty FROM aby_inventory WHERE user_id=? AND item_key=?;",
                (uid, k),
            ).fetchone()
            have = int(row[0]) if row else 0
            if have < need:
                missing.append((k, int(need), int(have)))

        if missing:
            return {"ok": False, "reason": "items", "missing": missing}

        if cost > 0:
            con.execute(
                "UPDATE aby_user_economy SET credits = MAX(0, credits - ?), updated_at=? WHERE user_id=?;",
                (cost, now, uid),
            )

        for k, need in req.items():
            con.execute(
                "UPDATE aby_inventory SET qty = MAX(0, qty - ?), updated_at=? WHERE user_id=? AND item_key=?;",
                (int(need), now, uid, k),
            )

        for k, q in out.items():
            con.execute(
                """
                INSERT INTO aby_inventory(user_id, item_key, qty, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(user_id, item_key) DO UPDATE SET
                  qty = MAX(0, qty + excluded.qty),
                  updated_at = excluded.updated_at;
                """,
                (uid, k, int(q), now),
            )

        con.execute(
            """
            INSERT INTO aby_economy_log(guild_id, user_id, kind, delta_credits, delta_water, delta_debt, memo, created_at)
            VALUES(NULL, ?, 'craft', ?, 0, 0, ?, ?);
            """,
            (uid, -cost, (str(memo)[:200] if memo else None), now),
        )

    # re-read
    after = get_user_economy(uid)
    return {"ok": True, "credits_after": int(after.get('credits') or 0)}


def sell_user_item(
    user_id: int,
    *,
    item_key: str,
    qty: int,
    unit_price: int,
    memo: str = '',
) -> dict:
    """Atomic sell: consume item qty and add credits.

    qty:
      - positive integer
      - -1 means 'all'

    Returns:
      {ok: bool, reason?: str, sold?: int, earned?: int, credits_after?: int, have?: int}
    """

    uid = int(user_id)
    key = str(item_key).strip().lower()
    if not key:
        return {"ok": False, "reason": "item"}

    price = max(0, int(unit_price))
    q_in = int(qty)

    ensure_user_economy(uid)
    now = now_ts()

    with transaction() as con:
        row = con.execute(
            "SELECT qty FROM aby_inventory WHERE user_id=? AND item_key=?;",
            (uid, key),
        ).fetchone()
        have = int(row[0]) if row else 0
        if have <= 0:
            return {"ok": False, "reason": "no_item", "have": have}

        sell_q = have if q_in == -1 else max(0, q_in)
        if sell_q <= 0:
            return {"ok": False, "reason": "qty"}
        if sell_q > have:
            sell_q = have

        earned = int(sell_q * price)

        con.execute(
            "UPDATE aby_inventory SET qty = MAX(0, qty - ?), updated_at=? WHERE user_id=? AND item_key=?;",
            (sell_q, now, uid, key),
        )

        con.execute(
            "UPDATE aby_user_economy SET credits = MAX(0, credits + ?), updated_at=? WHERE user_id=?;",
            (earned, now, uid),
        )

        con.execute(
            """
            INSERT INTO aby_economy_log(guild_id, user_id, kind, delta_credits, delta_water, delta_debt, memo, created_at)
            VALUES(NULL, ?, 'sell', ?, 0, 0, ?, ?);
            """,
            (uid, earned, (str(memo)[:200] if memo else None), now),
        )

    after = get_user_economy(uid)
    return {
        "ok": True,
        "sold": sell_q,
        "earned": earned,
        "credits_after": int(after.get('credits') or 0),
    }

def get_user_buff(user_id: int) -> Dict[str, Any]:
    uid = int(user_id)
    row = fetchone(
        """
        SELECT user_id, buff_key, stacks, expires_at, updated_at
        FROM aby_buffs
        WHERE user_id=?;
        """,
        (uid,),
    )
    if not row:
        return {"user_id": uid, "buff_key": "", "stacks": 0, "expires_at": 0, "updated_at": 0}
    return row


def set_user_buff(user_id: int, *, buff_key: str, stacks: int, expires_at: int) -> None:
    uid = int(user_id)
    key = str(buff_key).strip().lower()
    now = now_ts()
    execute(
        """
        INSERT INTO aby_buffs(user_id, buff_key, stacks, expires_at, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
          buff_key=excluded.buff_key,
          stacks=excluded.stacks,
          expires_at=excluded.expires_at,
          updated_at=excluded.updated_at;
        """,
        (uid, key, int(stacks), int(expires_at), now),
    )


def clear_user_buff(user_id: int) -> None:
    uid = int(user_id)
    execute(
        "UPDATE aby_buffs SET buff_key='', stacks=0, expires_at=0, updated_at=? WHERE user_id=?;",
        (now_ts(), uid),
    )


def ensure_user_buff_valid(user_id: int, *, now: Optional[int] = None) -> Dict[str, Any]:
    """Return buff; if expired, clear it first."""

    ts = int(now or now_ts())
    b = get_user_buff(user_id)
    exp = int(b.get("expires_at") or 0)
    key = str(b.get("buff_key") or "").strip()
    stacks = int(b.get("stacks") or 0)
    if not key or stacks <= 0:
        return {"user_id": int(user_id), "buff_key": "", "stacks": 0, "expires_at": 0, "updated_at": int(b.get("updated_at") or 0)}
    if exp > 0 and ts >= exp:
        clear_user_buff(user_id)
        return {"user_id": int(user_id), "buff_key": "", "stacks": 0, "expires_at": 0, "updated_at": ts}
    return b


def consume_user_buff_stack(user_id: int, *, now: Optional[int] = None) -> None:
    """Decrease buff stacks by 1 (and clear if exhausted)."""

    ts = int(now or now_ts())
    b = get_user_buff(user_id)
    key = str(b.get("buff_key") or "").strip().lower()
    stacks = int(b.get("stacks") or 0)
    exp = int(b.get("expires_at") or 0)
    if not key or stacks <= 0:
        return
    stacks -= 1
    if stacks <= 0:
        clear_user_buff(user_id)
        return
    set_user_buff(user_id, buff_key=key, stacks=stacks, expires_at=exp)


def ensure_guild_debt(guild_id: int, *, initial_debt: int = ABY_DEFAULT_DEBT, interest_rate: float = ABY_DEFAULT_INTEREST_RATE, today_ymd: Optional[str] = None) -> None:
    gid = int(guild_id)
    now = now_ts()
    if today_ymd is None:
        # Use UTC date as a fallback; callers should pass KST date.
        today_ymd = datetime.date.today().isoformat()

    with transaction() as con:
        con.execute(
            """
            INSERT INTO aby_guild_debt(guild_id, debt, interest_rate, last_interest_ymd, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO NOTHING;
            """,
            (gid, int(initial_debt), float(interest_rate), str(today_ymd), now, now),
        )


def _apply_interest_once(debt: int, rate: float) -> int:
    d = Decimal(int(debt))
    r = Decimal(str(rate))
    mult = Decimal("1") + r
    new_val = (d * mult).to_integral_value(rounding=ROUND_CEILING)
    # Never go below 0
    return int(new_val) if new_val > 0 else 0


def apply_guild_interest_upto_today(guild_id: int, today_ymd: str) -> Dict[str, Any]:
    """Apply missed daily interest up to today_ymd (inclusive).

    Interprets last_interest_ymd as "already applied for that date".
    If last_interest_ymd == today_ymd, does nothing.
    """

    gid = int(guild_id)
    ensure_guild_debt(gid, today_ymd=today_ymd)

    with transaction() as con:
        row = con.execute(
            "SELECT debt, interest_rate, last_interest_ymd FROM aby_guild_debt WHERE guild_id=?;",
            (gid,),
        ).fetchone()
        if not row:
            return {
                "guild_id": gid,
                "debt": ABY_DEFAULT_DEBT,
                "interest_rate": ABY_DEFAULT_INTEREST_RATE,
                "last_interest_ymd": today_ymd,
            }

        debt = int(row[0])
        rate = float(row[1])

        # Backward-compat: older builds accidentally stored "0.35" as 35%.
        # If it looks like a percent value, convert to a fraction.
        if rate > 0.05:  # >5%/day is not our intended scale
            rate = rate / 100.0
            con.execute(
                "UPDATE aby_guild_debt SET interest_rate=?, updated_at=? WHERE guild_id=?;",
                (rate, now_ts(), gid),
            )
        last_ymd = str(row[2] or "")
        if not last_ymd:
            last_ymd = today_ymd

        if last_ymd == today_ymd:
            return {
                "guild_id": gid,
                "debt": debt,
                "interest_rate": rate,
                "last_interest_ymd": last_ymd,
            }

        now = now_ts()
        old_debt = debt
        applied_days = 0
        for ymd in _ymd_iter_exclusive(last_ymd, today_ymd):
            new_debt = _apply_interest_once(debt, rate)
            delta = new_debt - debt
            debt = new_debt
            applied_days += 1

            con.execute(
                """
                INSERT INTO aby_economy_log(guild_id, user_id, kind, delta_credits, delta_water, delta_debt, memo, created_at)
                VALUES(?, NULL, 'interest', 0, 0, ?, ?, ?);
                """,
                (gid, int(delta), ymd, now),
            )

        con.execute(
            """
            UPDATE aby_guild_debt
            SET debt=?, last_interest_ymd=?, updated_at=?
            WHERE guild_id=?;
            """,
            (debt, today_ymd, now, gid),
        )

    return {
        "guild_id": gid,
        "debt": debt,
        "interest_rate": rate,
        "last_interest_ymd": today_ymd,
        "applied_days": applied_days,
        "old_debt": old_debt,
    }


def get_guild_debt(guild_id: int, today_ymd: Optional[str] = None) -> Dict[str, Any]:
    gid = int(guild_id)
    if today_ymd is not None:
        apply_guild_interest_upto_today(gid, today_ymd)

    row = fetchone(
        """
        SELECT guild_id, debt, interest_rate, last_interest_ymd, created_at, updated_at
        FROM aby_guild_debt
        WHERE guild_id=?;
        """,
        (gid,),
    )
    if not row:
        # fallback
        return {
            "guild_id": gid,
            "debt": ABY_DEFAULT_DEBT,
            "interest_rate": ABY_DEFAULT_INTEREST_RATE,
            "last_interest_ymd": today_ymd or "",
        }
    return row


def repay_guild_debt(guild_id: int, user_id: int, amount: int, today_ymd: str) -> Dict[str, Any]:
    """Repay part of the guild debt from user's credits.

    Returns a result dict with status.
    """

    gid = int(guild_id)
    uid = int(user_id)
    amt = int(amount)
    if amt <= 0:
        return {"ok": False, "reason": "amount"}

    # Apply interest first so the story feels relentless.
    apply_guild_interest_upto_today(gid, today_ymd)
    ensure_user_economy(uid)

    now = now_ts()
    with transaction() as con:
        u = con.execute(
            "SELECT credits FROM aby_user_economy WHERE user_id=?;",
            (uid,),
        ).fetchone()
        credits = int(u[0]) if u else 0
        if credits <= 0:
            return {"ok": False, "reason": "no_credits", "credits": credits}

        pay = min(amt, credits)

        g = con.execute(
            "SELECT debt, interest_rate, last_interest_ymd FROM aby_guild_debt WHERE guild_id=?;",
            (gid,),
        ).fetchone()
        if not g:
            # ensure then retry
            ensure_guild_debt(gid, today_ymd=today_ymd)
            g = con.execute(
                "SELECT debt, interest_rate, last_interest_ymd FROM aby_guild_debt WHERE guild_id=?;",
                (gid,),
            ).fetchone()
        debt = int(g[0]) if g else ABY_DEFAULT_DEBT

        new_debt = max(0, debt - pay)

        con.execute(
            "UPDATE aby_user_economy SET credits = MAX(0, credits - ?), updated_at=? WHERE user_id=?;",
            (pay, now, uid),
        )
        con.execute(
            "UPDATE aby_guild_debt SET debt=?, updated_at=? WHERE guild_id=?;",
            (new_debt, now, gid),
        )

        con.execute(
            """
            INSERT INTO aby_economy_log(guild_id, user_id, kind, delta_credits, delta_water, delta_debt, memo, created_at)
            VALUES(?, ?, 'repay', ?, 0, ?, ?, ?);
            """,
            (gid, uid, -pay, -(debt - new_debt), f"{today_ymd}", now),
        )

    return {
        "ok": True,
        "paid": pay,
        "old_debt": debt,
        "new_debt": new_debt,
        "credits_after": max(0, credits - pay),
    }


# ------------------------------
# Phase6-2 Phase6: Debt pressure helpers
# ------------------------------


def list_aby_debt_guild_ids() -> list[int]:
    """Return guild_ids that have a debt row.

    We only auto-apply interest for guilds that already opted into the
    Abydos debt system (i.e., a row exists).
    """

    rows = fetchall(
        """
        SELECT guild_id
        FROM aby_guild_debt
        ORDER BY guild_id ASC;
        """
    )
    out: list[int] = []
    for r in rows or []:
        try:
            out.append(int(r.get("guild_id") or 0))
        except Exception:
            continue
    return [x for x in out if x > 0]


def get_interest_delta_for_day(guild_id: int, ymd: str) -> int:
    """Sum of interest deltas for a given day (ymd)."""

    row = fetchone(
        """
        SELECT COALESCE(SUM(delta_debt), 0) AS total
        FROM aby_economy_log
        WHERE guild_id=? AND kind='interest' AND memo=?;
        """,
        (int(guild_id), str(ymd)),
    )
    return int((row or {}).get("total") or 0)


def get_repay_total_for_day_guild(guild_id: int, ymd: str) -> int:
    """Total repaid credits for a given day (across all users)."""

    row = fetchone(
        """
        SELECT COALESCE(SUM(-delta_credits), 0) AS total
        FROM aby_economy_log
        WHERE guild_id=? AND kind='repay' AND memo=?;
        """,
        (int(guild_id), str(ymd)),
    )
    return int((row or {}).get("total") or 0)


def get_interest_deltas_recent(guild_id: int, limit: int = 7) -> list[Dict[str, Any]]:
    """Recent per-day interest deltas (ymd, delta)."""

    lim = int(limit)
    if lim <= 0:
        lim = 7
    if lim > 30:
        lim = 30

    rows = fetchall(
        """
        SELECT memo AS ymd,
               COALESCE(SUM(delta_debt), 0) AS delta_debt,
               MAX(created_at) AS created_at
        FROM aby_economy_log
        WHERE guild_id=? AND kind='interest' AND memo != ''
        GROUP BY memo
        ORDER BY memo DESC
        LIMIT ?;
        """,
        (int(guild_id), lim),
    )
    return rows or []


def get_repay_totals_recent(guild_id: int, limit: int = 7) -> list[Dict[str, Any]]:
    """Recent per-day repay totals (ymd, total)."""

    lim = int(limit)
    if lim <= 0:
        lim = 7
    if lim > 30:
        lim = 30

    rows = fetchall(
        """
        SELECT memo AS ymd,
               COALESCE(SUM(-delta_credits), 0) AS total_repaid,
               MAX(created_at) AS created_at
        FROM aby_economy_log
        WHERE guild_id=? AND kind='repay' AND memo != ''
        GROUP BY memo
        ORDER BY memo DESC
        LIMIT ?;
        """,
        (int(guild_id), lim),
    )
    return rows or []


def debt_pressure_stage(debt: int, *, initial: int = ABY_DEFAULT_DEBT) -> Dict[str, Any]:
    """Compute a simple narrative "pressure" stage from the current debt."""

    d = max(0, int(debt))
    base = max(1, int(initial))
    ratio = d / float(base)

    if ratio < 1.02:
        return {"stage": "버티는 중", "emoji": "🙂", "ratio": ratio}
    if ratio < 1.10:
        return {"stage": "긴장", "emoji": "😬", "ratio": ratio}
    if ratio < 1.25:
        return {"stage": "위기", "emoji": "😵", "ratio": ratio}
    if ratio < 1.50:
        return {"stage": "절망", "emoji": "💀", "ratio": ratio}
    return {"stage": "종말", "emoji": "☠️", "ratio": ratio}

# ------------------------------
# Phase6-2 Phase5: Explore meta + Quest board + Weekly points
# ------------------------------

import hashlib
import re as _re

ABY_DAILY_QUEST_COUNT = 3
ABY_WEEKLY_QUEST_COUNT = 3


def week_key_from_ymd(ymd: str) -> str:
    """Return ISO week key like '2025W53' for a given KST ymd."""

    d = _date_from_ymd(str(ymd))
    iso = d.isocalendar()
    try:
        year = int(getattr(iso, "year"))
        week = int(getattr(iso, "week"))
    except Exception:
        year = int(iso[0])
        week = int(iso[1])
    return f"{year}W{week:02d}"


def _parse_week_key(week_key: str) -> tuple[int, int] | None:
    s = str(week_key or "").strip().upper()
    m = _re.fullmatch(r"(\d{4})W(\d{1,2})", s)
    if not m:
        return None
    y = int(m.group(1))
    w = int(m.group(2))
    if w < 1 or w > 53:
        return None
    return y, w


def week_ymds_from_week_key(week_key: str) -> list[str]:
    """Return 7 ymd strings (Mon..Sun) for an ISO week key."""

    parsed = _parse_week_key(week_key)
    if not parsed:
        return []
    y, w = parsed
    try:
        start = datetime.date.fromisocalendar(y, w, 1)  # Monday
    except Exception:
        # best-effort fallback
        start = datetime.date.today()
    return [(start + datetime.timedelta(days=i)).isoformat() for i in range(7)]


def upsert_explore_meta(
    user_id: int,
    date_ymd: str,
    *,
    weather: str,
    success: bool,
    credits_delta: int,
    water_delta: int,
) -> None:
    """Record a user's daily explore meta (for quest validation).

    Idempotent per (user_id, date_ymd).
    """

    uid = int(user_id)
    ymd = str(date_ymd)
    w = str(weather or "").strip().lower()[:20]
    s = 1 if success else 0
    cd = int(credits_delta)
    wd = int(water_delta)
    now = now_ts()

    execute(
        """
        INSERT INTO aby_explore_meta(user_id, date_ymd, weather, success, credits_delta, water_delta, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, date_ymd) DO UPDATE SET
          weather = excluded.weather,
          success = excluded.success,
          credits_delta = excluded.credits_delta,
          water_delta = excluded.water_delta,
          created_at = excluded.created_at;
        """,
        (uid, ymd, w, s, cd, wd, now),
    )


def get_explore_meta(user_id: int, date_ymd: str) -> Optional[Dict[str, Any]]:
    return fetchone(
        """
        SELECT user_id, date_ymd, weather, success, credits_delta, water_delta, created_at
        FROM aby_explore_meta
        WHERE user_id=? AND date_ymd=?;
        """,
        (int(user_id), str(date_ymd)),
    )


def _stable_seed_int(seed: str) -> int:
    h = hashlib.md5(seed.encode("utf-8")).hexdigest()[:8]
    return int(h, 16)


def _aby_rng(guild_id: int, *, scope: str, board_key: str) -> random.Random:
    seed = f"aby-quest|{int(guild_id)}|{scope}|{board_key}"
    return random.Random(_stable_seed_int(seed))


_ABY_MATS_BASIC = ["scrap", "cloth"]
_ABY_MATS_ADV = ["filter", "battery", "circuit"]


def _gen_daily_quests(guild_id: int, today_ymd: str, *, pressure_ratio: float = 1.0) -> list[dict]:
    r = _aby_rng(guild_id, scope="daily", board_key=today_ymd)

    q: list[dict] = []

    # 1) basic material delivery
    k1 = r.choice(_ABY_MATS_BASIC)
    qty1 = r.randint(5, 9) if k1 == "scrap" else r.randint(3, 6)
    pts1 = r.randint(8, 14)
    cred1 = r.randint(3_000, 7_000)
    title1 = "자재 납품(기초)"
    desc1 = f"{k1} x{qty1} 납품"
    q.append(
        {
            "quest_no": 1,
            "quest_type": "deliver_item",
            "title": title1,
            "description": desc1,
            "target_key": k1,
            "target_qty": qty1,
            "reward_points": pts1,
            "reward_credits": cred1,
            "reward_item_key": "",
            "reward_item_qty": 0,
        }
    )

    # 2) advanced material delivery
    k2 = r.choice(_ABY_MATS_ADV)
    qty2 = 1 if k2 == "circuit" else r.randint(1, 2)
    if k2 == "filter":
        qty2 = r.randint(2, 4)
    pts2 = r.randint(10, 18)
    cred2 = r.randint(4_000, 9_000)

    # small chance for a usable item reward
    reward_item = ""
    reward_item_qty = 0
    if r.random() < 0.22:
        reward_item = r.choice(["kit", "drone", "mask"])
        reward_item_qty = 1

    title2 = "자재 납품(정밀)"
    desc2 = f"{k2} x{qty2} 납품"
    q.append(
        {
            "quest_no": 2,
            "quest_type": "deliver_item",
            "title": title2,
            "description": desc2,
            "target_key": k2,
            "target_qty": qty2,
            "reward_points": pts2,
            "reward_credits": cred2,
            "reward_item_key": reward_item,
            "reward_item_qty": reward_item_qty,
        }
    )

    # 3) repay total (daily)
    # Phase6: as debt grows, repayment targets rise (still dwarfed by interest).
    choices = [15_000, 20_000, 30_000, 40_000, 50_000]
    if pressure_ratio >= 1.30:
        choices = [30_000, 40_000, 50_000, 60_000, 80_000]
    if pressure_ratio >= 1.60:
        choices = [50_000, 60_000, 80_000, 100_000, 120_000]
    repay_amt = r.choice(choices)
    pts3 = r.randint(10, 16)
    cred3 = r.randint(0, 3_000)
    title3 = "부채 상환 실적"
    desc3 = f"오늘 빚 상환 누적 {repay_amt:,} 이상"
    q.append(
        {
            "quest_no": 3,
            "quest_type": "repay_total",
            "title": title3,
            "description": desc3,
            "target_key": "debt",
            "target_qty": int(repay_amt),
            "reward_points": pts3,
            "reward_credits": cred3,
            "reward_item_key": "",
            "reward_item_qty": 0,
        }
    )

    return q


def _gen_weekly_quests(guild_id: int, week_key: str, *, pressure_ratio: float = 1.0) -> list[dict]:
    r = _aby_rng(guild_id, scope="weekly", board_key=week_key)

    q: list[dict] = []

    # 1) big delivery
    k1 = r.choice(["scrap", "filter", "battery", "circuit"])
    if k1 == "scrap":
        qty1 = r.randint(18, 28)
    elif k1 == "filter":
        qty1 = r.randint(8, 14)
    elif k1 == "battery":
        qty1 = r.randint(5, 10)
    else:
        qty1 = r.randint(3, 6)

    pts1 = r.randint(22, 34)
    cred1 = r.randint(10_000, 25_000)
    title1 = "주간 납품 계약"
    desc1 = f"이번 주 {k1} x{qty1} 납품"
    q.append(
        {
            "quest_no": 1,
            "quest_type": "deliver_item",
            "title": title1,
            "description": desc1,
            "target_key": k1,
            "target_qty": qty1,
            "reward_points": pts1,
            "reward_credits": cred1,
            "reward_item_key": "",
            "reward_item_qty": 0,
        }
    )

    # 2) repay total (weekly)
    # Phase6: as debt grows, weekly targets rise.
    choices = [120_000, 150_000, 200_000, 250_000]
    if pressure_ratio >= 1.30:
        choices = [200_000, 250_000, 300_000, 350_000]
    if pressure_ratio >= 1.60:
        choices = [350_000, 450_000, 550_000, 650_000]
    repay_amt = r.choice(choices)
    pts2 = r.randint(26, 40)
    cred2 = r.randint(15_000, 35_000)
    title2 = "주간 부채 상환"
    desc2 = f"이번 주 빚 상환 누적 {repay_amt:,} 이상"
    q.append(
        {
            "quest_no": 2,
            "quest_type": "repay_total",
            "title": title2,
            "description": desc2,
            "target_key": "debt",
            "target_qty": int(repay_amt),
            "reward_points": pts2,
            "reward_credits": cred2,
            "reward_item_key": "",
            "reward_item_qty": 0,
        }
    )

    # 3) sandstorm success once in this week
    pts3 = r.randint(18, 30)
    cred3 = r.randint(8_000, 18_000)
    title3 = "모래폭풍 관측 임무"
    desc3 = "이번 주 모래폭풍 날 탐사 **성공** 1회"
    q.append(
        {
            "quest_no": 3,
            "quest_type": "explore_sandstorm_success",
            "title": title3,
            "description": desc3,
            "target_key": "sandstorm",
            "target_qty": 1,
            "reward_points": pts3,
            "reward_credits": cred3,
            "reward_item_key": "mask",
            "reward_item_qty": 1,
        }
    )

    return q


def _get_quest_count(guild_id: int, scope: str, board_key: str) -> int:
    row = fetchone(
        """
        SELECT COUNT(*) AS cnt
        FROM aby_quest_board
        WHERE guild_id=? AND scope=? AND board_key=?;
        """,
        (int(guild_id), str(scope), str(board_key)),
    )
    return int((row or {}).get("cnt") or 0)


def _insert_quests(guild_id: int, scope: str, board_key: str, quests: list[dict]) -> None:
    gid = int(guild_id)
    sc = str(scope)
    bk = str(board_key)
    now = now_ts()

    with transaction() as con:
        for q in quests:
            con.execute(
                """
                INSERT INTO aby_quest_board(
                  guild_id, scope, board_key, quest_no,
                  quest_type, title, description,
                  target_key, target_qty,
                  reward_points, reward_credits,
                  reward_item_key, reward_item_qty,
                  created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, scope, board_key, quest_no) DO NOTHING;
                """,
                (
                    gid,
                    sc,
                    bk,
                    int(q.get("quest_no") or 0),
                    str(q.get("quest_type") or ""),
                    str(q.get("title") or "")[:80],
                    str(q.get("description") or "")[:200],
                    (str(q.get("target_key") or "")[:40] if q.get("target_key") is not None else None),
                    int(q.get("target_qty") or 0),
                    int(q.get("reward_points") or 0),
                    int(q.get("reward_credits") or 0),
                    (str(q.get("reward_item_key") or "")[:40] if q.get("reward_item_key") else None),
                    int(q.get("reward_item_qty") or 0),
                    now,
                ),
            )


def ensure_aby_daily_quest_board(guild_id: int, today_ymd: str) -> None:
    # Phase6: keep debt updated so the board reflects current "pressure".
    apply_guild_interest_upto_today(guild_id, today_ymd)

    s = get_guild_debt(guild_id)
    debt = int((s or {}).get("debt") or ABY_DEFAULT_DEBT)
    stage = debt_pressure_stage(debt, initial=ABY_DEFAULT_DEBT)
    try:
        ratio = float(stage.get("ratio") or 1.0)
    except Exception:
        ratio = 1.0

    if _get_quest_count(guild_id, "daily", today_ymd) >= ABY_DAILY_QUEST_COUNT:
        return
    quests = _gen_daily_quests(int(guild_id), str(today_ymd), pressure_ratio=ratio)
    _insert_quests(int(guild_id), "daily", str(today_ymd), quests)


def ensure_aby_weekly_quest_board(guild_id: int, week_key: str) -> None:
    # Phase6: weekly board also reacts to current debt pressure.
    today_ymd = datetime.datetime.now(tz=KST).date().isoformat()
    apply_guild_interest_upto_today(guild_id, today_ymd)

    s = get_guild_debt(guild_id)
    debt = int((s or {}).get("debt") or ABY_DEFAULT_DEBT)
    stage = debt_pressure_stage(debt, initial=ABY_DEFAULT_DEBT)
    try:
        ratio = float(stage.get("ratio") or 1.0)
    except Exception:
        ratio = 1.0

    if _get_quest_count(guild_id, "weekly", week_key) >= ABY_WEEKLY_QUEST_COUNT:
        return
    quests = _gen_weekly_quests(int(guild_id), str(week_key), pressure_ratio=ratio)
    _insert_quests(int(guild_id), "weekly", str(week_key), quests)


def get_aby_quests(guild_id: int, scope: str, board_key: str) -> list[Dict[str, Any]]:
    rows = fetchall(
        """
        SELECT guild_id, scope, board_key, quest_no, quest_type, title, description,
               COALESCE(target_key, '') AS target_key, target_qty,
               reward_points, reward_credits,
               COALESCE(reward_item_key, '') AS reward_item_key,
               reward_item_qty,
               created_at
        FROM aby_quest_board
        WHERE guild_id=? AND scope=? AND board_key=?
        ORDER BY quest_no ASC;
        """,
        (int(guild_id), str(scope), str(board_key)),
    )
    return rows or []


def get_aby_quest(guild_id: int, scope: str, board_key: str, quest_no: int) -> Optional[Dict[str, Any]]:
    return fetchone(
        """
        SELECT guild_id, scope, board_key, quest_no, quest_type, title, description,
               COALESCE(target_key, '') AS target_key, target_qty,
               reward_points, reward_credits,
               COALESCE(reward_item_key, '') AS reward_item_key,
               reward_item_qty,
               created_at
        FROM aby_quest_board
        WHERE guild_id=? AND scope=? AND board_key=? AND quest_no=?;
        """,
        (int(guild_id), str(scope), str(board_key), int(quest_no)),
    )


def is_aby_quest_claimed(guild_id: int, scope: str, board_key: str, quest_no: int, user_id: int) -> bool:
    row = fetchone(
        """
        SELECT 1 AS ok
        FROM aby_quest_claims
        WHERE guild_id=? AND scope=? AND board_key=? AND quest_no=? AND user_id=?;
        """,
        (int(guild_id), str(scope), str(board_key), int(quest_no), int(user_id)),
    )
    return bool(row)


def get_user_weekly_points(guild_id: int, week_key: str, user_id: int) -> int:
    row = fetchone(
        """
        SELECT points
        FROM aby_weekly_points
        WHERE guild_id=? AND week_key=? AND user_id=?;
        """,
        (int(guild_id), str(week_key), int(user_id)),
    )
    return int((row or {}).get("points") or 0)


def add_user_weekly_points(guild_id: int, week_key: str, user_id: int, delta: int) -> None:
    gid = int(guild_id)
    wk = str(week_key)
    uid = int(user_id)
    d = int(delta)
    if d == 0:
        return
    now = now_ts()
    with transaction() as con:
        con.execute(
            """
            INSERT INTO aby_weekly_points(guild_id, week_key, user_id, points, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, week_key, user_id) DO UPDATE SET
              points = MAX(0, points + excluded.points),
              updated_at = excluded.updated_at;
            """,
            (gid, wk, uid, d, now),
        )


def get_weekly_points_ranking(guild_id: int, week_key: str, limit: int = 10) -> list[Dict[str, Any]]:
    lim = int(limit)
    if lim <= 0:
        lim = 10
    if lim > 50:
        lim = 50

    rows = fetchall(
        """
        SELECT user_id, points, updated_at
        FROM aby_weekly_points
        WHERE guild_id=? AND week_key=?
        ORDER BY points DESC, updated_at ASC
        LIMIT ?;
        """,
        (int(guild_id), str(week_key), lim),
    )
    return rows or []


def _repay_total_for_ymds(guild_id: int, user_id: int, ymds: list[str]) -> int:
    gid = int(guild_id)
    uid = int(user_id)
    days = [str(x) for x in (ymds or []) if str(x)]
    if not days:
        return 0
    ph = ",".join(["?"] * len(days))
    row = fetchone(
        f"""
        SELECT COALESCE(SUM(-delta_credits), 0) AS total
        FROM aby_economy_log
        WHERE guild_id=? AND user_id=? AND kind='repay' AND memo IN ({ph});
        """,
        (gid, uid, *days),
    )
    return int((row or {}).get("total") or 0)


def repay_total_for_day(guild_id: int, user_id: int, ymd: str) -> int:
    return _repay_total_for_ymds(guild_id, user_id, [str(ymd)])


def repay_total_for_week(guild_id: int, user_id: int, week_key: str) -> int:
    ymds = week_ymds_from_week_key(week_key)
    return _repay_total_for_ymds(guild_id, user_id, ymds)


def _has_sandstorm_success_in_week(user_id: int, week_key: str) -> bool:
    uid = int(user_id)
    ymds = week_ymds_from_week_key(week_key)
    if not ymds:
        return False

    ph = ",".join(["?"] * len(ymds))
    row = fetchone(
        f"""
        SELECT 1 AS ok
        FROM aby_explore_meta
        WHERE user_id=? AND date_ymd IN ({ph}) AND weather='sandstorm' AND success=1
        LIMIT 1;
        """,
        (uid, *ymds),
    )
    return bool(row)


def has_sandstorm_success_in_week(user_id: int, week_key: str) -> bool:
    """Public wrapper for sandstorm-success quest checks."""
    return _has_sandstorm_success_in_week(int(user_id), str(week_key))


def claim_aby_quest(
    *,
    guild_id: int,
    user_id: int,
    scope: str,
    board_key: str,
    quest_no: int,
    today_ymd: str,
) -> Dict[str, Any]:
    """Claim a quest reward if requirements are met.

    Returns:
      {ok: bool, reason?: str, reward_points?: int, reward_credits?: int, reward_item_key?: str, reward_item_qty?: int}

    reason values:
      - not_found
      - claimed
      - items
      - repay
      - explore
      - sandstorm
    """

    gid = int(guild_id)
    uid = int(user_id)
    ensure_user_economy(uid)
    sc = str(scope)
    bk = str(board_key)
    qn = int(quest_no)

    quest = get_aby_quest(gid, sc, bk, qn)
    if not quest:
        return {"ok": False, "reason": "not_found"}

    qtype = str(quest.get("quest_type") or "")
    target_key = str(quest.get("target_key") or "").strip().lower()
    target_qty = int(quest.get("target_qty") or 0)

    reward_points = int(quest.get("reward_points") or 0)
    reward_credits = int(quest.get("reward_credits") or 0)
    reward_item_key = str(quest.get("reward_item_key") or "").strip().lower()
    reward_item_qty = int(quest.get("reward_item_qty") or 0)

    # Validate first (fast path)
    if is_aby_quest_claimed(gid, sc, bk, qn, uid):
        return {"ok": False, "reason": "claimed"}

    if qtype == "deliver_item":
        have = get_user_item_qty(uid, target_key)
        if have < target_qty:
            return {"ok": False, "reason": "items", "have": have, "need": target_qty, "item": target_key}

    elif qtype == "repay_total":
        if sc == "daily":
            rep = repay_total_for_day(gid, uid, bk)
        else:
            rep = repay_total_for_week(gid, uid, bk)
        if rep < target_qty:
            return {"ok": False, "reason": "repay", "have": rep, "need": target_qty}

    elif qtype == "explore_done":
        meta = get_explore_meta(uid, str(today_ymd))
        if not meta:
            return {"ok": False, "reason": "explore"}

    elif qtype == "explore_sandstorm_success":
        if sc == "daily":
            meta = get_explore_meta(uid, bk)
            if not meta or str(meta.get("weather") or "") != "sandstorm" or int(meta.get("success") or 0) != 1:
                return {"ok": False, "reason": "sandstorm"}
        else:
            if not _has_sandstorm_success_in_week(uid, bk):
                return {"ok": False, "reason": "sandstorm"}

    else:
        return {"ok": False, "reason": "not_found"}

    # Apply atomically
    ensure_user_economy(uid)

    week_key = bk if sc == "weekly" else week_key_from_ymd(str(today_ymd))
    now = now_ts()

    with transaction() as con:
        # claim check (re-check)
        row = con.execute(
            """
            SELECT 1
            FROM aby_quest_claims
            WHERE guild_id=? AND scope=? AND board_key=? AND quest_no=? AND user_id=?;
            """,
            (gid, sc, bk, qn, uid),
        ).fetchone()
        if row is not None:
            return {"ok": False, "reason": "claimed"}

        # consume items if needed
        if qtype == "deliver_item" and target_key and target_qty > 0:
            row2 = con.execute(
                "SELECT qty FROM aby_inventory WHERE user_id=? AND item_key=?;",
                (uid, target_key),
            ).fetchone()
            have2 = int(row2[0]) if row2 else 0
            if have2 < target_qty:
                return {"ok": False, "reason": "items", "have": have2, "need": target_qty, "item": target_key}
            con.execute(
                "UPDATE aby_inventory SET qty = MAX(0, qty - ?), updated_at=? WHERE user_id=? AND item_key=?;",
                (target_qty, now, uid, target_key),
            )

        # reward credits
        if reward_credits != 0:
            con.execute(
                "UPDATE aby_user_economy SET credits = MAX(0, credits + ?), updated_at=? WHERE user_id=?;",
                (reward_credits, now, uid),
            )

        # reward items
        if reward_item_key and reward_item_qty > 0:
            con.execute(
                """
                INSERT INTO aby_inventory(user_id, item_key, qty, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(user_id, item_key) DO UPDATE SET
                  qty = MAX(0, qty + excluded.qty),
                  updated_at = excluded.updated_at;
                """,
                (uid, reward_item_key, reward_item_qty, now),
            )

        # quest claim record
        con.execute(
            """
            INSERT INTO aby_quest_claims(guild_id, scope, board_key, quest_no, user_id, claimed_at)
            VALUES(?, ?, ?, ?, ?, ?);
            """,
            (gid, sc, bk, qn, uid, now),
        )

        # weekly points
        if reward_points > 0:
            con.execute(
                """
                INSERT INTO aby_weekly_points(guild_id, week_key, user_id, points, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, week_key, user_id) DO UPDATE SET
                  points = MAX(0, points + excluded.points),
                  updated_at = excluded.updated_at;
                """,
                (gid, week_key, uid, reward_points, now),
            )

        # light log (reward credits only)
        if reward_credits != 0:
            con.execute(
                """
                INSERT INTO aby_economy_log(guild_id, user_id, kind, delta_credits, delta_water, delta_debt, memo, created_at)
                VALUES(?, ?, 'quest', ?, 0, 0, ?, ?);
                """,
                (
                    gid,
                    uid,
                    int(reward_credits),
                    f"{sc}:{bk}:{qn}",
                    now,
                ),
            )

    return {
        "ok": True,
        "reward_points": reward_points,
        "reward_credits": reward_credits,
        "reward_item_key": reward_item_key,
        "reward_item_qty": reward_item_qty,
        "week_key": week_key,
    }


# ------------------------------
# Phase6-2 Phase7: Incidents + Weekly report helpers
# ------------------------------


def ensure_aby_incident_state(guild_id: int, *, now_override: Optional[int] = None) -> Dict[str, Any]:
    """Ensure a guild incident scheduler row exists.

    next_incident_at is randomized on first creation or when missing.
    """

    gid = int(guild_id)
    now = int(now_override) if now_override is not None else now_ts()
    # default: 2~6 hours
    default_next = now + random.randint(2 * 3600, 6 * 3600)

    row = fetchone(
        "SELECT guild_id, next_incident_at, last_incident_at, updated_at FROM aby_incident_state WHERE guild_id=?;",
        (gid,),
    )
    if row:
        nxt = int(row.get("next_incident_at") or 0)
        if nxt <= 0:
            execute(
                "UPDATE aby_incident_state SET next_incident_at=?, updated_at=? WHERE guild_id=?;",
                (default_next, now, gid),
            )
            row["next_incident_at"] = default_next
            row["updated_at"] = now
        return row

    execute(
        """
        INSERT INTO aby_incident_state(guild_id, next_incident_at, last_incident_at, updated_at)
        VALUES(?, ?, 0, ?)
        ON CONFLICT(guild_id) DO NOTHING;
        """,
        (gid, default_next, now),
    )
    return {
        "guild_id": gid,
        "next_incident_at": default_next,
        "last_incident_at": 0,
        "updated_at": now,
    }


def set_aby_next_incident_at(guild_id: int, next_incident_at: int, *, now_override: Optional[int] = None) -> None:
    gid = int(guild_id)
    now = int(now_override) if now_override is not None else now_ts()
    execute(
        """
        INSERT INTO aby_incident_state(guild_id, next_incident_at, last_incident_at, updated_at)
        VALUES(?, ?, 0, ?)
        ON CONFLICT(guild_id) DO UPDATE SET next_incident_at=excluded.next_incident_at, updated_at=excluded.updated_at;
        """,
        (gid, int(next_incident_at), now),
    )



def update_aby_incident_state(
    guild_id: int,
    *,
    next_incident_at: int,
    last_incident_at: Optional[int] = None,
    now_override: Optional[int] = None,
) -> None:
    """Update incident scheduler state for a guild."""

    gid = int(guild_id)
    now = int(now_override) if now_override is not None else now_ts()
    last = int(last_incident_at) if last_incident_at is not None else 0
    execute(
        """
        INSERT INTO aby_incident_state(guild_id, next_incident_at, last_incident_at, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
          next_incident_at=excluded.next_incident_at,
          last_incident_at=excluded.last_incident_at,
          updated_at=excluded.updated_at;
        """,
        (gid, int(next_incident_at), last, now),
    )

def add_aby_incident_log(guild_id: int, *, kind: str, title: str, description: str, delta_debt: int) -> None:
    gid = int(guild_id)
    k = str(kind or "incident")[:40]
    t = str(title or "")[:120]
    d = str(description or "")[:500]
    dd = int(delta_debt)
    now = now_ts()
    execute(
        """
        INSERT INTO aby_incident_log(guild_id, kind, title, description, delta_debt, created_at)
        VALUES(?, ?, ?, ?, ?, ?);
        """,
        (gid, k, t, d, dd, now),
    )


def list_recent_aby_incidents(guild_id: int, limit: int = 10) -> list[Dict[str, Any]]:
    lim = int(limit)
    if lim <= 0:
        lim = 10
    if lim > 50:
        lim = 50
    rows = fetchall(
        """
        SELECT id, guild_id, kind, title, description, delta_debt, created_at
        FROM aby_incident_log
        WHERE guild_id=?
        ORDER BY created_at DESC, id DESC
        LIMIT ?;
        """,
        (int(guild_id), lim),
    )
    return rows or []


def apply_guild_incident(
    guild_id: int,
    *,
    title: str,
    description: str,
    delta_debt: int,
    today_ymd: str,
) -> Dict[str, Any]:
    """Apply an incident effect to guild debt and log it.

    - Updates aby_guild_debt.debt (clamped at >=0)
    - Inserts into aby_economy_log(kind='incident', delta_debt, memo=today_ymd)
    - Inserts into aby_incident_log
    """

    gid = int(guild_id)
    dd = int(delta_debt)
    ymd = str(today_ymd)
    now = now_ts()

    ensure_guild_debt(gid, today_ymd=ymd)

    with transaction() as con:
        row = con.execute(
            "SELECT debt FROM aby_guild_debt WHERE guild_id=?;",
            (gid,),
        ).fetchone()
        debt = int(row[0]) if row else ABY_DEFAULT_DEBT
        new_debt = max(0, debt + dd)

        con.execute(
            "UPDATE aby_guild_debt SET debt=?, updated_at=? WHERE guild_id=?;",
            (new_debt, now, gid),
        )

        con.execute(
            """
            INSERT INTO aby_economy_log(guild_id, user_id, kind, delta_credits, delta_water, delta_debt, memo, created_at)
            VALUES(?, NULL, 'incident', 0, 0, ?, ?, ?);
            """,
            (gid, dd, ymd, now),
        )

    add_aby_incident_log(gid, kind="incident", title=title, description=description, delta_debt=dd)

    return {"ok": True, "old_debt": debt, "new_debt": new_debt, "delta_debt": dd}


def _sum_debt_delta_for_ymds(guild_id: int, ymds: list[str], kind: str) -> int:
    gid = int(guild_id)
    days = [str(x) for x in (ymds or []) if str(x)]
    if not days:
        return 0
    ph = ",".join(["?"] * len(days))
    row = fetchone(
        f"""
        SELECT COALESCE(SUM(delta_debt), 0) AS total
        FROM aby_economy_log
        WHERE guild_id=? AND kind=? AND memo IN ({ph});
        """,
        (gid, str(kind), *days),
    )
    return int((row or {}).get("total") or 0)


def _sum_repay_credits_for_ymds(guild_id: int, ymds: list[str]) -> int:
    gid = int(guild_id)
    days = [str(x) for x in (ymds or []) if str(x)]
    if not days:
        return 0
    ph = ",".join(["?"] * len(days))
    row = fetchone(
        f"""
        SELECT COALESCE(SUM(-delta_credits), 0) AS total
        FROM aby_economy_log
        WHERE guild_id=? AND kind='repay' AND memo IN ({ph});
        """,
        (gid, *days),
    )
    return int((row or {}).get("total") or 0)


def top_repay_users_for_week(guild_id: int, week_key: str, limit: int = 5) -> list[Dict[str, Any]]:
    gid = int(guild_id)
    ymds = week_ymds_from_week_key(str(week_key))
    if not ymds:
        return []
    lim = int(limit)
    if lim <= 0:
        lim = 5
    if lim > 20:
        lim = 20
    ph = ",".join(["?"] * len(ymds))
    rows = fetchall(
        f"""
        SELECT user_id, COALESCE(SUM(-delta_credits), 0) AS total
        FROM aby_economy_log
        WHERE guild_id=? AND kind='repay' AND memo IN ({ph}) AND user_id IS NOT NULL
        GROUP BY user_id
        ORDER BY total DESC
        LIMIT ?;
        """,
        (gid, *ymds, lim),
    )
    return rows or []


def get_weekly_debt_summary(guild_id: int, week_key: str) -> Dict[str, Any]:
    ymds = week_ymds_from_week_key(str(week_key))
    interest = _sum_debt_delta_for_ymds(guild_id, ymds, "interest")
    incidents = _sum_debt_delta_for_ymds(guild_id, ymds, "incident")
    repays = _sum_debt_delta_for_ymds(guild_id, ymds, "repay")  # negative
    repaid_credits = _sum_repay_credits_for_ymds(guild_id, ymds)

    return {
        "week_key": str(week_key),
        "ymds": ymds,
        "interest_delta": int(interest),
        "incident_delta": int(incidents),
        "repay_delta": int(repays),
        "net_delta": int(interest + incidents + repays),
        "repaid_credits": int(repaid_credits),
    }

