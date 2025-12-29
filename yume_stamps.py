"""yume_stamps.py

Phase5: '참 잘했어요! 도장판' (Gamification)

Design:
- 1일 1회(유저당) 도장 지급 (KST 기준)
- 도장은 유메와의 상호작용이 있었을 때만 지급 (명령어/대화)
- 10개 단위 마일스톤 도달 시: 칭호 + 손편지(LLM) DM

주의:
- 절대 오류로 봇이 멈추면 안 됨 -> 내부에서 모든 예외를 잡아먹는다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands

from yume_honorific import get_honorific
from yume_send import reply_message, send_ctx, send_channel
from yume_store import (
    add_stamp_event,
    add_stamp_reward,
    get_user_settings,
    set_stamp_state,
)
from yume_llm import generate_stamp_reward_letter

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


TITLE_BY_LEVEL = [
    "명예 아비도스 부회장",
    "사막 축제 홍보부장",
    "오아시스 탐사 선임요원",
    "모래폭풍 대책위원장",
    "아비도스 재건 특별고문",
]


def _kst_ymd(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=KST).strftime("%Y-%m-%d")
    except Exception:
        return "1970-01-01"


def _pick_title(level: int) -> str:
    if level <= 0:
        return ""
    if level <= len(TITLE_BY_LEVEL):
        return TITLE_BY_LEVEL[level - 1]
    return f"아비도스 특별 후원자 #{level}"


@dataclass
class StampResult:
    awarded: bool
    stamps: int
    milestone: int  # 0 or 10/20/...
    title: str
    dm_sent: bool


def _try_award_stamp_core(
    *,
    user_id: int,
    guild_id: int,
    reason: str,
) -> Optional[dict]:
    """Return updated state dict if a stamp was awarded, else None.

    This is pure-ish (DB only), no Discord I/O.
    """

    now = int(time.time())
    st = get_user_settings(user_id)

    if not st.get("stamps_opt_in", 1):
        return None

    last_stamp_at = int(st.get("last_stamp_at") or 0)
    if _kst_ymd(last_stamp_at) == _kst_ymd(now):
        return None

    new_stamps = int(st.get("stamps") or 0) + 1

    # Persist
    set_stamp_state(user_id, stamps=new_stamps, last_stamp_at=now)
    add_stamp_event(
        user_id=user_id,
        guild_id=guild_id,
        reason=reason,
        delta=1,
        stamps_after=new_stamps,
    )

    # Milestone
    rewarded_level = int(st.get("stamps_rewarded") or 0)
    level = new_stamps // 10
    milestone = 0
    title = str(st.get("stamp_title") or "")

    if new_stamps % 10 == 0 and level > rewarded_level:
        milestone = new_stamps
        title = _pick_title(level)
        set_stamp_state(
            user_id,
            stamps_rewarded=level,
            stamp_title=title,
            last_reward_at=now,
        )

    return {
        "now": now,
        "stamps": new_stamps,
        "milestone": milestone,
        "title": title,
        "dm_opt_in": int(st.get("dm_opt_in") or 0),
    }


async def maybe_award_stamp_ctx(
    ctx: commands.Context,
    *,
    reason: str,
    allow_glitch: bool = True,
) -> None:
    """Award a daily stamp for a command-based interaction."""

    try:
        if ctx.author.bot:
            return
        guild_id = ctx.guild.id if ctx.guild else 0
        res = _try_award_stamp_core(
            user_id=ctx.author.id,
            guild_id=guild_id,
            reason=reason,
        )
        if not res:
            return

        honorific = get_honorific(ctx.author, ctx.guild)
        stamps = int(res["stamps"])

        await send_ctx(
            ctx,
            f"{honorific}~ 참 잘했어요! 도장 꾹 💮 (현재 **{stamps}개**)",
            allow_glitch=allow_glitch,
        )

        if res["milestone"]:
            await _handle_milestone(
                user=ctx.author,
                guild=ctx.guild,
                channel=ctx.channel,
                honorific=honorific,
                milestone=int(res["milestone"]),
                title=str(res["title"]),
                dm_opt_in=bool(res["dm_opt_in"]),
                guild_id=guild_id,
                allow_glitch=allow_glitch,
            )
    except Exception:
        logger.exception("stamps: maybe_award_stamp_ctx failed")


async def maybe_award_stamp_message(
    message: discord.Message,
    *,
    reason: str,
    allow_glitch: bool = True,
) -> None:
    """Award a daily stamp for a free-talk interaction."""

    try:
        if message.author.bot:
            return
        guild = message.guild
        guild_id = guild.id if guild else 0
        res = _try_award_stamp_core(
            user_id=message.author.id,
            guild_id=guild_id,
            reason=reason,
        )
        if not res:
            return

        honorific = get_honorific(message.author, guild)
        stamps = int(res["stamps"])

        await reply_message(
            message,
            f"{honorific}~ 참 잘했어요! 도장 꾹 💮 (현재 **{stamps}개**)",
            allow_glitch=allow_glitch,
        )

        if res["milestone"]:
            await _handle_milestone(
                user=message.author,
                guild=guild,
                channel=message.channel,
                honorific=honorific,
                milestone=int(res["milestone"]),
                title=str(res["title"]),
                dm_opt_in=bool(res["dm_opt_in"]),
                guild_id=guild_id,
                allow_glitch=allow_glitch,
            )
    except Exception:
        logger.exception("stamps: maybe_award_stamp_message failed")


async def _handle_milestone(
    *,
    user: discord.abc.User,
    guild: Optional[discord.Guild],
    channel: discord.abc.Messageable,
    honorific: str,
    milestone: int,
    title: str,
    dm_opt_in: bool,
    guild_id: int,
    allow_glitch: bool,
) -> None:
    """DM letter + record reward; never raise."""

    try:
        # weather label (best-effort)
        try:
            from yume_store import get_world_state

            world = get_world_state()
            weather_label = str(world.get("weather") or "맑음")
        except Exception:
            weather_label = "맑음"

        letter = await generate_stamp_reward_letter(
            honorific=honorific,
            user_display_name=getattr(user, "display_name", "") or getattr(user, "name", "user"),
            milestone=milestone,
            title=title,
            weather_label=weather_label,
        )

        add_stamp_reward(
            user_id=int(user.id),
            guild_id=int(guild_id or 0),
            milestone=milestone,
            title=title,
            letter=letter,
        )

        dm_sent = False
        if dm_opt_in:
            try:
                await user.send(letter)
                dm_sent = True
            except Exception:
                dm_sent = False

        # announce
        if dm_sent:
            msg = f"도장 **{milestone}개** 달성! 🎉 선물로 '**{title}**' 임명장을 DM으로 보냈어."
        else:
            msg = (
                f"도장 **{milestone}개** 달성! 🎉 선물로 '**{title}**' 임명장을 적어뒀는데…"
                "DM이 막혀있는 것 같아. (원하면 `!설정 dm on`으로 켜줘)"
            )

        await send_channel(channel, msg, allow_glitch=allow_glitch)

        # If DM failed, show a short version in-channel (safe)
        if not dm_sent:
            short = letter
            if len(short) > 900:
                short = short[:900].rstrip()
            await send_channel(channel, f"```\n{short}\n```", allow_glitch=False)

    except Exception:
        logger.exception("stamps: milestone handling failed")
