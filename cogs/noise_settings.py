from __future__ import annotations

from discord.ext import commands

from yume_store import get_user_settings, set_user_opt_in


def _fmt_onoff(v: int) -> str:
    return "ON" if int(v or 0) == 1 else "OFF"


class NoiseSettingsCog(commands.Cog):
    """Phase2: 개인별 '무전기 노이즈(Glitch)' 수신 설정.

    - 모래폭풍(sandstorm) 상태일 때만, 유메의 메시지에 가끔 노이즈 연출이 들어갈 수 있어요.
    - 채널의 다른 사람도 같은 메시지를 보게 되므로, "대화 상대(명령어/멘션한 사람)" 기준으로
      적용 여부를 결정해요.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="노이즈")
    async def noise(self, ctx: commands.Context, arg: str | None = None) -> None:
        """!노이즈 [on/off]

        예)
          - !노이즈        : 현재 설정 확인
          - !노이즈 on     : 노이즈 연출 허용
          - !노이즈 off    : 노이즈 연출 끄기
        """
        user_id = int(ctx.author.id)
        st = get_user_settings(user_id)

        if arg is None:
            await ctx.send(
                "📻 무전기 노이즈 설정\n"
                f"- 현재: **{_fmt_onoff(int(st.get('noise_opt_in') or 0))}**\n"
                "- 모래폭풍일 때, 유메가 가끔 '지지직…' 하고 끊겨 보일 수 있어요.\n"
                "- 변경: `!노이즈 on` / `!노이즈 off`",
            )
            return

        a = (arg or "").strip().lower()
        if a in ("on", "켜", "켜기", "1", "true", "yes"):
            set_user_opt_in(user_id, noise_opt_in=True)
            await ctx.send("오케이~ 모래폭풍이 와도… 유메 무전, 받아줄게! 📻")
            return

        if a in ("off", "꺼", "끄", "끄기", "0", "false", "no"):
            set_user_opt_in(user_id, noise_opt_in=False)
            await ctx.send("알겠어. 모래폭풍이어도 메시지는 최대한 또렷하게 보낼게~")
            return

        await ctx.send("음… `on` 아니면 `off` 로만 부탁해~ 예: `!노이즈 on`")


async def setup(bot: commands.Bot):
    await bot.add_cog(NoiseSettingsCog(bot))
