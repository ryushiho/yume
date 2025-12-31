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
    ensure_world_weather_rotated,
    get_guild_debt,
    get_weekly_debt_summary,
    get_weekly_points_ranking,
    get_world_state,
    list_aby_guild_ids,
    list_aby_user_economy,
    list_recent_aby_incidents,
    list_recent_explore_meta,
    week_key_from_ymd,
)

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def _today_ymd_kst() -> str:
    return datetime.now(tz=KST).date().isoformat()


def _read_env() -> tuple[str | None, str | None]:
    url = (os.getenv("YUME_WEB_SYNC_URL", "") or "").strip()
    token = (os.getenv("YUME_WEB_SYNC_TOKEN", "") or "").strip()

    # Accept either a full sync endpoint, "/api/v1/aby" base, or a bare domain.
    # Examples:
    #   https://shihonoyume.xyz/api/v1/aby/sync
    #   https://shihonoyume.xyz/api/v1/aby
    #   https://shihonoyume.xyz
    if url:
        url = url.strip().rstrip("/")
        if not url.endswith("/sync"):
            if url.endswith("/api/v1/aby"):
                url = url + "/sync"
            elif "/api/" not in url:
                url = url + "/api/v1/aby/sync"

    return (url or None, token or None)


def _safe_display_name(bot: discord.Client, guild_id: int, user_id: int) -> str:
    try:
        g = bot.get_guild(int(guild_id))
        if not g:
            return ""
        m = g.get_member(int(user_id))
        if not m:
            return ""
        return str(getattr(m, "display_name", "") or "")
    except Exception:
        return ""


