"""yume_llm.py

Phase3: lightweight OpenAI helper for feature-specific generations.

- YumeSpeaker is optimized for short replies tied to an "event".
- Daily rules want a different output format, so we keep a small wrapper here
  while reusing the same persona prompt (yume_prompt.py).

Safety/robustness:
- If OpenAI isn't configured, functions return a deterministic fallback.
- Any exception is caught and returned as a fallback (and should be logged by caller).
"""

from __future__ import annotations

import os
import random
from typing import Any, Optional

from yume_prompt import YUME_ROLE_PROMPT_KR

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


_CLIENT: Optional["OpenAI"] = None


def _get_client() -> Optional["OpenAI"]:
    global _CLIENT

    api_key = os.getenv("OPENAI_API_KEY")
    if OpenAI is None or not api_key:
        return None

    if _CLIENT is None:
        _CLIENT = OpenAI(api_key=api_key)  # type: ignore[call-arg]
    return _CLIENT


def _cleanup_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""

    # Remove surrounding quotes.
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("“") and t.endswith("”")):
        t = t[1:-1].strip()

    # If model returned multi-line, keep the first meaningful line.
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if not lines:
        return ""

    # Drop bullets/prefixes.
    t0 = lines[0].lstrip("-•* ").strip()
    return t0


def generate_text(
    *,
    instructions: str,
    input_text: str,
    max_output_tokens: int = 256,
    model: Optional[str] = None,
) -> str:
    """Generate text via OpenAI Responses API.

    Returns a plain string. If OpenAI isn't configured, returns an empty string.
    """

    client = _get_client()
    if client is None:
        return ""

    m = model or os.getenv("YUME_OPENAI_MODEL", "gpt-4o-mini")

    response = client.responses.create(  # type: ignore[union-attr]
        model=m,
        instructions=instructions,
        input=input_text,
        max_output_tokens=int(max_output_tokens),
    )

    out_items = getattr(response, "output", None) or []
    if not out_items:
        return ""

    message = out_items[0]
    content_list = getattr(message, "content", None) or []
    if not content_list:
        return ""

    text_obj = content_list[0]
    text = getattr(text_obj, "text", None) or ""
    return _cleanup_text(str(text))


def generate_daily_rule(
    *,
    date_ymd: str,
    rule_no: int,
    weather_label: str,
    suggestion_hints: list[str] | None = None,
) -> str:
    """Generate a single daily rule line.

    Output should be one line, short, and suitable to embed in a channel announcement.
    """

    hints = suggestion_hints or []
    hint_text = "\n".join([f"- {h}" for h in hints[:3] if h.strip()])

    instructions = (
        YUME_ROLE_PROMPT_KR
        + "\n\n[출력 규칙]"
        + "\n- 오늘의 '아비도스 교칙'을 1개 만든다. (한 줄)"
        + "\n- 긍정적이지만 엉뚱하고, 사막 학교 느낌이 나야 한다."
        + "\n- 길이는 140자 이내. 이모지는 0~2개 정도."
        + "\n- AI/모델/LLM 같은 기술 언급 금지."
        + "\n- 결과는 교칙 문장만 출력한다. (머리말/해설 금지)"
    )

    prompt = (
        f"[날짜(KST)]: {date_ymd}\n"
        f"[교칙 번호]: 제 {int(rule_no)}조\n"
        f"[아비도스 날씨(가상)]: {weather_label}\n"
    )

    if hint_text:
        prompt += "\n[최근 교칙 건의(참고, 그대로 복붙 말 것)]:\n" + hint_text + "\n"

    prompt += (
        "\n위 정보들을 참고해, 오늘의 아비도스 교칙 한 줄을 작성해라.\n"
        "문장만 출력하라."
    )

    try:
        text = generate_text(instructions=instructions, input_text=prompt, max_output_tokens=128)
        if text:
            return text
    except Exception:
        # Let caller log; return fallback below.
        pass

    # Fallback (no OpenAI / error)
    fallbacks = [
        "모래바람이 불 때는 입을 벌리고 '아~' 소리를 내지 않는다! (사막 공기는 메뉴가 아니야~)",
        "축제 포스터를 붙일 땐 테이프를 두 겹으로! (한 겹은 모래가 가져가니까… 에헤헤)",
        "급식이 건빵이어도 코스 요리라고 믿는다! (믿음이 칼로리야~)",
    ]
    return random.choice(fallbacks)
