"""yume_runtime.py

Phase0 foundation: background tasks (scheduler loop) entrypoint.

We keep tasks minimal for now:
- periodic world-state refresh hook (Phase1 will rotate virtual weather)
- placeholder for future DM delivery/time-capsules

The key is: one reliable place to start/stop tasks without duplicating them
on reconnect.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

import discord

from yume_store import get_world_state, set_world_weather

logger = logging.getLogger(__name__)


async def _world_state_loop(bot: discord.Client):
    """Every minute, check if it's time to roll weather (Phase1 behavior)."""
    while True:
        try:
            state = get_world_state()
            now = int(time.time())

            # Phase0 behavior: do nothing unless the DB is somehow missing values.
            # Phase1 will implement 4~6 hour rotation + sandstorm effects.
            if not state.get("weather"):
                set_world_weather("clear", changed_at=now, next_change_at=now + 6 * 3600)

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