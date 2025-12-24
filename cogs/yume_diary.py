import datetime
from typing import Optional

import discord
from discord.ext import commands, tasks

from yume_brain import YumeBrain


# KST=UTC+9 기준, 매일 23:59(KST)에 자동 피드백을 보내기 위해
# 23:59 KST == 14:59 UTC
DAILY_FEEDBACK_TIME_UTC = datetime.time(hour=14, minute=59)

DAILY_FEEDBACK_CHANNEL_ID = 1438804132613066833  # 요청한 채널 ID


class YumeDiaryCog(commands.Cog):
    """
    유메 일기/기분/관계 LLM Cog (접두어 커맨드 버전)

    - !유메일기          : 오늘 하루를 유메 시점 일기처럼 요약
    - !유메오늘어땠어    : 오늘 하루를 1~3문장 정도로 짧게 요약
    - !유메기분          : 지금 유메 기분 설명 (LLM 상상 기반)
    - !유메관계          : 호출한 유저와의 관계 설명 (LLM 상상 기반)
    - 매일 KST 23:59     : 지정 채널에 하루 자동 피드백
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.brain: Optional[YumeBrain] = None

        # YumeBrain 초기화 시도 (실패해도 Cog은 살아있게)
        try:
            self.brain = YumeBrain()
        except Exception as e:
            print(f"[YumeDiaryCog] YumeBrain 초기화 실패: {e}")
            self.brain = None

        # 자동 피드백 루프 시작
        self.daily_feedback.start()

    def cog_unload(self):
        self.daily_feedback.cancel()

    # -----------------------
    # 내부 헬퍼
    # -----------------------

    def _ensure_brain(self) -> Optional[YumeBrain]:
        if self.brain is not None:
            return self.brain
        try:
            self.brain = YumeBrain()
        except Exception as e:
            print(f"[YumeDiaryCog] YumeBrain 재초기화 실패: {e}")
            self.brain = None
        return self.brain

    def _get_memory(self):
        # 감정 코어는 없고, 일기/로그용 메모리만 남아 있을 수 있음
        return getattr(self.bot, "yume_memory", None)

    def _short_say(
        self,
        *,
        kind: str,
        user: Optional[discord.abc.User],
        fallback: str,
    ) -> str:
        """
        짧은 안내/에러 멘트도 가능하면 YumeBrain으로 생성.
        - 감정 코어는 없으므로 scene / yume_state 는 비워서 넘긴다.
        - YumeBrain 사용이 불가하면 fallback 그대로 반환.
        """
        brain = self._ensure_brain()
        if brain is None:
            return fallback

        if user is not None:
            nickname = getattr(user, "display_name", None) or getattr(
                user, "name", "후배"
            )
        else:
            nickname = "후배"

        user_profile = {
            "nickname": nickname,
            "bond_level": "normal",
        }

        user_message = (
            "다음은 유메가 처한 상황에 대한 짧은 설명이야.\n"
            "이 상황에서 유메가 후배에게 할 법한 말을 1~2문장 정도로 만들어줘.\n"
            "설명이나 메타 발언은 하지 말고, 바로 유메의 대사만 말해.\n\n"
            f"[상황 태그]\n{kind}\n\n"
            f"[기본으로 써도 되는 예시 문장]\n{fallback}\n"
        )

        try:
            result = brain.chat(
                user_message=user_message,
                mode="special",
                scene=None,
                yume_state={},
                user_profile=user_profile,
                history=None,
                max_tokens=120,
                temperature=0.7,
            )
            reply = str(result.get("reply", "")).strip()
            return reply or fallback
        except Exception as e:
            print(f"[YumeDiaryCog] _short_say 실패(kind={kind}): {e}")
            return fallback

    # -----------------------
    # 접두어 커맨드들
    # -----------------------

    @commands.command(name="유메일기", help="유메가 오늘 하루를 일기처럼 정리해줘요.")
    async def cmd_yume_diary(self, ctx: commands.Context):
        """
        감정 코어 없이, 유메 시점의 '상상 일기'를 LLM으로 생성.
        """
        brain = self._ensure_brain()
        if brain is None:
            msg = self._short_say(
                kind="cmd_yume_diary_brain_not_ready",
                user=ctx.author,
                fallback="유메가 지금은 긴 이야기를 하기 힘들어. 설정을 한 번 봐줘, 으헤~",
            )
            await ctx.send(msg)
            return

        user = ctx.author
        guild = ctx.guild

        nickname = getattr(user, "display_name", None) or getattr(
            user, "name", "후배"
        )
        guild_name = guild.name if guild else "이 서버"

        today_str = datetime.date.today().isoformat()

        user_profile = {
            "nickname": nickname,
            "bond_level": "normal",
        }

        user_message = (
            f"오늘 날짜는 대략 {today_str}라고 생각해 줘.\n"
            "너는 디스코드 서버에서 하루를 보낸 아비도스 전 학생회장 '유메'야.\n"
            f"일기를 들어줄 사람은 디스코드 닉네임 '{nickname}'이고, 서버 이름은 '{guild_name}'이야.\n"
            "현실의 구체적인 로그나 감정 수치는 없으니까, 후배들과 보낸 평범하거나 조금 바빴던 하루를 상상해서\n"
            "유메 시점의 일기를 3~6문장 정도로 써줘.\n"
            "숫자나 시스템 이야기는 하지 말고, 그날의 분위기와 감정, 후배들을 챙기는 마음을 중심으로 말해.\n"
        )

        result = brain.chat(
            user_message=user_message,
            mode="diary",
            scene=None,
            yume_state={},
            user_profile=user_profile,
            history=None,
            max_tokens=400,
            temperature=0.8,
        )

        reply = str(result.get("reply", "")).strip() or self._short_say(
            kind="cmd_yume_diary_empty",
            user=user,
            fallback="오늘은 별일 없었지만… 그래도 후배들 덕분에 나름 괜찮은 하루였어, 으헤~",
        )

        memory = self._get_memory()
        if memory is not None:
            try:
                memory.log_today(f"[유메일기(manual)] {reply}")
            except Exception:
                pass

        await ctx.send(reply)

    @commands.command(name="유메오늘어땠어", help="오늘 유메가 느끼는 하루 총평을 짧게 들어요.")
    async def cmd_yume_today_short(self, ctx: commands.Context):
        """
        감정 코어 없이, 유메가 스스로 상상해서 오늘 하루를 1~3문장으로 요약.
        """
        brain = self._ensure_brain()
        if brain is None:
            msg = self._short_say(
                kind="cmd_yume_today_short_brain_not_ready",
                user=ctx.author,
                fallback="지금은 유메 머리가 좀 복잡해서, 하루를 정리하기가 어려워… 으헤~",
            )
            await ctx.send(msg)
            return

        user = ctx.author
        guild = ctx.guild

        nickname = getattr(user, "display_name", None) or getattr(
            user, "name", "후배"
        )
        guild_name = guild.name if guild else "이 서버"

        user_profile = {
            "nickname": nickname,
            "bond_level": "normal",
        }

        user_message = (
            "지금은 별도의 감정 코어나 수치 없이, 그냥 너의 느낌만으로 오늘 하루를 말하는 상황이야.\n"
            f"질문한 디스코드 유저 닉네임은 '{nickname}'이고, 서버 이름은 '{guild_name}'이야.\n"
            "오늘 하루를 떠올리면서, 전체적인 기분과 분위기를 1~3문장 정도로 짧게 말해줘.\n"
            "너무 거창하거나 극단적인 사건은 넣지 말고, 편안한 수다 톤으로 솔직하게 말해."
        )

        result = brain.chat(
            user_message=user_message,
            mode="diary",
            scene=None,
            yume_state={},
            user_profile=user_profile,
            history=None,
            max_tokens=200,
            temperature=0.7,
        )

        reply = str(result.get("reply", "")).strip() or self._short_say(
            kind="cmd_yume_today_short_empty",
            user=user,
            fallback="크게 특별한 건 없었지만… 조용히 버티긴 했어. 후배들이랑 얘기한 순간들은 좋았고.",
        )

        await ctx.send(reply)

    @commands.command(name="유메기분", help="지금 유메의 기분이 어떤지 설명해줘요.")
    async def cmd_yume_mood(self, ctx: commands.Context):
        """
        감정 수치 없이, '지금 기분'을 상상해서 대답.
        """
        brain = self._ensure_brain()
        if brain is None:
            msg = self._short_say(
                kind="cmd_yume_mood_brain_not_ready",
                user=ctx.author,
                fallback="지금은 유메도 스스로 기분을 잘 정리 못 하겠어… 조금만 있다가 다시 물어봐줄래?",
            )
            await ctx.send(msg)
            return

        user = ctx.author
        nickname = getattr(user, "display_name", None) or getattr(
            user, "name", "후배"
        )

        user_profile = {
            "nickname": nickname,
            "bond_level": "normal",
        }

        user_message = (
            "지금 너의 기분을 설명하는 상황이야.\n"
            f"질문한 후배의 디스코드 닉네임은 '{nickname}'이야.\n"
            "별도의 감정 수치나 코어는 없으니까, 평범한 하루 중 한때라고 생각하고\n"
            "지금 유메가 어떤 기분인지 1~3문장으로 설명해줘.\n"
            "너무 무거워지지 않도록, 솔직하지만 다정한 말투로."
        )

        result = brain.chat(
            user_message=user_message,
            mode="special",
            scene=None,
            yume_state={},
            user_profile=user_profile,
            history=None,
            max_tokens=200,
            temperature=0.7,
        )

        reply = str(result.get("reply", "")).strip() or self._short_say(
            kind="cmd_yume_mood_empty",
            user=user,
            fallback="음… 완전 최고는 아니어도, 후배들이랑 얘기할 힘 정도는 있는 기분이야. 으헤~",
        )

        await ctx.send(reply)

    @commands.command(name="유메관계", help="지금 유메와 나의 관계를 들려줘요.")
    async def cmd_yume_relation(self, ctx: commands.Context):
        """
        실제 수치/코어 없이, '관계 묘사'를 상상해서 생성.
        """
        brain = self._ensure_brain()
        if brain is None:
            msg = self._short_say(
                kind="cmd_yume_relation_brain_not_ready",
                user=ctx.author,
                fallback="지금은 관계 이야기를 정리할 여유가 없네… 나중에 다시 물어봐줘.",
            )
            await ctx.send(msg)
            return

        user = ctx.author
        guild = ctx.guild

        nickname = getattr(user, "display_name", None) or getattr(
            user, "name", "후배"
        )
        guild_name = guild.name if guild else "이 서버"

        user_profile = {
            "nickname": nickname,
            "bond_level": "normal",
        }

        user_message = (
            "지금 너와 한 후배 사이의 관계를 설명하는 상황이야.\n"
            f"그 후배의 디스코드 닉네임은 '{nickname}'이고, 같이 있는 서버 이름은 '{guild_name}'이야.\n"
            "내부 호감도 수치나 데이터는 없지만, 이 후배는 적어도 가끔은 유메를 찾아와 주는 사람이라고 가정해도 좋아.\n"
            "이 후배를 어떻게 보고 있는지, 얼마나 편하게 느끼는지, 앞으로 어떻게 지내고 싶은지 등을 2~4문장으로 말해줘.\n"
            "숫자나 시스템 용어는 쓰지 말고, 선배가 후배에게 건네는 말처럼 자연스럽게."
        )

        result = brain.chat(
            user_message=user_message,
            mode="special",
            scene=None,
            yume_state={},
            user_profile=user_profile,
            history=None,
            max_tokens=220,
            temperature=0.7,
        )

        reply = str(result.get("reply", "")).strip() or self._short_say(
            kind="cmd_yume_relation_empty",
            user=user,
            fallback="적어도 유메 기준으론, 꽤 신경 쓰이는 후배 쪽에 들어가. 너무 도망만 치지만 않으면 좋겠는데?",
        )

        await ctx.send(reply)

    # -----------------------
    # 매일 자동 피드백 (KST 23:59)
    # -----------------------

    @tasks.loop(time=DAILY_FEEDBACK_TIME_UTC)
    async def daily_feedback(self):
        """
        매일 UTC 14:59 == KST 23:59 에 하루 자동 피드백.
        감정 코어 없이, '오늘 하루 마무리 인사'를 상상해서 생성.
        """
        await self.bot.wait_until_ready()

        channel = self.bot.get_channel(DAILY_FEEDBACK_CHANNEL_ID)
        if channel is None or not isinstance(
            channel, (discord.TextChannel, discord.Thread)
        ):
            print(
                f"[YumeDiaryCog] 채널 ID {DAILY_FEEDBACK_CHANNEL_ID} 를 찾을 수 없습니다."
            )
            return

        brain = self._ensure_brain()
        if brain is None:
            msg = self._short_say(
                kind="daily_feedback_brain_not_ready",
                user=None,
                fallback="오늘 하루를 정리해 주고 싶은데… 지금은 유메 머리가 좀 과열됐어. 내일은 꼭 말해줄게, 으헤~",
            )
            await channel.send(msg)
            return

        guild = getattr(channel, "guild", None)
        guild_name = guild.name if guild else "이 서버"
        today_str = datetime.date.today().isoformat()

        user_profile = {
            "nickname": guild_name,
            "bond_level": "normal",
        }

        user_message = (
            f"오늘 날짜는 대략 {today_str}라고 생각해 줘.\n"
            f"여기는 디스코드 서버 '{guild_name}'이고, 너는 여기서 하루를 보낸 유메야.\n"
            "서버 전체에게 오늘 하루를 마무리하는 짧은 한마디를 전하고 싶어.\n"
            "오늘 있었을 법한 대화와 분위기를 상상해서, 모두를 향한 하루 마무리 인사를 2~4문장으로 작성해줘.\n"
            "너무 과장되거나 극단적인 사건은 넣지 말고, 편안하고 다정한 느낌으로 정리해."
        )

        result = brain.chat(
            user_message=user_message,
            mode="diary",
            scene=None,
            yume_state={},
            user_profile=user_profile,
            history=None,
            max_tokens=260,
            temperature=0.8,
        )

        reply = str(result.get("reply", "")).strip()
        if not reply:
            reply = self._short_say(
                kind="daily_feedback_empty_reply",
                user=None,
                fallback="오늘 하루도 고생 많았어. 유메는 여기서 살짝 쉬었다가, 내일 또 힘낼게. 으헤~",
            )

        await channel.send(reply)

        memory = self._get_memory()
        if memory is not None:
            try:
                memory.log_today(f"[유메일기(auto_daily)] {reply}")
            except Exception:
                pass

    @daily_feedback.before_loop
    async def before_daily_feedback(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(YumeDiaryCog(bot))
