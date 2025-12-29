from __future__ import annotations

import datetime
import logging
from typing import Optional

from discord.ext import commands

from yume_honorific import get_honorific
from yume_send import send_ctx
from yume_store import (
    ABY_DAILY_QUEST_COUNT,
    ABY_WEEKLY_QUEST_COUNT,
    week_key_from_ymd,
    ensure_aby_daily_quest_board,
    ensure_aby_weekly_quest_board,
    get_aby_quests,
    is_aby_quest_claimed,
    claim_aby_quest,
    get_user_inventory,
    repay_total_for_day,
    repay_total_for_week,
    get_user_weekly_points,
    get_weekly_points_ranking,
    get_explore_meta,
    has_sandstorm_success_in_week,
)

logger = logging.getLogger(__name__)


ITEM_NAME = {
    "scrap": "고철",
    "cloth": "천조각",
    "filter": "필터",
    "battery": "배터리",
    "circuit": "회로기판",
    "mask": "방진마스크",
    "drone": "탐사용 드론",
    "kit": "탐사키트",
}


def _now_kst() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=9)


def _today_ymd_kst() -> str:
    return _now_kst().date().isoformat()


def _fmt(n: int) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _reward_text(points: int, credits: int, item_key: str, item_qty: int) -> str:
    parts: list[str] = []
    if points:
        parts.append(f"{_fmt(points)}점")
    if credits:
        parts.append(f"{_fmt(credits)}크")
    if item_key and item_qty:
        name = ITEM_NAME.get(item_key, item_key)
        parts.append(f"{name}x{item_qty}")
    return " + ".join(parts) if parts else "-"


