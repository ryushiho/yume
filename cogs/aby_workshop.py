from __future__ import annotations

import logging
import re
from typing import Optional

from discord.ext import commands

from yume_honorific import get_honorific
from yume_send import send_ctx
from yume_store import (
    craft_user_items,
    sell_user_item,
    get_user_inventory,
)

logger = logging.getLogger(__name__)


# Phase6-2 Phase4: 공방(제작/판매)
# - 탐사로 얻는 재료(고철/천조각/필터/배터리/회로기판)로 아이템을 제작하거나 판매할 수 있어요.

MATERIAL_KEYS = {"scrap", "cloth", "filter", "battery", "circuit"}

ITEM_NAMES = {
    "mask": "방진마스크",
    "drone": "탐사용 드론",
    "kit": "탐사키트",
    "scrap": "고철",
    "cloth": "천조각",
    "filter": "필터",
    "battery": "배터리",
    "circuit": "회로기판",
}

ITEM_ALIASES = {
    # craft targets
    "방진": "mask",
    "마스크": "mask",
    "방진마스크": "mask",

    "드론": "drone",
    "탐사용드론": "drone",
    "탐사드론": "drone",

    "탐사키트": "kit",
    "키트": "kit",

    # materials
    "고철": "scrap",
    "스크랩": "scrap",
    "부품": "scrap",

    "천": "cloth",
    "천조각": "cloth",

    "필터": "filter",

    "배터리": "battery",

    "회로": "circuit",
    "기판": "circuit",
    "회로기판": "circuit",
}

SELL_PRICES = {
    "scrap": 800,
    "cloth": 500,
    "filter": 1200,
    "battery": 1500,
    "circuit": 1800,
}

RECIPES = {
    "mask": {
        "name": "방진마스크",
        "cost": 2000,
        "req": {"cloth": 2, "filter": 1},
        "out": {"mask": 1},
        "desc": "2시간 동안 모래폭풍 페널티 완화",
    },
    "drone": {
        "name": "탐사용 드론",
        "cost": 5000,
        "req": {"scrap": 5, "battery": 1, "circuit": 1},
        "out": {"drone": 1},
        "desc": "다음 탐사 크레딧 +25% (1회)",
    },
    "kit": {
        "name": "탐사키트",
        "cost": 3000,
        "req": {"scrap": 3, "cloth": 1},
        "out": {"kit": 1},
        "desc": "다음 탐사 성공률 +10% (1회)",
    },
}


def _fmt(n: int) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _resolve_item_key(raw: str) -> Optional[str]:
    s = re.sub(r"\s+", "", (raw or "")).lower()
    if not s:
        return None
    if s in ITEM_ALIASES:
        return ITEM_ALIASES[s]
    # try by display name
    for k, name in ITEM_NAMES.items():
        if s == re.sub(r"\s+", "", name).lower():
            return k
    return None


def _parse_qty(token: str) -> Optional[int]:
    t = (token or "").strip().lower()
    if not t:
        return None
    if t in {"all", "전체", "전부", "올인"}:
        return -1
    if re.fullmatch(r"\d+", t):
        v = int(t)
        return v if v > 0 else None
    return None


