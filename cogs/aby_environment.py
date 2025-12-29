from __future__ import annotations

import datetime
import random
import time
from typing import Optional

import discord
from discord.ext import commands

from yume_send import send_ctx
from yume_store import ensure_world_weather_rotated, get_world_state, set_world_weather


OWNER_ID = 1433962010785349634

KST = datetime.timezone(datetime.timedelta(hours=9))


WEATHER_LABEL = {
    "clear": "맑음",
    "cloudy": "흐림",
    "sandstorm": "대형 모래폭풍",
}


def _fmt_kst(ts: int) -> str:
    if not ts:
        return "-"
    dt = datetime.datetime.fromtimestamp(int(ts), tz=KST)
    return dt.strftime("%m/%d %H:%M")


def _roll_next_change_at(now: int) -> int:
    return int(now + random.randint(4 * 3600, 6 * 3600))


def _normalize_weather(arg: str) -> Optional[str]:
    a = (arg or "").strip().lower()
    if a in ("맑음", "clear", "sun", "sunny"):
        return "clear"
    if a in ("흐림", "cloudy", "cloud"):
        return "cloudy"
    if a in ("모래", "모래폭풍", "폭풍", "sandstorm", "storm"):
        return "sandstorm"
    return None


def _weather_one_liner(weather: str) -> str:
    w = (weather or "clear").strip()
    if w == "sandstorm":
        return "으아아… 퉤퉤! 입에 모래가 다 들어왔어… 잠깐만… 지…지지직…"
    if w == "cloudy":
        return "흐음~ 하늘이 좀 흐리네. 그래도 포스터는… 붙일 수 있겠지? 에헤헤."
    return "오늘 날씨 좋다! 포스터 붙이기 딱이야~"


class AbyEnvironmentCog(commands.Cog):
    """Phase1: 아비도스 가상 날씨(환경) 조회/관리"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="날씨")
    async def weather_status(self, ctx: commands.Context) -> None:
        """현재 아비도스 가상 날씨를 보여줘요."""
        try:
            # Phase1: lazy rotation (no background task)
            state = ensure_world_weather_rotated()
        except Exception:
            await send_ctx(ctx, "날씨 기록을 읽다가 모래가… 들어갔나 봐. 잠깐 뒤에 다시 해줄래?", allow_glitch=False)
            return

        weather = str(state.get("weather") or "clear")
        label = WEATHER_LABEL.get(weather, weather)

        changed_at = int(state.get("weather_changed_at") or 0)
        next_at = int(state.get("weather_next_change_at") or 0)

        embed = discord.Embed(
            title="아비도스 환경 리포트",
            description=_weather_one_liner(weather),
            color=discord.Color.gold(),
        )
        embed.add_field(name="현재 날씨", value=f"`{label}`", inline=True)
        embed.add_field(name="마지막 변화", value=f"`{_fmt_kst(changed_at)}`", inline=True)
        embed.add_field(name="다음 변화(예상)", value=f"`{_fmt_kst(next_at)}`", inline=True)

        await ctx.send(embed=embed)

    @commands.command(name="날씨설정")
    async def weather_set(self, ctx: commands.Context, *, weather_arg: str) -> None:
        """!날씨설정 <맑음|흐림|모래폭풍>

        - OWNER 또는 서버 관리 권한이 있는 사람만.
        """
        if ctx.guild is not None:
            is_manager = bool(getattr(ctx.author, "guild_permissions", None) and ctx.author.guild_permissions.manage_guild)
        else:
            is_manager = False

        if int(ctx.author.id) != OWNER_ID and not is_manager:
            await send_ctx(ctx, "이건… 학생회장(유메) 비상 스위치라서, 아무나 만지면 안 돼~", allow_glitch=False)
            return

        w = _normalize_weather(weather_arg)
        if not w:
            await send_ctx(ctx, "음… 그건 날씨로 인식이 안 돼. `맑음/흐림/모래폭풍` 중에서 골라줘~", allow_glitch=False)
            return

        now = int(time.time())
        next_at = _roll_next_change_at(now)
        try:
            set_world_weather(w, changed_at=now, next_change_at=next_at)
        except Exception:
            await send_ctx(ctx, "설정하다가 모래폭풍이… 덮쳤어. 다시 한 번만!", allow_glitch=False)
            return

        await send_ctx(ctx, f"오케이~ 지금부터 `{WEATHER_LABEL.get(w, w)}`! 다음 변화는 `{_fmt_kst(next_at)}`쯤이야.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AbyEnvironmentCog(bot))
