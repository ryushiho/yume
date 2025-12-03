# ReactionsCog: 멘션/키워드 리액션, 랜덤 바보 놀리기, 육포 패널티, 자동 "으헤~" 등 유메 기본 리액션 모음

import asyncio
import random
import time

import discord
from discord.ext import commands
from discord import app_commands

# 개발자 예외 ID
DEV_USER_ID = 1433962010785349634

# 유메가 자발적으로 "으헤~"를 보내는 채널
HEHE_CHANNEL_ID = 1445819862713893046


class ReactionsCog(commands.Cog):
    """멘션/키워드 리액션 + 바보 놀리기 + 육포 패널티 + 간단 멘션 대화 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 육포 패널티: user_id -> 해제 시각(monotonic 기준)
        self.yukpo_block_until: dict[int, float] = {}
        # 으헤 자동 발사 루프 태스크 핸들
        self._hehe_task: asyncio.Task | None = None

    async def cog_load(self):
        """Cog 로드 시 자동으로 으헤 루프 시작."""
        if self._hehe_task is None or self._hehe_task.done():
            self._hehe_task = asyncio.create_task(self._hehe_loop())

    def cog_unload(self):
        """Cog 언로드 시 루프 정리."""
        if self._hehe_task is not None:
            self._hehe_task.cancel()
            self._hehe_task = None

    # ===== yume_ai 연동 유틸 =====

    def _core(self):
        """YumeAI (감정/관계 엔진)"""
        return getattr(self.bot, "yume_core", None)

    def _speaker(self):
        """YumeSpeaker (말투 엔진) - 지금은 이 Cog에서 직접 많이 쓰진 않지만 확장 여지"""
        return getattr(self.bot, "yume_speaker", None)

    def _memory(self):
        """YumeMemory (일기장/로그)"""
        return getattr(self.bot, "yume_memory", None)

    def _log(self, text: str) -> None:
        mem = self._memory()
        if mem is None:
            return
        try:
            mem.log_today(text)
        except Exception:
            pass

    def _apply_event(self, event: str, message: discord.Message, weight: float = 1.0):
        """유메 감정 엔진에 '무슨 일이 있었다'고 알려주는 헬퍼."""
        core = self._core()
        if core is None:
            return
        try:
            core.apply_event(
                event,
                user_id=str(message.author.id),
                guild_id=str(message.guild.id) if message.guild else None,
                weight=weight,
            )
        except Exception:
            pass

    # ===== 공통 유틸 =====

    def _pick_random_member(self, guild: discord.Guild) -> discord.Member | None:
        """해당 길드에서 랜덤 유저 1명을 뽑는다. (봇 제외)"""
        candidates = [m for m in guild.members if not m.bot]
        if not candidates:
            return None
        return random.choice(candidates)

    def _build_babo_message(self, target: discord.Member) -> str:
        """멘션 대신 닉네임만 사용해서 메시지 구성."""
        return f"{target.display_name} 바보"

    def _is_yukpo_blocked(self, user_id: int) -> bool:
        """해당 유저가 육포 패널티 중인지 확인. 끝났으면 자동 해제."""
        if user_id == DEV_USER_ID:
            return False

        now = time.monotonic()
        until = self.yukpo_block_until.get(user_id)
        if until is None:
            return False

        if now >= until:
            # 패널티 끝났으면 해제
            del self.yukpo_block_until[user_id]
            return False

        return True

    def _apply_yukpo_pout_state(
        self,
        author: discord.abc.User,
        guild: discord.Guild | None,
    ) -> None:
        """
        '육포'를 사용한 유저에게 유메가 철저하게 경멸하는 상태를 메모리에 반영.
        - 감정 엔진에는 강한 insult 이벤트로 반영
        - 로그도 남겨 둔다.
        """
        core = self._core()
        if core is not None:
            try:
                core.apply_event(
                    "insult",
                    user_id=str(author.id),
                    guild_id=str(guild.id) if guild else None,
                    weight=1.5,
                )
            except Exception:
                pass

        self._log(
            f"육포 사용 감지: user={author} (id={author.id}), "
            f"guild={getattr(guild, 'id', None)}"
        )

    # ===== 텍스트 명령어: !바보 =====

    @commands.command(name="바보")
    async def babo_text(self, ctx: commands.Context):
        """서버 내 랜덤 유저를 바보라고 놀리는 텍스트 명령어."""
        if ctx.guild is None:
            await ctx.send(
                "이건 서버에서만 쓸 수 있어. 여기선 유메가 못 놀려줘~",
                delete_after=5,
            )
            return

        # 육포 패널티 체크
        if self._is_yukpo_blocked(ctx.author.id):
            return

        target = self._pick_random_member(ctx.guild)
        if target is None:
            await ctx.send(
                "여긴 놀릴 사람이 없네… 사람이 한 명도 없잖아.",
                delete_after=5,
            )
            return

        msg = self._build_babo_message(target)
        await ctx.send(msg)

    # ===== 슬래시 명령어: /바보 =====

    @app_commands.command(
        name="바보",
        description="서버 내 랜덤 플레이어를 골라서 바보라고 놀려요.",
    )
    async def babo_slash(self, interaction: discord.Interaction):
        """서버 내 랜덤 유저를 바보라고 놀리는 슬래시 명령어."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "이건 서버에서만 쓸 수 있어. DM에선 못 놀려.",
                ephemeral=True,
            )
            return

        if self._is_yukpo_blocked(interaction.user.id):
            await interaction.response.send_message(
                "육포 냄새가 아직 안 빠져서… 유메, 말하기 싫어. 5분만 더 있다 와.",
                ephemeral=True,
            )
            return

        target = self._pick_random_member(guild)
        if target is None:
            await interaction.response.send_message(
                "여긴 놀릴 사람이 없네… 사람이 한 명도 없어.",
                ephemeral=True,
            )
            return

        msg = self._build_babo_message(target)
        await interaction.response.send_message(msg)

    # ===== 멘션 대화 처리 =====

    async def _handle_mention_chat(self, message: discord.Message) -> None:
        """
        @유메 (메시지) 형태로 부르면 간단 대화.

        - yume_core 에 friendly_chat 이벤트로 반영
        - 기분 상태(mood, irritation)를 보고 말투 + 내용 조금 바꿈
        """

        raw = message.content
        # 봇 멘션 문자열 제거
        if self.bot.user:
            raw = raw.replace(self.bot.user.mention, "").strip()

        if not raw:
            user_text = "유메 부른 거야…?"
        else:
            user_text = raw

        # 감정 엔진에 친근한 대화 이벤트 반영
        self._apply_event("friendly_chat", message, weight=1.0)
        self._log(f"mention-chat: {message.author} → {user_text}")

        # 기분 상태 확인
        mood_val = 0.0
        irritation_val = 0.0
        core = self._core()
        if core is not None:
            try:
                s = core.get_core_state()
                mood_val = float(s.get("mood", 0.0))
                irritation_val = float(s.get("irritation", 0.0))
            except Exception:
                pass

        name = getattr(message.author, "display_name", "누구야")
        user_text_compact = user_text.replace(" ", "")

        # 대략적인 상태 태그
        if irritation_val > 0.6:
            mood_tag = "annoyed"
        elif mood_val > 0.4:
            mood_tag = "happy"
        elif mood_val < -0.3:
            mood_tag = "tired"
        else:
            mood_tag = "neutral"

        # 기분 묘사 문자열
        if irritation_val > 0.6:
            mood_desc = "솔직히 지금은 좀 짜증 나 있어."
        elif mood_val > 0.5:
            mood_desc = "요즘 유메 기분 꽤 괜찮아."
        elif mood_val < -0.3:
            mood_desc = "조금 피곤해서 멍해."
        else:
            mood_desc = "그냥 평소 유메 느낌이야."

        # 키워드/상태별 템플릿 뭉치
        replies: list[str]

        # 1) 안녕 계열 인사
        if "안녕" in user_text:
            if mood_tag == "happy":
                replies = [
                    "{name}, 안녕! 오늘은 유메 기분 좋아, 으헤에~",
                    "{name}, 안녕~ 뭐 하고 있었어?",
                    "안녕, {name}. 또 왔네?",
                ]
            elif mood_tag == "tired":
                replies = [
                    "{name}, 안녕… 유메는 살짝 졸려.",
                    "안녕, {name}. 오늘은 조용히 지내고 싶어.",
                ]
            elif mood_tag == "annoyed":
                replies = [
                    "…안녕은 안녕인데, 유메 지금은 살짝 예민해.",
                    "{name}, 안녕. 근데 지금은 장난치면 안 돼.",
                ]
            else:  # neutral
                replies = [
                    "{name}, 안녕… 음.",
                    "안녕, {name}. 무슨 일 있어?",
                    "{name}, 안녕. 오늘은 그냥 그런 하루야.",
                ]

        # 2) 기분 물어볼 때
        elif "기분" in user_text_compact:
            if mood_tag == "happy":
                replies = [
                    "{name}, 오늘 유메 기분 꽤 좋아. {mood}",
                    "{name}, 나름 상쾌해. {mood}",
                ]
            elif mood_tag == "tired":
                replies = [
                    "{name}, 솔직히 좀 피곤해. {mood}",
                    "흠… {name}, 그렇게 좋은 편은 아니야. {mood}",
                ]
            elif mood_tag == "annoyed":
                replies = [
                    "{name}, 기분? 지금 물어볼 타이밍은 아닌 것 같은데. {mood}",
                    "기분은… {mood} 그러니까 육포는 입에도 올리지 말아줘.",
                ]
            else:
                replies = [
                    "{name}, {mood} 너는 어때?",
                    "{name}, 뭐… 나쁘지도 좋지도 않아. {mood}",
                ]

        # 3) 기타 대화
        else:
            if mood_tag == "happy":
                replies = [
                    "{name}, {text}? 재밌는 얘기네. 유메는 좋아.",
                    "{name}, 그런 생각도 하는구나. 흥미로운데?",
                    "{name}, 듣고 있어. 계속 말해봐.",
                ]
            elif mood_tag == "tired":
                replies = [
                    "{name}, {text}라… 듣고는 있어. 조금만 천천히.",
                    "{name}, 지금은 살짝 피곤해서 반응이 느릴 수도 있어.",
                ]
            elif mood_tag == "annoyed":
                replies = [
                    "{name}, {text}라… 지금은 귀찮게 하면 안 좋을 텐데.",
                    "{name}, 미안하지만 지금은 말 거는 것보다 조용히 해주는 게 좋아.",
                ]
            else:  # neutral
                replies = [
                    "{name}, {text}라… 음.",
                    "{name}, {text}? 그렇게 생각하는구나.",
                    "{name}, 알겠어. 일단 잘 들었어.",
                ]

        template = random.choice(replies)
        reply = template.format(name=name, text=user_text, mood=mood_desc)

        await message.reply(reply, mention_author=False)

    # ===== 메시지 리스너 =====

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        content = message.content

        # 1) "육포" 감지 → 패널티 부여 (개발자 제외)
        if "육포" in content and message.author.id != DEV_USER_ID:
            # 5분 패널티
            self.yukpo_block_until[message.author.id] = time.monotonic() + 300
            # 유메 감정/로그에 경멸 상태 반영
            self._apply_yukpo_pout_state(message.author, message.guild)
            await message.channel.send("육포라니… 진짜 최악이야. 유메 그런 건 싫어.")
            return

        # 2) 명령어 메시지라면 여기서는 아무 리액션도 하지 않고 종료
        if content.startswith("!"):
            return

        # 3) @유메 멘션 대화
        if self.bot.user and self.bot.user in message.mentions:
            await self._handle_mention_chat(message)
            return
        # 4) 옛날 '으헤' / '비비안 바보' 로직은 삭제됨

    # ===== 으헤 자동 발사 루프 =====

    async def _hehe_loop(self):
        """
        유메가 가끔 심심하면 지정된 채널에 "으헤~"를 던지는 루프.
        - 봇 준비될 때까지 기다렸다가 시작
        - 30분~120분(2시간) 사이 랜덤 간격으로 깨어남
        - 깨어날 때마다 50% 확률로만 말함 (너무 시끄럽지 않게)
        - 짜증이 너무 높으면 그냥 조용히 넘김
        """
        await self.bot.wait_until_ready()

        channel: discord.TextChannel | None = None

        while not self.bot.is_closed():
            # 30분 ~ 120분 사이 랜덤 대기
            wait_sec = random.randint(1800, 7200)
            await asyncio.sleep(wait_sec)

            try:
                if channel is None:
                    ch = self.bot.get_channel(HEHE_CHANNEL_ID)
                    if isinstance(ch, discord.TextChannel):
                        channel = ch
                    else:
                        channel = None

                if channel is None:
                    continue

                # 50% 확률로만 말하기
                if random.random() < 0.5:
                    continue

                # 너무 짜증났으면 조용히 있기
                core = self._core()
                if core is not None:
                    try:
                        s = core.get_core_state()
                        if float(s.get("irritation", 0.0)) > 0.5:
                            continue
                    except Exception:
                        pass

                await channel.send("으헤~")
                self._log("auto-으헤 발사")

            except Exception:
                # 루프는 절대 죽지 않게 한다.
                continue


async def setup(bot: commands.Bot):
    cog = ReactionsCog(bot)
    await bot.add_cog(cog)

    # === 전역 커맨드 체크: 육포 패널티인 유저의 커맨드를 막는다. ===
    @bot.check
    async def _global_yukpo_check(ctx: commands.Context) -> bool:  # type: ignore[unused-ignore]
        rcog: ReactionsCog | None = bot.get_cog("ReactionsCog")  # type: ignore[assignment]
        if rcog is None:
            return True
        # True면 실행 허용, False면 모든 커맨드 무시
        return not rcog._is_yukpo_blocked(ctx.author.id)
