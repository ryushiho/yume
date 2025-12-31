
import datetime

import discord
from discord.ext import commands
from discord import app_commands

from yume_store import get_world_state
from yume_websync import post_sync_payload
from yume_presence import apply_random_presence

OWNER_ID = 1433962010785349634

KST = datetime.timezone(datetime.timedelta(hours=9))


def _fmt_kst(ts: int) -> str:
    if not ts:
        return "-"
    dt = datetime.datetime.fromtimestamp(int(ts), tz=KST)
    return dt.strftime("%m/%d %H:%M")


class AdminCog(commands.Cog):
    """관리자/개발자용 기능: 유틸 + 유메 상태 조회"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _core(self):
        """
        YumeAI 코어 (호감도/전역 상태 엔진).
        setup_yume_ai(bot) 에서 bot.yume_core 로 심어둔다.
        """
        return getattr(self.bot, "yume_core", None)


    @commands.command(name="유메상태")
    async def yume_status(self, ctx: commands.Context):
        """
        현재 유메 전역 상태 + 이 유저에 대한 호감도를 보여주는 디버그용 커맨드.
        - OWNER만 사용 가능.
        - yume_core (YumeCore)를 기준으로 mood/irritation/affection 을 숫자로 표시한다.
        """
        if ctx.author.id != OWNER_ID:
            await ctx.send(
                "이건… 유메랑 개발자만 보는 비밀 리포트라서, 미안해.",
                delete_after=5,
            )
            return

        core = self._core()
        if core is None:
            await ctx.send(
                "감정 엔진을 아직 안 켜 둬서… 유메 상태를 제대로 보여줄 수가 없어.",
                delete_after=8,
            )
            return

        try:
            core_state = core.get_core_state()
        except Exception:
            await ctx.send(
                "상태를 읽다가 살짝 꼬였어. 나중에 다시 시도해줄래?",
                delete_after=8,
            )
            return

        mood = float(core_state.get("mood", 0.0))              # -1.0 ~ 1.0
        irritation = float(core_state.get("irritation", 0.0))  # 0.0 ~ 1.0

        user_id = str(ctx.author.id)
        try:
            affection = float(core.get_affection(user_id))
            stage = core.get_affection_stage(user_id)
        except Exception:
            affection = 0.0
            stage = "normal"

        name = getattr(ctx.author, "display_name", "누구야")

        embed = discord.Embed(
            title="유메 상태 리포트… 같은 거.",
            description=f"{name} 기준으로 정리해봤어.",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="기분(mood)",
            value=f"`{mood:+.2f}`",
            inline=False,
        )
        embed.add_field(
            name="짜증(irritation)",
            value=f"`{irritation:+.2f}`",
            inline=False,
        )
        embed.add_field(
            name=f"{name}에 대한 호감도(affection)",
            value=f"`{affection:+.1f}` (stage: `{stage}`)",
            inline=False,
        )

        # Phase0: show virtual world state (weather) for debugging.
        try:
            world = get_world_state()
            w = str(world.get("weather") or "clear")
            changed_at = int(world.get("weather_changed_at") or 0)
            next_at = int(world.get("weather_next_change_at") or 0)
            embed.add_field(
                name="아비도스 환경(가상 날씨)",
                value=(
                    f"weather: `{w}`\n"
                    f"changed_at(KST): `{_fmt_kst(changed_at)}`\n"
                    f"next_change_at(KST): `{_fmt_kst(next_at)}`"
                ),
                inline=False,
            )
        except Exception:
            pass

        await ctx.send(embed=embed)


    @commands.command(name="상태갱신", aliases=["상태변경"])
    async def refresh_presence(self, ctx: commands.Context):
        """유메 디스코드 상태(프레즌스)를 즉시 랜덤 갱신한다. (OWNER 전용)"""
        if ctx.author.id != OWNER_ID:
            await ctx.send("이건… 유메랑 개발자만 쓰는 스위치야.", delete_after=5)
            return

        try:
            result = await apply_random_presence(self.bot)
            text = str(result.get("text") or "").strip()
            await ctx.send(f"✅ 상태 갱신 완료: {text}", delete_after=10)
        except Exception:
            await ctx.send("❌ 상태 갱신 중 오류가 났어…", delete_after=10)

    @commands.command(name="청소")
    async def clean_messages(self, ctx: commands.Context, amount: int):
        """
        !청소 <숫자>
        - 해당 채널에서 최근 메시지를 <숫자>개만큼 지운다.
        - OWNER 또는 '메시지 관리' 권한이 있는 사람만 사용 가능.
        """
        if ctx.guild is None:
            await ctx.send("여긴 DM이라, 치울 채팅이 없는데…", delete_after=5)
            return

        if ctx.author.id != OWNER_ID and not ctx.author.guild_permissions.manage_messages:
            await ctx.send(
                "이 채널을 치울 권한은 없는 것 같아. 관리자나 유메 개발자만 가능해.",
                delete_after=5,
            )
            return

        amount = max(1, min(100, amount))

        deleted = await ctx.channel.purge(limit=amount + 1)
        count = max(0, len(deleted) - 1)

        msg = await ctx.send(f"{count}개 정도… 정리해 뒀어.")
        await msg.delete(delay=5)



    @commands.command(name="아비동기화")
    async def aby_sync(self, ctx: commands.Context):
        """아비도스(탐사/주간포인트/사건/빚) 상태를 웹 패널로 즉시 동기화합니다."""
        # 오너는 항상 허용. 그 외에는 서버 내 "서버 관리" 권한자만 허용(운영 편의).
        if ctx.author.id != OWNER_ID:
            perms = getattr(ctx.author, "guild_permissions", None)
            if perms is None or not perms.manage_guild:
                await ctx.reply("권한이 없어요. (서버 관리 권한 필요)")
                return

        ok = await post_sync_payload(self.bot)
        if ok:
            await ctx.reply("✅ 아비도스 상태를 웹에 동기화했어요.")
        else:
            await ctx.reply("⚠️ 동기화에 실패했어요. 서버 로그를 확인해줘…")

    @app_commands.command(
        name="유메전달",
        description="유메가 대신 특정 채널로 메시지를 전달할게요.",
    )
    @app_commands.describe(
        channel="메시지를 보낼 텍스트 채널을 선택해줘.",
        content="유메가 대신 보낼 메시지 내용을 적어줘.",
    )
    async def yume_deliver(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        content: str,
    ):
        try:
            await channel.send(content)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"{channel.mention} 에는 말을 걸 권한이 없어.",
                ephemeral=True,
            )
            return
        except Exception:
            await interaction.response.send_message(
                "메시지를 보내는 중에, 알 수 없는 오류가 났어.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ {channel.mention} (`{channel.id}`) 로 전달해 뒀어.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))