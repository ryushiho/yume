# AdminCog: 유메 감정 상태 조회, 채팅 청소, /유메전달 등 관리자·개발자용 기능 모음

import discord
from discord.ext import commands
from discord import app_commands

# 너의 디스코드 사용자 ID (OWNER)
OWNER_ID = 1433962010785349634


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

    # ===== 프리픽스: !유메상태 =====

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

        # 코어 상태 (전역 mood/irritation)
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

        # 이 유저에 대한 호감도 (-100 ~ 100)
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

        await ctx.send(embed=embed)

    # ===== 프리픽스: !청소 =====

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

        # 권한 체크: OWNER이거나, manage_messages 권한이 있어야 함
        if ctx.author.id != OWNER_ID and not ctx.author.guild_permissions.manage_messages:
            await ctx.send(
                "이 채널을 치울 권한은 없는 것 같아. 관리자나 유메 개발자만 가능해.",
                delete_after=5,
            )
            return

        # 안전 장치: 최소 1, 최대 100개까지만
        amount = max(1, min(100, amount))

        # 이 커맨드 메시지까지 포함해서 amount+1개 삭제
        deleted = await ctx.channel.purge(limit=amount + 1)
        # 실제 지워진 메시지 개수에서 커맨드 1개 빼기
        count = max(0, len(deleted) - 1)

        msg = await ctx.send(f"{count}개 정도… 정리해 뒀어.")
        await msg.delete(delay=5)

    # ===== 슬래시: /유메전달 =====

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
        # ★ 권한 제한 제거: 이제 누구나 사용 가능
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
