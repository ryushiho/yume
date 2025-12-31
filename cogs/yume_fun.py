from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import discord
from discord.ext import commands

from yume_brain import YumeBrain
from yume_honorific import get_honorific
from yume_prompt import YUME_ROLE_PROMPT_KR

logger = logging.getLogger(__name__)


DEV_USER_ID = 1433962010785349634

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPT_PACK_DIR = BASE_DIR / "data" / "system" / "promptpacks"

HOSHINO_PACK_PATH = PROMPT_PACK_DIR / "hoshino.json"
POSTER_PACK_PATH = PROMPT_PACK_DIR / "poster.json"


@dataclass
class PromptPack:
    system_extra: str
    bands: Dict[str, str]


def _safe_load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.warning("promptpack 로드 실패(%s): %s", path, e)
    return default


def _sanitize_mentions(text: str) -> str:
    # Discord mention 방지: @ 를 @\u200b 로 치환
    return text.replace("@", "@\u200b")


_BAND_META: Dict[str, Tuple[str, Tuple[int, int]]] = {
    # key: (label, (hour, minute))  # 강제 시간대용 대표 시각
    "dawn": ("새벽", (3, 30)),
    "morning": ("아침", (8, 30)),
    "noon": ("점심", (12, 30)),
    "afternoon": ("오후", (15, 30)),
    "evening": ("저녁", (19, 30)),
    "late_night": ("심야", (23, 30)),
}


