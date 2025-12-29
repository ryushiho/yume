from __future__ import annotations

import datetime
import logging
import random
import re
import time
from decimal import Decimal, ROUND_CEILING
from typing import Optional

import discord
from discord.ext import commands

from yume_honorific import get_honorific
from yume_send import send_ctx
from yume_store import (
    apply_guild_interest_upto_today,
    claim_daily_explore,
    upsert_explore_meta,
    ensure_world_weather_rotated,
    get_guild_debt,
    get_user_economy,
    get_user_inventory,
    add_user_item,
    consume_user_item,
    ensure_user_buff_valid,
    set_user_buff,
    consume_user_buff_stack,
    repay_guild_debt,
    ABY_DEFAULT_DEBT,
    ABY_DEFAULT_INTEREST_RATE,
)

logger = logging.getLogger(__name__)


WEATHER_LABEL = {
    "clear": "맑음",
    "cloudy": "흐림",
    "sandstorm": "대형 모래폭풍",
}



# Phase3~4: 전리품/아이템(가벼운 제작/판매 재료 포함)
# - usable: mask, drone, kit
# - materials: scrap/cloth/filter/battery/circuit (공방 재료)
ITEMS = {
    "mask": {"name": "방진마스크", "desc": "2시간 동안 모래폭풍 페널티 완화"},
    "drone": {"name": "탐사용 드론", "desc": "다음 탐사 크레딧 +25% (1회)"},
    "kit": {"name": "탐사키트", "desc": "다음 탐사 성공률 +10% (1회)"},

    # Materials (Phase4)
    "scrap": {"name": "고철", "desc": "공방 재료(제작/판매용)"},
    "cloth": {"name": "천조각", "desc": "공방 재료(제작/판매용)"},
    "filter": {"name": "필터", "desc": "공방 재료(제작/판매용)"},
    "battery": {"name": "배터리", "desc": "공방 재료(제작/판매용)"},
    "circuit": {"name": "회로기판", "desc": "공방 재료(제작/판매용)"},
}



ITEM_ALIASES = {
    "방진": "mask",
    "마스크": "mask",
    "방진마스크": "mask",

    "드론": "drone",
    "탐사용드론": "drone",
    "탐사드론": "drone",

    "탐사키트": "kit",
    "키트": "kit",

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


MATERIAL_KEYS = {"scrap", "cloth", "filter", "battery", "circuit"}


def _apply_interest_once(debt: int, rate: float) -> int:
    """Match yume_store._apply_interest_once rounding (ceil)."""

    d = Decimal(int(debt))
    r = Decimal(str(rate))
    new_val = (d * (Decimal("1") + r)).to_integral_value(rounding=ROUND_CEILING)
    return int(new_val) if new_val > 0 else 0


def _now_kst() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=9)


def _today_ymd_kst() -> str:
    return _now_kst().date().isoformat()


