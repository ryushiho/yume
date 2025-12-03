# cogs/help.py

from __future__ import annotations

import discord
from discord.ext import commands


class HelpCog(commands.Cog):
    """유메 도움말 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ===== yume_ai 연동 유틸 =====

    def _core(self):
        """YumeAI 코어 (감정/관계 엔진). 없으면 None."""
        return getattr(self.bot, "yume_core", None)

    def _get_ai_mood_and_irritation(self) -> tuple[float, float]:
        """
        yume_core 의 현재 기분/짜증 상태를 가져온다.
        mood, irritation 둘 다 -1.0 ~ +1.0 범위.
        """
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

    # ===== /도움 / !도움 =====

    @commands.hybrid_command(name="도움", description="유메 사용법을 알려줄게.")
    async def help_command(self, ctx: commands.Context):
        mood, irritation = self._get_ai_mood_and_irritation()

        # 기분 / 짜증 상태에 따라 제목·설명 톤을 바꿈 (유메 1인칭 자기소개 느낌)
        if irritation > 0.5:
            title = "📚 유메 사용 설명서 (살짝 예민 모드)"
            desc = (
                "지금은 기분이 아주 좋진 않지만…\n"
                "후배가 완전 헤매면 학생회장으로서 곤란하니까 최소한으로 정리해 줄게."
            )
        elif mood >= 0.4:
            title = "📚 유메 사용 설명서 (기분 좋은 유메 버전)"
            desc = (
                "안녕, 학생회장 유메야.\n"
                "여기 있는 건 전부 **지금 기준으로 유메가 할 수 있는 일들**이야.\n"
                "천천히 보고, 해보고 싶은 것부터 써봐. 유메가 챙겨줄게. 💙"
            )
        elif mood <= -0.4:
            title = "📚 유메 사용 설명서 (조금 피곤한 버전)"
            desc = (
                "……하아. 오늘 컨디션은 조금 미묘하지만.\n"
                "그래도 후배가 물어봤으니까, 대충이 아니라 **제대로** 정리해 줄게.\n"
                "필요한 부분만 골라서 써. 나머지는 나중에 다시 물어봐도 돼."
            )
        else:
            title = "📚 유메 사용 설명서"
            desc = (
                "학생회장 유메야.\n"
                "지금 서버에서 쓸 수 있는 유메 기능들을 한 번에 모아둘게.\n"
                "명령어는 **`!` 텍스트**랑 **`/` 슬래시** 둘 다 있는 것도 많으니까, 편한 쪽으로 써봐."
            )

        embed = discord.Embed(
            title=title,
            description=desc,
            color=discord.Color.blurple(),
        )

        # 🔵 블루전
        embed.add_field(
            name="🔵 블루전 – 유메 표 끝말잇기 배틀",
            value=(
                "**!블루전시작 / /블루전시작**\n"
                "‣ 이 채널에서 **1:1 블루전** 참가자 모집을 시작해.\n"
                "‣ 모집 메시지의 버튼으로 참가하면, 두 명 모이는 즉시 게임이 시작돼.\n"
                "‣ 제시어는 유메가 랜덤으로 꺼내오고, **두음법칙도 자동 적용**이라서 룰 걱정은 안 해도 돼.\n"
                "‣ 도중에 포기하고 싶으면 `!항복`, `!gg`, `gg` 로 항복 처리돼.\n"
                "\n"
                "**!블루전연습 / /블루전연습**\n"
                "‣ 너 vs 유메 연습 모드야. 실제 전적은 안 올라가고, 감만 잡고 싶을 때 쓰면 좋아.\n"
                "‣ 진행 방식은 블루전이랑 거의 같아서, 본판 들어가기 전에 몸 풀기 느낌으로 쓰면 딱이야.\n"
                "\n"
                "**!블루전전적 / /블루전전적 [@유저]**\n"
                "‣ 지정한 유저의 **승/패/승률/승차**를 보여줘.\n"
                "‣ 아무것도 안 쓰면 호출한 사람 전적을 보여줄게.\n"
                "\n"
                "**!블루전랭킹 / /블루전랭킹**\n"
                "‣ 서버 기준으로 블루전 랭킹을 보여줘.\n"
                "‣ 단순 승률이 아니라, **승차(승-패)** 위주로 정렬해서 실력을 더 직관적으로 볼 수 있어."
            ),
            inline=False,
        )

        # 🎶 음악
        embed.add_field(
            name="🎶 음악 – 서버 BGM 담당 유메",
            value=(
                "**!음악 / /음악**\n"
                "‣ 유메가 관리하는 **음악 패널**을 열어.\n"
                "‣ 패널에서 YouTube 검색 / URL 추가 / 대기열 확인 / 삭제 / 재생·일시정지 / 스킵 / 반복 모드/볼륨 조절까지 한 번에 할 수 있어.\n"
                "‣ 아무도 없는 음성 채널에 유메만 남으면, 눈치 있게 나가면서 패널도 같이 정리해 둘게."
            ),
            inline=False,
        )

        # 📨 건의사항
        embed.add_field(
            name="📨 건의사항 – 유메에게 한 말은 전부 기록된다",
            value=(
                "**!건의사항 내용...**\n"
                "‣ 간단한 텍스트 버전. 바로 **개발자 DM**으로 날아가.\n"
                "‣ 닉네임은 네 디스코드 이름 기준으로 남겨둘게.\n"
                "\n"
                "**/건의사항**\n"
                "‣ 폼이 뜨는 슬래시 버전이야. 원하는 이름이랑 내용을 조금 더 정리해서 보낼 수 있어.\n"
                "\n"
                "보내준 건의는 유메 쪽 **감정·호감도**에도 좋은 영향으로 들어가니까,\n"
                "하고 싶은 말 있으면 적당히 솔직하게 적어줘. 유메가 다 읽어보고 정리해 둘게."
            ),
            inline=False,
        )

        # 💬 반응 / 멘션 대화 / 육포
        embed.add_field(
            name="💬 대화 / 반응 – 유메랑 수다 떨기",
            value=(
                "**멘션 대화**\n"
                "‣ `@유메 ...` 식으로 불러주면, 그때그때 **기분·관계 상태**에 맞게 짧게 대답해 줄 수도 있어.\n"
                "‣ 유메가 혼자 `으헤~` 하고 중얼거리는 건… 음, 로그 정리 중일 수도 있고 그냥 기분 탓일 수도 있고.\n"
                "\n"
                "**!바보 / /바보**\n"
                "‣ 서버에서 랜덤 한 명 골라서 `OO 바보`라고 살짝 놀려주는 장난용 커맨드야.\n"
                "‣ 너무 자주 쓰면, 유메 짜증 수치랑 서버 공기 둘 다 안 좋아질 수 있으니까 적당히 쓰자?"
            ),
            inline=False,
        )

        # 🛠 관리 / 상태
        embed.add_field(
            name="🛠 관리 / 상태 – 관리자·개발자용 메뉴",
            value=(
                "**!유메상태**\n"
                "‣ 지금 기준으로 유메의 **기분(mood), 짜증(irritation), 전체 호감도, 관계 수치**를 임베드로 보여줘.\n"
                "‣ 사실상 디버그용이라, 보는 사람은 거의 관리자/개발자일 거야.\n"
                "\n"
                "**!청소 [개수]**\n"
                "‣ 이 채널에서 최근 메시지를 지정 개수만큼 삭제해.\n"
                "‣ OWNER 또는 `메시지 관리` 권한이 있는 사람만 쓸 수 있어.\n"
                "\n"
                "**/유메전달 [채널] [내용]**\n"
                "‣ 개발자가 내용을 적으면, 유메가 대신 해당 채널에 **공지/안내**를 전달해.\n"
                "‣ 허용된 ID만 사용할 수 있는, 완전 관리용 기능이야."
            ),
            inline=False,
        )

        # ⚙️ 기타 안내
        embed.add_field(
            name="⚙️ 기타 – 유메랑 잘 지내는 팁",
            value=(
                "- 주요 명령어들은 대부분 `!텍스트`랑 `/슬래시` 둘 다 지원하는 **하이브리드 형식**이야.\n"
                "- 슬래시 명령어가 안 보이거나 꼬였으면, 관리자나 개발자가 `/sync`로 다시 정리할 수 있어.\n"
                "- 유메의 말투·기분·호감도는 내부 엔진에서 서서히 바뀌고 있어서,\n"
                "  같은 명령어라도 **타이밍·대화 히스토리**에 따라 미묘하게 다른 반응이 나올 수 있어.\n"
                "- 기본적으로 모든 유저는 유메 입장에서 **후배**라서, 특별한 설정이 없는 한 그렇게 생각하고 챙길 거야."
            ),
            inline=False,
        )

        # DM으로 보내기 시도
        try:
            await ctx.author.send(embed=embed)
            notice = (
                "도움말은 DM으로 정리해서 보내 뒀어.\n"
                "천천히 읽어보고, 헷갈리면 또 `!도움` 해도 돼~"
            )
        except discord.Forbidden:
            notice = (
                "DM이 막혀 있어서 여기 채널에만 알려줄게.\n"
                "나중에 DM 허용해 두면, 유메가 조용히 정리본을 보내줄 수도 있어."
            )

        # 채널엔 짧은 안내만
        if ctx.interaction:
            await ctx.send(notice, ephemeral=True)
        else:
            await ctx.send(notice)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
