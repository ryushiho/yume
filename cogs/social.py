
from __future__ import annotations

import asyncio
import datetime
import logging
import random
from typing import Optional, Dict, Any

from yume_brain import YumeBrain

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

DEV_USER_ID = 1433962010785349634

HEHE_CHANNEL_ID = 1445819862713893046


# 블루 아카이브 세계관(쿠치나시 유메) 참고 메모.
# - LLM이 "블루 아카이브 캐릭터/관계" 이야기를 더 자연스럽게 하도록 시스템 프롬프트에 섞어준다.
# - 스포일러가 될 수 있는 건 먼저 물어보고, 사용자가 원하면 말한다.
BLUE_ARCHIVE_LORE_KR = (
    "[블루 아카이브 세계관 참고 메모]\n"
    "- 쿠치나시 유메: 아비도스 고등학교 전 학생회장.\n"
    "- 타카나시 호시노: 유메와 깊은 인연이 있는 후배.\n"
    "- (아비도스 학생들) 유메 기준으로는 다 소중한 후배들.\n"
    "- 선생님: SCHALE(샬레) 소속. 유메는 존중하되, 후배들 앞에서는 선배처럼 다정하게 이끈다.\n"
    "- 주의: 스토리 스포일러(특히 유메 관련)는 먼저 \"스포일러 괜찮아?\" 하고 확인한 뒤 말한다.\n"
)



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

        title = "📚 유메 도움말"
        if irritation > 0.5:
            desc = "명령어는 `!`로 시작해. 필요한 것만 빠르게 적어둘게."
        elif mood >= 0.4:
            desc = "명령어는 `!`로 시작해. 중요한 것만 딱 정리해둘게, 으헤~"
        else:
            desc = "명령어는 `!`로 시작해. 헷갈릴 때는 여기만 보면 돼."

        embed = discord.Embed(
            title=title,
            description=desc,
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="🎮 블루전",
            value=(
                "`!블루전` / `!블루전연습` / `!연습종료`\n"
                "`!블루전전적 [@유저]` / `!블루전랭킹`"
            ),
            inline=False,
        )

        embed.add_field(
            name="🎵 음악",
            value="`!음악` / `!음악채널지정` / `!음악채널해제`",
            inline=False,
        )

        embed.add_field(
            name="📝 일기/관계",
            value="`!유메일기` / `!유메오늘어땠어` / `!유메기분` / `!유메관계`",
            inline=False,
        )

        embed.add_field(
            name="💬 프리토킹",
            value=(
                "`!프리토킹시작` / `!프리토킹종료`\n"
                "프리토킹 채널에선 그냥 말 걸면 유메가 받아줘."
            ),
            inline=False,
        )

        embed.add_field(
            name="📨 기타",
            value="`!건의사항 내용...` / `!바보`",
            inline=False,
        )

        embed.add_field(
            name="🔧 관리자(권한 필요)",
            value="`!유메상태` / `!청소 N`",
            inline=False,
        )

        embed.set_footer(text="잊어버리면 `!도움` 다시 치면 돼. 유메가 여기 있어.")

        try:
            await ctx.send(embed=embed)
        except discord.Forbidden:
            pass

        # DM에도 한 번 더 보내준다(서버에서 임베드 권한이 막혀있을 수 있어서)
        try:
            dm = await ctx.author.create_dm()
            await dm.send(embed=embed)
        except Exception:
            pass