class AbyQuestBoardCog(commands.Cog):
    """Phase6-2 Phase5: 의뢰 게시판 + 주간 랭킹."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="의뢰")
    async def quest_board(self, ctx: commands.Context):
        if ctx.guild is None:
            await send_ctx(ctx, "이건… 게시판이 서버에 붙어있어. 서버에서 불러줘… 으헤~", allow_glitch=True)
            return

        gid = int(ctx.guild.id)
        uid = int(ctx.author.id)
        hon = get_honorific(ctx.author, ctx.guild)

        today = _today_ymd_kst()
        week_key = week_key_from_ymd(today)

        # ensure boards exist
        try:
            ensure_aby_daily_quest_board(gid, today)
            ensure_aby_weekly_quest_board(gid, week_key)
        except Exception as e:
            logger.exception("ensure quest board failed: %s", e)
            await send_ctx(ctx, f"{hon}… 게시판이 잠깐 고장난 것 같아. (DB 확인 필요)", allow_glitch=True)
            return

        daily = get_aby_quests(gid, "daily", today)
        weekly = get_aby_quests(gid, "weekly", week_key)

        inv = get_user_inventory(uid)

        lines: list[str] = []
        lines.append(f"{hon} 의뢰 게시판이야.")
        lines.append(f"- 일일: `{today}` / 주간: `{week_key}`")

        # Daily
        lines.append("\n**[일일 의뢰]**")
        for q in daily:
            qn = int(q.get("quest_no") or 0)
            shown = qn
            qtype = str(q.get("quest_type") or "")
            title = str(q.get("title") or "")
            desc = str(q.get("description") or "")
            target_key = str(q.get("target_key") or "")
            target_qty = int(q.get("target_qty") or 0)
            rpts = int(q.get("reward_points") or 0)
            rcr = int(q.get("reward_credits") or 0)
            rit = str(q.get("reward_item_key") or "")
            riq = int(q.get("reward_item_qty") or 0)

            claimed = is_aby_quest_claimed(gid, "daily", today, qn, uid)
            tag = "완료" if claimed else " "

            progress = ""
            if not claimed:
                if qtype == "deliver_item" and target_key:
                    have = int(inv.get(target_key, 0))
                    name = ITEM_NAME.get(target_key, target_key)
                    progress = f" (진행: {name} {_fmt(have)}/{_fmt(target_qty)})"
                elif qtype == "repay_total":
                    repaid = repay_total_for_day(gid, uid, today)
                    progress = f" (진행: {_fmt(repaid)}/{_fmt(target_qty)})"
                elif qtype == "explore_done":
                    meta = get_explore_meta(uid, today)
                    progress = " (진행: 탐사 완료)" if meta else " (진행: 탐사 필요)"

            reward = _reward_text(rpts, rcr, rit, riq)
            lines.append(f"{shown}. [{tag}] **{title}** — {desc}{progress}\n   보상: {reward}")

        # Weekly
        lines.append("\n**[주간 의뢰]**")
        for q in weekly:
            qn = int(q.get("quest_no") or 0)
            shown = ABY_DAILY_QUEST_COUNT + qn
            qtype = str(q.get("quest_type") or "")
            title = str(q.get("title") or "")
            desc = str(q.get("description") or "")
            target_key = str(q.get("target_key") or "")
            target_qty = int(q.get("target_qty") or 0)
            rpts = int(q.get("reward_points") or 0)
            rcr = int(q.get("reward_credits") or 0)
            rit = str(q.get("reward_item_key") or "")
            riq = int(q.get("reward_item_qty") or 0)

            claimed = is_aby_quest_claimed(gid, "weekly", week_key, qn, uid)
            tag = "완료" if claimed else " "

            progress = ""
            if not claimed:
                if qtype == "deliver_item" and target_key:
                    have = int(inv.get(target_key, 0))
                    name = ITEM_NAME.get(target_key, target_key)
                    progress = f" (진행: {name} {_fmt(have)}/{_fmt(target_qty)})"
                elif qtype == "repay_total":
                    repaid = repay_total_for_week(gid, uid, week_key)
                    progress = f" (진행: {_fmt(repaid)}/{_fmt(target_qty)})"
                elif qtype == "explore_sandstorm_success":
                    done = has_sandstorm_success_in_week(uid, week_key)
                    progress = " (진행: 1/1)" if done else " (진행: 0/1)"

            reward = _reward_text(rpts, rcr, rit, riq)
            lines.append(f"{shown}. [{tag}] **{title}** — {desc}{progress}\n   보상: {reward}")

        lines.append("\n보상 받기: `!납품 <번호>`  /  랭킹: `!의뢰랭킹`")

        await send_ctx(ctx, "\n".join(lines), allow_glitch=True)

    @commands.command(name="납품")
    async def quest_claim(self, ctx: commands.Context, num: Optional[str] = None):
        if ctx.guild is None:
            await send_ctx(ctx, "이건… 서버에서만 할 수 있어. 으헤~", allow_glitch=True)
            return

        hon = get_honorific(ctx.author, ctx.guild)
        if not num or not str(num).strip().isdigit():
            await send_ctx(ctx, f"{hon} 납품 번호를 알려줘. 예: `!납품 1`", allow_glitch=True)
            return

        n = int(str(num).strip())
        if n <= 0:
            await send_ctx(ctx, f"{hon}… 그 번호는 좀 이상해. (1 이상)", allow_glitch=True)
            return

        gid = int(ctx.guild.id)
        uid = int(ctx.author.id)

        today = _today_ymd_kst()
        week_key = week_key_from_ymd(today)

        # ensure boards exist
        ensure_aby_daily_quest_board(gid, today)
        ensure_aby_weekly_quest_board(gid, week_key)

        if 1 <= n <= ABY_DAILY_QUEST_COUNT:
            scope = "daily"
            board_key = today
            quest_no = n
        elif ABY_DAILY_QUEST_COUNT < n <= (ABY_DAILY_QUEST_COUNT + ABY_WEEKLY_QUEST_COUNT):
            scope = "weekly"
            board_key = week_key
            quest_no = n - ABY_DAILY_QUEST_COUNT
        else:
            await send_ctx(ctx, f"{hon}… 그 번호는 게시판에 없어. `!의뢰`로 확인해줘.", allow_glitch=True)
            return

        try:
            res = claim_aby_quest(
                guild_id=gid,
                user_id=uid,
                scope=scope,
                board_key=board_key,
                quest_no=quest_no,
                today_ymd=today,
            )
        except Exception as e:
            logger.exception("claim quest failed: %s", e)
            await send_ctx(ctx, f"{hon}… 납품 처리 중에 뭔가 꼬였어. (DB 확인 필요)", allow_glitch=True)
            return

        if not res.get("ok"):
            reason = str(res.get("reason") or "")
            if reason == "claimed":
                await send_ctx(ctx, f"{hon} 그 의뢰는 이미 보상 받았어.", allow_glitch=True)
                return
            if reason == "items":
                item = str(res.get("item") or "")
                have = int(res.get("have") or 0)
                need = int(res.get("need") or 0)
                name = ITEM_NAME.get(item, item)
                await send_ctx(ctx, f"{hon} 아직 부족해. {name} {_fmt(have)}/{_fmt(need)}", allow_glitch=True)
                return
            if reason == "repay":
                cur = int(res.get("current") or 0)
                need = int(res.get("need") or 0)
                await send_ctx(ctx, f"{hon} 아직 상환 실적이 부족해. {_fmt(cur)}/{_fmt(need)}", allow_glitch=True)
                return
            if reason == "explore":
                await send_ctx(ctx, f"{hon} 아직 탐사 조건이 안 됐어. 오늘 `!탐사`를 해봐.", allow_glitch=True)
                return
            await send_ctx(ctx, f"{hon}… 음? 조건이 안 맞아. `!의뢰`로 확인해줘.", allow_glitch=True)
            return

        pts = int(res.get("reward_points") or 0)
        cr = int(res.get("reward_credits") or 0)
        itk = str(res.get("reward_item_key") or "")
        itq = int(res.get("reward_item_qty") or 0)
        wk = str(res.get("week_key") or week_key)

        reward = _reward_text(pts, cr, itk, itq)
        my_pts = get_user_weekly_points(gid, wk, uid)

        await send_ctx(
            ctx,
            f"{hon} 납품 완료!\n보상: **{reward}**\n이번 주 포인트: **{_fmt(my_pts)}점** (`{wk}`)\n랭킹은 `!의뢰랭킹`",
            allow_glitch=True,
        )

    @commands.command(name="의뢰랭킹", aliases=["주간의뢰", "의뢰점수", "주간랭킹"])
    async def quest_ranking(self, ctx: commands.Context):
        if ctx.guild is None:
            await send_ctx(ctx, "이건… 서버에서만 볼 수 있어. 으헤~", allow_glitch=True)
            return

        gid = int(ctx.guild.id)
        uid = int(ctx.author.id)
        hon = get_honorific(ctx.author, ctx.guild)

        today = _today_ymd_kst()
        week_key = week_key_from_ymd(today)

        rows = get_weekly_points_ranking(gid, week_key, limit=10)
        my_pts = get_user_weekly_points(gid, week_key, uid)

        if not rows:
            await send_ctx(ctx, f"{hon} 이번 주 의뢰 점수… 아직 아무도 없어. 첫 타자 해볼래?", allow_glitch=True)
            return

        lines: list[str] = []
        lines.append(f"{hon} 주간 의뢰 랭킹 (`{week_key}`)")
        for i, r in enumerate(rows, start=1):
            rid = int(r.get("user_id") or 0)
            pts = int(r.get("points") or 0)
            lines.append(f"{i:>2}. <@{rid}> — **{_fmt(pts)}점**")

        lines.append(f"\n내 점수: **{_fmt(my_pts)}점**")
        lines.append("(점수는 일일/주간 의뢰 보상에서 누적돼.)")

        await send_ctx(ctx, "\n".join(lines), allow_glitch=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AbyQuestBoardCog(bot))
