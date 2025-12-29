from __future__ import annotations

import datetime
import logging
import random
import time
from typing import Optional, Dict, Any, Tuple

import discord
from discord.ext import commands, tasks

from yume_honorific import get_honorific
from yume_send import send_ctx, send_channel
from yume_store import (
    ABY_DEFAULT_DEBT,
    apply_guild_interest_upto_today,
    debt_pressure_stage,
    ensure_aby_incident_state,
    update_aby_incident_state,
    apply_guild_incident,
    get_config,
    set_config,
    get_guild_debt,
    list_aby_debt_guild_ids,
    list_recent_aby_incidents,
    week_key_from_ymd,
    week_ymds_from_week_key,
    get_weekly_debt_summary,
    top_repay_users_for_week,
    get_weekly_points_ranking,
)

logger = logging.getLogger(__name__)

KST = datetime.timezone(datetime.timedelta(hours=9))

CFG_NOTICE_CH = "aby_notice_channel_id:{gid}"
CFG_WEEKLY_LAST_SENT = "aby_weekly_report_last_sent_week:{gid}"


def _now_kst() -> datetime.datetime:
    return datetime.datetime.now(tz=KST)


def _today_ymd_kst() -> str:
    return _now_kst().date().isoformat()


def _fmt(n: int) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _parse_channel_mention(ctx: commands.Context) -> Optional[int]:
    try:
        if ctx.message.channel_mentions:
            return int(ctx.message.channel_mentions[0].id)
    except Exception:
        return None
    return None


def _get_notice_channel_id(guild_id: int) -> Optional[int]:
    try:
        raw = get_config(CFG_NOTICE_CH.format(gid=int(guild_id)), "")
        if not raw:
            return None
        v = int(str(raw).strip())
        return v if v > 0 else None
    except Exception:
        return None


def _set_notice_channel_id(guild_id: int, channel_id: Optional[int]) -> None:
    key = CFG_NOTICE_CH.format(gid=int(guild_id))
    if not channel_id:
        set_config(key, "")
    else:
        set_config(key, str(int(channel_id)))


def _get_text_channel(bot: commands.Bot, guild_id: int, channel_id: int) -> Optional[discord.abc.Messageable]:
    g = bot.get_guild(int(guild_id))
    if not g:
        return None
    ch = g.get_channel(int(channel_id))
    return ch


def _prev_week_key(today_ymd: str) -> str:
    d = datetime.date.fromisoformat(today_ymd)
    prev = d - datetime.timedelta(days=7)
    return week_key_from_ymd(prev.isoformat())


def _week_range_text(week_key: str) -> str:
    ymds = week_ymds_from_week_key(str(week_key))
    if not ymds:
        return ""
    return f"{ymds[0]} ~ {ymds[-1]}"


def _roll_incident(debt: int) -> Dict[str, Any]:
    """Return an incident dict: {title, desc, delta_debt}."""
    d = int(debt)
    stage = int(debt_pressure_stage(d).get("stage") or 0)

    # As pressure rises, bad incidents become more likely.
    bad_weight = min(0.85, 0.45 + stage * 0.08)
    good_weight = 1.0 - bad_weight

    if random.random() < good_weight:
        choices: list[Tuple[str, str, Tuple[int, int]]] = [
            ("익명 후원", "정체불명의 후원금이 들어왔어. 누가… 우리를 아직 포기 안 했나 봐.", (-250_000, -50_000)),
            ("중고 부품 매각", "쓸만한 고철을 정리해서 팔았어. 아주 조금 숨통이 트였어.", (-180_000, -30_000)),
            ("미세한 우호", "오늘은 추심 연락이 없었어. 이상하게 조용해… 더 무섭지?", (-80_000, -10_000)),
        ]
        title, desc, (lo, hi) = random.choice(choices)
        return {"title": title, "desc": desc, "delta_debt": int(random.randint(lo, hi))}

    base_lo = 40_000 + stage * 40_000
    base_hi = min(1_200_000, 180_000 + stage * 120_000)

    choices2: list[Tuple[str, str, float]] = [
        ("추심 연락", "시끌벅적한 통화가 이어졌어. '오늘 중으로…' 라는 말이 너무 익숙해.", 1.00),
        ("장비 파손", "탐사 장비 일부가 망가졌어. 수리비… 또 돈이야.", 1.10),
        ("서류 누락", "납품 서류가 하나 사라졌대. 벌금이 붙었어. 으헤~…", 0.85),
        ("연체 수수료", "작은 연체가 누적됐대. 작은데… 계속 쌓여.", 0.95),
        ("물가 폭등", "필터랑 배터리 가격이 올랐어. 유지비가 늘었어.", 0.90),
    ]
    title, desc, mult = random.choice(choices2)
    lo = int(base_lo * mult)
    hi = int(base_hi * mult)
    return {"title": title, "desc": desc, "delta_debt": int(random.randint(lo, hi))}


