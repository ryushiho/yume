# AdminCog: DM에서 옛 블루전 기분 조절, 유메 감정 상태 조회, 채팅 청소, /유메전달 등 관리자·개발자용 기능 모음

import discord
from discord.ext import commands
from discord import app_commands

OWNER_ID = 1433962010785349634  # 너의 디스코드 사용자 ID

# /유메전달 사용 가능 계정
YUME_DELIVER_ALLOWED_IDS = {
    1433962010785349634,
}


class AdminCog(commands.Cog):
    """관리자/개발자용 기능: DM에서 옛 기분 조절 + 유틸 + 유메 상태 조회"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ===== yume_ai 연동 헬퍼 =====

    def _core(self):
        """YumeAI 코어 (감정/관계 엔진). 없으면 None."""
        return getattr(self.bot, "yume_core", None)

    # ===== DM 전용: !유메기분 (옛 BlueWar 기반 – 있으면 쓰고, 없으면 안내만) =====

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 다른 봇 / 서버 채팅은 무시
        if message.author.bot:
            return
        if message.guild is not None:
            return  # 서버 채팅은 ReactionsCog 쪽에서 처리

        # DM + OWNER만 허용
        if message.author.id != OWNER_ID:
            return

        content = message.content.strip()
        if not content.startswith("!유메기분"):
            return

        cog = self.bot.get_cog("BlueWarCog")
        if cog is None or not hasattr(cog, "mood"):
            await message.channel.send(
                "지금은 예전 블루전 기분 시스템을 안 올려 둬서,\n"
                "`!유메기분`은 잠깐 쉬는 중이야."
            )
            return

        parts = content.split(maxsplit=1)

        # 조회만
        if len(parts) == 1:
            current = cog.mood
            await message.channel.send(
                f"지금 유메 기분 값은 {current}야. 범위는 -3 ~ 3이야."
            )
            return

        arg = parts[1].strip()

        if arg in ("초기화", "reset", "리셋"):
            new_mood = 0
        else:
            try:
                value = int(arg)
            except ValueError:
                await message.channel.send(
                    "숫자나 '초기화'처럼 간단하게 말해줘.\n예: `!유메기분 2`"
                )
                return
            new_mood = max(-3, min(3, value))

        cog.mood = new_mood
        await message.channel.send(
            f"유메 기분 값을 {new_mood}로 맞춰 뒀어.\n오늘은 이 정도 느낌으로 움직일게."
        )

    # ===== 프리픽스: !유메상태 =====

    @commands.command(name="유메상태")
    async def yume_status(self, ctx: commands.Context):
        """
        현재 유메 감정/관계 상태를 보여주는 디버그용 커맨드.
        - 감정 코어(yume_core)가 없으면 안내만.
        - OWNER만 사용 가능.
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

        # 코어 상태
        try:
            core_state = core.get_core_state()
        except Exception:
            await ctx.send(
                "상태를 읽다가 살짝 꼬였어. 나중에 다시 시도해줄래?",
                delete_after=8,
            )
            return

        mood = float(core_state.get("mood", 0.0))
        energy = float(core_state.get("energy", 0.0))
        affection = float(core_state.get("affection", 0.0))
        irritation = float(core_state.get("irritation", 0.0))

        # 이 유저 / 길드 기준 관계
        user_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id) if ctx.guild else None
        try:
            rel = core.get_relation_summary(user_id=user_id, guild_id=guild_id)
        except Exception:
            rel = {"user": None, "guild": None}

        user_rel = rel.get("user") or {}
        bond = float(user_rel.get("bond", 0.0))
        trust = float(user_rel.get("trust", 0.0))

        # 수치를 말로 풀어주는 간단한 매핑들
        def mood_label(x: float) -> str:
            if x > 0.5:
                return "오늘 유메 기분, 꽤 좋아."
            if x > 0.2:
                return "조금은 괜찮은 편이야."
            if x < -0.5:
                return "솔직히 많이 다운돼 있어."
            if x < -0.2:
                return "살짝 기분이 안 좋아."
            return "평소랑 비슷해."

        def energy_label(x: float) -> str:
            if x > 0.4:
                return "지금은 나름 텐션 있어."
            if x > 0.1:
                return "적당히 움직일 수 있는 정도?"
            if x < -0.4:
                return "엄청 피곤해. 이불이랑 결혼하고 싶어."
            if x < -0.1:
                return "조금 졸려."
            return "보통 정도야."

        def irritation_label(x: float) -> str:
            if x > 0.6:
                return "솔직히 말하면 꽤 짜증난 상태야."
            if x > 0.3:
                return "조금 거슬리는 게 많아."
            if x < 0.1:
                return "그렇게 신경 쓰이는 건 없어."
            return "애매하게 신경 쓰이는 정도?"

        def bond_label(x: float, t: float) -> str:
            # 유저와의 관계 한 줄 요약
            if x > 0.6 and t > 0.4:
                return "…너랑은 꽤 많이 붙어 다니는 사이랄까."
            if x > 0.3:
                return "익숙한 편이야. 같이 있는 게 편하거든."
            if x < -0.4:
                return "솔직히 좀 마이너스 감정 쪽이 커."
            if x < -0.1:
                return "아주 친한 쪽은 아니야."
            return "아직은 관찰 중… 정도?"

        name = getattr(ctx.author, "display_name", "누구야")

        embed = discord.Embed(
            title="유메 상태 리포트… 같은 거.",
            description=f"{name} 기준으로 정리해봤어.",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="기분(mood)",
            value=f"{mood_label(mood)}\n`{mood:+.2f}`",
            inline=False,
        )
        embed.add_field(
            name="에너지(energy)",
            value=f"{energy_label(energy)}\n`{energy:+.2f}`",
            inline=False,
        )
        embed.add_field(
            name="짜증(irritation)",
            value=f"{irritation_label(irritation)}\n`{irritation:+.2f}`",
            inline=False,
        )
        embed.add_field(
            name="전체 호감도(affection)",
            value=f"`{affection:+.2f}`",
            inline=False,
        )

        embed.add_field(
            name=f"{name}랑의 관계",
            value=f"{bond_label(bond, trust)}\n"
                  f"`bond: {bond:+.2f} / trust: {trust:+.2f}`",
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
                "여기 치우는 건, 권한 있는 쪽이 하는 게 좋아.",
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
        description="유메가 대신 특정 채널로 메시지를 전달할게요. (개발자 전용)",
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
        # 호출자 권한 체크
        if interaction.user.id not in YUME_DELIVER_ALLOWED_IDS:
            await interaction.response.send_message(
                "이 명령어는 개발자랑, 유메가 허락한 몇 명만 쓸 수 있어.",
                ephemeral=True,
            )
            return

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
