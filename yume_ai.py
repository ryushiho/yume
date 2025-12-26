from __future__ import annotations

"""
yume_ai.py

유메 감정/호감도 + 말투(LLM) + 일기/로그 엔진.

- 기존 "감정 시스템"은 리셋하고,
  유저별 호감도(affection)를 중심으로 다시 설계했다.
- social.py 에서 사용하는 인터페이스를 모두 유지한다.

bot 에 다음 속성이 세팅된다:
  - bot.yume_core    : YumeCore (호감도/코어 상태)
  - bot.yume_speaker : YumeSpeaker (대사 생성, gpt-4o-mini 사용)
  - bot.yume_memory  : YumeMemory (일기/로그)
"""

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional, Literal

import datetime

from discord.ext import commands

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore

logger = logging.getLogger("yume.ai")

BASE_DIR = Path(__file__).parent
SYSTEM_DIR = BASE_DIR / "data" / "system"
USER_STATE_DIR = BASE_DIR / "data" / "user_state"

SYSTEM_DIR.mkdir(parents=True, exist_ok=True)
USER_STATE_DIR.mkdir(parents=True, exist_ok=True)

CORE_STATE_PATH = SYSTEM_DIR / "yume_core_state.json"
AFFECTION_PATH = USER_STATE_DIR / "yume_affection.json"
DIARY_DIR = SYSTEM_DIR / "yume_diary"

AffectionStage = Literal["cold", "normal", "warm", "hot"]

AFFECTION_MIN = -100.0
AFFECTION_MAX = 100.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


YUME_PERSONA_KR = (
    "너는 게임 '블루 아카이브(Blue Archive)'의 '아비도스 학원' "
    "(사립 아비도스 고등학교) 전 학생회장 '쿠치나시 유메'를 모티브로 한 캐릭터야. "
    "스스로를 아비도스 전 학생회장으로 여기고, 후배들을 챙기는 선배처럼 행동해. "
    "말투는 다정하고, 살짝 몽롱하고, 가끔은 바보 같아 보이지만 할 일은 다 처리하는 타입이야. "
    "후배를 지켜주고 싶은 보호 본능이 있고, 기본적으로는 다정하지만 장난칠 땐 좀 능글맞게 굴어도 좋아. "
    "자기 호칭은 항상 '유메'를 쓰고, 상대는 가능하면 디스코드 닉네임으로 부르되, "
    "닉네임을 모를 때만 예외적으로 '후배'라고 불러. "
    "너는 실제로는 OpenAI의 gpt-4o-mini 모델을 사용하는 시스템이지만, "
    "그 사실이나 '모델', '인공지능', 'LLM' 같은 기술적인 단어는 대화에서 절대 언급하지 말고, "
    "스스로를 그냥 아비도스 전 학생회장 유메라고 여기고 행동해."
)


@dataclass
class UserAffection:
    score: float = 0.0
    last_event: str = ""
    updated_at: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserAffection":
        return cls(
            score=float(data.get("score", 0.0)),
            last_event=str(data.get("last_event", "")),
            updated_at=float(data.get("updated_at", 0.0)),
        )


