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


def _cleanup_text_multiline(text: str, *, max_lines: int = 6, max_chars: int = 900) -> str:
    """Keep a few readable lines for menu/poster style outputs."""

    t = (text or "").strip()
    if not t:
        return ""

    # Remove surrounding quotes.
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("“") and t.endswith("”")):
        t = t[1:-1].strip()

    # Drop empty lines and trim.
    lines = [ln.rstrip() for ln in t.splitlines() if ln.strip()]
    if not lines:
        return ""

    # Remove common bullet prefixes per-line.
    cleaned: list[str] = []
    for ln in lines:
        s = ln.lstrip("-•* ").rstrip()
        if s:
            cleaned.append(s)

    out = "\n".join(cleaned[: max(1, int(max_lines))]).strip()
    if len(out) > int(max_chars):
        out = out[: int(max_chars)].rstrip()
    return out


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


def generate_text_multiline(
    *,
    instructions: str,
    input_text: str,
    max_output_tokens: int = 256,
    model: Optional[str] = None,
    max_lines: int = 6,
    max_chars: int = 900,
) -> str:
    """Generate text but keep multiple lines (used for menus/posters)."""

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
    return _cleanup_text_multiline(str(text), max_lines=max_lines, max_chars=max_chars)


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


def generate_survival_meal(
    *,
    date_ymd: str,
    base_ingredient: str,
    weather_label: str,
) -> str:
    """Generate a fancy 'imaginary cafeteria menu' for Abydos.

    Output guideline:
    - 2~4 lines
    - Must mention the base ingredient is actually something humble
    - Must sound like Yume (no tech/AI talk)
    """

    instructions = (
        YUME_ROLE_PROMPT_KR
        + "\n\n[출력 규칙]"
        + "\n- 사실은 '{base}' 같은 허름한 음식이다. 이걸 최고급 레스토랑 메뉴처럼 포장한다.".format(
            base=str(base_ingredient)
        )
        + "\n- 2~4줄로 짧게. 첫 줄은 메뉴 이름(영문 느낌 + 한국어 괄호 해석)으로, 나머지는 설명 1~2문장." \
        + "\n- 과장되지만 귀엽고 희망찬 톤. 아비도스/사막/호시노 짱을 가끔 언급해도 됨(필수 아님)." \
        + "\n- 이모지는 0~3개." \
        + "\n- AI/모델/LLM/프롬프트 같은 기술 언급 금지." \
        + "\n- 출력은 결과 텍스트만. 머리말/해설/번호 금지."
    )

    prompt = (
        f"[날짜(KST)]: {date_ymd}\n"
        f"[아비도스 날씨(가상)]: {weather_label}\n"
        f"[현실 재료]: {base_ingredient}\n\n"
        "위 정보를 참고해서 '상상 급식표' 1개를 작성해라."
    )

    try:
        text = generate_text_multiline(
            instructions=instructions,
            input_text=prompt,
            max_output_tokens=220,
            max_lines=5,
            max_chars=850,
        )
        if text:
            return text
    except Exception:
        pass

    # Fallback
    return (
        "**'Double-Baked Wheat Cracker with Desert Air' (두 번 구운 건빵과 사막 공기 곁들임)**\n"
        "바삭함은 확실해! 목이 좀 막힐 수도 있지만… 그게 또 매력이지, 에헤헤~ 🌵"
    )
