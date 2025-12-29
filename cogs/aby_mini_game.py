from __future__ import annotations

import datetime
import logging
import random
import re
from typing import Optional, Tuple

import discord
from discord.ext import commands

from yume_honorific import get_honorific
from yume_send import send_ctx
from yume_store import (
    apply_guild_interest_upto_today,
    claim_daily_explore,
    get_guild_debt,
    get_user_economy,
    repay_guild_debt,
    ABY_DEFAULT_DEBT,
    ABY_DEFAULT_INTEREST_RATE,
)

logger = logging.getLogger(__name__)


def _now_kst() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=9)


def _today_ymd_kst() -> str:
    return _now_kst().date().isoformat()


def _fmt(n: int) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


_KOREAN_UNIT = {
    "천": 1_000,
    "만": 10_000,
    "억": 100_000_000,
}


def _parse_amount(raw: str) -> Optional[int]:
    """Parse user amount.

    Supports:
    - digits with commas: 1,234
    - suffix: k, m, b (1k=1000, 1m=1_000_000, 1b=1_000_000_000)
    - simple Korean units: 3만, 2억
    """

    if not raw:
        return None

    s = raw.strip().lower().replace(",", "")
    if not s:
        return None

    if s in {"all", "전체", "전부", "올인"}:
        return -1

    m = re.fullmatch(r"(\d+)([kmb])", s)
    if m:
        v = int(m.group(1))
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[m.group(2)]
        return v * mult

    # Korean unit (single unit)
    m = re.fullmatch(r"(\d+)(천|만|억)", s)
    if m:
        v = int(m.group(1))
        return v * _KOREAN_UNIT[m.group(2)]

    # plain integer
    if re.fullmatch(r"\d+", s):
        return int(s)

    return None