class AbyWorkshopCog(commands.Cog):
    """Abydos 공방 (Phase6-2 Phase4)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="공방", aliases=["상점", "제작소"])
    async def workshop_info(self, ctx: commands.Context):
        hon = get_honorific(ctx.author, ctx.guild)

        lines: list[str] = [
            f"{hon} 공방 열었어.",
            "- 제작: `!제작 <아이템>`",
            "- 판매: `!판매 <재료> [수량|전체]`",
            "- 보유 확인: `!가방`",
            "",
            "**제작 레시피**",
        ]

        for key, r in RECIPES.items():
            req = r["req"]
            req_txt = ", ".join([f"{ITEM_NAMES.get(k, k)} x{v}" for k, v in req.items()])
            lines.append(f"- {r['name']} ({_fmt(r['cost'])}크레딧) :: {req_txt}")
            lines.append(f"  └ {r['desc']}")

        lines.append("\n**재료 판매가(1개당)**")
        for k, p in SELL_PRICES.items():
            lines.append(f"- {ITEM_NAMES.get(k, k)}: {_fmt(p)} 크레딧")

        await send_ctx(ctx, "\n".join(lines), allow_glitch=True)

    @commands.command(name="제작")
    async def craft(self, ctx: commands.Context, *, item_name: str = ""):
        hon = get_honorific(ctx.author, ctx.guild)
        raw = (item_name or "").strip()
        if not raw:
            await send_ctx(ctx, f"{hon} 제작법: `!제작 <아이템>`\n예) `!제작 방진마스크` / `!제작 드론` / `!제작 탐사키트`", allow_glitch=True)
            return

        key = _resolve_item_key(raw)
        if key not in RECIPES:
            await send_ctx(ctx, f"{hon} 그건 제작 레시피가 없어. `!공방`에서 목록 확인해줘.", allow_glitch=True)
            return

        r = RECIPES[key]
        memo = f"craft:{key}"
        res = craft_user_items(
            ctx.author.id,
            cost_credits=int(r["cost"]),
            req_items=dict(r["req"]),
            out_items=dict(r["out"]),
            memo=memo,
        )

        if not res.get("ok"):
            reason = str(res.get("reason") or "")
            if reason == "credits":
                have = int(res.get("credits") or 0)
                need = int(res.get("need") or r["cost"])
                await send_ctx(ctx, f"{hon} 크레딧이 부족해… (보유 {_fmt(have)} / 필요 {_fmt(need)})", allow_glitch=True)
                return
            if reason == "items":
                missing = res.get("missing") or []
                miss_txt = ", ".join([f"{ITEM_NAMES.get(k, k)} x{need-have} 부족" for k, need, have in missing])
                await send_ctx(ctx, f"{hon} 재료가 모자라…\n- {miss_txt}\n`!탐사`로 재료를 더 모아보자.", allow_glitch=True)
                return
            await send_ctx(ctx, f"{hon} 제작이… 뭔가 이상하게 실패했어. (reason={reason})", allow_glitch=True)
            return

        after = int(res.get("credits_after") or 0)
        await send_ctx(ctx, f"{hon} 제작 완료! **{r['name']}** x1\n현재 크레딧: **{_fmt(after)}**\n`!가방`에서 확인해봐.")

    @commands.command(name="판매")
    async def sell(self, ctx: commands.Context, *, args: str = ""):
        hon = get_honorific(ctx.author, ctx.guild)
        raw = (args or "").strip()
        if not raw:
            await send_ctx(ctx, f"{hon} 판매법: `!판매 <재료> [수량|전체]`\n예) `!판매 고철 3` / `!판매 회로 전체`", allow_glitch=True)
            return

        parts = raw.split()
        qty = 1
        item_part = raw
        if len(parts) >= 2:
            q = _parse_qty(parts[-1])
            if q is not None:
                qty = q
                item_part = " ".join(parts[:-1])

        key = _resolve_item_key(item_part)
        if key not in MATERIAL_KEYS or key not in SELL_PRICES:
            await send_ctx(ctx, f"{hon} 그건 판매 가능한 '재료'가 아니야. `!공방`에서 판매 목록 확인해줘.", allow_glitch=True)
            return

        price = int(SELL_PRICES[key])
        res = sell_user_item(
            ctx.author.id,
            item_key=key,
            qty=qty,
            unit_price=price,
            memo=f"sell:{key}",
        )

        if not res.get("ok"):
            reason = str(res.get("reason") or "")
            if reason in {"no_item", "qty"}:
                inv = get_user_inventory(ctx.author.id)
                have = int(inv.get(key) or 0)
                await send_ctx(ctx, f"{hon} 그 재료가 부족해. (보유 {ITEM_NAMES.get(key, key)} x{have})", allow_glitch=True)
                return
            await send_ctx(ctx, f"{hon} 판매가… 뭔가 실패했어. (reason={reason})", allow_glitch=True)
            return

        sold = int(res.get("sold") or 0)
        earned = int(res.get("earned") or 0)
        after = int(res.get("credits_after") or 0)
        await send_ctx(
            ctx,
            f"{hon} 판매 완료! {ITEM_NAMES.get(key, key)} x{sold} → **+{_fmt(earned)} 크레딧**\n현재 크레딧: **{_fmt(after)}**",
            allow_glitch=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AbyWorkshopCog(bot))

