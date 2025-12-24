# social.py
# FeedbackCog + HelpCog + ReactionsCog 통합 파일
# - 유저 건의사항
# - 유메 도움말
# - 멘션/키워드 리액션, 바보 놀리기, 육포 패널티, 자동 "으헤~"
#
# ※ 프리토킹(LLM)은 cogs.yume_chat 에서 처리
#   여기서는 프리토킹 활성 채널에서는 멘션 대화(_handle_mention_chat)를 비활성화하고,
#   육포 처리 등만 유지한다.

from __future__ import annotations

import asyncio
import datetime
import logging
import random
from typing import Optional

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

# 개발자(너) 디스코드 사용자 ID
DEV_USER_ID = 1433962010785349634

# 유메가 자발적으로 "으헤~"를 보내는 채널
HEHE_CHANNEL_ID = 1445819862713893046


# ==============================
# 1) FeedbackCog
# ==============================

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
        if user is not None:
            return user
        try:
            return await self.bot.fetch_user(DEV_USER_ID)
        except Exception:
            return None

    # -------------------------------
    # YumeSpeaker 대사 생성
    # -------------------------------
    def _speak_feedback_received(
        self,
        user: discord.abc.User,
    ) -> str:
        """
        '건의사항 잘 받았다'는 느낌의 짧은 대사를 YumeSpeaker를 통해 생성한다.
        speaker.say("feedback_received", ...)에 위임.
        """
        speaker = self._speaker()
        if speaker is None:
            return (
                "건의사항은 잘 받았어요.\n"
                "지금은 말투 엔진을 불러 올 수 없어서, 정해진 문장으로만 대답할 수 있는 상태예요."
            )

        try:
            is_dev = (user.id == DEV_USER_ID)
            return speaker.say(
                "feedback_received",
                user=user,
                extra={
                    "is_dev": is_dev,
                },
            )
        except Exception as e:
            logger.error("YumeSpeaker feedback_received 오류: %s", e)
            return "건의사항은 정상적으로 기록되었지만, 대사를 생성하는 중 오류가 발생했습니다."

    def _log_today_feedback(
        self,
        interaction_user: discord.abc.User,
        *,
        content: str,
        guild_name: str,
    ) -> None:
        """오늘의 기록에 '건의사항 도착'을 남기고, 감정 엔진에도 '좋은 상호작용'으로 반영."""
        mem = self._memory()
        if mem is not None:
            try:
                mem.log_today(
                    f"건의사항 도착: from {interaction_user} ({interaction_user.id}) "
                    f"@ {guild_name} | 내용 일부: {content[:80]!r}"
                )
            except Exception as e:
                logger.error("오늘 기록(log_today) 중 오류: %s", e)

        core = self._core()
        if core is not None:
            try:
                core.apply_event(
                    "feedback_sent",
                    user_id=str(interaction_user.id),
                    guild_id=None,
                    weight=1.2,
                )
            except Exception as e:
                logger.error("감정 엔진(feedback_sent) 반영 중 오류: %s", e)

    async def process_feedback(
        self,
        interaction: discord.Interaction,
        nickname: str,
        content: str,
    ):
        """
        슬래시 모달에서 제출된 건의사항을 처리하는 공통 로직.
        - 개발자 DM으로 embed 전송
        - 오늘의 기록 + 감정 엔진 반영
        """
        dev_user = await self.get_dev_user()
        if not dev_user:
            await interaction.response.send_message(
                "현재 개발자 쪽 DM을 찾을 수 없어서, 건의사항을 대신 전달할 수 없습니다.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📬 새 건의사항 (슬래시)",
            color=discord.Color.orange(),
            timestamp=datetime.datetime.utcnow(),
        )
        embed.add_field(
            name="👤 보낸 사람",
            value=f"{interaction.user} (`{interaction.user.id}`)",
            inline=False,
        )
        if nickname:
            embed.add_field(
                name="📛 적은 이름",
                value=nickname,
                inline=False,
            )
        embed.add_field(
            name="💬 내용",
            value=content,
            inline=False,
        )

        guild_info = interaction.guild.name if interaction.guild else "DM"
        embed.add_field(
            name="📍 서버",
            value=guild_info,
            inline=False,
        )

        try:
            await dev_user.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                "개발자 DM 문이 닫혀 있어서, 여기서 더는 전달할 수 없습니다.",
                ephemeral=True,
            )
            return

        self._log_today_feedback(
            interaction_user=interaction.user,
            content=content,
            guild_name=guild_info,
        )

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
        """!건의사항 [내용]"""
        if not content:
            await ctx.send(
                "사용법: `!건의사항 [내용]`\n"
                "조금만 구체적으로 써주면 정리하기가 더 수월해요.",
            )
            return

        dev_user = await self.get_dev_user()
        if not dev_user:
            await ctx.send(
                "현재 개발자 쪽 DM이 열려 있지 않아서, 건의사항을 대신 전달할 수 없습니다.",
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
                "개발자 DM 문이 닫혀 있어서, 여기서 더는 전달할 수 없습니다.",
            )
            return

        self._log_today_feedback(
            ctx.author,
            content=content,
            guild_name=guild_info,
        )

        reply_text = self._speak_feedback_received(user=ctx.author)
        await ctx.send(reply_text)