def _now_kst(now: Optional[datetime] = None) -> datetime:
    """KST 기준 datetime을 반환한다(zoneinfo 실패 시 로컬)."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Seoul")
        return (now or datetime.now(tz=tz)).astimezone(tz)
    except Exception:
        return now or datetime.now()


def _pick_time_band_kst(now: Optional[datetime] = None, forced_key: Optional[str] = None) -> Tuple[str, str, str]:
    """KST 기준 시간대 키/라벨/시각 문자열을 만든다.

    - forced_key가 있으면 해당 시간대로 '연출용 대표 시각'을 만들어 반환한다.
    """
    now_kst = _now_kst(now)

    if forced_key and forced_key in _BAND_META:
        label, (hh, mm) = _BAND_META[forced_key]
        try:
            forced_dt = now_kst.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except Exception:
            forced_dt = now_kst
        clock = forced_dt.strftime("%Y-%m-%d %H:%M")
        return forced_key, label, clock

    hh = int(now_kst.strftime("%H"))

    # 6구간: 새벽/아침/점심/오후/저녁/심야
    if 0 <= hh < 6:
        key = "dawn"
    elif 6 <= hh < 11:
        key = "morning"
    elif 11 <= hh < 14:
        key = "noon"
    elif 14 <= hh < 18:
        key = "afternoon"
    elif 18 <= hh < 22:
        key = "evening"
    else:
        key = "late_night"

    label = _BAND_META.get(key, ("", (0, 0)))[0] or ""
    clock = now_kst.strftime("%Y-%m-%d %H:%M")
    return key, label, clock


def _parse_force_band_arg(raw: str) -> Optional[str]:
    """!호시노 시간대 강제 옵션 파서.

    허용 예:
    - 새벽/아침/점심/낮/오후/저녁/심야/밤
    - dawn/morning/noon/afternoon/evening/late_night
    """
    s = (raw or "").strip().lower()
    if not s:
        return None

    # 자주 쓰는 한국어 키워드
    ko_map = {
        "새벽": "dawn",
        "아침": "morning",
        "오전": "morning",
        "점심": "noon",
        "정오": "noon",
        "낮": "noon",
        "오후": "afternoon",
        "저녁": "evening",
        "밤": "late_night",
        "심야": "late_night",
        "야밤": "late_night",
    }
    if raw.strip() in ko_map:
        return ko_map[raw.strip()]

    # 영어 키워드
    en_map = {
        "dawn": "dawn",
        "morning": "morning",
        "noon": "noon",
        "afternoon": "afternoon",
        "evening": "evening",
        "late": "late_night",
        "latenight": "late_night",
        "late_night": "late_night",
        "late-night": "late_night",
        "night": "late_night",
    }
    return en_map.get(s)


class YumeFunCog(commands.Cog):
    """유메의 특수 컨텐츠 커맨드: !호시노 / !포스터"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.brain: Optional[YumeBrain] = None
        self.brain_error: Optional[str] = None

        # channel_id -> {"ts": float, "summary": str}
        self._hoshino_cache: Dict[int, Dict[str, Any]] = {}

        PROMPT_PACK_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_default_promptpacks()

    # --------- common helpers ---------

    def _core(self):
        return getattr(self.bot, "yume_core", None)

    def _get_user_profile(self, user: discord.abc.User, guild: Optional[discord.Guild]) -> dict:
        profile: dict = {
            "nickname": getattr(user, "display_name", user.name),
            "bond_level": "normal",
            "honorific": get_honorific(user, guild),
        }

        core = self._core()
        if core is None:
            return profile

        try:
            uid = str(user.id)
            profile["affection"] = float(core.get_affection(uid))
            profile["bond_level"] = str(core.get_affection_stage(uid))
        except Exception:
            pass

        return profile

    def _ensure_brain(self) -> bool:
        if self.brain is not None:
            return True

        try:
            self.brain = YumeBrain()
            self.brain_error = None
            logger.info("[YumeFunCog] YumeBrain 지연 초기화 성공")
            return True
        except Exception as e:  # noqa: BLE001
            self.brain = None
            self.brain_error = repr(e)
            logger.error("[YumeFunCog] YumeBrain 초기화 실패: %r", e)
            return False

    async def cog_load(self):
        self._ensure_brain()

    def _ensure_default_promptpacks(self) -> None:
        # hoshino.json
        if not HOSHINO_PACK_PATH.exists():
            default_hoshino = {
                "system_extra": (
                    "\n\n[추가 규칙 - !호시노]\n"
                    "- 너는 유메(학생회장)이고, 호시노(1학년 시절)는 가장 소중한 후배다.\n"
                    "- 출력은 '실시간 중계'처럼, 지금 호시노가 하는 행동/대사/주변 상황을 묘사한다.\n"
                    "- 너무 진지하게 무겁지 않게, 엉뚱하고 다정하게.\n"
                    "- 멘션(@)을 직접 찍지 말고, 필요하면 이름만 쓰기.\n"
                    "- 길이는 5~10줄 정도(상황에 따라).\n"
                    "- 마지막 줄에 아주 짧은 한 줄 요약을 [[STATE]] 로 남겨도 좋다.\n"
                ),
                "bands": {
                    "dawn": "지금은 새벽. 호시노가 졸린 와중에도 버티는 모습을 중계해줘.",
                    "morning": "지금은 아침. 호시노가 등교/청소/준비를 하는 모습을 중계해줘.",
                    "noon": "지금은 점심. 호시노의 점심/간식/물 아껴먹기(?)를 중계해줘.",
                    "afternoon": "지금은 오후. 호시노가 업무/탐사/소소한 사건을 겪는 걸 중계해줘.",
                    "evening": "지금은 저녁. 호시노가 피곤하지만 버티는 모습을 중계해줘.",
                    "late_night": "지금은 심야. 호시노가 졸거나 경계근무(?) 하는 모습을 중계해줘.",
                },
            }
            try:
                HOSHINO_PACK_PATH.parent.mkdir(parents=True, exist_ok=True)
                HOSHINO_PACK_PATH.write_text(json.dumps(default_hoshino, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning("기본 hoshino promptpack 생성 실패: %s", e)

        # poster.json
        if not POSTER_PACK_PATH.exists():
            default_poster = {
                "system_extra": (
                    "\n\n[추가 규칙 - !포스터]\n"
                    "- 너는 유메이고, '축제 포스터 제작소'에서 포스터를 만든다.\n"
                    "- 출력은 오직 하나의 코드블록(``` ... ```)로만. 설명/사족 금지.\n"
                    "- 아주 화려하고 촌스럽고 레트로하게(ASCII/이모지/구분선/테두리).\n"
                    "- 폭은 40자 이내, 18줄 이내.\n"
                    "- 멘션(@everyone/@here/유저멘션)을 만들지 말 것.\n"
                )
            }
            try:
                POSTER_PACK_PATH.parent.mkdir(parents=True, exist_ok=True)
                POSTER_PACK_PATH.write_text(json.dumps(default_poster, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning("기본 poster promptpack 생성 실패: %s", e)

    def _load_pack(self, path: Path, *, default: Dict[str, Any]) -> Dict[str, Any]:
        return _safe_load_json(path, default)

    # --------- commands ---------

    @commands.command(name="포스터")
    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
    async def cmd_poster(self, ctx: commands.Context, *, text: str = ""):
        """!포스터 (할말)"""
        raw = (text or "").strip()
        if not raw:
            await ctx.send("`!포스터 (문구)` 처럼 써줘. 예: `!포스터 신입생 모집`", delete_after=10)
            return

        if not self._ensure_brain():
            debug = f"\n\n[디버그 brain_error: {self.brain_error}]" if (ctx.author.id == DEV_USER_ID and self.brain_error) else ""
            await ctx.send(
                "현재 대사를 생성하는 엔진 초기화에 실패해서, 포스터를 만들 수 없어." + debug,
                delete_after=15,
            )
            return

        # prompt pack
        pack = self._load_pack(POSTER_PACK_PATH, default={"system_extra": ""})
        system_extra = str(pack.get("system_extra") or "")

        system_prompt = (YUME_ROLE_PROMPT_KR + "\n" + system_extra).strip()

        user_prompt = (
            "아래 문구로 아비도스 감성 '축제 포스터'를 만들어.\n"
            "- 출력은 코드블록 하나만.\n"
            "- 폭 40자 이내, 18줄 이내.\n"
            "- 멘션이 될 만한 @ 문자는 쓰지 마(필요하면 전각＠로).\n\n"
            f"[문구]\n{raw}\n"
        )

        loop = asyncio.get_running_loop()

        def _call():
            assert self.brain is not None
            return self.brain.chat_custom(
                system_prompt=system_prompt,
                user_message=user_prompt,
                history=None,
                max_tokens=360,
                temperature=0.9,
            )

        result = await loop.run_in_executor(None, _call)

        ok = bool(result.get("ok", False))
        reason = str(result.get("reason", "error"))
        reply = str(result.get("reply") or "").strip()

        # Guard: ensure the last line is a parentheses scene line (prompt requires it).
        if ok and reply:
            tail = reply.strip().splitlines()[-1].strip()
            if not (tail.startswith("(") and tail.endswith(")")):
                import random

                fallbacks = [
                    "호시노 방패 뒤에 숨어서 떨면서 씀",
                    "복도 모서리에서 숨죽이며 씀",
                    "전봇대 뒤에서 힐끔거리며 씀",
                    "책상 밑에 쭈그려 앉아 몰래 씀",
                    "모래바람 속에서 노트를 품에 숨기고 씀",
                ]
                reply = reply.rstrip() + "\n(" + random.choice(fallbacks) + ")"

        if not ok and reason == "limit_exceeded":
            await ctx.send(
                "이번 달에 유메가 쓸 수 있는 말 예산을 다 써버렸어… 다음 달에 다시 만들어줄게.",
                delete_after=12,
            )
            return
        if not ok:
            dev = f"\n\n[디버그 reason: {reason!r}]" if ctx.author.id == DEV_USER_ID else ""
            err = str(result.get("error") or "")
            await ctx.send(
                "포스터 만들다가 길을 잃었어…" + (f"\n{err}" if ctx.author.id == DEV_USER_ID and err else "") + dev,
                delete_after=15,
            )
            return

        if not reply:
            reply = "```\n(포스터가 바람에 날아가버렸다…)\n```"

        # 코드블록만 남기기(모델이 설명을 붙였을 때 대비)
        if "```" in reply:
            first = reply.find("```")
            last = reply.rfind("```")
            if first != -1 and last != -1 and last > first:
                reply = reply[first : last + 3]
        else:
            reply = f"```\n{reply}\n```"

        reply = _sanitize_mentions(reply)

        # Discord 2000 제한 안전장치
        if len(reply) > 1900:
            # 코드블록 내부만 잘라내기
            inner = reply
            m = re.match(r"^```[^\n]*\n(?P<body>[\s\S]*?)\n```$", reply)
            if m:
                body = m.group("body")
                body = body[:1700].rstrip() + "\n…"
                reply = f"```\n{body}\n```"
            else:
                reply = reply[:1900]

        await ctx.send(reply, allowed_mentions=discord.AllowedMentions.none())


    @commands.command(name="호시노", aliases=["1학년"])
    @commands.cooldown(1, 12, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
    async def cmd_hoshino(self, ctx: commands.Context, *, force: str = ""):
        """!호시노 / !1학년 - 호시노 관찰 일기(시간대 강제 옵션 지원)

        사용 예:
        - !호시노
        - !호시노 새벽
        - !호시노 아침
        - !호시노 점심
        - !호시노 오후
        - !호시노 저녁
        - !호시노 심야
        """

        if not self._ensure_brain():
            debug = f"\n\n[디버그 brain_error: {self.brain_error}]" if (ctx.author.id == DEV_USER_ID and self.brain_error) else ""
            await ctx.send(
                "현재 대사를 생성하는 엔진 초기화에 실패해서, 호시노 중계를 할 수 없어." + debug,
                delete_after=15,
            )
            return

        forced_key = _parse_force_band_arg(force)
        if force.strip() and not forced_key:
            await ctx.send(
                "시간대 강제는 이렇게 써줘: `!호시노 새벽/아침/점심/오후/저녁/심야`",
                delete_after=10,
            )
            return

        band_key, band_label, clock = _pick_time_band_kst(forced_key=forced_key)

        pack_default = {
            "system_extra": "",
            "bands": {band_key: "지금 호시노를 중계해줘."},
        }
        pack = self._load_pack(HOSHINO_PACK_PATH, default=pack_default)

        system_extra = str(pack.get("system_extra") or "")
        bands = pack.get("bands") if isinstance(pack.get("bands"), dict) else {}
        band_prompt = str((bands or {}).get(band_key) or "지금 호시노가 뭘 하는지 중계해줘.")

        system_prompt = (YUME_ROLE_PROMPT_KR + "\n" + system_extra).strip()

        user_profile = self._get_user_profile(ctx.author, ctx.guild)

        last_summary = ""
        cache = self._hoshino_cache.get(int(getattr(ctx.channel, "id", 0) or 0))
        if cache and isinstance(cache.get("summary"), str):
            last_summary = str(cache.get("summary") or "").strip()

        context_block = (
            "\n\n[현재 시간]\n"
            f"{clock}\n"
            "\n[시간대]\n"
            f"{band_label}\n"
            "\n[추가 컨텍스트]\n"
            f"- 유저 닉네임: {user_profile.get('nickname','')}\n"
            f"- 유저 기본 호칭: {user_profile.get('honorific','선생님')}\n"
            f"- bond_level: {user_profile.get('bond_level','normal')}\n"
            f"- affection: {user_profile.get('affection','')}\n"
        )
        if last_summary:
            context_block += f"- 직전 관찰 요약: {last_summary}\n"

        user_prompt = band_prompt.strip() + context_block

        loop = asyncio.get_running_loop()

        def _call():
            assert self.brain is not None
            return self.brain.chat_custom(
                system_prompt=system_prompt,
                user_message=user_prompt,
                history=None,
                max_tokens=360,
                temperature=0.9,
            )

        result = await loop.run_in_executor(None, _call)

        ok = bool(result.get("ok", False))
        reason = str(result.get("reason", "error"))
        reply = str(result.get("reply") or "").strip()

        # Guard: ensure the last line is a parentheses scene line (prompt requires it).
        if ok and reply:
            tail = reply.strip().splitlines()[-1].strip()
            if not (tail.startswith("(") and tail.endswith(")")):
                import random

                fallbacks = [
                    "호시노 방패 뒤에 숨어서 떨면서 씀",
                    "복도 모서리에서 숨죽이며 씀",
                    "전봇대 뒤에서 힐끔거리며 씀",
                    "책상 밑에 쭈그려 앉아 몰래 씀",
                    "모래바람 속에서 노트를 품에 숨기고 씀",
                ]
                reply = reply.rstrip() + "\n(" + random.choice(fallbacks) + ")"

        if not ok and reason == "limit_exceeded":
            await ctx.send(
                "이번 달에 유메가 쓸 수 있는 말 예산을 다 써버렸어… 다음 달에 다시 중계해줄게.",
                delete_after=12,
            )
            return
        if not ok:
            dev = f"\n\n[디버그 reason: {reason!r}]" if ctx.author.id == DEV_USER_ID else ""
            err = str(result.get("error") or "")
            msg = "호시노를 보러 갔다가… 모래바람에 길을 잃었어."
            if ctx.author.id == DEV_USER_ID and err:
                msg += f"\n{err}"
            msg += dev
            await ctx.send(msg, delete_after=15)
            return

        if not reply:
            reply = "(호시노 짱이… 어딘가에서 졸고 있는 것 같아…)"

        reply = _sanitize_mentions(reply)

        # cache summary
        summary = ""
        for line in reply.splitlines():
            t = line.strip()
            if t.startswith("[[STATE]]"):
                summary = t.replace("[[STATE]]", "", 1).strip()
                break

        if not summary:
            summary = re.sub(r"\s+", " ", reply).strip()[:90]

        try:
            ch_id = int(getattr(ctx.channel, "id", 0) or 0)
            if ch_id:
                self._hoshino_cache[ch_id] = {"ts": datetime.utcnow().timestamp(), "summary": summary}
        except Exception:
            pass

        await ctx.send(reply, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(YumeFunCog(bot))
