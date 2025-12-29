from __future__ import annotations

import datetime
import logging
import random
import re
import time
from typing import Optional

import discord
from discord.ext import commands, tasks

from yume_send import send_ctx
from yume_store import (
    ABY_DEFAULT_DEBT,
    ABY_DEFAULT_INTEREST_RATE,
    apply_guild_interest_upto_today,
    debt_pressure_stage,
    ensure_world_weather_rotated,
    get_config,
    get_guild_debt,
    get_world_state,
    list_aby_debt_guild_ids,
    set_config,
    set_world_weather,
)


logger = logging.getLogger(__name__)


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


def _roll_next_change_at(now: int, weather: str) -> int:
    """Phase2: 날씨별 지속 시간을 랜덤으로 굴려요."""

    w = (weather or "clear").strip()
    if w == "sandstorm":
        return int(now + random.randint(45 * 60, 120 * 60))
    if w == "cloudy":
        return int(now + random.randint(3 * 3600, 6 * 3600))
    return int(now + random.randint(4 * 3600, 8 * 3600))


def _normalize_weather(arg: str) -> Optional[str]:
    a = (arg or "").strip().lower()
    if a in ("맑음", "clear", "sun", "sunny"):
        return "clear"
    if a in ("흐림", "cloudy", "cloud"):
        return "cloudy"
    if a in ("모래", "모래폭풍", "폭풍", "sandstorm", "storm"):
        return "sandstorm"
    return None


def _today_ymd_kst() -> str:
    return datetime.datetime.now(tz=KST).date().isoformat()


def _weather_one_liner(weather: str) -> str:
    w = (weather or "clear").strip()
    if w == "sandstorm":
        return "으아아… 퉤퉤! 입에 모래가 다 들어왔어… 잠깐만… 지…지지직…"
    if w == "cloudy":
        return "흐음~ 하늘이 좀 흐리네. 그래도 포스터는… 붙일 수 있겠지? 에헤헤."
    return "오늘 날씨 좋다! 포스터 붙이기 딱이야~"


