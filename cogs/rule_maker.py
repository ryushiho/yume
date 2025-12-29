"""cogs/rule_maker.py

Phase3: "아비도스 교칙 제정 위원회".

Features
- !교칙 : 오늘의 교칙(08:00 KST 이후 자동 생성/공지되는 것)을 보여줌
- !교칙건의 <내용> : 유저가 엉뚱한 교칙을 건의 (DB 저장)
- !교칙채널 [#채널] : (관리자) 매일 교칙 공지 채널을 지정/확인
- !교칙생성 : (관리자) 오늘 교칙을 즉시 생성/공지 (테스트용)

Notes
- 실제 자동 공지는 yume_runtime.py의 background loop가 담당.
- 이 Cog는 수동 조회/설정/테스트를 제공.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord.ext import commands

from yume_honorific import get_honorific
from yume_llm import generate_daily_rule
from yume_send import send_ctx, send_channel
from yume_store import (
    ensure_daily_rule_row,
    get_config,
    get_daily_rule,
    get_recent_rule_suggestions,
    get_world_state,
    mark_daily_rule_posted,
    save_rule_suggestion,
    set_config,
    update_daily_rule_text,
)


KST = timezone(timedelta(hours=9))

WEATHER_LABEL = {
    "clear": "맑음",
    "cloudy": "흐림",
    "sandstorm": "대형 모래폭풍",
}


def _now_kst() -> datetime:
    return datetime.now(tz=KST)


def _clean_channel_id(x: str) -> Optional[int]:
    raw = (x or "").strip()
    if not raw:
        return None

    # <#123>
    if raw.startswith("<#") and raw.endswith(">"):
        raw = raw[2:-1].strip()

    try:
        cid = int(raw)
        return cid if cid > 0 else None
    except Exception:
        return None


def _is_admin(ctx: commands.Context) -> bool:
    try:
        if ctx.guild is None:
            return False
        perms = getattr(ctx.author, "guild_permissions", None)
        if perms and getattr(perms, "administrator", False):
            return True
    except Exception:
        pass
    return False


class RuleMakerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="교칙")
    async def cmd_rule(self, ctx: commands.Context):
        """Show today's rule (and generate it if missing after 08:00 KST)."""

        honorific = get_honorific(ctx.author, ctx.guild)
        now_kst = _now_kst()
        date_ymd = now_kst.date().isoformat()

        row = get_daily_rule(date_ymd)

        if (now_kst.hour, now_kst.minute) < (8, 0) and not row:
            await send_ctx(
                ctx,
                f"{honorific}~ 아직 교칙 발표 시간이 아니야! (매일 08:00에 발표해~ 에헤헤)",
            )
            return

        # Ensure exists (assign rule_no)
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

        await send_ctx(
            ctx,
            f"📢 오늘의 아비도스 교칙 (제 {rule_no}조)\n\n{rule_text}",
        )

    @commands.command(name="교칙건의")
    async def cmd_rule_suggest(self, ctx: commands.Context, *, content: str = ""):
        """Suggest a silly rule."""

        honorific = get_honorific(ctx.author, ctx.guild)
        content = (content or "").strip()
        if not content:
            await send_ctx(ctx, f"{honorific}~ 건의 내용도 같이 적어줘야지! 예: `!교칙건의 모래바람 불면 모자를 꼭 쓴다`")
            return

        save_rule_suggestion(
            user_id=int(ctx.author.id),
            guild_id=int(ctx.guild.id) if ctx.guild else None,
            content=content,
        )

        await send_ctx(ctx, f"와아! 그거 좋은데? 임시 교칙으로 수첩에 적어둘게~ 에헤헤")

    @commands.command(name="교칙채널")
    async def cmd_rule_channel(self, ctx: commands.Context, channel: str = ""):
        """Get or set the rule announcement channel."""

        honorific = get_honorific(ctx.author, ctx.guild)
        if not channel:
            v = get_config("rule_channel_id")
            if v:
                await send_ctx(ctx, f"현재 교칙 공지 채널은 <#{v}> 이야! (ID: {v})")
            else:
                await send_ctx(ctx, f"아직 교칙 공지 채널이 설정되지 않았어. 관리자면 `!교칙채널 #채널`로 지정해줘~")
            return

        if not _is_admin(ctx):
            await send_ctx(ctx, f"{honorific}~ 이건 관리자만 바꿀 수 있어! (학교 규칙은 중요하니까…)")
            return

        cid = _clean_channel_id(channel)
        if not cid:
            await send_ctx(ctx, "채널을 제대로 지정해줘~ 예: `!교칙채널 #공지` 혹은 `!교칙채널 1234567890`")
            return

        set_config("rule_channel_id", str(cid))
        await send_ctx(ctx, f"오케이! 이제 교칙은 <#{cid}> 채널에 매일 08:00에 올릴게~")

    @commands.command(name="교칙생성")
    async def cmd_rule_force(self, ctx: commands.Context):
        """(Admin) Force-generate & announce today's rule now."""

        honorific = get_honorific(ctx.author, ctx.guild)
        if not _is_admin(ctx):
            await send_ctx(ctx, f"{honorific}~ 이건 관리자만 할 수 있어! (교칙 위원회 회의는 비밀이야~)")
            return

        now_kst = _now_kst()
        date_ymd = now_kst.date().isoformat()
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

        # Determine announcement channel
        channel_id = _clean_channel_id(get_config("rule_channel_id") or "")
        target = None
        if channel_id:
            target = self.bot.get_channel(channel_id)
            if target is None:
                try:
                    target = await self.bot.fetch_channel(channel_id)  # type: ignore[assignment]
                except Exception:
                    target = None

        if target is None:
            # fallback: current channel
            target = ctx.channel

        msg = f"📢 오늘의 아비도스 교칙 (제 {rule_no}조)\n\n{rule_text}"
        await send_channel(target, msg, allow_glitch=True)

        if channel_id:
            mark_daily_rule_posted(date_ymd, channel_id=int(channel_id))

        await send_ctx(ctx, "완료! 오늘 교칙을 발표했어~")


async def setup(bot: commands.Bot):
    await bot.add_cog(RuleMakerCog(bot))
