# FeedbackCog: 유저 건의사항을 슬래시/텍스트로 받아서 개발자 DM으로 보내고, 유메 감정·기록에 반영하는 기능

from __future__ import annotations

import datetime
import logging
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

logger = logging.getLogger(__name__)

# 개발자(너) 디스코드 사용자 ID
DEV_USER_ID = 1433962010785349634


class FeedbackModal(discord.ui.Modal):
    """슬래시 버전 건의사항 입력 UI"""

    def __init__(self, cog: "FeedbackCog", interaction: discord.Interaction):
        super().__init__(title="📨 유메에게 건의하기")
        self.cog = cog
        self.interaction = interaction

        self.username_input = discord.ui.TextInput(
            label="이름 / 닉네임",
            placeholder="예: 검은갈매기 / 적고 싶은 이름",
            max_length=50,
            required=True,
        )
        self.add_item(self.username_input)

        self.text = discord.ui.TextInput(
            label="건의하고 싶은 내용을 입력해주세요.",
            placeholder="예: 블루전에서 이런 기능 있었으면 좋겠어요.",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        nickname = self.username_input.value.strip()
        content = self.text.value.strip()
        await self.cog.process_feedback(interaction, nickname, content)


class FeedbackCog(commands.Cog):
    """유저 건의사항을 개발자 DM으로 보내고, 유메 감정/기록에 반영하는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -------------------------------
    # yume_ai 연동 헬퍼
    # -------------------------------
    def _core(self):
        """YumeAI 코어 (감정/관계 엔진). 없으면 None."""
        return getattr(self.bot, "yume_core", None)

    def _speaker(self):
        """YumeSpeaker (말투 엔진). 없으면 None."""
        return getattr(self.bot, "yume_speaker", None)

    def _memory(self):
        """YumeMemory (일기장/로그). 없으면 None."""
        return getattr(self.bot, "yume_memory", None)

    async def get_dev_user(self) -> Optional[discord.User]:
        user = self.bot.get_user(DEV_USER_ID)
        if user:
            return user

        try:
            return await self.bot.fetch_user(DEV_USER_ID)
        except Exception as e:
            logger.error("개발자 유저 fetch 실패: %s", e)
            return None

    # -------------------------------
    # 유메 말투 / 감정 반영
    # -------------------------------
    def _speak_feedback_received(
        self,
        *,
        user: discord.abc.User,
    ) -> str:
        """
        건의가 정상적으로 전달되었을 때,
        유메의 입으로 감사 멘트를 만들어주는 헬퍼.
        """
        speaker = self._speaker()
        base_fallback = (
            "건의는 유메가 전부 정리해서 개발자한테 넘겨둘게. "
            "이렇게 신경 써줘서 고마워, 으헤~ 💙"
        )

        if speaker is None:
            # AI 시스템이 초기화되지 않은 경우 안전한 기본값
            return base_fallback

        is_dev = user.id == DEV_USER_ID
        try:
            msg = speaker.say(
                "feedback_received",
                user_id=user.id,
                user_name=getattr(user, "display_name", None),
                is_dev=is_dev,
            )
            # 프롬프트에서 못 만들어줬거나 비어 있으면 기본 멘트로
            return msg or base_fallback
        except Exception as e:  # 혹시 모를 예외 방지
            logger.error("YumeSpeaker feedback_received 오류: %s", e)
            return base_fallback

    def _log_today_feedback(
        self,
        interaction_user: discord.abc.User,
        *,
        content: str,
        guild_name: str,
    ) -> None:
        """
        오늘의 기록에 '건의사항 도착'을 남기고,
        감정 엔진에도 '좋은 상호작용'으로 반영.
        """
        # 1) 메모리 로그
        mem = self._memory()
        if mem is not None:
            try:
                mem.log_today(
                    f"건의사항 도착: from {interaction_user} ({interaction_user.id}) "
                    f"@ {guild_name} | 내용 일부: {content[:80]!r}"
                )
            except Exception as e:
                logger.error("오늘 기록(log_today) 중 오류: %s", e)

        # 2) 감정 엔진 이벤트
        core = self._core()
        if core is not None:
            try:
                core.apply_event(
                    "feedback_sent",
                    user_id=str(interaction_user.id),
                    guild_id=None,  # 길드는 따로 중요하진 않으니 생략
                    weight=1.2,
                )
            except Exception as e:
                logger.error("YumeAI feedback_sent 이벤트 반영 실패: %s", e)

    # -------------------------------
    # 공통 처리
    # -------------------------------
    async def process_feedback(
        self,
        interaction: discord.Interaction,
        nickname: str,
        content: str,
    ):
        """슬래시 건의사항 공통 처리"""

        dev_user = await self.get_dev_user()
        if not dev_user:
            await interaction.response.send_message(
                "음… 지금은 건의사항을 받아줄 개발자가 안 보이네.\n"
                "유메 혼자서는 여기까지만 할 수 있어~",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📬 새 건의사항 도착!",
            color=discord.Color.orange(),
            timestamp=datetime.datetime.utcnow(),
        )

        embed.add_field(name="📌 입력한 이름", value=nickname, inline=False)
        embed.add_field(
            name="👤 디스코드 사용자",
            value=f"{interaction.user} (`{interaction.user.id}`)",
            inline=False,
        )
        embed.add_field(name="💬 건의 내용", value=content, inline=False)

        guild_info = interaction.guild.name if interaction.guild else "DM"
        embed.add_field(name="📍 서버", value=guild_info, inline=False)

        try:
            await dev_user.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                "개발자 쪽 DM 문이 닫혀 있어서… 유메가 직접 전달을 못 하겠어.",
                ephemeral=True,
            )
            return

        # 유메 메모리 / 감정에 "좋은 상호작용 + 건의 도착" 기록
        self._log_today_feedback(
            interaction.user,
            content=content,
            guild_name=guild_info,
        )

        # 유메스러운 감사 멘트 생성
        reply_text = self._speak_feedback_received(user=interaction.user)

        await interaction.response.send_message(
            reply_text,
            ephemeral=True,
        )

    # -------------------------------
    # 텍스트 명령어: !건의사항
    # -------------------------------
    @commands.command(name="건의사항")
    async def text_feedback(self, ctx: commands.Context, *, content: str = None):
        """
        !건의사항 [내용]
        - 간단히 텍스트로도 건의 보낼 수 있는 버전.
        - 닉네임은 디스코드 이름 기준으로.
        """
        if not content:
            await ctx.send(
                "사용법: `!건의사항 [내용]`\n"
                "조금만 구체적으로 써주면 유메가 정리하기 편해."
            )
            return

        dev_user = await self.get_dev_user()
        if not dev_user:
            await ctx.send(
                "지금은 개발자 쪽 DM이 안 잡혀서, 유메가 대신 전달을 못 하겠어."
            )
            return

        embed = discord.Embed(
            title="📬 새 건의사항",
            color=discord.Color.orange(),
            timestamp=datetime.datetime.utcnow(),
        )
        embed.add_field(
            name="👤 보낸 유저",
            value=f"{ctx.author} (`{ctx.author.id}`)",
            inline=False,
        )
        embed.add_field(name="💬 내용", value=content, inline=False)

        guild_info = ctx.guild.name if ctx.guild else "DM"

        try:
            await dev_user.send(embed=embed)
        except discord.Forbidden:
            await ctx.send(
                "개발자 DM 문이 닫혀 있어서, 여기서 더는 못 보내겠어."
            )
            return

        # 오늘 기록 / 감정 반영
        self._log_today_feedback(
            ctx.author,
            content=content,
            guild_name=guild_info,
        )

        # 유메가 직접 고마워하는 느낌으로
        reply_text = self._speak_feedback_received(user=ctx.author)

        await ctx.send(reply_text)

    # -------------------------------
    # 슬래시 명령어: /건의사항
    # -------------------------------
    @app_commands.command(
        name="건의사항",
        description="유메에게 바라는 점을 개발자에게 전달해요.",
    )
    async def slash_feedback(self, interaction: discord.Interaction):
        modal = FeedbackModal(self, interaction)
        await interaction.response.send_modal(modal)


async def setup(bot: commands.Bot):
    await bot.add_cog(FeedbackCog(bot))