def _roll_next_incident_at(now_ts: int, debt: int) -> int:
    d = int(debt)
    stage = int(debt_pressure_stage(d).get("stage") or 0)

    if stage >= 6:
        lo, hi = 60 * 60, 3 * 60 * 60
    elif stage >= 4:
        lo, hi = 90 * 60, 4 * 60 * 60
    elif stage >= 2:
        lo, hi = 2 * 60 * 60, 6 * 60 * 60
    else:
        lo, hi = 4 * 60 * 60, 10 * 60 * 60

    return int(now_ts + random.randint(lo, hi))


class AbyBroadcastCog(commands.Cog):
    """Phase6-2 Phase7: 사건/추심 + 주간 리포트 자동 공지."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        if not self.incident_loop.is_running():
            self.incident_loop.start()
        if not self.weekly_report_loop.is_running():
            self.weekly_report_loop.start()

    def cog_unload(self) -> None:
        try:
            self.incident_loop.cancel()
        except Exception:
            pass
        try:
            self.weekly_report_loop.cancel()
        except Exception:
            pass

    # ------------------------------
    # Config
    # ------------------------------

    @commands.command(name="아비도스공지")
    async def set_notice_channel(self, ctx: commands.Context, *args: str):
        """사건/주간리포트 공지 채널을 설정해."""
        if ctx.guild is None:
            await send_ctx(ctx, "이건… 서버에서만 설정할 수 있어. 으헤~")
            return

        hon = get_honorific(ctx.author, ctx.guild)
        gid = int(ctx.guild.id)

        try:
            if not (ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator):
                if int(getattr(ctx.guild, "owner_id", 0) or 0) != int(ctx.author.id):
                    await send_ctx(ctx, f"{hon} 이건 서버 설정이라… '서버 관리' 권한이 필요해.")
                    return
        except Exception:
            pass

        if not args:
            cur = _get_notice_channel_id(gid)
            if cur:
                await send_ctx(ctx, f"{hon} 현재 아비도스 공지 채널: <#{cur}>\n끄려면 `!아비도스공지 끄기`")
            else:
                await send_ctx(ctx, f"{hon} 현재 아비도스 공지가 꺼져 있어.\n켜려면 `!아비도스공지 #채널`")
            return

        a0 = str(args[0]).strip()
        if a0 in {"끄기", "off", "disable", "0"}:
            _set_notice_channel_id(gid, None)
            await send_ctx(ctx, f"{hon} 알겠어. 아비도스 공지를 껐어.")
            return

        ch_id = _parse_channel_mention(ctx)
        if not ch_id:
            await send_ctx(ctx, f"{hon} 채널을 `#채널`로 멘션해줘. 예) `!아비도스공지 #아비도스-공지`")
            return

        _set_notice_channel_id(gid, ch_id)
        await send_ctx(ctx, f"{hon} 좋아. 앞으로 사건/주간 리포트는 <#{ch_id}>에 올릴게.")

    # ------------------------------
    # Commands
    # ------------------------------

    @commands.command(name="사건내역")
    async def incident_history(self, ctx: commands.Context, limit: Optional[str] = None):
        if ctx.guild is None:
            await send_ctx(ctx, "이건… 서버에서만 볼 수 있어. 으헤~")
            return

        hon = get_honorific(ctx.author, ctx.guild)
        gid = int(ctx.guild.id)

        lim = 8
        if limit and str(limit).strip().isdigit():
            lim = int(str(limit).strip())
        lim = max(1, min(lim, 20))

        rows = list_recent_aby_incidents(gid, lim)
        if not rows:
            await send_ctx(ctx, f"{hon} 아직 기록된 사건이 없어.")
            return

        lines = [f"{hon} 최근 사건 내역이야. (최신 {len(rows)}개)"]

        for r in rows:
            ts = int(r.get("created_at") or 0)
            dt = datetime.datetime.fromtimestamp(ts, tz=KST)
            title = str(r.get("title") or "")
            desc = str(r.get("description") or "")
            delta = int(r.get("delta_debt") or 0)
            sign = "+" if delta >= 0 else ""
            lines.append(f"- `{dt:%m/%d %H:%M}` **{title}** ({sign}{_fmt(delta)} 빚)\n  {desc}")

        await send_ctx(ctx, "\n".join(lines), allow_glitch=True)

    @commands.command(name="주간리포트")
    async def weekly_report(self, ctx: commands.Context, *args: str):
        if ctx.guild is None:
            await send_ctx(ctx, "이건… 서버에서만 볼 수 있어. 으헤~")
            return

        hon = get_honorific(ctx.author, ctx.guild)
        gid = int(ctx.guild.id)

        today = _today_ymd_kst()
        cur_wk = week_key_from_ymd(today)
        prev_wk = _prev_week_key(today)

        target = prev_wk
        if args:
            a = str(args[0]).strip()
            if a in {"이번주", "이번", "current"}:
                target = cur_wk
            elif a in {"지난주", "저번주", "last"}:
                target = prev_wk
            elif a.startswith("20") and "-W" in a:
                target = a

        embed = self._build_weekly_report_embed(gid, target)
        await send_ctx(ctx, f"{hon} 주간 리포트 가져왔어.", embed=embed, allow_glitch=False)

    # ------------------------------
    # Background loops
    # ------------------------------

    @tasks.loop(seconds=120)
    async def incident_loop(self):
        if not self.bot.is_ready():
            return

        now_ts = int(time.time())
        today = _today_ymd_kst()

        for gid in list_aby_debt_guild_ids():
            try:
                st = ensure_aby_incident_state(gid)
                nxt = int(st.get("next_incident_at") or 0)
                if nxt <= 0:
                    nxt = now_ts + 2 * 3600
                    update_aby_incident_state(gid, next_incident_at=nxt, last_incident_at=int(st.get("last_incident_at") or 0))
                if now_ts < nxt:
                    continue

                try:
                    apply_guild_interest_upto_today(gid, today)
                except Exception:
                    pass

                debt_info = get_guild_debt(gid, today_ymd=today)
                debt = int(debt_info.get("debt") or ABY_DEFAULT_DEBT)

                inc = _roll_incident(debt)
                title = str(inc.get("title") or "사건")
                desc = str(inc.get("desc") or "")
                delta = int(inc.get("delta_debt") or 0)

                res = apply_guild_incident(
                    gid,
                    title=title,
                    description=desc,
                    delta_debt=delta,
                    today_ymd=today,
                )
                new_debt = int(res.get("new_debt") or debt)

                next_ts = _roll_next_incident_at(now_ts, new_debt)
                update_aby_incident_state(gid, next_incident_at=next_ts, last_incident_at=now_ts)

                ch_id = _get_notice_channel_id(gid)
                if ch_id:
                    ch = _get_text_channel(self.bot, gid, ch_id)
                    if ch:
                        stage = debt_pressure_stage(new_debt)
                        stage_label = str(stage.get("label") or "")
                        sign = "+" if delta >= 0 else ""
                        msg = (
                            f"📌 **아비도스 사건 발생**\n"

                            f"**{title}** — {desc}\n"

                            f"- 빚 변화: **{sign}{_fmt(delta)}**\n"

                            f"- 현재 빚: **{_fmt(new_debt)}**\n"

                            f"- 압박 단계: **{stage_label}**"

                        )
                        await send_channel(ch, msg, target_user_id=None, allow_glitch=False)

            except Exception as e:
                logger.exception("incident loop error (gid=%s): %s", gid, e)
                continue

    @tasks.loop(minutes=10)
    async def weekly_report_loop(self):
        if not self.bot.is_ready():
            return

        now = _now_kst()
        if now.weekday() != 0:
            return
        if not (now.hour == 0 and 5 <= now.minute <= 55):
            return

        today = now.date().isoformat()
        prev_wk = _prev_week_key(today)

        for gid in list_aby_debt_guild_ids():
            try:
                ch_id = _get_notice_channel_id(gid)
                if not ch_id:
                    continue

                last = get_config(CFG_WEEKLY_LAST_SENT.format(gid=gid), "")
                if str(last or "") == prev_wk:
                    continue

                ch = _get_text_channel(self.bot, gid, ch_id)
                if not ch:
                    continue

                embed = self._build_weekly_report_embed(gid, prev_wk)
                await send_channel(ch, "🗞️ **아비도스 주간 리포트**", embed=embed, target_user_id=None, allow_glitch=False)
                set_config(CFG_WEEKLY_LAST_SENT.format(gid=gid), prev_wk)

            except Exception as e:
                logger.exception("weekly report loop error (gid=%s): %s", gid, e)
                continue

    # ------------------------------
    # Report builder
    # ------------------------------

    def _build_weekly_report_embed(self, guild_id: int, week_key: str) -> discord.Embed:
        gid = int(guild_id)
        wk = str(week_key)

        summary = get_weekly_debt_summary(gid, wk)
        interest = int(summary.get("interest_delta") or 0)
        incidents = int(summary.get("incident_delta") or 0)
        repays = int(summary.get("repay_delta") or 0)
        net = int(summary.get("net_delta") or 0)
        repaid_credits = int(summary.get("repaid_credits") or 0)

        sign_net = "+" if net >= 0 else ""
        sign_int = "+" if interest >= 0 else ""
        sign_inc = "+" if incidents >= 0 else ""
        sign_rep = "+" if repays >= 0 else ""

        debt_info = get_guild_debt(gid, today_ymd=_today_ymd_kst())
        cur_debt = int(debt_info.get("debt") or ABY_DEFAULT_DEBT)
        stage = debt_pressure_stage(cur_debt)
        stage_label = str(stage.get("label") or "")

        emb = discord.Embed(
            title=f"주간 리포트 · {wk}",
            description=f"기간: {_week_range_text(wk)}\n현재 빚: **{_fmt(cur_debt)}** (압박: {stage_label})",
            timestamp=_now_kst(),
        )

        emb.add_field(
            name="빚 증감(주간)",
            value=(
                f"- 순증감: **{sign_net}{_fmt(net)}**\n"
                f"- 이자: {sign_int}{_fmt(interest)}\n"
                f"- 사건: {sign_inc}{_fmt(incidents)}\n"
                f"- 상환: {sign_rep}{_fmt(repays)}"
            ),
            inline=False,
        )

        emb.add_field(
            name="상환 규모",
            value=f"총 **{_fmt(repaid_credits)}** 크레딧이 상환됐어.",
            inline=False,
        )

        tops = top_repay_users_for_week(gid, wk, limit=5)
        if tops:
            lines = []
            rank = 1
            for r in tops:
                uid = int(r.get("user_id") or 0)
                total = int(r.get("total") or 0)
                lines.append(f"{rank}. <@{uid}> — **{_fmt(total)}**")
                rank += 1
            emb.add_field(name="상환 TOP", value="\n".join(lines), inline=False)

        pts = get_weekly_points_ranking(gid, wk, limit=5)
        if pts:
            lines = []
            rank = 1
            for r in pts:
                uid = int(r.get("user_id") or 0)
                p = int(r.get("points") or 0)
                lines.append(f"{rank}. <@{uid}> — **{_fmt(p)}pt**")
                rank += 1
            emb.add_field(name="의뢰 포인트 TOP", value="\n".join(lines), inline=False)

        emb.set_footer(text="(Phase7) 사건/추심 + 주간 리포트")  # tiny label for debugging
        return emb


async def setup(bot: commands.Bot):
    await bot.add_cog(AbyBroadcastCog(bot))
