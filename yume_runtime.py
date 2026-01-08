"""yume_runtime.py

Background tasks (scheduler loops).

Phase1
- Virtual "Abydos weather" rotates automatically every 4~6 hours (clear/cloudy/sandstorm).

Phase3
- Daily "Abydos rule" announcement at 08:00 KST.

Design goals:
- Predictable + low CPU
- Safe to call start multiple times
- All state stored in SQLite
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import discord

from yume_llm import generate_daily_rule
from yume_send import send_channel
from yume_presence import apply_random_presence, get_next_presence_interval_seconds
from yume_websync import post_sync_payload, websync_enabled
from yume_store import (
    bump_daily_rule_attempt,
    ensure_daily_rule_row,
    get_config,
    get_daily_rule,
    get_recent_rule_suggestions,
    get_world_state,
    mark_daily_rule_posted,
    set_world_weather,
    update_daily_rule_text,
    ensure_guild_debt,
    apply_guild_interest_upto_today,
)

logger = logging.getLogger(__name__)


WEATHER_STATES = ("clear", "cloudy", "sandstorm")

WEATHER_LABEL = {
    "clear": "맑음",
    "cloudy": "흐림",
    "sandstorm": "대형 모래폭풍",
}

KST = timezone(timedelta(hours=9))


def _now_kst() -> datetime:
    return datetime.now(tz=KST)


async def _get_messageable(bot: discord.Client, channel_id: int) -> Optional[discord.abc.Messageable]:
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)  # type: ignore[assignment]
        except Exception:
            return None
    return ch  # type: ignore[return-value]


def _get_rule_channel_id() -> Optional[int]:
    """Resolve the daily-rule announcement channel id.

    Priority:
    1) SQLite bot_config('rule_channel_id')
    2) env YUME_RULE_CHANNEL_ID
    """

    raw = (get_config("rule_channel_id") or os.getenv("YUME_RULE_CHANNEL_ID", "") or "").strip()
    if not raw:
        return None
    try:
        cid = int(raw)
    except ValueError:
        return None
    return cid if cid > 0 else None


async def _daily_rule_loop(bot: discord.Client) -> None:
    """Every minute, ensure today's rule is generated and announced after 08:00 KST."""

    await asyncio.sleep(random.uniform(0.5, 3.0))

    while True:
        try:
            now_kst = _now_kst()

            # Only after 08:00 KST
            if (now_kst.hour, now_kst.minute) < (8, 0):
                await asyncio.sleep(60)
                continue

            date_ymd = now_kst.date().isoformat()
            row = get_daily_rule(date_ymd)

            # Already posted today
            if row and row.get("posted_at"):
                await asyncio.sleep(60)
                continue

            # Backoff after repeated failures
            attempts = int((row or {}).get("attempts") or 0)
            if attempts >= 5:
                await asyncio.sleep(300)
                continue

            # Ensure a row exists (assigns rule_no once)
            row = ensure_daily_rule_row(date_ymd)
            rule_no = int(row.get("rule_no") or 0)
            rule_text = str(row.get("rule_text") or "").strip()

            if not rule_text:
                world = get_world_state()
                weather = str(world.get("weather") or "clear")
                weather_label = WEATHER_LABEL.get(weather, weather)

                sug = get_recent_rule_suggestions(limit=5)
                hints = [str(s.get("content") or "").strip() for s in sug if str(s.get("content") or "").strip()]

                rule_text = generate_daily_rule(
                    date_ymd=date_ymd,
                    rule_no=rule_no,
                    weather_label=weather_label,
                    suggestion_hints=hints,
                ).strip()

                if rule_text:
                    update_daily_rule_text(date_ymd, rule_text)

            channel_id = _get_rule_channel_id()
            if not channel_id:
                # Channel not configured: keep the rule stored, but don't attempt to post.
                await asyncio.sleep(300)
                continue

            channel = await _get_messageable(bot, channel_id)
            if channel is None:
                bump_daily_rule_attempt(date_ymd, error=f"rule_channel_not_found:{channel_id}")
                await asyncio.sleep(120)
                continue

            msg = f"📢 오늘의 아비도스 교칙 (제 {rule_no}조)\n\n{rule_text}"

            try:
                await send_channel(channel, msg, allow_glitch=True)
                mark_daily_rule_posted(date_ymd, channel_id=int(channel_id))
            except Exception as e:
                bump_daily_rule_attempt(date_ymd, error=str(e))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[daily_rule_loop] error: %s", e)

        await asyncio.sleep(60)


