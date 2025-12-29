"""yume_effects.py

Phase2: "무전기 노이즈(Glitch Effect)" 연출.

- 아비도스의 '대형 모래폭풍' 상태일 때, 유메의 메시지가 가끔 끊기거나 잡음이 섞여 보이도록
  텍스트에 미세한 노이즈를 주는 유틸리티.

원칙:
- 가독성 최우선: 전체의 약 20% 정도만 변형
- 멘션/채널태그/URL 같은 구조적 토큰은 손대지 않음
- 코드블록/인라인 코드(백틱 포함)는 안전을 위해 변형하지 않음
"""

from __future__ import annotations

import random
import re
from typing import List


_NOISE_MARKS = [
    "…",
    "…지지직…",
    "…끊김…",
    "##",
    "//",
    "(잡음)",
]

_MENTION_LIKE = re.compile(r"^<[@#].+>$")
_URL_LIKE = re.compile(r"^https?://", re.IGNORECASE)


def _is_protected_token(tok: str) -> bool:
    if not tok:
        return True
    if _MENTION_LIKE.match(tok):
        return True
    if _URL_LIKE.match(tok):
        return True
    return False


def _glitch_word(word: str) -> str:
    """Glitch a single token while keeping it recognizable."""
    if not word:
        return word

    # Very short tokens: just append a small mark.
    if len(word) <= 2:
        return word + random.choice(["…", "##", "…지지직…"])

    mode = random.random()

    # 1) Insert a noise mark in the middle.
    if mode < 0.50:
        cut = random.randint(1, max(1, len(word) - 1))
        mark = random.choice(["…", "…지지직…", "##"])
        return word[:cut] + mark + word[cut:]

    # 2) Replace the word with a generic noise once in a while.
    if mode < 0.60:
        return random.choice(["(잡음)", "…지지직…"])

    # 3) Add a trailing mark.
    return word + random.choice(["…", "##", "//"])


def apply_glitch(text: str, *, max_ratio: float = 0.20) -> str:
    """Apply mild glitch effect to text.

    max_ratio:
      - 0.20 means at most 20% of tokens are modified.

    Safety:
      - If the text contains backticks (inline code or code blocks), we return it as-is.
        (포스터/코드블록 등 형식을 깨지 않기 위한 방어)
    """
    if not text:
        return text

    # Protect markdown code formatting.
    if "`" in text or "```" in text:
        return text

    # Tokenize by spaces (preserve basic readability).
    tokens = text.split(" ")
    if len(tokens) <= 1:
        # No spaces: insert a small mark at a random position.
        if len(text) < 4:
            return text + "…"
        pos = random.randint(1, len(text) - 1)
        return text[:pos] + random.choice(["…", "…지지직…"]) + text[pos:]

    candidates: List[int] = []
    for i, tok in enumerate(tokens):
        if _is_protected_token(tok):
            continue
        # Skip tokens that are only punctuation.
        if tok.strip(".?!,~…#/") == "":
            continue
        candidates.append(i)

    if not candidates:
        return text

    max_ratio = max(0.0, min(float(max_ratio), 0.35))

    n = int(len(tokens) * max_ratio)
    if n <= 0:
        n = 1
    n = min(n, len(candidates))

    chosen = random.sample(candidates, n)
    for idx in chosen:
        tokens[idx] = _glitch_word(tokens[idx])

    out = " ".join(tokens)

    # With small probability, add a leading or trailing "radio" mark.
    if random.random() < 0.18:
        mark = random.choice(["…지지직…", "…", "(잡음)"])
        if random.random() < 0.5:
            out = f"{mark} {out}"
        else:
            out = f"{out} {mark}"

    return out


def split_for_radio(text: str) -> List[str]:
    """Split a text into 2 parts to mimic transmission hiccups.

    Returns:
      - [text] if splitting isn't helpful.
      - [part1, part2] when a good split point is found.
    """
    if not text:
        return [text]

    if len(text) < 80:
        return [text]

    if "`" in text or "```" in text:
        return [text]

    # Prefer splitting on newline near the center.
    if "\n" in text:
        lines = text.split("\n")
        if len(lines) >= 2:
            mid = len(lines) // 2
            p1 = "\n".join(lines[:mid]).strip()
            p2 = "\n".join(lines[mid:]).strip()
            if p1 and p2:
                return [p1, p2]

    # Otherwise split on whitespace near the middle.
    tokens = text.split(" ")
    if len(tokens) < 6:
        return [text]

    mid = len(tokens) // 2
    p1 = " ".join(tokens[:mid]).strip()
    p2 = " ".join(tokens[mid:]).strip()

    if not p1 or not p2:
        return [text]

    return [p1, p2]


def chunk_for_discord(text: str, *, limit: int = 1900) -> List[str]:
    """Chunk text into safe discord-sized messages."""
    if not text:
        return [text]

    limit = max(200, int(limit))

    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    s = text
    while s:
        if len(s) <= limit:
            chunks.append(s)
            break

        # Try to cut at newline.
        cut = s.rfind("\n", 0, limit)
        if cut < limit * 0.5:
            # Try to cut at space.
            cut = s.rfind(" ", 0, limit)
        if cut < limit * 0.5:
            cut = limit

        head = s[:cut].rstrip()
        if head:
            chunks.append(head)
        s = s[cut:].lstrip()

    return chunks