class AbyMiniGameCog(commands.Cog):
    """Abydos 탐사/부채 미니게임 (Phase6-2)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------
    # Help
    # ------------------------------

    @commands.command(name="탐사지원")
    async def explore_help(self, ctx: commands.Context):
        hon = get_honorific(ctx.author)
        txt = (
            f"{hon}~ 아비도스 탐사 지원센터야. (유메 선배가 만든... 잔뜩 허술한 안내서)\n\n"
            "**기본 커맨드**\n"
            "- `!탐사` : 하루 1회 탐사해서 크레딧(가끔 물) 얻기\n"
            "- `!지갑` : 내 재화 확인\n"
            "- `!빚현황` : 우리 학교 빚/이자 확인\n"
            "- `!빚상환 <금액|전체>` : 내 크레딧으로 빚 상환\n\n"
            "**스토리**\n"
            f"- 시작 빚: **{_fmt(ABY_DEFAULT_DEBT)} 크레딧**\n"
            f"- 일일 이자율: **{ABY_DEFAULT_INTEREST_RATE:.2f}** (매일 한 번 적용)\n\n"
            "으헤헤… 갚을 수 있을지는 모르겠지만, 그래도… 같이 노력해보자.\n"
        )
        await send_ctx(ctx, txt, allow_glitch=True)

    # ------------------------------
    # Wallet
    # ------------------------------

    @commands.command(name="지갑", aliases=["내지갑", "재화"])
    async def wallet(self, ctx: commands.Context):
        econ = get_user_economy(ctx.author.id)
        credits = int(econ.get("credits", 0))
        water = int(econ.get("water", 0))
        hon = get_honorific(ctx.author)

        txt = (
            f"{hon} 지갑 확인~\n"
            f"- 크레딧: **{_fmt(credits)}**\n"
            f"- 물: **{_fmt(water)}**\n"
        )
        await send_ctx(ctx, txt, allow_glitch=True)

    # ------------------------------
    # Explore (daily)
    # ------------------------------

    @commands.command(name="탐사")
    async def explore(self, ctx: commands.Context):
        if ctx.guild is None:
            await send_ctx(ctx, "이건… 아비도스에서만 할 수 있어. 서버에서 불러줘… 으헤~", allow_glitch=True)
            return

        today = _today_ymd_kst()
        hon = get_honorific(ctx.author)

        # Reward balance (MVP)
        # Success: 7~16k credits; Fail: 0~3k
        success = random.random() < 0.72
        if success:
            credits = random.randint(7_000, 16_000)
        else:
            credits = random.randint(0, 3_000)

        water = 0
        if random.random() < 0.06:
            water = 1

        result = claim_daily_explore(ctx.author.id, today, credits, water)
        if result is None:
            txt = (
                f"{hon}… 오늘은 이미 탐사 다녀왔어.\n"
                "하루 1회만! (유메 선배 수첩에 적혀있어…)\n"
            )
            await send_ctx(ctx, txt, allow_glitch=True)
            return

        new_credits = int(result.get("credits", 0))
        new_water = int(result.get("water", 0))

        # Flavor
        if success:
            flavor = random.choice(
                [
                    "모래 사이에서 반짝이는 걸 발견했어!",
                    "오아시스…는 아니지만, 그 근처였던 것 같아…",
                    "호시노 짱이랑 같이 걸었다고 상상하니까 힘이 나네…",
                ]
            )
        else:
            flavor = random.choice(
                [
                    "바람이 너무 세서 거의 아무것도 못 챙겼어… 퉤퉤.",
                    "발자국만 잔뜩 남겼다… 다음엔 더 잘할 수 있어.",
                    "모래가… 입에… 들어왔어… 으아아…",
                ]
            )

        gained = f"**+{_fmt(credits)} 크레딧**"
        if water > 0:
            gained += f"  +{water} 물"

        txt = (
            f"{hon} 탐사 결과!\n"
            f"{flavor}\n"
            f"획득: {gained}\n\n"
            f"현재 보유: 크레딧 **{_fmt(new_credits)}**, 물 **{_fmt(new_water)}**\n"
        )
        await send_ctx(ctx, txt, allow_glitch=True)

    # ------------------------------
    # Debt
    # ------------------------------

    @commands.command(name="빚현황", aliases=["부채", "빚"])
    async def debt_status(self, ctx: commands.Context):
        if ctx.guild is None:
            await send_ctx(ctx, "빚은… 서버(아비도스) 단위로 관리해. 서버에서 확인해줘…", allow_glitch=True)
            return

        today = _today_ymd_kst()
        apply_guild_interest_upto_today(ctx.guild.id, today)
        s = get_guild_debt(ctx.guild.id)

        debt = int(s.get("debt", ABY_DEFAULT_DEBT))
        rate = float(s.get("interest_rate", ABY_DEFAULT_INTEREST_RATE))
        last = str(s.get("last_interest_ymd", ""))

        hon = get_honorific(ctx.author)
        txt = (
            f"{hon}… 아비도스 재정 보고서 가져왔어.\n"
            f"- 현재 빚: **{_fmt(debt)} 크레딧**\n"
            f"- 일일 이자율: **{rate:.2f}**\n"
            f"- 오늘 기준 적용일: `{last}`\n\n"
            "이 숫자… 계속 커져. 그래서 더… 다 같이 버티는 거야. 으헤~\n"
        )
        await send_ctx(ctx, txt, allow_glitch=True)

    @commands.command(name="빚상환")
    async def repay(self, ctx: commands.Context, *, amount: str = ""):
        if ctx.guild is None:
            await send_ctx(ctx, "빚상환은… 서버에서만 가능해. 아비도스 공동 목표니까!", allow_glitch=True)
            return

        today = _today_ymd_kst()
        econ = get_user_economy(ctx.author.id)
        credits = int(econ.get("credits", 0))

        parsed = _parse_amount(amount)
        if parsed is None:
            await send_ctx(
                ctx,
                "사용법: `!빚상환 <금액|전체>`\n예) `!빚상환 50000` / `!빚상환 3만` / `!빚상환 전체`",
                allow_glitch=True,
            )
            return

        if parsed == -1:
            amt = credits
        else:
            amt = parsed

        if amt <= 0:
            await send_ctx(ctx, "상환 금액이… 0 이하야. 으헤~", allow_glitch=True)
            return

        res = repay_guild_debt(ctx.guild.id, ctx.author.id, amt, today)
        hon = get_honorific(ctx.author)
        if not res.get("ok"):
            reason = res.get("reason")
            if reason == "no_credits":
                await send_ctx(ctx, f"{hon}… 지갑이 텅 비었어. 탐사부터 다녀오자…", allow_glitch=True)
                return
            await send_ctx(ctx, f"{hon}… 뭔가 이상해. (reason={reason})", allow_glitch=True)
            return

        paid = int(res.get("paid", 0))
        old_debt = int(res.get("old_debt", 0))
        new_debt = int(res.get("new_debt", 0))
        after = int(res.get("credits_after", 0))

        txt = (
            f"{hon} 상환 완료!\n"
            f"- 납부: **{_fmt(paid)} 크레딧**\n"
            f"- 빚: **{_fmt(old_debt)}** → **{_fmt(new_debt)}**\n"
            f"- 내 지갑: **{_fmt(after)}**\n\n"
            "조금 줄었어…! (내일 이자가 또 오겠지만… 그래도 의미 있어.)\n"
        )
        await send_ctx(ctx, txt, allow_glitch=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AbyMiniGameCog(bot))