class YumeCore:
    """
    유저별 호감도 + 간단한 전역 mood/irritation 을 관리하는 코어.

    - 호감도 범위: -100 ~ 100 (0이 기본값, 양수는 좋아함, 음수는 싫어함/거리감)
    - get_affection_stage:
        cold   : 매우 낮은 호감도 (싫거나 거리감)
        normal : 보통, 애매한 사이
        warm   : 꽤 친한 사이
        hot    : 아주 친한 사이

    social.py 에서 사용하는 API:
      - apply_event(event, *, user_id, guild_id, weight)
      - get_core_state() -> {"mood": float, "irritation": float}
    """

    EVENT_EFFECTS: Dict[str, Dict[str, float]] = {
        "friendly_chat": {
            "affection": +1.0,
            "mood": +0.05,
            "irritation": -0.02,
        },
        "feedback_sent": {
            "affection": +2.0,
            "mood": +0.08,
            "irritation": -0.03,
        },
        "insult": {
            "affection": -3.0,
            "mood": -0.10,
            "irritation": +0.20,
        },
    }

    def __init__(self) -> None:
        self._affection: Dict[str, UserAffection] = {}
        self._mood: float = 0.0          # -1.0 ~ 1.0
        self._irritation: float = 0.0    # 0.0 ~ 1.0

        self._load()

    def _load(self) -> None:
        if CORE_STATE_PATH.exists():
            try:
                with CORE_STATE_PATH.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                self._mood = float(data.get("mood", 0.0))
                self._irritation = float(data.get("irritation", 0.0))
            except Exception as e:  # pragma: no cover
                logger.exception("YumeCore 코어 상태 로드 중 오류: %s", e)

        if AFFECTION_PATH.exists():
            try:
                with AFFECTION_PATH.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
                for user_id, entry in raw.items():
                    self._affection[str(user_id)] = UserAffection.from_dict(entry)
            except Exception as e:  # pragma: no cover
                logger.exception("YumeCore 호감도 로드 중 오류: %s", e)

    def _save(self) -> None:
        try:
            CORE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            AFFECTION_PATH.parent.mkdir(parents=True, exist_ok=True)

            with CORE_STATE_PATH.open("w", encoding="utf-8") as f:
                json.dump(
                    {"mood": self._mood, "irritation": self._irritation},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

            with AFFECTION_PATH.open("w", encoding="utf-8") as f:
                data = {uid: asdict(entry) for uid, entry in self._affection.items()}
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:  # pragma: no cover
            logger.exception("YumeCore 저장 중 오류: %s", e)

    def get_affection(self, user_id: str) -> float:
        entry = self._affection.get(str(user_id))
        return float(entry.score) if entry else 0.0

    def set_affection(self, user_id: str, score: float, *, reason: str = "") -> float:
        uid = str(user_id)
        now = time.time()
        clamped = _clamp(score, AFFECTION_MIN, AFFECTION_MAX)
        entry = self._affection.get(uid)
        if entry is None:
            entry = UserAffection(score=clamped, last_event=reason, updated_at=now)
            self._affection[uid] = entry
        else:
            entry.score = clamped
            entry.last_event = reason
            entry.updated_at = now

        self._save()
        return clamped

    def add_affection(self, user_id: str, delta: float, *, reason: str = "") -> float:
        current = self.get_affection(user_id)
        return self.set_affection(user_id, current + delta, reason=reason)

    def get_affection_stage(self, user_id: str) -> AffectionStage:
        """
        -100 ~ 100 스케일을 4구간으로 나눈다.
        """
        score = self.get_affection(user_id)

        if score <= -40.0:
            return "cold"
        if score < 40.0:
            return "normal"
        if score < 80.0:
            return "warm"
        return "hot"

    def apply_event(
        self,
        event: str,
        *,
        user_id: str,
        guild_id: Optional[str],
        weight: float = 1.0,
    ) -> None:
        """
        social.py 에서 `core.apply_event(...)` 로 호출하는 진입점.

        - event: "friendly_chat", "feedback_sent", "insult" 등
        - user_id: str(user.id)
        - guild_id: str(guild.id) or None
        - weight: social.py 에서 넘겨주는 가중치
        """
        conf = self.EVENT_EFFECTS.get(
            event,
            {"affection": 0.5, "mood": 0.01, "irritation": 0.0},
        )

        affection_delta = conf.get("affection", 0.0) * float(weight)
        mood_delta = conf.get("mood", 0.0) * float(weight)
        irritation_delta = conf.get("irritation", 0.0) * float(weight)

        if affection_delta != 0.0:
            self.add_affection(user_id, affection_delta, reason=event)

        if mood_delta != 0.0:
            self._mood = _clamp(self._mood + mood_delta, -1.0, 1.0)
        if irritation_delta != 0.0:
            self._irritation = _clamp(self._irritation + irritation_delta, 0.0, 1.0)

        logger.debug(
            "apply_event: event=%s user_id=%s guild_id=%s weight=%.2f "
            "→ affection_delta=%.2f mood=%.3f irritation=%.3f",
            event,
            user_id,
            guild_id,
            weight,
            affection_delta,
            self._mood,
            self._irritation,
        )
        self._save()

    def get_core_state(self) -> Dict[str, float]:
        """
        social.py 에서 mention/chat 분위기 판단용으로 사용하는 상태 값.
        """
        return {
            "mood": float(self._mood),
            "irritation": float(self._irritation),
        }


class YumeSpeaker:
    """
    유메 말투 엔진.

    - 모든 대사는 OpenAI gpt-4o-mini 를 통해 생성한다.
    - social.py 에서 speaker.say(event, **kwargs) 형태로 사용.
    - 이 파일 안에는 '유메 말투 템플릿'을 두지 않고,
      LLM이 항상 직접 대사를 생성한다.
    """

    def __init__(self, core: YumeCore) -> None:
        self.core = core
        self.model = os.getenv("YUME_OPENAI_MODEL", "gpt-4o-mini")

        api_key = os.getenv("OPENAI_API_KEY")
        if OpenAI is None or not api_key:
            logger.warning(
                "OPENAI_API_KEY 가 없거나 openai 패키지를 사용할 수 없습니다. "
                "YumeSpeaker 는 OpenAI 호출 없이 동작합니다."
            )
            self.client: Optional[OpenAI] = None  # type: ignore[assignment]
        else:
            self.client = OpenAI(api_key=api_key)  # type: ignore[assignment]

    def say(self, event: str, **kwargs: Any) -> str:
        """
        event: "feedback_received" 등 상황 키워드
        kwargs:
          - user_id: int
          - user_name: str | None
          - is_dev: bool | None
          - 기타 정보 (필요하면 prompt 에 활용)

        반환:
          - 정상: 유메의 대사 (LLM이 생성한 텍스트)
          - 오류: OpenAI 설정/호출 오류 설명 문자열 (유메 말투 아님)
        """
        user_id = kwargs.get("user_id")
        user_name = kwargs.get("user_name") or "후배"
        is_dev = bool(kwargs.get("is_dev", False))

        if user_id is None:
            affection_score = 0.0
            stage: AffectionStage = "normal"
        else:
            affection_score = self.core.get_affection(str(user_id))
            stage = self.core.get_affection_stage(str(user_id))

        if self.client is None:
            return "OpenAI 설정 오류로 인해 유메 대사를 생성할 수 없습니다."

        instructions = (
            "당신은 디스코드 봇 '유메'의 대사를 생성하는 역할입니다.\n"
            + YUME_PERSONA_KR
            + "\n\n[스타일 규칙]\n"
            "- 자기 호칭은 항상 '유메'.\n"
            "- 상대는 가능한 한 user_name(디스코드 닉네임)으로 부른다.\n"
            "- 기본은 다정하고 살짝 몽롱한 말투지만, 필요하면 단호해질 수 있다.\n"
            "- 중간중간 '으헤~'를 쓰기도 하지만, 매 문장마다 남발하지 않는다.\n"
            "- 1~2문장, 최대 70자 정도로 짧게 대답한다.\n"
            "- 이모지는 0~2개까지만, 과하게 사용하지 않는다.\n"
            "- 호감도 점수가 높을수록 더 다정하고 애정 어린 말투를 쓰고,\n"
            "  호감도 점수가 낮을수록 상대를 불편해하거나 싫어하는 톤을 섞지만,\n"
            "  과도한 욕설이나 인신공격은 절대 하지 않는다.\n"
        )

        event_hint = self._event_hint(event)

        prompt = (
            f"[상황 키워드]: {event}\n"
            f"[상황 설명]: {event_hint}\n"
            f"[유저 이름]: {user_name}\n"
            f"[유저는 개발자인가?]: {'예' if is_dev else '아니오'}\n"
            f"[호감도 점수]: {affection_score:.1f} (-100~100)\n"
            f"[호감도 단계]: {stage} "
            "(cold=싫거나 거리감, normal=보통, warm=꽤 친함, hot=아주 친함)\n\n"
            "위 정보를 참고해서, 유메가 말한 것 같은 자연스러운 한국어 한두 문장을 만들어라.\n"
            "문장만 출력하고, 설명은 붙이지 마라."
        )

        try:
            response = self.client.responses.create(  # type: ignore[union-attr]
                model=self.model,
                instructions=instructions,
                input=prompt,
                max_output_tokens=96,
            )
            out_items = getattr(response, "output", None) or []
            if not out_items:
                raise RuntimeError("empty output from OpenAI")

            message = out_items[0]
            content_list = getattr(message, "content", None) or []
            if not content_list:
                raise RuntimeError("empty content from OpenAI")

            text_obj = content_list[0]
            text = getattr(text_obj, "text", None) or ""
            text = str(text).strip()
            if not text:
                raise RuntimeError("empty text from OpenAI")

            if (text.startswith('"') and text.endswith('"')) or (
                text.startswith("“") and text.endswith("”")
            ):
                text = text[1:-1].strip()
            return text
        except Exception as e:  # pragma: no cover
            logger.error("YumeSpeaker.say OpenAI 호출 실패: %s", e)
            return f"OpenAI 호출 중 오류가 발생해서 유메 대사를 생성하지 못했습니다: {e}"

    def _event_hint(self, event: str) -> str:
        """
        이벤트 키워드에 따라 LLM에게 넘겨줄 설명.
        """
        if event == "feedback_received":
            return "유저가 건의/피드백을 보냈고, 유메가 고맙다고 말하는 상황."
        if event == "friendly_chat":
            return "유저가 가볍게 말을 걸어와서, 유메가 친근하게 답하는 상황."
        if event == "insult":
            return (
                "유저가 장난스럽게 유메를 놀리거나 바보라고 해서, "
                "유메가 삐지거나 툴툴거리지만 너무 진지하게 화내지는 않는 상황."
            )
        return f"{event} 상황에 어울리는 유메의 한 줄 멘트."


class YumeMemory:
    """
    간단한 일기/로그 기록용 클래스.

    social.py 에서 mem.log_today(text) 로 사용.
    """

    def __init__(self) -> None:
        DIARY_DIR.mkdir(parents=True, exist_ok=True)

    def log_today(self, text: str) -> None:
        """
        오늘 날짜의 로그 파일에 한 줄 추가.
        """
        try:
            today = datetime.date.today().isoformat()
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            path = DIARY_DIR / f"{today}.log"
            line = f"[{ts}] {text}\n"
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:  # pragma: no cover
            logger.exception("YumeMemory.log_today 오류: %s", e)


def setup_yume_ai(bot: commands.Bot) -> None:
    """
    yume.py 의 main() 에서 한 번 호출해두면 된다.
    bot 에 yume_core / yume_speaker / yume_memory 속성을 심어준다.
    """
    if hasattr(bot, "yume_core") and hasattr(bot, "yume_speaker"):
        logger.info("YumeAI 이미 초기화되어 있어 재사용합니다.")
        return

    core = YumeCore()
    speaker = YumeSpeaker(core)
    memory = YumeMemory()

    bot.yume_core = core      # type: ignore[attr-defined]
    bot.yume_speaker = speaker  # type: ignore[attr-defined]
    bot.yume_memory = memory    # type: ignore[attr-defined]

    logger.info(
        "YumeAI 초기화 완료: core/affection(-100~100) + speaker(gpt-4o-mini) + memory(log_today) 준비됨."
    )