def _weather_alert_line(weather: str) -> str:
    w = (weather or "clear").strip()
    if w == "sandstorm":
        return "📡⚠️ 대형 모래폭풍 경보! 퉤퉤… 통신이 불안정해질지도…"
    if w == "cloudy":
        return "📡 구름이 끼었어. 모래가 더 잘 보이려나? 에헤헤."
    return "📡 하늘이 맑아! 오늘은 포스터 붙이기 딱이야~"


async def _announce_weather(bot: discord.Client, weather: str) -> None:
    """Optional: announce to a configured channel."""

    raw = os.getenv("YUME_WEATHER_CHANNEL_ID", "").strip()
    if not raw:
        return

    try:
        channel_id = int(raw)
    except ValueError:
        return
    if channel_id <= 0:
        return

    channel = await _get_messageable(bot, channel_id)
    if channel is None:
        return

    try:
        await send_channel(
            channel,
            f"{_weather_alert_line(weather)}  (현재: `{WEATHER_LABEL.get(weather, weather)}`)",
            allow_glitch=True,
        )
    except Exception:
        return


def _roll_next_weather(current: str) -> str:
    """Pick the next weather with a light weighting.

    - sandstorm is rarer (special event)
    - avoid repeating the same state when possible
    """

    current = (current or "clear").strip()

    # Weighted draw: clear 55%, cloudy 30%, sandstorm 15%
    pool = [
        ("clear", 0.55),
        ("cloudy", 0.30),
        ("sandstorm", 0.15),
    ]

    r = random.random()
    acc = 0.0
    picked = "clear"
    for k, w in pool:
        acc += w
        if r <= acc:
            picked = k
            break

    if picked == current:
        candidates = [s for s in WEATHER_STATES if s != current]
        picked = random.choice(candidates) if candidates else picked

    return picked


def _roll_next_change_at(now: int) -> int:
    # 4~6 hours
    return int(now + random.randint(4 * 3600, 6 * 3600))


async def _world_state_loop(bot: discord.Client) -> None:
    """Every minute, rotate weather when it's time."""

    await asyncio.sleep(random.uniform(0.5, 3.0))

    while True:
        try:
            state = get_world_state()
            now = int(time.time())

            current_weather = str(state.get("weather") or "clear")
            next_at = int(state.get("weather_next_change_at") or 0)

            # Defensive: bad writes (e.g. milliseconds timestamp) can make next_at
            # absurdly far in the future, freezing rotation.
            if next_at >= 10**12 or (next_at > 0 and next_at > (now + 14 * 24 * 3600)):
                set_world_weather(
                    current_weather,
                    changed_at=int(state.get("weather_changed_at") or now),
                    next_change_at=now - 1,
                )
                next_at = now - 1

            # Defensive: if next_at is missing or in the past, schedule a new one.
            if next_at <= 0:
                next_at = _roll_next_change_at(now)
                set_world_weather(
                    current_weather,
                    changed_at=int(state.get("weather_changed_at") or now),
                    next_change_at=next_at,
                )
                logger.info("[world] weather schedule fixed: weather=%s next_at=%s", current_weather, next_at)

            if now >= next_at:
                new_weather = _roll_next_weather(current_weather)
                new_next_at = _roll_next_change_at(now)
                set_world_weather(new_weather, changed_at=now, next_change_at=new_next_at)
                logger.info("[world] weather rotated: %s -> %s (next at %s)", current_weather, new_weather, new_next_at)
                await _announce_weather(bot, new_weather)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[world_state_loop] error: %s", e)

        await asyncio.sleep(60)


def _websync_enabled() -> bool:
    # Phase6-1: bot -> web dashboard sync is optional.
    # 여러 env 키 이름을 yume_websync 쪽에서 흡수하므로, 여기서는 그 결과만 본다.
    return websync_enabled()