class ReactionsCog(commands.Cog):
    """유메 리액션 / 바보 놀리기 / 육포 패널티 / 랜덤 '으헤~'"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._yukpo_block_until: dict[int, datetime.datetime] = {}

        # 멘션 대화용 LLM(프리토킹 채널과 공유를 우선 시도)
        self.brain: Optional[YumeBrain] = None
        self.brain_error: Optional[str] = None

        self._hehe_task = self.bot.loop.create_task(self._hehe_loop())

    def cog_unload(self):
        if self._hehe_task:
            self._hehe_task.cancel()

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

    def _pick_random_member(self, guild: discord.Guild) -> Optional[discord.Member]:
        members = [
            m for m in guild.members
            if not m.bot
        ]
        if not members:
            return None
        return random.choice(members)

    def _build_babo_message(self, target: discord.Member) -> str:
        name = discord.utils.escape_mentions(target.display_name or target.name)
        return f"{name} 바보. (…라고 누가 그러더라, 유메가 그런 거 아냐. 으헤~)"

    def _core(self):
        return getattr(self.bot, "yume_core", None)

    def _memory(self):
        return getattr(self.bot, "yume_memory", None)

    def _log_today(self, text: str) -> None:
        mem = self._memory()
        if mem is None:
            return
        try:
            mem.log_today(text)
        except Exception:
            pass

    def _get_user_profile(self, user: discord.abc.User, guild: Optional[discord.Guild]) -> Dict[str, Any]:
        profile: Dict[str, Any] = {
            "nickname": getattr(user, "display_name", user.name),
            "bond_level": "normal",
        }

        core = self._core()
        if core is None:
            return profile

        try:
            user_id = str(user.id)
            profile["affection"] = float(core.get_affection(user_id))
            profile["bond_level"] = str(core.get_affection_stage(user_id))
        except Exception:
            pass

        return profile

    def _get_yume_state(self) -> Dict[str, Any]:
        core = self._core()
        if core is None:
            return {"mood": "neutral", "energy": "normal"}

        try:
            state = core.get_core_state()
            mood = float(state.get("mood", 0.0))
            if mood >= 0.4:
                mood_label = "positive"
            elif mood <= -0.4:
                mood_label = "negative"
            else:
                mood_label = "neutral"
            return {
                "mood": mood_label,
                "irritation": float(state.get("irritation", 0.0)),
                "energy": "normal",
                "loneliness": "normal",
                "focus": "normal",
            }
        except Exception:
            return {"mood": "neutral", "energy": "normal"}

    def _try_get_shared_brain(self) -> Optional[YumeBrain]:
        """yume_chat Cog가 이미 Brain을 들고 있으면 그걸 재사용한다."""
        ychat = getattr(self.bot, "yume_chat", None)
        brain = getattr(ychat, "brain", None) if ychat else None
        return brain if isinstance(brain, YumeBrain) else None

    def _ensure_brain(self) -> Optional[YumeBrain]:
        shared = self._try_get_shared_brain()
        if shared is not None:
            self.brain = shared
            self.brain_error = None
            return shared

        if self.brain is not None:
            return self.brain

        try:
            self.brain = YumeBrain()
            self.brain_error = None
            return self.brain
        except Exception as e:  # noqa: BLE001
            self.brain = None
            self.brain_error = repr(e)
            logger.error("[ReactionsCog] YumeBrain 초기화 실패: %r", e)
            return None

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
        """@유메 멘션에 대한 간단 대화(블루 아카이브 세계관/관계 지식 포함)."""
        raw = message.content
        if self.bot.user:
            raw = raw.replace(self.bot.user.mention, "").strip()

        if not raw:
            return

        # 프리토킹 채널은 YumeChatCog가 처리하므로, 여기선 짧은 멘션 대화만.
        brain = self._ensure_brain()
        if brain is None:
            # 시스템 안내/에러는 템플릿 허용
            await message.channel.send(
                "지금은 유메 머리가 잠깐 멈췄어… 으헤~\n"
                "(OPENAI_API_KEY나 한도 설정을 한 번만 확인해줘.)"
            )
            return

        guild = message.guild
        profile = self._get_user_profile(message.author, guild)
        yume_state = self._get_yume_state()

        # 멘션 대화는 짧게. (OpenAI 호출은 블로킹이므로 executor로 돌린다.)
        scene = "discord_mention_chat\n" + BLUE_ARCHIVE_LORE_KR
        loop = asyncio.get_running_loop()

        def _call_brain() -> Dict[str, Any]:
            return brain.chat(
                user_message=raw,
                mode="free_talk",
                scene=scene,
                yume_state=yume_state,
                user_profile=profile,
                max_tokens=128,
                temperature=0.85,
            )

        result = await loop.run_in_executor(None, _call_brain)

        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "limit_exceeded":
                await message.channel.send(
                    "이번 달엔 유메가 너무 많이 떠들어서… 잠깐 쉬어야겠어. 으헤~"
                )
            else:
                await message.channel.send(
                    "지금은 말이 잘 안 나와… 잠깐만 다시 불러줘. 으헤~"
                )
            return

        reply = (result.get("reply") or "").strip()
        if not reply:
            return

        # 감정/관계에 살짝 반영
        core = getattr(self.bot, "yume_core", None)
        if core is not None:
            try:
                core.apply_event(
                    "friendly_chat",
                    user_id=str(message.author.id),
                    guild_id=str(guild.id) if guild else None,
                    weight=0.6,
                )
            except Exception:
                pass

        # 일기/로그에 짧게만 남김
        self._log_today(f"[멘션대화] {profile.get('nickname','?')}: {raw} -> {reply}")

        await message.channel.send(reply)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

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
