from __future__ import annotations

import asyncio
import json
import logging
import re
import random
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

HOSHINO_DIARY_PATH = PROMPT_PACK_DIR / "hoshino_diary.json"


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
    "night": ("새벽", (3, 22)),
    "morning": ("아침", (8, 15)),
    "day": ("낮", (14, 10)),
    "evening": ("저녁", (20, 30)),
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
        clock = forced_dt.strftime("%H:%M")
        return forced_key, label, clock

    hh = int(now_kst.strftime("%H"))

    # 4구간(요청): 새벽/아침/낮/저녁
    # - 새벽: 00:00 ~ 06:00
    # - 아침: 07:00 ~ 11:00
    # - 낮  : 12:00 ~ 17:00
    # - 저녁: 18:00 ~ 23:00
    if 0 <= hh < 7:
        key = "night"
    elif 7 <= hh < 12:
        key = "morning"
    elif 12 <= hh < 18:
        key = "day"
    else:
        key = "evening"

    label = _BAND_META.get(key, ("", (0, 0)))[0] or ""
    clock = now_kst.strftime("%H:%M")
    return key, label, clock


def _parse_force_band_arg(raw: str) -> Optional[str]:
    """!호시노 시간대 강제 옵션 파서.

    허용 예:
    - 새벽/아침/낮/저녁/밤
    - night/morning/day/evening
    """
    s = (raw or "").strip().lower()
    if not s:
        return None

    ko_map = {
        "새벽": "night",
        "밤": "night",
        "심야": "night",
        "아침": "morning",
        "오전": "morning",
        "낮": "day",
        "점심": "day",
        "오후": "day",
        "저녁": "evening",
        "석양": "evening",
        "밤중": "night",
    }
    for k, v in ko_map.items():
        if k in s:
            return v

    # 영문 키
    en_set = {"night", "morning", "day", "evening"}
    if s in en_set:
        return s

    # 구버전 키 호환(dawn/noon/afternoon/late_night)
    legacy = {
        "dawn": "night",
        "late_night": "night",
        "noon": "day",
        "afternoon": "day",
    }
    if s in legacy:
        return legacy[s]

    return None


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

        # hoshino_diary.json (LLM 없이 랜덤 출력용)
        if not HOSHINO_DIARY_PATH.exists():
            try:
                default_diary = json.loads(r'''{
  "version": 1,
  "bands": {
    "morning": [
      {
        "obs": "호시노 쨩이 방금 일어났는데 머리가 까치집이야…! 눈 비비는 거, 진짜 귀여워…",
        "yume": "셔터 소리 나면 혼날까 봐 숨 참고 감상 중… 으앵~ 📸😳"
      },
      {
        "obs": "아침밥 먹으라고 깨웠더니 베개 던졌어… 으앵, 아팠다구 호시노 쨩…",
        "yume": "그래도 토스트 입에 물고 나가는 건 챙겨줬지! 후후 🍞💕"
      },
      {
        "obs": "교복 단추를 한 칸 삐뚤게 잠갔길래 슬쩍 고쳐줬어. 바로 눈치 못 챘지?",
        "yume": "선배의 손길은 바람처럼 조용하니까… 호시노 쨩~ 🍃😌"
      },
      {
        "obs": "가방 무게가 너무 무거워 보여서 내가 들어주려 했더니, '선배 시끄러워요'래…",
        "yume": "부끄러워서 그런 거지? 그치? 그치?? 으헤~ 😇"
      },
      {
        "obs": "등교 전쟁 시작! 모래바람 속에서도 방패는 꼭 챙기는 호시노 쨩… 든든해.",
        "yume": "근데… 가끔은 유메가 우산도 되어줄게…☂️💙"
      },
      {
        "obs": "아침 햇빛에 눈 찡그리면서도 한 발 한 발 꾸준히 걷는 모습, 왠지 어른 같아.",
        "yume": "유메 마음이 괜히 뭉클해졌어… 호시노 쨩, 최고야 🌞🥺"
      }
    ],
    "day": [
      {
        "obs": "방금 전술 훈련하는 거 봤어? 방패를 쾅! 하고 내리찍는데… 와… 듬직해.",
        "yume": "내 후배지만 진짜 멋있어… (근데 키는 언제 크려나?) 😌🛡️"
      },
      {
        "obs": "호시노 쨩 미간에 주름 잡혔어. 내가 옆에서 너무 떠들었나 봐…",
        "yume": "'선배, 시끄러워요'라며 째려보는데… 째려보는 것도 귀여우면 중증인가? 😳"
      },
      {
        "obs": "수업 시간에 고개가 천천히… 천천히… 떨어지고 있어. 거의 슬로모션이야.",
        "yume": "호시노 쨩… 조는 얼굴도 A+… 유메가 노트 대신 필기해줄까? ✍️💤"
      },
      {
        "obs": "훈련 끝나고 물 한 모금 마시더니, '이 정도는 별거 아니에요'라고 했어.",
        "yume": "별거 아니면… 유메는 왜 이렇게 심장이 두근거려…? 🥺💓"
      },
      {
        "obs": "잔소리 듣는 중인데도 표정은 끝까지 무덤덤. 약간 삐친 거 같기도 하고.",
        "yume": "혼나는 호시노 쨩도… 귀여워서… 유메가 대신 혼날게… 으앵~ 🙇‍♀️"
      },
      {
        "obs": "방패를 어깨에 걸치고 그늘에서 쉬고 있어. 바람에 머리카락이 살랑살랑…",
        "yume": "그 장면, 유메만 몰래 소장할래… (마음속에) 🌾💙"
      }
    ],
    "evening": [
      {
        "obs": "아비도스 사막 순찰 다녀오는 길인가 봐. 땀범벅인데도 눈빛은 살아있네.",
        "yume": "얼른 가서 시원한 물수건 줘야지! 유메가 준비했어 🧊🧼"
      },
      {
        "obs": "목욕하고 나온 호시노 쨩 발견! 볼이 발그레해서 평소보다 100배 말랑해 보여…",
        "yume": "한 번만 찌르면 안 될까? 딱 한 번만…! (안 돼) 😵‍💫💕"
      },
      {
        "obs": "저녁 노을 아래에서 방패 닦는 손놀림이 너무 진지해. 완전 장인.",
        "yume": "그 모습이 멋있어서… 유메는 말이 자꾸 길어져… 으헤~ 🌇🛡️"
      },
      {
        "obs": "식당 앞에서 잠깐 멈춰서 메뉴를 고민하더니, 결국 같은 걸 고르더라.",
        "yume": "취향이 확고한 호시노 쨩… 그게 또 귀여워… 🍛😌"
      },
      {
        "obs": "순찰 끝나고 의자에 털썩. '피곤해요' 한마디가 너무 솔직해서 놀랐어.",
        "yume": "피곤하면 기대도 돼… 유메 어깨, 오늘만 할인…! 💺💙"
      },
      {
        "obs": "저녁 바람이 차가워졌는데도 겉옷 안 챙기는 후배… 위험해.",
        "yume": "담요 투척 준비 완료. 호시노 쨩, 도망치지 마…! 🧣🫣"
      }
    ],
    "night": [
      {
        "obs": "쉿… 드디어 잠들었어. 자면서도 웅얼웅얼 잠꼬대 하네. 악몽은 아니겠지…",
        "yume": "손 잡아주고 있어야겠다. 유메는 여기 있을게 🤝🌙"
      },
      {
        "obs": "이불을 걷어찼길래 다시 덮어줬어. 자는 얼굴은 진짜 천사라니까…",
        "yume": "깨어있을 때도 이렇게 솔직하면 좋을 텐데~ 헤헤 😇🛏️"
      },
      {
        "obs": "베개를 꼭 끌어안고 있어… 방패는 침대 옆에 딱. 역시 호시노 쨩.",
        "yume": "안심했어… 유메도 이제 조용히 지킬게… 🛡️💤"
      },
      {
        "obs": "코가 아주 살짝… 들썩. 숨소리가 규칙적이야. 생존 확인 완료.",
        "yume": "확인만 했어! 진짜로! 몰래 관찰일기… 으앵~ 🫣📓"
      },
      {
        "obs": "머리카락이 이마에 내려와서 간지러울 것 같아… 살짝 정리해줬어.",
        "yume": "이건… 보호 활동이야! 주접 아님! (아마도) 🥹✨"
      },
      {
        "obs": "창밖이 조용해. 호시노 쨩도 조용해. 이 순간이 너무 소중해…",
        "yume": "유메는 오늘도 ‘후배가 안전한 세계’를 꿈꿔… 🌌💙"
      }
    ]
  }
}''')
                HOSHINO_DIARY_PATH.parent.mkdir(parents=True, exist_ok=True)
                HOSHINO_DIARY_PATH.write_text(
                    json.dumps(default_diary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning("기본 hoshino_diary 생성 실패: %s", e)

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
        """유메 선배의 비밀 관찰 일기(!호시노 / !1학년).

        - LLM 호출 없이, 시간대별 멘트 DB에서 랜덤 출력.
        - 출력은 2줄 고정:
          1) [시간] + [관찰 내용]
          2) [유메의 한마디]
        - force 인자에 새벽/아침/낮/저녁/밤 등을 넣으면 시간대 강제(연출/디버그).
        """
        forced_key = _parse_force_band_arg(force)
        band_key, _band_label, clock = _pick_time_band_kst(forced_key=forced_key)

        # DB 로드
        default_db: Dict[str, Any] = {"version": 1, "bands": {}}
        db = _safe_load_json(HOSHINO_DIARY_PATH, default_db)
        bands = db.get("bands") if isinstance(db, dict) else {}
        if not isinstance(bands, dict):
            bands = {}

        entries = bands.get(band_key) if isinstance(bands.get(band_key), list) else []
        if not entries:
            # fallback: 아무 밴드라도 있으면 사용
            for v in bands.values():
                if isinstance(v, list) and v:
                    entries = v
                    break

        if not entries:
            obs = "호시노 쨩이… 어디선가… 힘내고 있어…!"
            yline = "유메는… 몰래 응원 중이야… 으헤~ 💙"
            reply = f"📓 [시간] {clock} | [관찰 내용] {obs}\n💬 [유메의 한마디] {yline}"
            await ctx.send(_sanitize_mentions(reply), allowed_mentions=discord.AllowedMentions.none())
            return

        # 같은 채널에서 연속 호출 시 같은 문구 반복을 최대한 피하기
        ch_id = int(getattr(ctx.channel, "id", 0) or 0)
        last_sig: Optional[str] = None
        try:
            if ch_id:
                cache = self._hoshino_cache.get(ch_id) or {}
                last_sig = str(cache.get("sig") or "") or None
        except Exception:
            last_sig = None

        picked = random.choice(entries)
        sig = None

        def _sig_of(item: Any) -> str:
            if not isinstance(item, dict):
                return str(item)
            return f"{band_key}|{item.get('obs','')}|{item.get('yume','')}"

        if last_sig and len(entries) > 1:
            for _ in range(12):
                cand = random.choice(entries)
                cand_sig = _sig_of(cand)
                if cand_sig != last_sig:
                    picked = cand
                    break

        sig = _sig_of(picked)

        if isinstance(picked, dict):
            obs = str(picked.get("obs") or "").strip()
            yline = str(picked.get("yume") or "").strip()
        else:
            obs = str(picked).strip()
            yline = "유메는… 몰래 응원 중이야… 으헤~ 💙"

        if not obs:
            obs = "호시노 쨩이… 방패를 꼭 쥐고… 멋지게… 버티고 있어."
        if not yline:
            yline = "유메는… 들키지 않게… 좋아하는 중이야… 🫣💙"

        obs = _sanitize_mentions(obs)
        yline = _sanitize_mentions(yline)

        reply = f"📓 [시간] {clock} | [관찰 내용] {obs}\n💬 [유메의 한마디] {yline}"

        try:
            if ch_id:
                self._hoshino_cache[ch_id] = {"ts": datetime.utcnow().timestamp(), "sig": sig}
        except Exception:
            pass

        await ctx.send(reply, allowed_mentions=discord.AllowedMentions.none())
async def setup(bot: commands.Bot):
    await bot.add_cog(YumeFunCog(bot))