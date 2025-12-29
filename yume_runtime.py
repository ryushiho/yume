"""yume_runtime.py

Phase1: background tasks (scheduler loop).

- Virtual "Abydos weather" rotates automatically every 4~6 hours.
- This file only runs the world-state loop.

Design goals:
- predictable + low CPU
- safe to call start multiple times
- all state stored in SQLite (world_state table)
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import List, Optional

import discord

from yume_store import get_world_state, set_world_weather

logger = logging.getLogger(__name__)


WEATHER_STATES = ("clear", "cloudy", "sandstorm")


WEATHER_LABEL = {
    "clear": "맑음",
    "cloudy": "흐림",
    "sandstorm": "대형 모래폭풍",
}


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

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)  # type: ignore[assignment]
        except Exception:
            return

    try:
        # channel could be TextChannel, Thread, DMChannel...
        await channel.send(f"{_weather_alert_line(weather)}  (현재: `{WEATHER_LABEL.get(weather, weather)}`)")  # type: ignore[attr-defined]
    except Exception:
        return


def _roll_next_weather(current: str) -> str:
    """Pick the next weather with a light weighting.

    - sandstorm is rarer (it's a special event)
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
        # Re-roll once using the remaining states.
        candidates = [s for s in WEATHER_STATES if s != current]
        picked = random.choice(candidates) if candidates else picked

    return picked


def _roll_next_change_at(now: int) -> int:
    # 4~6 hours
    return int(now + random.randint(4 * 3600, 6 * 3600))


async def _world_state_loop(bot: discord.Client) -> None:
    """Every minute, rotate weather when it's time."""

    # Small jitter so multiple shards/instances don't hit DB at the exact same second.
    await asyncio.sleep(random.uniform(0.5, 3.0))

    while True:
        try:
            state = get_world_state()
            now = int(time.time())

            current_weather = str(state.get("weather") or "clear")
            next_at = int(state.get("weather_next_change_at") or 0)

            # Defensive: if next_at is missing or in the past, schedule a new one.
            if next_at <= 0:
                next_at = _roll_next_change_at(now)
                set_world_weather(current_weather, changed_at=int(state.get("weather_changed_at") or now), next_change_at=next_at)
                logger.info("[world] weather schedule fixed: weather=%s next_at=%s", current_weather, next_at)

            # Phase1: rotate when it's time.
            if now >= next_at:
                new_weather = _roll_next_weather(current_weather)
                new_next_at = _roll_next_change_at(now)
                set_world_weather(new_weather, changed_at=now, next_change_at=new_next_at)
                logger.info(
                    "[world] weather rotated: %s -> %s (next at %s)",
                    current_weather,
                    new_weather,
                    new_next_at,
                )
                await _announce_weather(bot, new_weather)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[world_state_loop] error: %s", e)

        await asyncio.sleep(60)


def start_background_tasks(bot: discord.Client) -> None:
    """Start background tasks once. Safe to call multiple times."""
    if getattr(bot, "_yume_bg_started", False):
        return

    bot._yume_bg_started = True  # type: ignore[attr-defined]

    tasks: List[asyncio.Task] = []
    try:
        tasks.append(asyncio.create_task(_world_state_loop(bot)))
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
