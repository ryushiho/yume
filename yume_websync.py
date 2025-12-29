"""yume_websync.py

Phase6-1 (minimal): Sync a tiny summary of the bot state to shihonoyume.xyz.

Design
- Web is a read-only dashboard in Phase6-1.
- The bot periodically POSTs a small snapshot.
- If env isn't set, sync is disabled.
- Any error should be logged but must never kill the bot.

Env
- YUME_WEB_SYNC_URL
    Full URL, e.g. https://shihonoyume.xyz/api/bot/sync
- YUME_WEB_SYNC_TOKEN
    Shared secret token (bearer). Keep it in .env.

HTTP Contract (suggestion)
- POST {url}
- Headers: Authorization: Bearer <token>
- JSON body: see build_sync_payload()
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import aiohttp
import discord

from yume_store import (
    get_daily_meal,
    get_daily_rule,
    get_top_stamps,
    get_world_state,
)

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def _today_ymd_kst() -> str:
    return datetime.now(tz=KST).date().isoformat()


def _read_env() -> tuple[str | None, str | None]:
    url = (os.getenv("YUME_WEB_SYNC_URL", "") or "").strip()
    token = (os.getenv("YUME_WEB_SYNC_TOKEN", "") or "").strip()
    return (url or None, token or None)


def build_sync_payload(bot: discord.Client) -> Dict[str, Any]:
    """Build a small, stable JSON payload.

    Keep it boring (stable keys), and avoid sending any message content.
    """

    now = int(time.time())

    world = get_world_state()
    date_ymd = _today_ymd_kst()

    rule = get_daily_rule(date_ymd)
    meal = get_daily_meal(date_ymd)

    top = get_top_stamps(limit=10)

    guilds: List[Dict[str, Any]] = []
    try:
        for g in getattr(bot, "guilds", []) or []:
            guilds.append({"guild_id": int(g.id), "name": str(g.name)})
    except Exception:
        guilds = []

    bot_user = getattr(bot, "user", None)

    payload: Dict[str, Any] = {
        "generated_at": now,
        "bot": {
            "user_id": int(getattr(bot_user, "id", 0) or 0),
            "username": str(getattr(bot_user, "name", "")) if bot_user else "",
        },
        "guilds": guilds,
        "world": {
            "weather": str(world.get("weather") or "clear"),
            "weather_changed_at": int(world.get("weather_changed_at") or 0),
            "weather_next_change_at": int(world.get("weather_next_change_at") or 0),
            "updated_at": int(world.get("updated_at") or 0),
        },
        "daily_rule": {
            "date": date_ymd,
            "rule_no": int((rule or {}).get("rule_no") or 0),
            "rule_text": str((rule or {}).get("rule_text") or ""),
            "posted_channel_id": (rule or {}).get("posted_channel_id"),
            "posted_at": (rule or {}).get("posted_at"),
        },
        "daily_meal": {
            "date": date_ymd,
            "meal_text": str((meal or {}).get("meal_text") or ""),
            "last_requested_at": int((meal or {}).get("last_requested_at") or 0),
        },
        "stamps": {
            "top": [
                {
                    "user_id": int(r.get("user_id") or 0),
                    "stamps": int(r.get("stamps") or 0),
                    "stamp_title": str(r.get("stamp_title") or ""),
                    "updated_at": int(r.get("updated_at") or 0),
                }
                for r in (top or [])
            ]
        },
    }

    return payload


async def post_sync_payload(bot: discord.Client) -> bool:
    """POST one snapshot. Returns True on success."""

    url, token = _read_env()
    if not url or not token:
        return False

    payload = build_sync_payload(bot)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "yume-bot/phase6-1",
    }

    timeout = aiohttp.ClientTimeout(total=8)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if 200 <= resp.status < 300:
                    return True

                # Try to read response text (limited) for debug.
                try:
                    txt = await resp.text()
                    txt = (txt or "")[:300]
                except Exception:
                    txt = ""

                logger.warning("[websync] non-2xx: %s %s", resp.status, txt)
                return False

    except Exception as e:
        logger.warning("[websync] post failed: %s", e)
        return False
