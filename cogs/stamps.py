"""cogs/stamps.py

Phase5: '참 잘했어요! 도장판'

- !도장 : 현재 도장/칭호 확인
- !도장 on/off : 도장 시스템 켜기/끄기

도장 지급은 1일 1회(KST)이며,
명령어 실행 완료 시 on_command_completion에서 자동 시도한다.
(프리토킹은 cogs.yume_chat 쪽에서 별도 처리)
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from yume_honorific import get_honorific
from yume_send import send_ctx
from yume_store import get_user_settings, set_stamps_opt_in
from yume_stamps import maybe_award_stamp_ctx

logger = logging.getLogger(__name__)


class StampsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="도장", aliases=["도장판", "스탬프", "stamp"])
    async def cmd_stamp(self, ctx: commands.Context, arg: Optional[str] = None):
        """Show/toggle stamps."""
        if ctx.author.bot:
            return

        # Toggle
        if arg:
            a = arg.strip().lower()
            if a in ("on", "켜", "켜기", "enable", "enabled", "1", "true"):
                set_stamps_opt_in(int(ctx.author.id), True)
                await send_ctx(ctx, "응. 도장판 다시 켜둘게.", allow_glitch=True)
                return
            if a in ("off", "끄", "끄기", "disable", "disabled", "0", "false"):
                set_stamps_opt_in(int(ctx.author.id), False)
                await send_ctx(ctx, "알겠어. 도장판은 잠깐 쉬게 해둘게.", allow_glitch=True)
                return

        settings = get_user_settings(int(ctx.author.id))
        honorific = get_honorific(ctx.author, ctx.guild)
        stamps = int(settings.get("stamps", 0))
        title = (settings.get("stamp_title") or "").strip()
        opt_in = int(settings.get("stamps_opt_in", 1))

        lines = [
            f"{honorific} 도장판!",
            f"- 도장: **{stamps}개**",
            f"- 상태: {'ON' if opt_in else 'OFF'}",
        ]
        if title:
            lines.append(f"- 현재 칭호: **{title}**")
        lines.append("\n설정: `!도장 on` / `!도장 off`")

        await send_ctx(ctx, "\n".join(lines), allow_glitch=True)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        try:
            if not isinstance(ctx, commands.Context):
                return
            if ctx.author.bot:
                return
            # Prefix 명령어에만 도장 시도
            if not ctx.invoked_with:
                return

            await maybe_award_stamp_ctx(ctx, reason=f"command:{ctx.invoked_with}")
        except Exception:
            # 절대 여기서 봇이 죽으면 안 됨
            logger.exception("on_command_completion stamp error")


async def setup(bot: commands.Bot):
    await bot.add_cog(StampsCog(bot))

