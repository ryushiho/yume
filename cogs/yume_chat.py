
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Set

import discord
from discord.ext import commands

from yume_brain import YumeBrain
from yume_honorific import get_honorific
from yume_send import reply_message
from yume_stamps import maybe_award_stamp_message

logger = logging.getLogger(__name__)

DEFAULT_CHAT_CHANNEL_IDS: Set[int] = {
    1438804132613066833,
    1445664712133181513,
}

DEV_USER_ID = 1433962010785349634


@dataclass
class ChatSession:
    """채널별 프리토킹 세션 상태"""
    last_user_id: int
    history: List[Tuple[str, str]] = field(default_factory=list)  # (role, content)


class YumeChatCog(commands.Cog):
    """유메 전용 채널에서 LLM으로 프리토킹을 처리하는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.brain: Optional[YumeBrain] = None
        self.brain_error: Optional[str] = None  # 최근 초기화 에러 내용

        self.active_channels: Set[int] = set(DEFAULT_CHAT_CHANNEL_IDS)

        self.sessions: Dict[int, ChatSession] = {}  # channel_id -> ChatSession

        setattr(self.bot, "yume_chat_active_channels", self.active_channels)
        setattr(self.bot, "yume_chat", self)


    def _core(self):
        """호감도 엔진(YumeCore). 없으면 None."""
        return getattr(self.bot, "yume_core", None)

    def _memory(self):
        """YumeMemory (일기장/로그). 없으면 None."""
        return getattr(self.bot, "yume_memory", None)

    def _log_today(self, text: str) -> None:
        mem = self._memory()
        if mem is None:
            return
        try:
            mem.log_today(text)
        except Exception:
            pass


    def _get_user_profile(
        self,
        user: discord.abc.User,
        guild: Optional[discord.Guild],
    ) -> dict:
        """
        호감도 엔진이 있다면, affection 값/단계를 반영해서 bond_level 을 채워준다.
        - nickname : 디스코드 닉네임
        - bond_level : affection_stage (예: cold/normal/warm/hot 등)
        """
        profile: dict = {
            "nickname": getattr(user, "display_name", user.name),
            "bond_level": "normal",
            "honorific": get_honorific(user, guild),
        }

        core = self._core()
        if core is None:
            return profile

        try:
            user_id = str(user.id)
            aff = core.get_affection(user_id)
            stage = core.get_affection_stage(user_id)
            profile["bond_level"] = str(stage)
            profile["affection"] = float(aff)
        except Exception:
            pass

        return profile


    def _ensure_brain(self) -> bool:
        """
        YumeBrain 이 아직 없다면 지금 생성 시도.
        - 성공하면 self.brain 세팅 후 True
        - 실패하면 self.brain=None, self.brain_error에 예외내용 저장 후 False
        """
        if self.brain is not None:
            return True

        try:
            self.brain = YumeBrain()
            self.brain_error = None
            logger.info("[YumeChatCog] YumeBrain 지연 초기화 성공")
            return True
        except Exception as e:  # noqa: BLE001
            self.brain = None
            self.brain_error = repr(e)
            logger.error("[YumeChatCog] YumeBrain 초기화 실패: %r", e)
            return False

    async def cog_load(self):
        self._ensure_brain()


    def _get_session(self, channel_id: int) -> Optional[ChatSession]:
        return self.sessions.get(channel_id)

    def _start_session(self, channel_id: int, user_id: int) -> ChatSession:
        sess = ChatSession(last_user_id=user_id)
        self.sessions[channel_id] = sess
        return sess

    def _reset_session(self, channel_id: int) -> None:
        if channel_id in self.sessions:
            del self.sessions[channel_id]

    def is_active_channel(self, channel_id: int) -> bool:
        return channel_id in self.active_channels


    @commands.command(name="프리토킹시작")
    async def cmd_start_free_talk(self, ctx: commands.Context):
        """
        !프리토킹시작
        - 현재 채널을 프리토킹 채널로 활성화
        """
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send(
                "여긴 텍스트 채널이 아니라서 프리토킹을 시작할 수 없어.",
                delete_after=5,
            )
            return

        ch_id = ctx.channel.id
        if ch_id in self.active_channels:
            await ctx.send(
                "여기는 이미 유메 프리토킹 채널이야. 그냥 편하게 말 걸어줘.",
                delete_after=8,
            )
            return

        self.active_channels.add(ch_id)
        await ctx.send(
            "이제 이 채널에서도 유메랑 프리토킹할 수 있어.\n"
            "먼저 `@유메`라고 부르거나, 메시지에 `유메`를 넣어서 말을 걸어줘.",
            delete_after=15,
        )

    @commands.command(name="프리토킹종료")
    async def cmd_stop_free_talk(self, ctx: commands.Context):
        """
        !프리토킹종료
        - 현재 채널의 세션을 종료
        - 기본 채널이 아니면 프리토킹 활성 상태 자체도 해제
        """
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send(
                "여긴 텍스트 채널이 아니라서 프리토킹 세션을 관리할 수 없어.",
                delete_after=5,
            )
            return

        ch_id = ctx.channel.id

        had_session = ch_id in self.sessions
        self._reset_session(ch_id)

        if ch_id not in DEFAULT_CHAT_CHANNEL_IDS and ch_id in self.active_channels:
            self.active_channels.remove(ch_id)
            msg = (
                "이제 이 채널에선 유메 프리토킹을 멈출게. "
                "필요하면 나중에 `!프리토킹시작`으로 다시 불러줘."
            )
        else:
            if had_session:
                msg = (
                    "여기서 하던 프리토킹은 일단 정리해 둘게. "
                    "또 불러주면 다시 이야기해 줄게."
                )
            else:
                msg = "여기서는 지금 진행 중인 프리토킹 세션이 없었어."

        await ctx.send(msg, delete_after=15)


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return

        ch_id = channel.id

        if ch_id not in self.active_channels:
            return

        content = message.content.strip()
        if not content:
            return

        if content.startswith("!"):
            return

        sess = self._get_session(ch_id)
        in_session = sess is not None and sess.last_user_id == message.author.id

        triggered = False

        if not in_session:
            if self.bot.user and self.bot.user in message.mentions:
                triggered = True
                content = content.replace(self.bot.user.mention, "").strip()
            elif "유메" in content:
                triggered = True

            if not triggered:
                return

            sess = self._start_session(ch_id, message.author.id)
        else:
            pass

        if not self._ensure_brain():
            debug_suffix = ""
            if message.author.id == DEV_USER_ID and self.brain_error:
                debug_suffix = f"\n\n[디버그 brain_error: {self.brain_error}]"

            await reply_message(
                message,
                "현재 대사를 생성하는 엔진 초기화에 실패해서, 프리토킹을 사용할 수 없습니다."
                + debug_suffix,
                mention_author=False,
                allow_glitch=False,
            )
            return

        user_profile = self._get_user_profile(message.author, message.guild)
        history = list(sess.history[-8:]) if sess else []

        loop = asyncio.get_running_loop()

        def _call_brain():
            assert self.brain is not None
            return self.brain.chat(
                user_message=content,
                mode="free_talk",
                scene=None,          # 감정 상태는 YumeBrain 내 상상에 맡김
                yume_state={},       # 별도 상태 구조 없음
                user_profile=user_profile,
                history=history,
                max_tokens=256,
                temperature=0.8,
            )

        result = await loop.run_in_executor(None, _call_brain)

        reply = result.get("reply") or ""
        ok = result.get("ok", False)
        reason = result.get("reason", "ok")
        err = str(result.get("error") or "").strip()

        if not ok and reason == "limit_exceeded":
            await reply_message(
                message,
                "이번 달에 유메가 쓸 수 있는 말 예산을 다 써버렸어요. "
                "다음 달에 다시 이야기해요.",
                mention_author=False,
                allow_glitch=False,
            )
            return
        elif not ok:
            dev_suffix = ""
            if message.author.id == DEV_USER_ID:
                dev_suffix = f"\n\n[디버그 reason: {reason!r}]"
                if err:
                    # 너무 길면 디스코드 메시지/보안 측면에서 위험하니 잘라서 노출
                    safe_err = err.replace("\n", " ")
                    if len(safe_err) > 300:
                        safe_err = safe_err[:300] + "…"
                    dev_suffix += f"\n[디버그 error: {safe_err}]"

            if not reply:
                reply = (
                    "대사를 생성하는 중 오류가 발생해서, 답변을 줄 수 없어요."
                    + dev_suffix
                )
            else:
                reply = reply + dev_suffix

            await reply_message(
                message,
                reply,
                mention_author=False,
                allow_glitch=False,
            )
            return

        if not reply.strip():
            reply = "..."  # 비어 있으면 최소한 무언가 응답

        await reply_message(message, reply, mention_author=False, allow_glitch=True)

        # Phase5: daily stamp (KST) on meaningful interaction
        await maybe_award_stamp_message(message, reason="chat")

        if sess:
            sess.last_user_id = message.author.id
            sess.history.append(("user", content))
            sess.history.append(("assistant", reply))
            sess.history = sess.history[-12:]

        self._log_today(
            f"프리토킹: {message.author} ({message.author.id}) "
            f"@ {channel.name} | {content[:80]!r}"
        )

        core = self._core()
        if core is not None:
            try:
                core.apply_event(
                    "friendly_chat",
                    user_id=str(message.author.id),
                    guild_id=str(message.guild.id) if message.guild else None,
                    weight=1.0,
                )
            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(YumeChatCog(bot))