def build_sync_payload(bot: discord.Client) -> Dict[str, Any]:
    """Build an Abydos sync payload for /api/v1/aby/sync.

    Server-side accepts either:
      - {"guild_id": ..., ...}
      - {"guilds": [ {"guild_id": ..., ...}, ... ]}

    We send the multi-guild form so one POST can refresh everything.
    """

    now_ts = int(time.time())
    date_ymd = _today_ymd_kst()
    week_key = week_key_from_ymd(date_ymd)

    # Make sure weather has been rotated at least once per process lifetime.
    try:
        ensure_world_weather_rotated()
    except Exception:
        pass

    world = get_world_state()

    # Discover guilds that are actually using the Abydos systems.
    guild_ids = list_aby_guild_ids()
    if not guild_ids:
        # Fallback: just report currently joined guilds.
        try:
            guild_ids = [int(g.id) for g in getattr(bot, "guilds", []) or []]
        except Exception:
            guild_ids = []

    # Shared user + explore meta are currently stored globally (user_id 기반).
    # DB 스키마/컬럼이 바뀌는 과도기(마이그레이션)에도 websync가 봇을 흔들지 않게 방어적으로 처리한다.
    users_raw: List[Dict[str, Any]] = []
    explores_raw: List[Dict[str, Any]] = []
    try:
        users_raw = list_aby_user_economy(limit=5000)
    except Exception as e:
        logger.warning("[websync] list_aby_user_economy failed: %r", e)
    try:
        explores_raw = list_recent_explore_meta(limit=300)
    except Exception as e:
        logger.warning("[websync] list_recent_explore_meta failed: %r", e)

    # Pre-resolve display names per guild (best effort).
    users_payload_cache: Dict[int, List[Dict[str, Any]]] = {}
    explores_payload_cache: Dict[int, List[Dict[str, Any]]] = {}

    def users_for_guild(gid: int) -> List[Dict[str, Any]]:
        if gid in users_payload_cache:
            return users_payload_cache[gid]
        out: List[Dict[str, Any]] = []
        for u in users_raw:
            uid = int(u.get("user_id") or 0)
            out.append(
                {
                    "discord_id": uid,
                    "nickname": _safe_display_name(bot, gid, uid) or str(u.get("user_name") or ""),
                    "discord_name": str(u.get("user_name") or ""),
                    "credits": int(u.get("credits") or 0),
                    "water": int(u.get("water") or 0),
                    "last_explore_ymd": str(u.get("last_explore_ymd") or ""),
                    "updated_at": int(u.get("updated_at") or 0),
                }
            )
        users_payload_cache[gid] = out
        return out

    def explores_for_guild(gid: int) -> List[Dict[str, Any]]:
        if gid in explores_payload_cache:
            return explores_payload_cache[gid]
        out: List[Dict[str, Any]] = []
        for e in explores_raw:
            uid = int(e.get("user_id") or 0)
            day = str(e.get("date_ymd") or "")
            out.append(
                {
                    "source_id": f"explore:{uid}:{day}",
                    "user_id": uid,
                    "nickname": _safe_display_name(bot, gid, uid),
                    "day_ymd": day,
                    "weather": str(e.get("weather") or ""),
                    "success": bool(e.get("success")),
                    "credits_delta": int(e.get("credits_delta") or 0),
                    "water_delta": int(e.get("water_delta") or 0),
                    "debt_delta": int(e.get("debt_delta") or 0),
                    "note": str(e.get("note") or ""),
                    "top_loot_json": e.get("top_loot_json"),
                    "created_at": str(e.get("created_at") or ""),
                }
            )
        explores_payload_cache[gid] = out
        return out

    guild_envs: List[Dict[str, Any]] = []
    for gid in guild_ids:
        gid_i = int(gid)
        g_obj = None
        try:
            g_obj = bot.get_guild(gid_i)
        except Exception:
            g_obj = None

        debt = get_guild_debt(gid_i)

        # Weekly snapshots
        weekly = {
            "week_key": week_key,
            "debt_summary": get_weekly_debt_summary(gid_i, week_key),
            "points_ranking": get_weekly_points_ranking(gid_i, week_key),
        }

        incidents_raw = list_recent_aby_incidents(gid_i, limit=80)
        incidents_payload: List[Dict[str, Any]] = []
        for inc in incidents_raw:
            iid = int(inc.get("id") or 0)
            incidents_payload.append(
                {
                    "source_id": f"incident:{iid}",
                    "incident_type": str(inc.get("kind") or ""),
                    "title": str(inc.get("title") or ""),
                    "severity": "normal",
                    "summary": str(inc.get("description") or ""),
                    "detail": str(inc.get("description") or ""),
                    "debt_delta": int(inc.get("delta_debt") or 0),
                    "created_at": str(inc.get("created_at") or ""),
                }
            )

        guild_envs.append(
            {
                "guild_id": gid_i,
                "source_id": f"yume:{now_ts}",
                "timestamp": datetime.now(tz=KST).isoformat(),
                "guild": {
                    "guild_id": gid_i,
                    "guild_name": str(getattr(g_obj, "name", "") or ""),
                    "debt": int(debt.get("debt_credits") or 0),
                    "interest_rate": float(debt.get("interest_rate") or 0.0),
                    "interest_total": int(debt.get("interest_total") or 0),
                    "last_interest_ymd": str(debt.get("last_interest_applied") or ""),
                    "updated_at": int(debt.get("updated_at") or 0),
                },
                "world": {
                    "weather": str(world.get("weather") or "clear"),
                    "weather_changed_at": int(world.get("weather_changed_at") or 0),
                    "weather_next_change_at": int(world.get("weather_next_change_at") or 0),
                },
                "users": users_for_guild(gid_i),
                "explore_logs": explores_for_guild(gid_i),
                "incidents": incidents_payload,
                "weekly": weekly,
            }
        )

    return {
        "generated_at": now_ts,
        "guilds": guild_envs,
    }


async def post_sync_payload(bot: discord.Client) -> bool:
    """POST one snapshot. Returns True on success."""

    url, token = _read_env()
    if not url or not token:
        return False

    payload = build_sync_payload(bot)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "yume-bot/aby-sync",
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
