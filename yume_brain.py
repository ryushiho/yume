import json
import os
import datetime
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Literal, Optional, Tuple

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # 나중에 오류 메시지로 안내

from yume_prompt import YUME_ROLE_PROMPT_KR
from yume_store import get_world_state


WEATHER_LABEL = {
    "clear": "맑음",
    "cloudy": "흐림",
    "sandstorm": "대형 모래폭풍",
}



_ENV_LOADED = False


def _load_env_from_dotenv() -> None:
    """
    yume.py 와 마찬가지로, 프로젝트 루트의 .env 만 읽어서 os.environ 에 넣는다.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    root_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(root_dir, ".env")

    try:
        from dotenv import load_dotenv  # type: ignore

        if os.path.exists(env_path):
            load_dotenv(env_path, override=True)
    except ImportError:
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()
                        if key:
                            os.environ[key] = value
            except Exception:
                pass

    _ENV_LOADED = True



@dataclass
class YumeLLMPrice:
    """
    1K 토큰 기준 가격(USD).
    실제 가격은 OpenAI 대시보드 기준으로 맞춰서 .env에서 조정하면 됨.
    """
    input_per_1k: float = 0.00015  # 예시 값
    output_per_1k: float = 0.0006  # 예시 값


@dataclass
class YumeLLMConfig:
    api_key: str
    model: str = "gpt-4o-mini"
    hard_limit_usd: float = 10.0
    price: YumeLLMPrice = field(default_factory=YumeLLMPrice)
    usage_path: str = "data/system/llm_usage.json"


@dataclass
class YumeLLMMonthUsage:
    """
    한 달 단위 사용량 기록 구조.
    month: "YYYY-MM"
    """
    month: str
    total_usd: float = 0.0
    total_tokens: int = 0
    total_calls: int = 0



def _get_current_month_str() -> str:
    now = datetime.datetime.now()
    return now.strftime("%Y-%m")


def _safe_load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _safe_save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)



class YumeBrain:
    """
    유메 전용 LLM 래퍼.
    - 월별 사용량 관리
    - 감정/관계/Scene 상태를 받아 프롬프트 구성
    - free talk / diary / special message 등 모드별 프롬프트 템플릿 분리

    ⚠️ 주의:
    - 유저에게 보여지는 '유메의 대사'는 전부 OpenAI 모델이 생성한다.
    - 이 파일 안에서 유메 말투의 fallback 템플릿을 두지 않는다.
      (에러/한도초과 시에는 ok=False + 빈 reply 를 반환하고, 상위 레이어에서 처리하게 한다.)
    """

    def __init__(self, config: Optional[YumeLLMConfig] = None):
        _load_env_from_dotenv()

        if config is None:
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError(
                    "[YumeBrain] OPENAI_API_KEY가 설정되어 있지 않습니다. "
                    ".env 파일에 OPENAI_API_KEY=... 를 추가해 주세요."
                )

            model = os.getenv("YUME_OPENAI_MODEL", "gpt-4o-mini").strip()
            try:
                hard_limit = float(os.getenv("YUME_OPENAI_LIMIT_USD", "10.0"))
            except ValueError:
                hard_limit = 10.0

            try:
                price_input = float(os.getenv("YUME_OPENAI_PRICE_INPUT", "0.00015"))
                price_output = float(os.getenv("YUME_OPENAI_PRICE_OUTPUT", "0.0006"))
            except ValueError:
                price_input, price_output = 0.00015, 0.0006

            price = YumeLLMPrice(
                input_per_1k=price_input,
                output_per_1k=price_output,
            )

            config = YumeLLMConfig(
                api_key=api_key,
                model=model,
                hard_limit_usd=hard_limit,
                price=price,
                usage_path="data/system/llm_usage.json",
            )

        self.config = config

        if OpenAI is None:
            raise RuntimeError(
                "[YumeBrain] openai 패키지가 설치되어 있지 않습니다.\n"
                "pip install --upgrade openai\n"
                "명령어로 설치해 주세요."
            )

        self.client = OpenAI(api_key=self.config.api_key)

        self._month_usage = self._load_month_usage()


    def _load_month_usage(self) -> YumeLLMMonthUsage:
        raw = _safe_load_json(self.config.usage_path, {})
        current_month = _get_current_month_str()

        if not raw or raw.get("month") != current_month:
            usage = YumeLLMMonthUsage(month=current_month)
            self._save_month_usage(usage)
            return usage

        return YumeLLMMonthUsage(
            month=raw.get("month", current_month),
            total_usd=float(raw.get("total_usd", 0.0)),
            total_tokens=int(raw.get("total_tokens", 0)),
            total_calls=int(raw.get("total_calls", 0)),
        )

    def _save_month_usage(self, usage: Optional[YumeLLMMonthUsage] = None) -> None:
        if usage is None:
            usage = self._month_usage
        data = asdict(usage)
        _safe_save_json(self.config.usage_path, data)

    def _estimate_cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        p = self.config.price
        input_cost = (prompt_tokens / 1000.0) * p.input_per_1k
        output_cost = (completion_tokens / 1000.0) * p.output_per_1k
        return input_cost + output_cost

    def _can_spend(self, extra_cost: float) -> bool:
        return (self._month_usage.total_usd + extra_cost) <= self.config.hard_limit_usd

    def _update_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> float:
        cost = self._estimate_cost_usd(prompt_tokens, completion_tokens)
        self._month_usage.total_usd += cost
        self._month_usage.total_tokens += total_tokens
        self._month_usage.total_calls += 1
        self._save_month_usage()
        return cost

    def get_usage_summary(self) -> Dict[str, Any]:
        """
        /유메상태 또는 디버그 전용으로 사용 가능.
        """
        return {
            "month": self._month_usage.month,
            "total_usd": round(self._month_usage.total_usd, 6),
            "total_tokens": self._month_usage.total_tokens,
            "total_calls": self._month_usage.total_calls,
            "limit_usd": self.config.hard_limit_usd,
            "remain_usd": max(
                0.0, self.config.hard_limit_usd - self._month_usage.total_usd
            ),
        }


    def _build_system_prompt(
        self,
        mode: Literal["free_talk", "diary", "special"],
        scene: Optional[str],
        yume_state: Optional[Dict[str, Any]],
        user_profile: Optional[Dict[str, Any]],
    ) -> str:
        """
        유메 성격은 yume_ai.py + 템플릿 쪽에서 이미 고정되어 있다고 가정.
        여기서는 'LLM에게 넘겨줄 요약 버전'만 사용.
        """
        scene_text = scene or "unknown"

        mood = (yume_state or {}).get("mood", "neutral")
        energy = (yume_state or {}).get("energy", "normal")
        loneliness = (yume_state or {}).get("loneliness", "normal")
        focus = (yume_state or {}).get("focus", "normal")

        bond_level = (user_profile or {}).get("bond_level", "normal")
        user_nick = (user_profile or {}).get("nickname", "")
        honorific = (user_profile or {}).get("honorific", "선생님")

        # Phase1: virtual "Abydos weather" context.
        try:
            world = get_world_state()
            weather = str(world.get("weather") or "clear")
        except Exception:
            weather = "clear"
        weather_label = WEATHER_LABEL.get(weather, weather)

        # 유저가 지정한 유메 Role Definition을 시스템 프롬프트로 그대로 사용한다.
        # (모델/AI/LLM 언급 금지 포함)
        base_desc = YUME_ROLE_PROMPT_KR

        state_desc = (
            f"\n\n[유메 현재 상태]\n"
            f"- Scene(시간대): {scene_text}\n"
            f"- mood(기분): {mood}\n"
            f"- energy(에너지): {energy}\n"
            f"- loneliness(외로움): {loneliness}\n"
            f"- focus(집중도): {focus}\n"
            f"- 이 유저와의 bond(친밀도): {bond_level}\n"
            f"- 아비도스 날씨(가상): {weather_label}\n"
        )

        if weather == "sandstorm":
            state_desc += (
                "- (연출) 대형 모래폭풍이라서, 가끔 모래/통신 장애로 잠깐 당황하는 묘사를 섞어도 돼. "
                "단, 가독성은 유지하고 잡음(지…지지직…)은 0~1회만.\n"
            )

        nick_line = f"- 닉네임(참고): {user_nick}\n" if user_nick else ""
        user_desc = (
            f"\n[상대 유저]\n"
            f"- 기본 호칭: '{honorific}'\n"
            + nick_line
        )

        mode_desc = ""
        if mode == "free_talk":
            mode_desc = (
                "\n[모드]\n"
                "- 지금은 프리토킹 모드야.\n"
                "- 상대의 말을 잘 듣고, 자연스럽게 이어지는 대화를 해.\n"
                "- 너무 장황하게 설명하지 말고, 친근한 1~3문장 정도로 답변해.\n"
                "- 질문에는 성실하게 대답하지만, 분위기를 너무 무겁게 만들지 말 것.\n"
            )
        elif mode == "diary":
            mode_desc = (
                "\n[모드]\n"
                "- 지금은 '유메일기' / 하루 요약 모드야.\n"
                "- 오늘 있었던 일을 유메의 시점에서 일기처럼 정리해.\n"
                "- 감정과 분위기를 중심으로 서술하고, 상황에 따라 길이는 지시에 맞춰.\n"
                "- 직접적인 명령문보다는, 유메가 혼잣말하듯 적는 느낌으로.\n"
            )
        elif mode == "special":
            mode_desc = (
                "\n[모드]\n"
                "- 지금은 특별 멘트 모드야 (위로/응원/축하 등).\n"
                "- 상황에 맞게 다정하게 공감해주고, 마지막에 살짝 힘이 되는 말을 남겨줘.\n"
                "- 2~5문장 정도로 답변해.\n"
            )

        style_rules = (
            "\n[스타일 가이드]\n"
            "- 항상 한국어로 답변해.\n"
            "- 너무 과장된 이모지는 자제하고, 가볍게 사용하는 건 괜찮아.\n"
            "- 유메가 직접 행동하는 것처럼, 1인칭 시점으로 말해.\n"
            "- '유메는 ~'이라고 자기소개하듯 말하기보다는, 그냥 자연스럽게 대화하듯 말해.\n"
        )

        return base_desc + state_desc + user_desc + mode_desc + style_rules

    def _build_messages(
        self,
        user_message: str,
        mode: Literal["free_talk", "diary", "special"],
        scene: Optional[str],
        yume_state: Optional[Dict[str, Any]],
        user_profile: Optional[Dict[str, Any]],
        history: Optional[List[Tuple[str, str]]] = None,
    ) -> List[Dict[str, str]]:
        """
        history: [(role, content)] 형태의 리스트 (role은 "user" 또는 "assistant")
        """
        system_prompt = self._build_system_prompt(
            mode=mode,
            scene=scene,
            yume_state=yume_state,
            user_profile=user_profile,
        )

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt}
        ]

        if history:
            for role, content in history:
                if role not in ("user", "assistant"):
                    continue
                messages.append({"role": role, "content": content})

        if user_message:
            messages.append({"role": "user", "content": user_message})

        return messages


    def chat(
        self,
        user_message: str,
        mode: Literal["free_talk", "diary", "special"] = "free_talk",
        scene: Optional[str] = None,
        yume_state: Optional[Dict[str, Any]] = None,
        user_profile: Optional[Dict[str, Any]] = None,
        history: Optional[List[Tuple[str, str]]] = None,
        max_tokens: int = 256,
        temperature: float = 0.8,
    ) -> Dict[str, Any]:
        """
        실제로 Cog 등에서 호출하는 메인 엔트리.

        반환 구조 예:
        {
            "ok": True/False,
            "reason": "ok" | "limit_exceeded" | "error",
            "reply": "유메의 대사 (항상 LLM이 생성한 것) 또는 빈 문자열",
            "usage": { ... 사용량 정보 ... },
            "error": "에러 메시지 (선택)"
        }
        """
        messages = self._build_messages(
            user_message=user_message,
            mode=mode,
            scene=scene,
            yume_state=yume_state,
            user_profile=user_profile,
            history=history,
        )

        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:
            return {
                "ok": False,
                "reason": "error",
                "reply": "",
                "error": str(e),
                "usage": {},
            }

        choice = response.choices[0]
        reply_text = choice.message.content.strip() if choice.message.content else ""

        usage = getattr(response, "usage", None)
        if usage is None:
            return {
                "ok": True,
                "reason": "ok",
                "reply": reply_text,
                "usage": {
                    "tracked": False,
                    "month": self._month_usage.month,
                },
            }

        prompt_tokens = getattr(usage, "prompt_tokens", 0)
        completion_tokens = getattr(usage, "completion_tokens", 0)
        total_tokens = getattr(usage, "total_tokens", prompt_tokens + completion_tokens)

        estimated_cost = self._estimate_cost_usd(prompt_tokens, completion_tokens)
        if not self._can_spend(estimated_cost):
            return {
                "ok": False,
                "reason": "limit_exceeded",
                "reply": "",
                "usage": self.get_usage_summary(),
            }

        cost = self._update_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

        usage_summary = self.get_usage_summary()
        usage_summary.update(
            {
                "last_prompt_tokens": prompt_tokens,
                "last_completion_tokens": completion_tokens,
                "last_total_tokens": total_tokens,
                "last_cost_usd": round(cost, 6),
            }
        )

        return {
            "ok": True,
            "reason": "ok",
            "reply": reply_text,
            "usage": usage_summary,
        }


    def chat_custom(
        self,
        *,
        system_prompt: str,
        user_message: str,
        history: Optional[List[Tuple[str, str]]] = None,
        max_tokens: int = 384,
        temperature: float = 0.85,
    ) -> Dict[str, Any]:
        """커스텀 시스템 프롬프트로 LLM을 호출한다.

        - 특정 기능(포스터/호시노 중계 등)에서 모드별 기본 프롬프트 대신,
          기능 전용 프롬프트를 쓰고 싶을 때 사용.
        - 월 사용량/한도 체크는 기본 chat()와 동일하게 적용된다.

        history: [(role, content), ...]
          - role은 'user' 또는 'assistant'만 허용
        """

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt.strip()},
        ]

        if history:
            for role, content in history[-12:]:
                r = str(role).strip().lower()
                if r not in ("user", "assistant"):
                    continue
                c = (content or "").strip()
                if not c:
                    continue
                messages.append({"role": r, "content": c})

        messages.append({"role": "user", "content": user_message.strip()})

        # OpenAI 호출 + 사용량/한도 관리
        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:
            return {
                "ok": False,
                "reason": "error",
                "reply": "",
                "error": str(e),
                "usage": {},
            }

        choice = response.choices[0]
        reply_text = choice.message.content.strip() if choice.message.content else ""

        usage = getattr(response, "usage", None)
        if usage is None:
            return {
                "ok": True,
                "reason": "ok",
                "reply": reply_text,
                "usage": {
                    "tracked": False,
                    "month": self._month_usage.month,
                },
            }

        prompt_tokens = getattr(usage, "prompt_tokens", 0)
        completion_tokens = getattr(usage, "completion_tokens", 0)
        total_tokens = getattr(usage, "total_tokens", prompt_tokens + completion_tokens)

        estimated_cost = self._estimate_cost_usd(prompt_tokens, completion_tokens)
        if not self._can_spend(estimated_cost):
            return {
                "ok": False,
                "reason": "limit_exceeded",
                "reply": "",
                "usage": self.get_usage_summary(),
            }

        cost = self._update_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

        usage_summary = self.get_usage_summary()
        usage_summary.update(
            {
                "last_prompt_tokens": prompt_tokens,
                "last_completion_tokens": completion_tokens,
                "last_total_tokens": total_tokens,
                "last_cost_usd": round(cost, 6),
            }
        )

        return {
            "ok": True,
            "reason": "ok",
            "reply": reply_text,
            "usage": usage_summary,
        }



if __name__ == "__main__":
    print("[YumeBrain] 간단 테스트를 시작합니다.")

    try:
        brain = YumeBrain()
    except Exception as e:
        print("초기화 실패:", e)
        exit(1)

    dummy_state = {
        "mood": "happy",
        "energy": "high",
        "loneliness": "low",
        "focus": "normal",
    }
    dummy_user = {
        "nickname": "테스트후배",
        "bond_level": "close",
    }

    result = brain.chat(
        user_message="유메, 요즘 어때?",
        mode="free_talk",
        scene="evening",
        yume_state=dummy_state,
        user_profile=dummy_user,
        history=None,
        max_tokens=128,
    )

    print("결과:", json.dumps(result, ensure_ascii=False, indent=2))
