# yume_chat.py
# 유메 프리토킹 전용 LLM Cog (호감도 엔진 연동 + 브레인 초기화 디버그 버전)
#
# - 기본 프리토킹 채널:
#     1438804132613066833
#     1445664712133181513
# - 기본 채널은 항상 프리토킹 허용 상태
# - 다른 채널에서는 기본적으로 프리토킹 OFF
#   → !프리토킹시작 으로 채널별로 켜기
#   → !프리토킹종료 로 세션/채널 끄기
#
# - 동작 개요:
#   1) 프리토킹 활성 채널에서
#      - 처음엔 @유메 멘션 또는 "유메" 포함해야 세션 시작
#      - 이후엔 같은 유저가 말하면 계속 이어서 대화
#   2) YumeBrain.chat(...) 에 user_message + user_profile + history 만 넘김
#      - user_profile.bond_level 은 yume_core 의 affection_stage 를 사용
#   3) 프리토킹이 성공하면 yume_core.apply_event("friendly_chat", ...) 호출
#   4) YumeBrain 초기화 실패 시, 개발자에게 brain_error 를 디버그로 보여준다.

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Set

import discord
from discord.ext import commands

from yume_brain import YumeBrain

logger = logging.getLogger(__name__)

# 기본 프리토킹 전용 채널 ID (항상 활성)
DEFAULT_CHAT_CHANNEL_IDS: Set[int] = {
    1438804132613066833,
    1445664712133181513,
}

# 개발자(너) 디스코드 ID – 디버그 정보 노출용
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

        # 현재 프리토킹이 활성화된 채널 집합
        self.active_channels: Set[int] = set(DEFAULT_CHAT_CHANNEL_IDS)

        # 채널별 세션
        self.sessions: Dict[int, ChatSession] = {}  # channel_id -> ChatSession

        # 다른 Cog 에서도 참조할 수 있도록 공유
        setattr(self.bot, "yume_chat_active_channels", self.active_channels)
        setattr(self.bot, "yume_chat", self)

    # ===== yume_ai 연동 유틸 =====

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

    # ===== YumeBrain용 유저 프로필 =====

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
            # 엔진 내부 세팅 문제여도, 프로필은 기본값으로 계속 사용
            pass

        return profile

    # ===== YumeBrain 초기화 헬퍼 =====

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
        # Cog 로드 시 한 번 시도
        self._ensure_brain()

    # ===== 세션 헬퍼 =====

    def _get_session(self, channel_id: int) -> Optional[ChatSession]:
        return self.sessions.get(channel_id)

    def _start_session(self, channel_id: int, user_id: int) -> ChatSession:
        sess = ChatSession(last_user_id=user_id)
        # 기존 대화는 새 세션으로 덮어씀
        self.sessions[channel_id] = sess
        return sess

    def _reset_session(self, channel_id: int) -> None:
        if channel_id in self.sessions:
            del self.sessions[channel_id]

    # 다른 Cog에서 프리토킹 활성 여부를 물어볼 수 있는 API
    def is_active_channel(self, channel_id: int) -> bool:
        return channel_id in self.active_channels

    # ===== 텍스트 명령어: !프리토킹시작 / !프리토킹종료 =====

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

        # 세션 정리
        had_session = ch_id in self.sessions
        self._reset_session(ch_id)

        # 기본 채널이 아니라면 프리토킹 비활성화
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

    # ===== 메인 on_message =====

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 1) 기본 필터
        if message.author.bot:
            return

        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return

        ch_id = channel.id

        # 이 채널이 프리토킹 활성 채널이 아니면 무시
        if ch_id not in self.active_channels:
            return

        content = message.content.strip()
        if not content:
            return

        # 명령어는 여기서 처리하지 않고, commands 확장으로 넘김
        if content.startswith("!"):
            return

        # 2) 세션 여부 판정
        sess = self._get_session(ch_id)
        in_session = sess is not None and sess.last_user_id == message.author.id

        triggered = False

        # 새 세션을 시작하려면 멘션 또는 "유메" 포함이 필요
        if not in_session:
            if self.bot.user and self.bot.user in message.mentions:
                triggered = True
                # 멘션 문자열 제거
                content = content.replace(self.bot.user.mention, "").strip()
            elif "유메" in content:
                triggered = True

            if not triggered:
                # 아직 이 유저 기준 세션도 없고, 유메를 부른 것도 아니면 무시
                return

            # 새 세션 시작
            sess = self._start_session(ch_id, message.author.id)
        else:
            # 이미 같은 유저와 세션이 있는 경우 → 계속 이어가기
            pass

        # 3) YumeBrain 초기화 확인 (지연 초기화 포함)
        if not self._ensure_brain():
            debug_suffix = ""
            if message.author.id == DEV_USER_ID and self.brain_error:
                debug_suffix = f"\n\n[디버그 brain_error: {self.brain_error}]"

            await message.reply(
                "현재 대사를 생성하는 엔진 초기화에 실패해서, 프리토킹을 사용할 수 없습니다."
                + debug_suffix,
                mention_author=False,
            )
            return

        # 4) 유저 프로필 / 히스토리 구성
        user_profile = self._get_user_profile(message.author, message.guild)
        history = list(sess.history[-8:]) if sess else []

        # 5) LLM 호출 (스레드풀에서 실행해서 이벤트 루프 안 막기)
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

        # 6) 에러/예산 초과 처리
        if not ok and reason == "limit_exceeded":
            await message.reply(
                "이번 달에 유메가 쓸 수 있는 말 예산을 다 써버렸어요. "
                "다음 달에 다시 이야기해요.",
                mention_author=False,
            )
            return
        elif not ok:
            # 디버그: 개발자일 때만 reason 꼬리표 달기
            dev_suffix = ""
            if message.author.id == DEV_USER_ID:
                dev_suffix = f"\n\n[디버그 reason: {reason!r}]"

            if not reply:
                reply = (
                    "대사를 생성하는 중 오류가 발생해서, 답변을 줄 수 없어요."
                    + dev_suffix
                )
            else:
                reply = reply + dev_suffix

            await message.reply(
                reply,
                mention_author=False,
            )
            return

        if not reply.strip():
            reply = "..."  # 비어 있으면 최소한 무언가 응답

        # 7) 실제 답변 보내기
        await message.reply(reply, mention_author=False)

        # 8) 세션/기록 업데이트
        if sess:
            sess.last_user_id = message.author.id
            sess.history.append(("user", content))
            sess.history.append(("assistant", reply))
            # 너무 길어지지 않게 자르기
            sess.history = sess.history[-12:]

        # 9) 오늘 기록에 로그 남기기
        self._log_today(
            f"프리토킹: {message.author} ({message.author.id}) "
            f"@ {channel.name} | {content[:80]!r}"
        )

        # 10) 호감도 엔진에 이벤트 반영 (있을 때만)
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
                # 이벤트 반영 실패해도 프리토킹은 계속 동작해야 하므로 조용히 무시
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(YumeChatCog(bot))
