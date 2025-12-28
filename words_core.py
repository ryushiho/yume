import os
import random
from collections import defaultdict
from typing import DefaultDict, Dict, List, Set, Tuple

from config import WORDS_FILE, BLUEWAR_CACHED_WORDS_FILE


def load_words_from_file(path: str) -> Tuple[Set[str], Dict[str, List[str]]]:
    """단어 txt 파일을 읽어서 (words_set, words_by_first)로 반환."""
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"단어 파일 못 찾겠어: {path}")

    words_set: Set[str] = set()
    words_by_first: DefaultDict[str, List[str]] = defaultdict(list)

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            w = (line or "").strip()
            if not w:
                continue
            if len(w) < 2:
                continue
            words_set.add(w)

    for w in words_set:
        words_by_first[w[0]].append(w)

    for k in words_by_first:
        words_by_first[k].sort(key=len, reverse=True)

    return words_set, dict(words_by_first)


def pick_words_source(*, prefer_cache: bool = True) -> str:
    """단어 파일 경로를 선택한다.

    우선순위:
      1) 캐시(웹에서 내려받은 파일)
      2) 로컬 dictionary/blue_archive_words.txt
    """
    if prefer_cache and os.path.exists(BLUEWAR_CACHED_WORDS_FILE):
        try:
            if os.path.getsize(BLUEWAR_CACHED_WORDS_FILE) > 0:
                return BLUEWAR_CACHED_WORDS_FILE
        except Exception:
            pass
    return WORDS_FILE


def exists_follow_word(start_char: str, used_words: Set[str], words_by_first: Dict[str, List[str]]) -> bool:
    """start_char로 시작하면서 아직 사용되지 않은 단어가 있는지 확인."""
    for w in words_by_first.get(start_char, []):
        if w not in used_words:
            return True
    return False


def choose_start_word(words_set: Set[str], words_by_first: Dict[str, List[str]]) -> str:
    """제시어(시작 단어)를 랜덤으로 하나 뽑는다.

    - 그 단어의 마지막 글자로 시작하는, 아직 사용되지 않은 단어가 최소 1개 이상 있는 경우만 선택.
    """
    candidates: List[str] = []
    for w in words_set:
        last_char = w[-1]
        if exists_follow_word(last_char, used_words={w}, words_by_first=words_by_first):
            candidates.append(w)

    if not candidates:
        raise RuntimeError("이어갈 수 있는 제시어 후보가 하나도 없어... 단어 목록을 한 번 점검해 줘.")

    return random.choice(candidates)