class AbyEnvironmentCog(commands.Cog):
    """Phase2: 아비도스 가상 날씨(환경)

    - 자동(백그라운드) 로테이션: next_change_at이 지나면 주기적으로 알아서 갱신
    - 변화 시간 랜덤(날씨별 지속 시간 범위)
    - (옵션) 날씨 변화 알림 채널 지원
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not self._weather_loop.is_running():
            self._weather_loop.start()
        if not self._debt_loop.is_running():
            self._debt_loop.start()

    def cog_unload(self) -> None:
        try:
            self._weather_loop.cancel()
        except Exception:
            pass

        try:
            self._debt_loop.cancel()
        except Exception:
            pass

    @tasks.loop(seconds=60)
    async def _weather_loop(self) -> None:
        """Background weather rotation.

        - Safe: only writes when the scheduled time has passed.
        - Does not spam by default; announcements are opt-in.
        """

        try:
            prev = get_world_state()
            now = int(time.time())
            next_at = int(prev.get("weather_next_change_at") or 0)
            if next_at > 0 and now < next_at:
                return

            prev_weather = str(prev.get("weather") or "clear")
            prev_changed_at = int(prev.get("weather_changed_at") or 0)

            new_state = ensure_world_weather_rotated(now_ts=now)
            new_weather = str(new_state.get("weather") or "clear")
            new_changed_at = int(new_state.get("weather_changed_at") or 0)

            # If rotated (changed_at updated), announce if configured.
            if new_changed_at != prev_changed_at:
                await self._maybe_announce_change(prev_weather, new_weather, new_state)

        except Exception:
            logger.exception("AbyEnvironment: weather loop failed")

    @_weather_loop.before_loop
    async def _before_weather_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------
    # Phase6: Debt auto-interest + announcements
    # ------------------------------

    @tasks.loop(minutes=5)
    async def _debt_loop(self) -> None:
        """Background debt interest application.

        Safe by design:
        - Interest is applied *at most once* per KST day (tracked in DB).
        - Running frequently is ok; no repeated compounding.
        """

        try:
            today = _today_ymd_kst()
            guild_ids = list_aby_debt_guild_ids()
            if not guild_ids:
                return

            for gid in guild_ids:
                res = apply_guild_interest_upto_today(int(gid), today)
                applied_days = int(res.get("applied_days") or 0)
                if applied_days <= 0:
                    continue
                await self._maybe_announce_debt_update(int(gid), res, today)

        except Exception:
            logger.exception("AbyEnvironment: debt loop failed")

    @_debt_loop.before_loop
    async def _before_debt_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _maybe_announce_debt_update(self, guild_id: int, res: dict, today_ymd: str) -> None:
        key_chan = f"aby_debt_announce_channel_id:{int(guild_id)}"
        chan_id_s = get_config(key_chan, None)
        if not chan_id_s:
            return

        try:
            chan_id = int(chan_id_s)
        except Exception:
            return

        key_last = f"aby_debt_last_announce_ymd:{int(guild_id)}"
        last_ymd = str(get_config(key_last, "") or "")
        if last_ymd == str(today_ymd):
            return

        ch = self.bot.get_channel(chan_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(chan_id)
            except Exception:
                return

        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return

        s = get_guild_debt(int(guild_id))
        new_debt = int(s.get("debt", ABY_DEFAULT_DEBT))
        rate = float(s.get("interest_rate", ABY_DEFAULT_INTEREST_RATE))

        old_debt = int(res.get("old_debt") or new_debt)
        applied_days = int(res.get("applied_days") or 0)
        delta = max(0, new_debt - old_debt)

        stage = debt_pressure_stage(new_debt, initial=ABY_DEFAULT_DEBT)
        stage_txt = f"{stage.get('emoji', '')} {stage.get('stage', '')}".strip()

        note = ""
        if applied_days > 1:
            note = f"(봇이 꺼져있던 기간 포함: {applied_days}일치 이자 반영)"

        embed = discord.Embed(
            title="아비도스 채무 갱신",
            description=f"오늘도 빚이… 자랐어. {note}".strip(),
        )
        embed.add_field(name="현재 빚", value=f"{new_debt:,}", inline=True)
        embed.add_field(name="증가", value=f"+{delta:,}", inline=True)
        embed.add_field(name="일일 이자율", value=f"{rate * 100:.2f}%", inline=True)
        if stage_txt:
            embed.add_field(name="채무 압박", value=stage_txt, inline=True)

        try:
            await ch.send(embed=embed)
        except Exception:
            return

        # Mark announced for today (even if we applied multiple days)
        try:
            set_config(key_last, str(today_ymd))
        except Exception:
            pass

    async def _maybe_announce_change(self, prev_weather: str, new_weather: str, state: dict) -> None:
        chan_id_s = get_config("aby_weather_announce_channel_id", None)
        if not chan_id_s:
            return

        try:
            chan_id = int(chan_id_s)
        except Exception:
            return

        ch = self.bot.get_channel(chan_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(chan_id)
            except Exception:
                return

        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return

        label_prev = WEATHER_LABEL.get(prev_weather, prev_weather)
        label_new = WEATHER_LABEL.get(new_weather, new_weather)

        changed_at = int(state.get("weather_changed_at") or 0)
        next_at = int(state.get("weather_next_change_at") or 0)

        embed = discord.Embed(
            title="아비도스 환경 변화",
            description=_weather_one_liner(new_weather),
            color=discord.Color.orange(),
        )
        embed.add_field(name="변화", value=f"`{label_prev}` → `{label_new}`", inline=False)
        embed.add_field(name="변화 시각", value=f"`{_fmt_kst(changed_at)}`", inline=True)
        embed.add_field(name="다음 변화(예상)", value=f"`{_fmt_kst(next_at)}`", inline=True)

        try:
            await ch.send(embed=embed)
        except Exception:
            # Ignore send failures (missing perms etc.)
            return

    @commands.command(name="날씨")
    async def weather_status(self, ctx: commands.Context) -> None:
        """현재 아비도스 가상 날씨를 보여줘요."""
        try:
            # Phase2: background loop also rotates; this is still safe and idempotent.
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

    @commands.command(name="날씨알림")
    async def weather_announce(self, ctx: commands.Context, *, arg: str = "") -> None:
        """!날씨알림 [#채널|끄기]

        - 설정하면 날씨가 바뀔 때 자동으로 공지해요.
        - 기본은 OFF(스팸 방지).
        """

        if ctx.guild is not None:
            is_manager = bool(getattr(ctx.author, "guild_permissions", None) and ctx.author.guild_permissions.manage_guild)
        else:
            is_manager = False

        if int(ctx.author.id) != OWNER_ID and not is_manager:
            await send_ctx(ctx, "이건… 공지 채널 설정이라서, 관리자만 만질 수 있어.", allow_glitch=False)
            return

        a = (arg or "").strip()
        if not a:
            cur = get_config("aby_weather_announce_channel_id", None)
            if cur:
                await send_ctx(ctx, f"날씨 알림: ON (채널 ID `{cur}`)")
            else:
                await send_ctx(ctx, "날씨 알림: OFF\n켜려면 `!날씨알림 #채널` 또는 끄려면 `!날씨알림 끄기`")
            return

        if a in {"끄기", "off", "OFF", "해제", "없음"}:
            set_config("aby_weather_announce_channel_id", "")
            await send_ctx(ctx, "오케이~ 이제부터 날씨 변화 공지는 안 해. (OFF)")
            return

        # Accept channel mention: <#id>
        ch = None
        if ctx.message.channel_mentions:
            ch = ctx.message.channel_mentions[0]
        else:
            # Try parse raw id
            try:
                cid = int(re.sub(r"[^0-9]", "", a))
                ch = self.bot.get_channel(cid)
            except Exception:
                ch = None

        if ch is None:
            await send_ctx(ctx, "음… 채널을 못 찾았어. `#채널`로 지정해줘.")
            return

        set_config("aby_weather_announce_channel_id", str(int(ch.id)))
        await send_ctx(ctx, f"좋아! 이제 날씨 바뀌면 {ch.mention} 에 공지할게.")

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
        next_at = _roll_next_change_at(now, w)
        try:
            set_world_weather(w, changed_at=now, next_change_at=next_at)
        except Exception:
            await send_ctx(ctx, "설정하다가 모래폭풍이… 덮쳤어. 다시 한 번만!", allow_glitch=False)
            return

        await send_ctx(ctx, f"오케이~ 지금부터 `{WEATHER_LABEL.get(w, w)}`! 다음 변화는 `{_fmt_kst(next_at)}`쯤이야.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AbyEnvironmentCog(bot))