def _fmt_ts_kst(ts: int) -> str:
    if not ts:
        return "-"
    dt = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone(datetime.timedelta(hours=9)))
    return dt.strftime("%m/%d %H:%M")


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
        hon = get_honorific(ctx.author, ctx.guild)
        txt = (
            f"{hon}~ 아비도스 탐사 지원센터야. (유메 선배가 만든... 잔뜩 허술한 안내서)\n\n"
            "**기본 커맨드**\n"
            "- `!탐사` : 하루 1회 탐사해서 크레딧(가끔 물) 얻기\n"
            "- `!지갑` : 내 재화 확인\n"
            "- `!가방` : 탐사 전리품(아이템) 확인\n"
            "- `!사용 <아이템>` : 아이템 사용 (버프)\n- `!공방` : 제작/판매 안내\n- `!제작 <아이템>` : 아이템 제작\n- `!판매 <재료> [수량|전체]` : 재료 판매\n- `!의뢰` : 의뢰 게시판 보기(일일/주간)\n- `!납품 <번호>` : 의뢰 보상 받기(조건 달성 시)\n- `!의뢰랭킹` : 주간 의뢰 포인트 랭킹\n"
            "- `!빚현황` : 우리 학교 빚/이자 확인\n"
            "- `!빚상환 <금액|전체>` : 내 크레딧으로 빚 상환\n\n"
            "**환경(날씨)**\n"
            "- `!날씨` : 아비도스 가상 날씨 확인\n"
            "- 날씨에 따라 `!탐사` 성공률/보상이 조금 변해\n\n"
            "**스토리**\n"
            f"- 시작 빚: **{_fmt(ABY_DEFAULT_DEBT)} 크레딧**\n"
            f"- 일일 이자율: **{ABY_DEFAULT_INTEREST_RATE * 100:.2f}%** (매일 한 번 적용)\n\n"
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
        hon = get_honorific(ctx.author, ctx.guild)

        txt = (
            f"{hon} 지갑 확인~\n"
            f"- 크레딧: **{_fmt(credits)}**\n"
            f"- 물: **{_fmt(water)}**\n"
        )
        await send_ctx(ctx, txt, allow_glitch=True)

    # ------------------------------
    # Inventory / Items (Phase3)
    # ------------------------------

    @commands.command(name="가방", aliases=["인벤", "인벤토리", "전리품"])
    async def bag(self, ctx: commands.Context):
        inv = get_user_inventory(ctx.author.id)
        buff = ensure_user_buff_valid(ctx.author.id)

        hon = get_honorific(ctx.author, ctx.guild)

        lines = [f"{hon} 가방 열어봤어."]

        # Active buff
        bkey = str(buff.get("buff_key") or "").strip().lower()
        stacks = int(buff.get("stacks") or 0)
        exp = int(buff.get("expires_at") or 0)
        if bkey and stacks > 0:
            if bkey == "mask":
                lines.append(f"- 활성 버프: **방진마스크** (만료 `{_fmt_ts_kst(exp)}`)")
            elif bkey == "drone":
                lines.append(f"- 활성 버프: **탐사용 드론** (남은 {stacks}회, 만료 `{_fmt_ts_kst(exp)}`)")
            elif bkey == "kit":
                lines.append(f"- 활성 버프: **탐사키트** (남은 {stacks}회, 만료 `{_fmt_ts_kst(exp)}`)")
            else:
                lines.append(f"- 활성 버프: **{bkey}** (만료 `{_fmt_ts_kst(exp)}`)")
        else:
            lines.append("- 활성 버프: 없음")

        # Items
        if not inv:
            lines.append("- 전리품: (텅)\n탐사하다가 주워오면 여기 쌓여. 으헤~")
        else:
            lines.append("- 전리품:")
            for k, qty in sorted(inv.items(), key=lambda x: x[0]):
                meta = ITEMS.get(k)
                name = meta.get("name") if meta else k
                lines.append(f"  • {name}: **{_fmt(qty)}**")

        lines.append("\n사용: `!사용 방진마스크` / `!사용 드론` / `!사용 탐사키트`")
        lines.append("공방: `!공방` (제작/판매)")
        await send_ctx(ctx, "\n".join(lines), allow_glitch=True)

    @commands.command(name="사용")
    async def use_item(self, ctx: commands.Context, *, item_name: str = ""):
        hon = get_honorific(ctx.author, ctx.guild)
        raw = (item_name or "").strip()
        if not raw:
            await send_ctx(ctx, f"{hon} 사용법: `!사용 <아이템>`\n예) `!사용 방진마스크` / `!사용 드론` / `!사용 탐사키트`", allow_glitch=True)
            return

        key = None
        norm = re.sub(r"\s+", "", raw).lower()

        # Direct alias
        if norm in ITEM_ALIASES:
            key = ITEM_ALIASES[norm]
        else:
            # Try match by display name
            for k, meta in ITEMS.items():
                if norm == re.sub(r"\s+", "", str(meta.get("name") or "")).lower():
                    key = k
                    break

        if key not in ITEMS:
            avail = ", ".join([m["name"] for m in ITEMS.values()])
            await send_ctx(ctx, f"{hon} 그 아이템은… 잘 모르겠어.\n가능: {avail}", allow_glitch=True)
            return



        # 재료 아이템은 여기서 사용하지 않아요(실수로 소모 방지)
        if key not in {"mask", "drone", "kit"}:
            meta = ITEMS.get(key) or {"name": key}
            await send_ctx(
                ctx,
                f"{hon} 그건 그냥 재료야.\n`!공방`에서 제작하거나 `!판매`로 팔 수 있어. (아이템: {meta.get('name')})",
                allow_glitch=True,
            )
            return

        if not consume_user_item(ctx.author.id, key, 1):

            await send_ctx(ctx, f"{hon}… 그건 가방에 없어. (`!가방` 확인!)", allow_glitch=True)
            return

        now = int(time.time())
        prev = ensure_user_buff_valid(ctx.author.id, now=now)
        prev_key = str(prev.get("buff_key") or "").strip().lower()
        prev_stacks = int(prev.get("stacks") or 0)

        if key == "mask":
            exp = now + 2 * 3600
            set_user_buff(ctx.author.id, buff_key="mask", stacks=1, expires_at=exp)
            extra = ""
            if prev_key and prev_stacks > 0 and prev_key != "mask":
                extra = " (기존 버프는 덮어썼어…)"
            await send_ctx(ctx, f"{hon} 방진마스크 장착!{extra}\n2시간 동안 모래폭풍이 좀… 덜 아파. (만료 `{_fmt_ts_kst(exp)}`)")
            return

        if key == "drone":
            exp = now + 24 * 3600
            set_user_buff(ctx.author.id, buff_key="drone", stacks=1, expires_at=exp)
            extra = ""
            if prev_key and prev_stacks > 0 and prev_key != "drone":
                extra = " (기존 버프는 덮어썼어…)"
            await send_ctx(ctx, f"{hon} 탐사용 드론 준비 완료!{extra}\n다음 탐사에서 크레딧이 **+25%** (1회). (만료 `{_fmt_ts_kst(exp)}`)")
            return


        if key == "kit":
            exp = now + 24 * 3600
            set_user_buff(ctx.author.id, buff_key="kit", stacks=1, expires_at=exp)
            extra = ""
            if prev_key and prev_stacks > 0 and prev_key != "kit":
                extra = " (기존 버프는 덮어썼어…)"
            await send_ctx(ctx, f"{hon} 탐사키트 준비 완료!{extra}\n다음 탐사에서 성공률이 **+10%** (1회). (만료 `{_fmt_ts_kst(exp)}`)")
            return

        # Fallback (shouldn't happen)
        await send_ctx(ctx, f"{hon}… 음? 뭔가 이상해. (item={key})", allow_glitch=True)
        return

    # ------------------------------
    # Explore (daily)
    # ------------------------------

    @commands.command(name="탐사")
    async def explore(self, ctx: commands.Context):
        if ctx.guild is None:
            await send_ctx(ctx, "이건… 아비도스에서만 할 수 있어. 서버에서 불러줘… 으헤~", allow_glitch=True)
            return

        today = _today_ymd_kst()
        hon = get_honorific(ctx.author, ctx.guild)

        # 중복 지급(전리품/버프 악용) 방지: 먼저 오늘 탐사 여부를 빠르게 확인한다.
        econ0 = get_user_economy(ctx.author.id)
        if str(econ0.get("last_explore_ymd") or "") == today:
            txt = (
                f"{hon}… 오늘은 이미 탐사 다녀왔어.\n"
                "하루 1회만! (유메 선배 수첩에 적혀있어…)\n"
            )
            await send_ctx(ctx, txt, allow_glitch=True)
            return

        # Phase1/2: 아비도스 날씨(환경) 연동
        # - 모래폭풍이면 성공률/보상이 깎이고, 물 드랍도 낮아짐.
        # - (Phase3) 방진마스크 버프가 있으면 "모래폭풍"을 계산상 "흐림"처럼 취급한다.
        try:
            ws = ensure_world_weather_rotated()
            weather = str(ws.get("weather") or "clear")
        except Exception:
            weather = "clear"

        buff = ensure_user_buff_valid(ctx.author.id)
        bkey = str(buff.get("buff_key") or "").strip().lower()
        bstacks = int(buff.get("stacks") or 0)

        calc_weather = weather
        mask_used = False
        if bkey == "mask" and bstacks > 0 and weather == "sandstorm":
            calc_weather = "cloudy"
            mask_used = True

        label_env = WEATHER_LABEL.get(weather, weather)
        label_calc = WEATHER_LABEL.get(calc_weather, calc_weather)

        if calc_weather == "sandstorm":
            success_p = 0.55
            succ_rng = (4_000, 12_000)
            fail_rng = (0, 2_000)
            water_p = 0.02
        elif calc_weather == "cloudy":
            success_p = 0.70
            succ_rng = (6_000, 15_000)
            fail_rng = (0, 3_000)
            water_p = 0.06
        else:
            success_p = 0.72
            succ_rng = (7_000, 16_000)
            fail_rng = (0, 3_000)
            water_p = 0.06

        # Phase4: 탐사키트(성공률 +10%, 1회)
        kit_applied = False
        if bkey == "kit" and bstacks > 0:
            success_p = min(0.90, success_p + 0.10)
            water_p = min(0.20, water_p + 0.01)
            kit_applied = True

        success = random.random() < success_p
        if success:
            credits = random.randint(*succ_rng)
        else:
            credits = random.randint(*fail_rng)

        water = 1 if (random.random() < water_p) else 0

        # Phase3: 랜덤 조우/전리품
        encounter_lines: list[str] = []
        items_to_add: list[tuple[str, int]] = []
        r = random.random()
        if r < 0.12:
            bonus = random.randint(2_000, 9_000)
            credits += bonus
            encounter_lines.append(f"- 조우: **잊혀진 상자** (+{_fmt(bonus)} 크레딧)")
        elif r < 0.17:
            loss = random.randint(1_000, 4_000)
            credits -= loss
            encounter_lines.append(f"- 조우: **모래에 미끄러짐** (-{_fmt(loss)} 크레딧)")
        elif r < 0.21:
            items_to_add.append(("mask", 1))
            encounter_lines.append("- 조우: **방진마스크**를 주웠어!")
        elif r < 0.24:
            items_to_add.append(("drone", 1))
            encounter_lines.append("- 조우: **탐사용 드론** 잔해를 살렸어!")
        elif r < 0.28:
            water += 1
            encounter_lines.append("- 조우: **물통** 발견! (+1 물)")

        # Phase4: 공방 재료(고철/천/필터/배터리/회로)
        # - 탐사 1회당 최대 1종만 드랍
        mr = random.random()
        mat_key = None
        mat_qty = 0
        if calc_weather == "sandstorm":
            # 폭풍 속엔 고철이 더 굴러다녀…
            if mr < 0.26:
                mat_key = "scrap"
                mat_qty = random.randint(2, 3)
            elif mr < 0.34:
                mat_key = "cloth"
                mat_qty = 1
            elif mr < 0.38:
                mat_key = "filter"
                mat_qty = 1
            elif mr < 0.41:
                mat_key = "battery"
                mat_qty = 1
            elif mr < 0.43:
                mat_key = "circuit"
                mat_qty = 1
        else:
            if mr < 0.18:
                mat_key = "scrap"
                mat_qty = random.randint(1, 2)
            elif mr < 0.26:
                mat_key = "cloth"
                mat_qty = 1
            elif mr < 0.31:
                mat_key = "filter"
                mat_qty = 1
            elif mr < 0.34:
                mat_key = "battery"
                mat_qty = 1
            elif mr < 0.36:
                mat_key = "circuit"
                mat_qty = 1

        if mat_key and mat_qty > 0:
            items_to_add.append((mat_key, mat_qty))

        # Phase3: 드론 버프(다음 탐사 크레딧 +25%, 1회)
        drone_applied = False
        if bkey == "drone" and bstacks > 0:
            if credits > 0:
                mult = Decimal("1.25")
                credits = int((Decimal(int(credits)) * mult).to_integral_value(rounding=ROUND_CEILING))
                drone_applied = True

        result = claim_daily_explore(ctx.author.id, today, credits, water)
        if result is None:
            # 레이스 상황(거의 없음)에서도 전리품/버프가 새지 않도록 조용히 끝낸다.
            txt = (
                f"{hon}… 오늘은 이미 탐사 다녀왔어.\n"
                "하루 1회만! (유메 선배 수첩에 적혀있어…)\n"
            )
            await send_ctx(ctx, txt, allow_glitch=True)
            return


        # Phase5: 탐사 메타(날씨/성공 여부) 기록(퀘스트 검증용). 실패해도 게임은 계속 진행.
        try:
            upsert_explore_meta(
                ctx.author.id,
                today,
                weather=weather,
                success=success,
                credits_delta=int(credits),
                water_delta=int(water),
            )
        except Exception:
            pass
        # 성공적으로 반영된 후에만 전리품/버프 소모를 적용한다.
        for k, q in items_to_add:
            add_user_item(ctx.author.id, k, q)
        if bkey in {"drone", "kit"} and bstacks > 0:
            consume_user_buff_stack(ctx.author.id)

        new_credits = int(result.get("credits", 0))
        new_water = int(result.get("water", 0))

        # Flavor
        if weather == "sandstorm" and mask_used:
            if success:
                flavor = random.choice(
                    [
                        "방진마스크 덕분에… 숨 좀 쉬겠더라. 그래도 뭐 하나 주웠어!",
                        "바람은 미쳤는데… 마스크가 버텨줬어. 으헤~",
                        "시야가 흐릿했지만… 마스크로 버티면서 수확 성공!",
                    ]
                )
            else:
                flavor = random.choice(
                    [
                        "마스크가 있어도… 오늘 바람은 진짜 무리였어…",
                        "버텨보려 했는데… 모래가 전부 덮었어… 퇴각!",
                        "마스크 필터가… 지지직… 다음에 다시 가자.",
                    ]
                )
        elif weather == "sandstorm":
            if success:
                flavor = random.choice(
                    [
                        "모래폭풍 속에서도… 뭔가 반짝이는 걸 주웠어! 으헤~",
                        "시야가 거의 안 보였는데… 손에 잡히는 게 있더라…!",
                        "입에 모래… 퉤퉤… 그래도 성과는 있었어.",
                    ]
                )
            else:
                flavor = random.choice(
                    [
                        "바람이 너무 세서… 거의 아무것도 못 챙겼어… 지지직…",
                        "모래가 다 내 편지를… 아니 내 포스터를…!",
                        "퇴각! 퇴각! 오늘은… 진짜 무리야…",
                    ]
                )
        elif weather == "cloudy":
            if success:
                flavor = random.choice(
                    [
                        "하늘이 흐려도… 발밑은 반짝이네!",
                        "기분은 좀 축축하지만, 수확은 괜찮아.",
                        "구름 아래에서… 의외로 찾기 쉬웠어.",
                    ]
                )
            else:
                flavor = random.choice(
                    [
                        "흐린 날은… 길을 자꾸 헷갈려.",
                        "오늘은 꽝… 다음엔 더 잘할 수 있어.",
                        "발자국만 잔뜩 남겼다…",
                    ]
                )
        else:
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

        # 획득/손실 표기(Phase3: 음수 크레딧 가능)
        if credits > 0:
            gained = f"**+{_fmt(credits)} 크레딧**"
        elif credits < 0:
            gained = f"**-{_fmt(abs(credits))} 크레딧**"
        else:
            gained = "0 크레딧"

        if water > 0:
            gained += f"  +{water} 물"

        # 전리품 메시지
        loot_lines: list[str] = []
        if items_to_add:
            for k, q in items_to_add:
                meta = ITEMS.get(k)
                name = meta.get("name") if meta else k
                tag = "재료" if k in MATERIAL_KEYS else "전리품"
                loot_lines.append(f"- {tag}: {name} x{q}")

        note = ""
        if weather != calc_weather:
            note = f" (버프 적용: `{label_calc}`로 계산)"
        if drone_applied:
            loot_lines.append("- 버프: 탐사용 드론 +25% 적용")
        if kit_applied:
            loot_lines.append("- 버프: 탐사키트 성공률 +10% 적용")

        parts = [
            f"{hon} 탐사 결과!",
            f"날씨: `{label_env}`{note}",
            flavor,
            f"획득: {gained}",
        ]

        if encounter_lines:
            parts.append("\n" + "\n".join(encounter_lines))
        if loot_lines:
            parts.append("\n" + "\n".join(loot_lines))

        parts.append(f"\n현재 보유: 크레딧 **{_fmt(new_credits)}**, 물 **{_fmt(new_water)}**")
        txt = "\n".join(parts) + "\n"
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

        tomorrow_debt = _apply_interest_once(debt, rate)
        tomorrow_interest = max(0, int(tomorrow_debt - debt))

        hon = get_honorific(ctx.author, ctx.guild)
        txt = (
            f"{hon}… 아비도스 재정 보고서 가져왔어.\n"
            f"- 현재 빚: **{_fmt(debt)} 크레딧**\n"
            f"- 일일 이자율: **{rate * 100:.2f}%**\n"
            f"- 오늘 기준 적용일: `{last}`\n"
            f"- 내일 예상 이자: **+{_fmt(tomorrow_interest)}** (예상 빚 **{_fmt(tomorrow_debt)}**)\n\n"
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
        hon = get_honorific(ctx.author, ctx.guild)
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