# ==============================
# 2) HelpCog
# ==============================

class HelpCog(commands.Cog):
    """유메 도움말 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _core(self):
        return getattr(self.bot, "yume_core", None)

    def _get_ai_mood_and_irritation(self) -> tuple[float, float]:
        core = self._core()
        if core is None:
            return 0.0, 0.0
        try:
            state = core.get_core_state()
            mood = float(state.get("mood", 0.0))
            irritation = float(state.get("irritation", 0.0))
            return mood, irritation
        except Exception:
            return 0.0, 0.0

    @commands.command(name="도움", help="유메 사용법을 알려줄게.")
    async def help_command(self, ctx: commands.Context):
        mood, irritation = self._get_ai_mood_and_irritation()

        if irritation > 0.5:
            title = "📚 유메 사용 설명서 (살짝 예민 모드)"
            desc = (
                "지금은 기분이 아주 좋진 않지만…\n"
                "완전히 방치해 둘 순 없으니까, 필요한 만큼만 정리해 줄게."
            )
        elif mood >= 0.4:
            title = "📚 유메 사용 설명서 (기분 좋은 유메 버전)"
            desc = (
                "지금은 기분이 꽤 좋아서~\n"
                "조금 길어져도 괜찮겠지? 천천히 같이 한 번 볼까, 후배?"
            )
        else:
            title = "📚 유메 사용 설명서"
            desc = (
                "어디서부터 도와줘야 할지 모를 땐, 일단 설명서부터 보는 거야.\n"
                "후배가 헷갈리지 않게, 중요한 것부터 정리해 줄게."
            )

        embed = discord.Embed(
            title=title,
            description=desc,
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="🎮 블루전 (끝말잇기 게임)",
            value=(
                "**!블루전시작** – 다른 유저와 1:1 블루전 대결을 시작해.\n"
                "**!블루전연습** – 유메랑 1:1 연습 모드.\n"
                "**!블루전전적 [@유저]** – 승/패, 승차 등 전적 확인.\n"
                "**!블루전랭킹** – 서버 내 블루전 랭킹 확인.\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="🎵 음악",
            value=(
                "**!음악** – 음악 패널 열기.\n"
                "  → 패널에서 YouTube / Spotify 검색 버튼으로 노래 추가.\n"
                "**!음악재생 [제목 또는 URL]** – 유튜브에서 바로 검색해서 재생.\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="📨 건의사항 – 유메에게 한 말은 전부 기록된다",
            value=(
                "**!건의사항 내용...**\n"
                "‣ 개발자 DM으로 건의 전달 + 유메 감정에 반영.\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="💬 프리토킹 / 멘션 대화",
            value=(
                "**!프리토킹시작 / !프리토킹종료** – 채널 단위로 유메 프리토킹 ON/OFF.\n"
                "`@유메` 멘션 → 짧은 대화 (프리토킹 채널 제외).\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="😈 장난 / 육포 관련",
            value=(
                "**!바보** – 서버 내 랜덤 유저를 골라서 바보라고 놀리기.\n"
                "채팅에 '육포'를 적으면… 5분 동안 명령어 사용이 제한될지도?\n"
            ),
            inline=False,
        )

        embed.set_footer(text="궁금한 게 더 있으면 그냥 편하게 물어봐. 유메가 최대한 도와줄게.")

        try:
            await ctx.send(embed=embed)
        except discord.Forbidden:
            pass

        # DM으로도 보내주기 시도 (실패해도 조용히 무시)
        try:
            dm = await ctx.author.create_dm()
            await dm.send(embed=embed)
        except Exception:
            pass


# ==============================
# 3) ReactionsCog
# ==============================

class ReactionsCog(commands.Cog):
    """유메 리액션 / 바보 놀리기 / 육포 패널티 / 랜덤 '으헤~'"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._yukpo_block_until: dict[int, datetime.datetime] = {}

        # 자동 "으헤~" 태스크
        self._hehe_task = self.bot.loop.create_task(self._hehe_loop())

    def cog_unload(self):
        if self._hehe_task:
            self._hehe_task.cancel()

    # -------------------------------
    # 육포 패널티 관련
    # -------------------------------
    def _is_yukpo_blocked(self, user_id: int) -> bool:
        now = datetime.datetime.utcnow()
        until = self._yukpo_block_until.get(user_id)
        if until is None:
            return False
        if now >= until:
            self._yukpo_block_until.pop(user_id, None)
            return False
        return True

    def _block_yukpo(self, user_id: int, minutes: int = 5):
        now = datetime.datetime.utcnow()
        until = now + datetime.timedelta(minutes=minutes)
        self._yukpo_block_until[user_id] = until

    # -------------------------------
    # 바보 놀리기 공통 로직
    # -------------------------------
    def _pick_random_member(self, guild: discord.Guild) -> Optional[discord.Member]:
        members = [
            m for m in guild.members
            if not m.bot
        ]
        if not members:
            return None
        return random.choice(members)

    def _build_babo_message(self, target: discord.Member) -> str:
        return f"{target.mention} 바보. (…라고 누가 그러더라, 유메가 그런 거 아냐. 으헤~)"

    # -------------------------------
    # 텍스트 명령어: !바보
    # -------------------------------
    @commands.command(name="바보")
    async def babo_text(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send(
                "이건 서버에서만 쓸 수 있어. 여기선 못 놀려.",
                delete_after=5,
            )
            return

        if self._is_yukpo_blocked(ctx.author.id):
            return

        target = self._pick_random_member(ctx.guild)
        if target is None:
            await ctx.send(
                "여긴 놀릴 사람이 없네… 사람이 한 명도 없어.",
                delete_after=5,
            )
            return

        msg = self._build_babo_message(target)
        await ctx.send(msg)

    async def _handle_mention_chat(self, message: discord.Message) -> None:
        """
        @유메 멘션에 대한 간단 대화.
        - 대사는 전부 YumeSpeaker(OpenAI)를 통해 생성한다.
        """
        raw = message.content
        if self.bot.user:
            raw = raw.replace(self.bot.user.mention, "").strip()

        # 프리토킹 활성 채널에서는 여기서 멘션 대화를 하지 않는다.
        ychat = getattr(self.bot, "yume_chat", None)
        if ychat is not None:
            if hasattr(ychat, "is_active_channel"):
                try:
                    if ychat.is_active_channel(message.channel.id):  # type: ignore[attr-defined]
                        return
                except Exception:
                    pass

        speaker = getattr(self.bot, "yume_speaker", None)
        if speaker is None:
            await message.channel.send(
                "지금은 긴 대화를 할 준비가 안 되어 있어서… 미안해. 나중에 다시 불러 줄래?",
                delete_after=8,
            )
            return

        try:
            reply = speaker.say(
                "friendly_chat",
                user=message.author,
                extra={
                    "message_text": raw,
                    "channel_id": message.channel.id,
                },
            )
        except Exception:
            await message.channel.send(
                "지금은 머리가 살짝 복잡해서, 말이 잘 안 나오는 날이야.\n"
                "나중에 다시 한 번만 불러 줄래?",
                delete_after=8,
            )
            return

        await message.channel.send(reply)

    # -------------------------------
    # 이벤트 리스너
    # -------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 자기 자신, 봇들 무시
        if message.author.bot:
            return

        # 육포 감지
        lowered = message.content.lower()
        if "육포" in lowered or "육포 " in lowered:
            self._block_yukpo(message.author.id, minutes=5)
            try:
                await message.channel.send(
                    f"{message.author.mention} 육포 냄새가 진동해서… 잠깐 명령어는 못 쓰게 막아 둘게. 으헤~",
                    delete_after=10,
                )
            except Exception:
                pass
            return

        # 멘션 대화
        if self.bot.user and self.bot.user.mention in message.content:
            await self._handle_mention_chat(message)

    async def _hehe_loop(self):
        """특정 채널에 가끔 랜덤으로 '으헤~' 한마디씩 던지는 태스크."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(random.randint(60 * 300, 60 * 600))
                channel = self.bot.get_channel(HEHE_CHANNEL_ID)
                if isinstance(channel, discord.TextChannel):
                    await channel.send("으헤~")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("으헤~ 루프에서 오류 발생")


async def setup(bot: commands.Bot):
    await bot.add_cog(FeedbackCog(bot))
    await bot.add_cog(HelpCog(bot))

    rcog = ReactionsCog(bot)
    await bot.add_cog(rcog)

    @bot.check
    async def _global_yukpo_check(ctx: commands.Context) -> bool:  # type: ignore[unused-ignore]
        rc: ReactionsCog | None = bot.get_cog("ReactionsCog")  # type: ignore[assignment]
        if rc is None:
            return True
        return not rc._is_yukpo_blocked(ctx.author.id)
