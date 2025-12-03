import os
import random
from collections import defaultdict

from config import WORDS_FILE


def load_words():
    if not os.path.exists(WORDS_FILE):
        raise FileNotFoundError(f"단어 파일 못 찾겠어: {WORDS_FILE}")

    words_set: set[str] = set()
    words_by_first: dict[str, list[str]] = defaultdict(list)

    with open(WORDS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if not w:
                continue
            # 한 글자 단어는 사용하지 않음
            if len(w) < 2:
                continue
            words_set.add(w)

    for w in words_set:
        words_by_first[w[0]].append(w)

    # 길이 긴 단어 우선 정렬
    for k in words_by_first:
        words_by_first[k].sort(key=len, reverse=True)

    print(f"[INFO] 단어 {len(words_set)}개 로드 완료.")
    return words_set, words_by_first


# 모듈 import 시점에 한 번만 로딩
WORDS_SET, WORDS_BY_FIRST = load_words()


def exists_follow_word(start_char: str, used_words: set[str]) -> bool:
    """start_char로 시작하면서 아직 사용되지 않은 단어가 있는지 확인."""
    for w in WORDS_BY_FIRST.get(start_char, []):
        if w not in used_words:
            return True
    return False


def choose_start_word() -> str:
    """
    제시어(시작 단어)를 랜덤으로 하나 뽑되,
    - 그 단어의 마지막 글자로 시작하는, 아직 사용되지 않은 단어가 최소 1개 이상 있는 경우만 선택.
    """
    candidates: list[str] = []
    for w in WORDS_SET:
        last_char = w[-1]
        if exists_follow_word(last_char, used_words={w}):
            candidates.append(w)

    if not candidates:
        raise RuntimeError(
            "이어갈 수 있는 제시어 후보가 하나도 없어... 단어 목록을 한 번 점검해 줘."
        )

    return random.choice(candidates)