async def _web_sync_loop(bot: discord.Client) -> None:
    """Periodically POST a small status snapshot to the web.

    - Disabled unless env is configured.
    - Never raises (keeps the bot alive).
    """

    await asyncio.sleep(random.uniform(2.0, 6.0))

    def _env_int(name: str, default: int, lo: int, hi: int) -> int:
        raw = (os.getenv(name, "") or "").strip()
        try:
            v = int(raw) if raw else default
        except Exception:
            v = default
        return max(lo, min(v, hi))

    fail_sec = _env_int("YUME_WEB_SYNC_FAIL_INTERVAL_SEC", 120, 30, 3600)
    ok_sec = _env_int("YUME_WEB_SYNC_OK_INTERVAL_SEC", 300, 30, 3600)
    disabled_sec = _env_int("YUME_WEB_SYNC_DISABLED_INTERVAL_SEC", 600, 60, 3600)
    error_sec = _env_int("YUME_WEB_SYNC_ERROR_INTERVAL_SEC", 300, 30, 3600)

    while True:
        try:
            if _websync_enabled():
                ok = await post_sync_payload(bot)
                # On failure, retry a bit sooner (but not too spammy).
                await asyncio.sleep(fail_sec if not ok else ok_sec)
            else:
                # Check again later.
                await asyncio.sleep(600)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[web_sync_loop] error: %s", e)
            await asyncio.sleep(300)


async def _presence_rotation_loop(bot: discord.Client) -> None:
    """Rotate Yume's Discord presence at a random interval.

    Config: data/system/status_messages.json
    - interval_minutes: {min,max}
    - items: [{type,text,bands}]
    """

    # gentle startup jitter
    await asyncio.sleep(random.uniform(1.0, 4.0))
    logger.info("[runtime] presence rotation loop started")

    while True:
        try:
            await apply_random_presence(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[runtime] presence rotation failed")

        try:
            sec = int(get_next_presence_interval_seconds())
            if sec < 10 * 60:
                sec = 10 * 60
        except Exception:
            sec = 60 * 60

        await asyncio.sleep(sec)

async def _debt_interest_loop(bot: discord.Client) -> None:
    """Apply daily debt interest once per KST day.

    Interest is also applied lazily when users check debt/repay, but this loop makes the
    world feel "alive" even if nobody calls commands for a while.
    """

    last_done_ymd: str = ""
    logger.info("[runtime] debt interest loop started")

    while True:
        try:
            now = _now_kst()
            today_ymd = now.date().isoformat()

            # Run once per day, shortly after midnight.
            if today_ymd != last_done_ymd and now.hour == 0 and now.minute >= 5:
                for g in getattr(bot, "guilds", []) or []:
                    try:
                        ensure_guild_debt(g.id)
                        apply_guild_interest_upto_today(g.id, today_ymd)
                    except Exception:
                        logger.exception("[runtime] debt interest apply failed: guild_id=%s", g.id)
                last_done_ymd = today_ymd

        except Exception:
            logger.exception("[runtime] debt interest loop top-level error")

        await asyncio.sleep(30)


def start_background_tasks(bot: discord.Client) -> None:
    """Start background tasks once. Safe to call multiple times."""

    if getattr(bot, "_yume_bg_started", False):
        return

    bot._yume_bg_started = True  # type: ignore[attr-defined]

    tasks: List[asyncio.Task] = []
    try:
        tasks.append(asyncio.create_task(_world_state_loop(bot)))
        tasks.append(asyncio.create_task(_daily_rule_loop(bot)))
        tasks.append(asyncio.create_task(_debt_interest_loop(bot)))
        tasks.append(asyncio.create_task(_web_sync_loop(bot)))
        tasks.append(asyncio.create_task(_presence_rotation_loop(bot)))
    except Exception as e:
        logger.error("Failed to start background tasks: %s", e)

    bot._yume_bg_tasks = tasks  # type: ignore[attr-defined]


async def stop_background_tasks(bot: discord.Client) -> None:
    tasks: Optional[List[asyncio.Task]] = getattr(bot, "_yume_bg_tasks", None)
    if not tasks:
        return

    for t in tasks:
        t.cancel()

    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    bot._yume_bg_tasks = []  # type: ignore[attr-defined]