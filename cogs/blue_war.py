from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import pickle
from collections import defaultdict
from typing import Dict, List, Set, Tuple, Optional, Any, Literal
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands

from words_core import WORDS_SET, WORDS_BY_FIRST, exists_follow_word
from records_core import load_records, save_records

logger = logging.getLogger(__name__)

# --------------------------------
# 설정값 (config.py에서 못 불러와도 기본값 사용)
# --------------------------------
try:
    from config import (  # type: ignore
        TURN_TIMEOUT,
        REVIEW_CHANNEL_ID,
        RESULT_CHANNEL_ID,
        RANK_CHANNEL_ID,
    )
except Exception:
    TURN_TIMEOUT: int = 30
    REVIEW_CHANNEL_ID: int = 0
    RESULT_CHANNEL_ID: int = 0
    RANK_CHANNEL_ID: int = 0

SUGGESTION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "dictionary",
    "suggestion.txt",
)

GRAPH_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "system",
    "bluewar_graph.pkl",
)

# --------------------------------
# OpenAI (연습 모드 / 블루전 멘트용)
# --------------------------------
try:
    from openai import AsyncOpenAI  # type: ignore
except Exception:
    AsyncOpenAI = None  # type: ignore

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
YUME_OPENAI_MODEL = os.getenv("YUME_OPENAI_MODEL") or "gpt-4o-mini"
YUME_BLUEWAR_USE_LLM = os.getenv("YUME_BLUEWAR_USE_LLM", "1").lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)

# bluewar 전용 LLM 클라이언트 (lazy init)
_BLUEWAR_LLM_CLIENT = None  # type: ignore[assignment]

# 유메 캐릭터 시스템 프롬프트
YUME_SYSTEM_PROMPT = (
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

# --------------------------------
# 두음법칙 맵 (사용자 제공 버전, 단방향)
# --------------------------------
DOOUM_MAP: Dict[str, Set[str]] = {
    "녀": {"여"},
    "녁": {"역"},
    "년": {"연"},
    "녈": {"열"},
    "념": {"염"},
    "녑": {"엽"},
    "녓": {"엿"},
    "녕": {"영"},
    "뇨": {"요"},
    "뇰": {"욜"},
    "뇽": {"용"},
    "뉴": {"유"},
    "뉵": {"육"},
    "늄": {"윰"},
    "늉": {"융"},
    "니": {"이"},
    "닉": {"익"},
    "닌": {"인"},
    "닐": {"일"},
    "님": {"임"},
    "닙": {"입"},
    "닛": {"잇"},
    "닝": {"잉"},
    "닢": {"잎"},
    "라": {"나"},
    "락": {"낙"},
    "란": {"난"},
    "랄": {"날"},
    "람": {"남"},
    "랍": {"납"},
    "랏": {"낫"},
    "랑": {"낭"},
    "랒": {"낮"},
    "래": {"내"},
    "랙": {"낵"},
    "랜": {"낸"},
    "램": {"냄"},
    "랩": {"냅"},
    "랫": {"냇"},
    "랭": {"냉"},
    "랴": {"야"},
    "략": {"약"},
    "랸": {"얀"},
    "량": {"양"},
    "려": {"여"},
    "력": {"역"},
    "련": {"연"},
    "렫": {"엳"},
    "렬": {"열"},
    "렴": {"염"},
    "렷": {"엿"},
    "령": {"영"},
    "례": {"예"},
    "롄": {"옌"},
    "로": {"노"},
    "록": {"녹"},
    "론": {"논"},
    "롤": {"놀"},
    "롬": {"놈"},
    "롭": {"놉"},
    "롯": {"놋"},
    "롱": {"농"},
    "뢰": {"뇌"},
    "료": {"요"},
    "룡": {"용"},
    "루": {"누"},
    "룩": {"눅"},
    "룬": {"눈"},
    "룸": {"눔"},
    "룹": {"눕"},
    "룻": {"눗"},
    "룽": {"눙"},
    "뤂": {"눞"},
    "류": {"유"},
    "륙": {"육"},
    "륜": {"윤"},
    "률": {"율"},
    "륭": {"융"},
    "르": {"느"},
    "륵": {"늑"},
    "른": {"는"},
    "를": {"늘"},
    "름": {"늠"},
    "릇": {"늣"},
    "릉": {"능"},
    "릎": {"늪"},
    "리": {"이"},
    "릭": {"익"},
    "린": {"인"},
    "릴": {"일"},
    "림": {"임"},
    "립": {"입"},
    "릿": {"잇"},
    "링": {"잉"},
}


def get_allowed_starts(required_char: str) -> Set[str]:
    allowed: Set[str] = {required_char}
    mapped = DOOUM_MAP.get(required_char)
    if mapped:
        allowed |= mapped
    return allowed


# --------------------------------
# 유메 호감도/LLM 헬퍼
# --------------------------------
AffectionTone = Literal["negative", "neutral", "positive"]


def _get_affection_score(bot: commands.Bot, player: discord.Member) -> float:
    """
    yume_core.get_affection(str(user_id)) 를 -100 ~ 100 정도의 스케일로 본다고 가정.
    없으면 0으로 처리.
    """
    core = getattr(bot, "yume_core", None)
    if core is None or not hasattr(core, "get_affection"):
        return 0.0

    try:
        return float(core.get_affection(str(player.id)))  # type: ignore[attr-defined]
    except Exception:
        return 0.0


def _affection_to_tone(score: float) -> AffectionTone:
    if score <= -40:
        return "negative"
    if score >= 40:
        return "positive"
    return "neutral"


def _get_bluewar_llm_client() -> Optional["AsyncOpenAI"]:  # type: ignore[name-defined]
    global _BLUEWAR_LLM_CLIENT
    if AsyncOpenAI is None:
        return None
    if OPENAI_API_KEY is None or not OPENAI_API_KEY.strip():
        return None
    if _BLUEWAR_LLM_CLIENT is None:
        try:
            _BLUEWAR_LLM_CLIENT = AsyncOpenAI(api_key=OPENAI_API_KEY)
        except Exception as e:  # pragma: no cover
            logger.warning("[BlueWar] AsyncOpenAI 초기화 실패: %s", e)
            _BLUEWAR_LLM_CLIENT = None
    return _BLUEWAR_LLM_CLIENT


async def _bluewar_say(
    *,
    bot: commands.Bot,
    kind: Literal["timeout", "too_short", "not_in_dict", "already_used", "wrong_start"],
    player: discord.Member,
    timeout: Optional[int] = None,
    word: Optional[str] = None,
    required_char: Optional[str] = None,
    allowed_starts: Optional[Set[str]] = None,
) -> str:
    """
    블루전 중 나오는 안내 멘트를 LLM 기반으로 생성.
    - kind: 어떤 상황인지
    - LLM 꺼져 있거나 실패하면 템플릿 fallback
    """
    nickname = player.display_name
    affection_score = _get_affection_score(bot, player)
    tone = _affection_to_tone(affection_score)

    # ---- 템플릿 fallback 먼저 정의 ----
    if kind == "timeout":
        if tone == "positive":
            fallback = (
                f"{nickname}, {timeout}초나 기다렸는데도 말이 없네…\n"
                f"이번 판은 시간 초과야. 다음엔 같이 더 오래 버텨보자, 으헤~"
            )
        elif tone == "negative":
            fallback = (
                f"{nickname}, {timeout}초 안에 한 단어도 못 내면 곤란해.\n"
                f"이번 판은 시간 초과 처리할게."
            )
        else:
            fallback = (
                f"{nickname} 이(가) {timeout}초 안에 대답하지 못했어. 시간 초과야."
            )
    elif kind == "too_short":
        if tone == "positive":
            fallback = (
                f"{nickname}, 한 글자는 너무 심심해. "
                f"두 글자 이상으로 멋지게 이어보자, 으헤~"
            )
        elif tone == "negative":
            fallback = (
                f"{nickname}, 규칙 기억 안 나? 한 글자는 안 돼. 최소 두 글자 이상이야."
            )
        else:
            fallback = "한 글자 단어는 안 돼. 두 글자 이상으로 해줘!"
    elif kind == "not_in_dict":
        w = word or "???"
        if tone == "positive":
            fallback = (
                f"**{w}**… 유메 사전에 아직 없는 단어야.\n"
                f"나중에 같이 넣어볼까? 지금은 다른 단어를 써줘, {nickname}."
            )
        elif tone == "negative":
            fallback = (
                f"**{w}** 는 등록도 안 된 단어야. 장난치지 말고, "
                f"제대로 된 단어를 내줘, {nickname}."
            )
        else:
            fallback = f"**{w}** 는 유메 단어 목록에 없는 단어야. 다른 걸 써봐!"
    elif kind == "already_used":
        w = word or "???"
        if tone == "positive":
            fallback = (
                f"**{w}** 는 아까 한 번 썼었어.\n"
                f"같은 단어 재탕은 금지니까, 이번엔 다른 거 생각해보자, {nickname}."
            )
        elif tone == "negative":
            fallback = (
                f"**{w}** 는 이미 나온 단어야. 제대로 기억하면서 해줘, {nickname}."
            )
        else:
            fallback = f"**{w}** 는 이미 나온 단어야. 새 걸로 도전해줘!"
    elif kind == "wrong_start":
        w = word or "???"
        starts = allowed_starts or set()
        if len(starts) <= 1 and required_char:
            base = f"**{w}** 는 `{required_char}`(으)로 시작 안 하잖아."
        else:
            if starts:
                starts_str = "/".join(sorted(starts))
            else:
                starts_str = required_char or "?"
            base = f"**{w}** 는 `{starts_str}` 중 하나로 시작해야 해."

        if tone == "positive":
            fallback = (
                base
                + f"\n조금만 더 신경 쓰면 완벽할 텐데… 다시 한 번 생각해볼래, {nickname}? 으헤~"
            )
        elif tone == "negative":
            fallback = base + f"\n규칙은 바뀌지 않아, {nickname}. 제대로 맞춰서 내줘."
        else:
            fallback = base
    else:
        fallback = "뭔가 이상한 상황이네… 다시 한 번 시도해볼까?"

    # ---- LLM 사용 불가하면 바로 fallback ----
    if not YUME_BLUEWAR_USE_LLM:
        return fallback

    client = _get_bluewar_llm_client()
    if client is None:
        return fallback

    # ---- LLM 프롬프트 구성 ----
    user_desc_parts = [
        f"kind={kind}",
        f"player_nickname={nickname}",
        f"affection_score={affection_score}",
        f"tone_hint={tone}",
    ]
    if timeout is not None:
        user_desc_parts.append(f"timeout={timeout}")
    if word is not None:
        user_desc_parts.append(f"word={word}")
    if required_char is not None:
        user_desc_parts.append(f"required_char={required_char}")
    if allowed_starts:
        user_desc_parts.append(f"allowed_starts={','.join(sorted(allowed_starts))}")

    user_content = (
        "지금 상황을 정리하면 다음과 같아:\n"
        + "\n".join(f"- {p}" for p in user_desc_parts)
        + "\n\n"
        "위 상황에서 유메가 디스코드 채팅으로 한두 문장 정도만 짧게 코멘트해 줘.\n"
        "조건:\n"
        "- 한국어로 말하기.\n"
        "- 말투는 유메답게 다정하고, 조금 능글맞고, 가끔 '으헤~'를 섞어도 좋아.\n"
        "- 너무 길게 설명하지 말고, 1~2문장으로 끝내기.\n"
        "- 규칙 설명이 필요하면 간단히만 짚어줘.\n"
        "- 상대를 부를 땐 가능하면 플레이어 닉네임을 그대로 사용해."
    )

    try:
        resp = await client.chat.completions.create(
            model=YUME_OPENAI_MODEL,
            messages=[
                {"role": "system", "content": YUME_SYSTEM_PROMPT},
                {
                    "role": "system",
                    "content": (
                        "지금 너는 '블루전'이라는 끝말잇기 게임을 진행하면서, "
                        "플레이어가 규칙을 어기거나, 잘못된 단어를 냈거나, "
                        "시간이 초과됐을 때 상황에 맞는 짧은 멘트를 해주는 중이야."
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            max_tokens=80,
            temperature=0.75,
            n=1,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return fallback
        return text
    except Exception as e:
        logger.warning("[BlueWar] LLM 멘트 생성 실패(kind=%s): %s", kind, e)
        return fallback


# --------------------------------
# 플레이어 입력 대기 (LLM 기반 대사)
# --------------------------------
async def wait_for_player_word(
    bot: commands.Bot,
    channel: discord.TextChannel,
    player: discord.Member,
    required_char: str,
    used_words: Set[str],
    timeout: int = TURN_TIMEOUT,
):
    def check(msg: discord.Message) -> bool:
        if msg.author.bot:
            return False
        if msg.channel.id != channel.id:
            return False
        if msg.author.id != player.id:
            return False
        return True

    deadline = time.monotonic() + timeout
    allowed_starts = get_allowed_starts(required_char)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg_text = await _bluewar_say(
                bot=bot,
                kind="timeout",
                player=player,
                timeout=timeout,
            )
            await channel.send(msg_text)
            return False, None, "timeout"

        try:
            msg: discord.Message = await bot.wait_for(
                "message", check=check, timeout=remaining
            )
        except asyncio.TimeoutError:
            msg_text = await _bluewar_say(
                bot=bot,
                kind="timeout",
                player=player,
                timeout=timeout,
            )
            await channel.send(msg_text)
            return False, None, "timeout"

        content = msg.content
        if not content:
            continue
        content = content.strip()
        if not content:
            continue

        lowered_no_space = content.replace(" ", "").lower()
        if lowered_no_space in ("!항복", "gg", "!gg"):
            await channel.send(f"🏳 **{player.display_name}** 이(가) 항복을 선언했어.")
            return False, None, "surrender"

        if content.startswith("!"):
            # 다른 명령어는 무시하고 다시 대기
            continue

        word = content

        # 1) 길이 체크
        if len(word) < 2:
            msg_text = await _bluewar_say(
                bot=bot,
                kind="too_short",
                player=player,
            )
            await channel.send(msg_text)
            continue

        # 2) 사전에 존재하는지
        if word not in WORDS_SET:
            msg_text = await _bluewar_say(
                bot=bot,
                kind="not_in_dict",
                player=player,
                word=word,
            )
            await channel.send(msg_text)
            continue

        # 3) 이미 사용된 단어인지
        if word in used_words:
            msg_text = await _bluewar_say(
                bot=bot,
                kind="already_used",
                player=player,
                word=word,
            )
            await channel.send(msg_text)
            continue

        # 4) 시작 글자 규칙 체크 (두음 허용)
        if word[0] not in allowed_starts:
            msg_text = await _bluewar_say(
                bot=bot,
                kind="wrong_start",
                player=player,
                word=word,
                required_char=required_char,
                allowed_starts=allowed_starts,
            )
            await channel.send(msg_text)
            continue

        # 통과
        return True, word, None


# =====================================================
#                  메인 Cog 클래스
# =====================================================
class BlueWarCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.active_channels: Set[int] = set()
        self.join_sessions: Dict[int, Dict[str, Any]] = {}
        self.records: Dict[int, Dict[str, Any]] = defaultdict(lambda: {"win": 0, "loss": 0})
        self._load_records_from_file()

        self.game_counter: int = 0
        self.rank_message_id: Optional[int] = None

        self.suggestion_words: List[str] = []
        self._load_suggestions()

        self.core = getattr(bot, "yume_core", None)

        self.llm_client: Optional[AsyncOpenAI] = None
        if AsyncOpenAI is not None and OPENAI_API_KEY and YUME_BLUEWAR_USE_LLM:
            try:
                self.llm_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
            except Exception:
                self.llm_client = None

        self.char_index: Dict[str, int] = {}
        self.index_char: List[str] = []
        self.edge_base: List[List[int]] = []
        self.edge_words: Dict[Tuple[int, int], List[str]] = {}
        self.word_to_pair: Dict[str, Tuple[int, int]] = {}
        self._load_or_build_word_graph()

        self.api_base: Optional[str] = os.getenv("YUME_WEB_API_BASE")
        self.api_token: Optional[str] = os.getenv("YUME_WEB_API_TOKEN")

    # -----------------------
    # 데이터 / 그래프 초기화
    # -----------------------
    def _load_suggestions(self):
        try:
            with open(SUGGESTION_FILE, "r", encoding="utf-8") as f:
                words = [line.strip() for line in f if line.strip()]
            self.suggestion_words = words
        except Exception:
            self.suggestion_words = []

    def _choose_start_word(self) -> str:
        if self.suggestion_words:
            return random.choice(self.suggestion_words)
        if WORDS_SET:
            return random.choice(list(WORDS_SET))
        return "블루아카이브"

    def _load_records_from_file(self):
        raw = load_records()
        for key, rec in raw.items():
            try:
                uid = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            win = int(rec.get("win", 0))
            loss = int(rec.get("loss", 0))
            name = rec.get("name")
            self.records[uid]["win"] = win
            self.records[uid]["loss"] = loss
            if isinstance(name, str) and name.strip():
                self.records[uid]["name"] = name.strip()

    def _save_all_records(self):
        data: Dict[str, Dict[str, Any]] = {}
        for uid, rec in self.records.items():
            entry: Dict[str, Any] = {
                "win": int(rec.get("win", 0)),
                "loss": int(rec.get("loss", 0)),
            }
            name = rec.get("name")
            if isinstance(name, str) and name.strip():
                entry["name"] = name.strip()
            data[str(uid)] = entry
        save_records(data)

    def _update_record(self, winner: discord.Member, loser: discord.Member):
        self.records[winner.id]["win"] += 1
        self.records[loser.id]["loss"] += 1
        self._save_all_records()

    def _get_stats(self, user_id: int):
        rec = self.records.get(user_id, {"win": 0, "loss": 0})
        w = int(rec.get("win", 0))
        l = int(rec.get("loss", 0))
        total = w + l
        rate = (w / total * 100) if total > 0 else 0.0
        diff = w - l
        return w, l, rate, diff

    # -----------------------
    # 단어 그래프 + 캐시
    # -----------------------
    def _build_word_graph_from_words(self):
        chars: Set[str] = set()
        pairs: List[Tuple[str, str, str]] = []

        for w in WORDS_SET:
            if len(w) < 2:
                continue
            s = w[0]
            e = w[-1]
            chars.add(s)
            chars.add(e)
            pairs.append((s, e, w))

        self.index_char = sorted(chars)
        self.char_index = {ch: idx for idx, ch in enumerate(self.index_char)}
        n = len(self.index_char)
        self.edge_base = [[0 for _ in range(n)] for _ in range(n)]
        self.edge_words = {}
        self.word_to_pair = {}

        for s, e, w in pairs:
            si = self.char_index[s]
            ei = self.char_index[e]
            self.edge_base[si][ei] += 1
            self.edge_words.setdefault((si, ei), []).append(w)
            self.word_to_pair[w] = (si, ei)

        logger.info(
            "[BlueWar] 그래프 빌드 완료: chars=%d, words=%d", len(self.index_char), len(pairs)
        )

    def _save_word_graph_cache(self):
        try:
            os.makedirs(os.path.dirname(GRAPH_CACHE_FILE), exist_ok=True)
            data = {
                "char_index": self.char_index,
                "index_char": self.index_char,
                "edge_base": self.edge_base,
                "edge_words": self.edge_words,
                "word_to_pair": self.word_to_pair,
            }
            with open(GRAPH_CACHE_FILE, "wb") as f:
                pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
            logger.info("[BlueWar] 그래프 캐시 저장: %s", GRAPH_CACHE_FILE)
        except Exception as e:
            logger.warning("[BlueWar] 그래프 캐시 저장 실패: %s", e)

    def _load_or_build_word_graph(self):
        try:
            with open(GRAPH_CACHE_FILE, "rb") as f:
                data = pickle.load(f)
            self.char_index = data["char_index"]
            self.index_char = data["index_char"]
            self.edge_base = data["edge_base"]
            self.edge_words = data["edge_words"]
            self.word_to_pair = data["word_to_pair"]
            logger.info("[BlueWar] 그래프 캐시 로드: %s", GRAPH_CACHE_FILE)
            return
        except FileNotFoundError:
            logger.info("[BlueWar] 그래프 캐시 없음. 새로 생성합니다.")
        except Exception as e:
            logger.warning("[BlueWar] 그래프 캐시 로드 실패(%s). 새로 생성합니다.", e)

        self._build_word_graph_from_words()
        self._save_word_graph_cache()

    # -----------------------
    # 감정 시스템 연동
    # -----------------------
    def _get_core_state(self):
        core = self.core
        if core is None:
            return {}
        try:
            return core.get_core_state()
        except Exception:
            return {}

    def _get_mood_level(self) -> float:
        core_state = self._get_core_state()
        try:
            return float(core_state.get("mood", 0.0))
        except Exception:
            return 0.0

    def _mood_suffix_on_win(self) -> str:
        mood = self._get_mood_level()
        if mood >= 0.3:
            return " 으헤~ 이런 건 기본이지."
        if mood <= -0.3:
            return " …오늘 컨디션 별로인데도 겨우 이겼네."
        return ""

    def _mood_suffix_on_lose(self) -> str:
        mood = self._get_mood_level()
        if mood >= 0.3:
            return " 뭐, 가끔은 져주는 쪽이 재미있을 때도 있거든?"
        if mood <= -0.3:
            return " 아, 또 졌네… 오늘은 진짜 컨디션 조절 안 된다."
        return ""

    def _note_event(
        self,
        event: str,
        *,
        user: Optional[discord.Member] = None,
        guild: Optional[discord.Guild] = None,
        weight: float = 1.0,
    ) -> None:
        if self.core is None:
            return
        try:
            uid = str(user.id) if user is not None else None
            gid = str(guild.id) if guild is not None else None
            self.core.apply_event(event, user_id=uid, guild_id=gid, weight=weight)
        except Exception:
            pass

    # -----------------------
    # 랭킹 / 로그
    # -----------------------
    def _build_rank_text_for_guild(self, guild: Optional[discord.Guild]) -> str:
        if guild is None:
            return "이건 서버에서만 쓸 수 있어."

        entries = []
        for uid, rec in self.records.items():
            w, l, rate, diff = self._get_stats(uid)
            member = guild.get_member(uid)
            if member is not None:
                display_name = member.display_name
            else:
                name = rec.get("name")
                display_name = name if isinstance(name, str) and name.strip() else f"ID {uid}"
            entries.append((display_name, w, l, rate, diff))

        if not entries:
            return "아직 블루전 기록이 하나도 없어. 첫 승자는 누가 될까?"

        entries.sort(key=lambda x: (x[4], x[1], x[1] + x[2]), reverse=True)

        mood = self._get_mood_level()
        if mood >= 0.3:
            header = "랭킹 정리해 뒀어. 위에 있는 이름들, 왠지 자꾸 눈에 들어오지 않아?"
        elif mood <= -0.3:
            header = "컨디션은 별로지만… 랭킹 정리 정도는 학생회장이 해줘야지."
        else:
            header = "현재 블루전 랭킹은 이 정도야."

        lines = [header, ""]
        for idx, (name, w, l, rate, diff) in enumerate(entries, start=1):
            if idx == 1:
                prefix = "🥇 "
            elif idx == 2:
                prefix = "🥈 "
            elif idx == 3:
                prefix = "🥉 "
            else:
                prefix = f"{idx:2d}. "
            lines.append(
                f"{prefix}{name} - {w}승 {l}패 (승차 {diff}, 승률 {rate:.1f}%)"
            )

        return "\n".join(lines)

    async def _update_rank_message(self, guild: Optional[discord.Guild]):
        if guild is None or not RANK_CHANNEL_ID:
            return

        channel = self.bot.get_channel(RANK_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        text = self._build_rank_text_for_guild(guild)

        if self.rank_message_id is None:
            msg = await channel.send(text)
            self.rank_message_id = msg.id
        else:
            try:
                msg = await channel.fetch_message(self.rank_message_id)
                await msg.edit(content=text)
            except discord.NotFound:
                msg = await channel.send(text)
                self.rank_message_id = msg.id

    # --- 복기 로그 문자열 ---
    def _build_review_log_text(self, word_history: List[str]) -> str:
        return " → ".join(word_history) if word_history else "(기록 없음)"

    async def _post_game_logs(
        self,
        guild: Optional[discord.Guild],
        channel: discord.TextChannel,
        players,
        winner: discord.Member,
        loser: discord.Member,
        word_history,
        game_no: int,
    ):
        p1, p2 = players
        history_text = self._build_review_log_text(word_history)

        if REVIEW_CHANNEL_ID:
            log_channel = self.bot.get_channel(REVIEW_CHANNEL_ID)
            if isinstance(log_channel, discord.TextChannel):
                embed = discord.Embed(
                    title=f"🔵 블루전 GAME No.{game_no:02d} 복기 로그",
                    description=f"{p1.display_name} vs {p2.display_name}",
                    color=discord.Color.blue(),
                )
                embed.add_field(
                    name="승자 / 패자",
                    value=f"승 : **{winner.display_name}**\n패 : **{loser.display_name}**",
                    inline=False,
                )
                embed.add_field(
                    name="단어 흐름",
                    value=history_text,
                    inline=False,
                )
                if guild:
                    embed.set_footer(text=f"서버: {guild.name} / 채널: #{channel.name}")
                await log_channel.send(embed=embed)

        if RESULT_CHANNEL_ID:
            res_channel = self.bot.get_channel(RESULT_CHANNEL_ID)
            if isinstance(res_channel, discord.TextChannel):
                await res_channel.send(
                    f"🔵 **블루전 결과 보고**\n"
                    f"- 서버: {guild.name if guild else 'DM / 알 수 없음'}\n"
                    f"- 채널: {channel.mention}\n"
                    f"- 승 : **{winner.display_name}**\n"
                    f"- 패 : **{loser.display_name}**\n"
                    f"- 진행 단어 수 : {len(word_history)}"
                )

    # -----------------------
    # 관리자 웹으로 전적 전송
    # -----------------------
    async def _post_match_to_admin(self, payload: Dict[str, Any]) -> None:
        if not self.api_base or not self.api_token:
            return

        url = self.api_base.rstrip("/") + "/bluewar/matches"
        headers = {"X-API-Token": self.api_token}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers, timeout=10
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning(
                            "[BlueWar] 전적 전송 실패 (%s): status=%s body=%s",
                            url,
                            resp.status,
                            text[:500],
                        )
        except Exception as e:
            logger.warning("[BlueWar] 전적 전송 중 예외 발생: %s", e)

    async def _report_pvp_result_to_admin(
        self,
        *,
        game_no: int,
        p1: discord.Member,
        p2: discord.Member,
        winner: discord.Member,
        loser: discord.Member,
        word_history: List[str],
        start_time: datetime,
        end_time: datetime,
        end_reason: str,
    ) -> None:
        total_rounds = len(word_history)
        review_log = self._build_review_log_text(word_history)

        payload: Dict[str, Any] = {
            "mode": "pvp",
            "status": "finished",
            "starter_discord_id": str(p1.id),
            "winner_discord_id": str(winner.id),
            "loser_discord_id": str(loser.id),
            "win_gap": None,
            "total_rounds": total_rounds,
            "started_at": start_time.isoformat(),
            "finished_at": end_time.isoformat(),
            "note": f"game_no={game_no}, reason={end_reason}",
            "review_log": review_log,
            "participants": [
                {
                    "discord_id": str(p1.id),
                    "name": p1.display_name,
                    "ai_name": None,
                    "side": 1,
                    "is_winner": winner.id == p1.id,
                    "score": None,
                    "turns": None,
                },
                {
                    "discord_id": str(p2.id),
                    "name": p2.display_name,
                    "ai_name": None,
                    "side": 2,
                    "is_winner": winner.id == p2.id,
                    "score": None,
                    "turns": None,
                },
            ],
        }

        await self._post_match_to_admin(payload)

    async def _report_practice_result_to_admin(
        self,
        *,
        user: discord.Member,
        user_is_winner: bool,
        word_history: List[str],
        start_time: datetime,
        end_time: datetime,
        reason: str,
    ) -> None:
        total_rounds = len(word_history)
        review_log = self._build_review_log_text(word_history)

        winner_discord_id: Optional[str]
        loser_discord_id: Optional[str]

        if user_is_winner:
            winner_discord_id = str(user.id)
            loser_discord_id = None
        else:
            winner_discord_id = None
            loser_discord_id = str(user.id)

        payload: Dict[str, Any] = {
            "mode": "practice",
            "status": "finished",
            "starter_discord_id": str(user.id),
            "winner_discord_id": winner_discord_id,
            "loser_discord_id": loser_discord_id,
            "win_gap": None,
            "total_rounds": total_rounds,
            "started_at": start_time.isoformat(),
            "finished_at": end_time.isoformat(),
            "note": f"practice, reason={reason}",
            "review_log": review_log,
            "participants": [
                {
                    "discord_id": str(user.id),
                    "name": user.display_name,
                    "ai_name": None,
                    "side": 1,
                    "is_winner": user_is_winner,
                    "score": None,
                    "turns": None,
                },
                {
                    "discord_id": None,
                    "name": None,
                    "ai_name": "유메",
                    "side": 2,
                    "is_winner": not user_is_winner,
                    "score": None,
                    "turns": None,
                },
            ],
        }

        await self._post_match_to_admin(payload)

    # -----------------------
    # AI 단어 선택 유틸 (연습 모드)
    # -----------------------
    def _find_candidate_words(self, required_char: str, used_words: Set[str]) -> List[str]:
        candidates: List[str] = []
        for ch in get_allowed_starts(required_char):
            for w in WORDS_BY_FIRST.get(ch, []):
                if len(w) >= 2 and w not in used_words:
                    candidates.append(w)
        return candidates

    def _choose_ai_word(self, required_char: str, used_words: Set[str]) -> Optional[str]:
        """
        간단한 전략:
        - 우선, 이 단어를 쓰면 상대가 바로 막히는 수(존재하는 후속 단어 없음)를 노린다.
        - 그다음에는 가능한 한 짧은 단어 위주로 고른다.
        """
        candidates = self._find_candidate_words(required_char, used_words)
        if not candidates:
            return None

        win_moves: List[str] = []
        neutral_moves: List[str] = []
        losing_moves: List[str] = []

        for w in candidates:
            end_ch = w[-1]
            # 이 단어를 사용한 후, 상대가 이어갈 수 있는 단어가 없다면 '즉시 승리 수'
            if not exists_follow_word(end_ch, used_words | {w}):
                win_moves.append(w)
            elif len(w) <= 3:
                neutral_moves.append(w)
            else:
                losing_moves.append(w)

        if win_moves:
            return random.choice(win_moves)
        if neutral_moves:
            return random.choice(neutral_moves)
        return random.choice(losing_moves or candidates)

    async def _speak_practice_result(
        self,
        user: discord.Member,
        user_is_winner: bool,
        word_history: List[str],
    ) -> str:
        """
        연습 모드 게임이 끝난 뒤 짧은 코멘트를 LLM으로 생성.
        실패하거나 비활성화면 템플릿 사용.
        """
        nickname = user.display_name
        history_text = self._build_review_log_text(word_history[-20:])  # 너무 길면 잘라내기

        base_win = (
            f"{nickname}, 이번 판은 네 승리야. 단어 고르는 센스가 꽤 괜찮은데?"
            + self._mood_suffix_on_win()
        )
        base_lose = (
            f"이번에는 유메가 이겼네. {nickname}, 아쉽다면 다음 판에서 복수해볼래?"
            + self._mood_suffix_on_lose()
        )
        fallback = base_win if user_is_winner else base_lose

        if not YUME_BLUEWAR_USE_LLM or self.llm_client is None:
            return fallback

        result_str = "user_win" if user_is_winner else "yume_win"

        user_message = (
            "지금까지 플레이한 블루전 연습 모드 게임 결과야.\n"
            "이 정보를 바탕으로, 연습을 함께한 후배에게 1~3문장 정도로 짧은 코멘트를 해줘.\n"
            "조건:\n"
            "- 한국어로 말하기.\n"
            "- 말투는 유메답게 다정하고, 살짝 능글맞고, 가끔 '으헤~'를 섞어도 좋아.\n"
            "- 결과에 대한 소감과, 가벼운 응원이나 도발 한마디 정도를 섞어줘.\n\n"
            f"[플레이어 닉네임] {nickname}\n"
            f"[게임 결과] {result_str}\n"
            f"[단어 흐름 예시] {history_text}\n"
        )

        try:
            resp = await self.llm_client.chat.completions.create(
                model=YUME_OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": YUME_SYSTEM_PROMPT},
                    {
                        "role": "system",
                        "content": (
                            "지금 너는 '블루전' 연습 모드를 후배와 함께 플레이한 뒤에, "
                            "결과에 대한 짧은 소감을 말해 주는 상황이야."
                        ),
                    },
                    {"role": "user", "content": user_message},
                ],
                max_tokens=120,
                temperature=0.8,
                n=1,
            )
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                return fallback
            return text
        except Exception as e:
            logger.warning("[BlueWar] 연습 모드 결과 멘트 생성 실패: %s", e)
            return fallback

    # -----------------------
    # 실제 게임 루프: PVP
    # -----------------------
    async def _run_pvp_game(
        self,
        channel: discord.TextChannel,
        p1: discord.Member,
        p2: discord.Member,
        game_no: int,
    ):
        guild = channel.guild
        start_word = self._choose_start_word()
        used_words: Set[str] = {start_word}
        word_history: List[str] = [start_word]

        await channel.send(
            f"🔵 블루전 GAME No.{game_no:02d} 시작할게.\n"
            f"시작 단어는 **{start_word}** 이고,\n"
            f"먼저 공격하는 사람은 **{p1.display_name}**, 이어서 **{p2.display_name}** 순서야."
        )

        players = [p1, p2]
        current_word = start_word
        turn_index = 0  # 0 -> p1, 1 -> p2
        winner: Optional[discord.Member] = None
        loser: Optional[discord.Member] = None
        end_reason: str = "unknown"

        start_time = datetime.now(timezone.utc)

        while True:
            player = players[turn_index]
            required_char = current_word[-1]

            # 먼저, 이 플레이어가 이 글자로 시작하는 단어를 낼 수 있는지 확인
            if not exists_follow_word(required_char, used_words):
                other = players[1 - turn_index]
                winner = other
                loser = player
                end_reason = "no_move"
                await channel.send(
                    f"더 이상 `{required_char}`(으)로 이어지는 단어가 없어.\n"
                    f"**{player.display_name}** 쪽이 막혔으니까, "
                    f"이번 판 승리는 **{other.display_name}**에게로 갈게."
                )
                break

            ok, word, reason = await wait_for_player_word(
                self.bot, channel, player, required_char, used_words
            )
            if not ok:
                other = players[1 - turn_index]
                winner = other
                loser = player
                end_reason = reason or "fail"
                # timeout / surrender 멘트는 wait_for_player_word 쪽에서 이미 출력됨
                break

            current_word = word
            used_words.add(word)
            word_history.append(word)
            turn_index = 1 - turn_index

        end_time = datetime.now(timezone.utc)

        if winner and loser:
            self._update_record(winner, loser)
            self._note_event("bluewar_win", user=winner, guild=guild, weight=1.5)
            self._note_event("bluewar_lose", user=loser, guild=guild, weight=1.0)

            result_msg = (
                f"🔵 블루전 GAME No.{game_no:02d} 종료!\n"
                f"승리: **{winner.display_name}**, 패배: **{loser.display_name}**."
                f"{self._mood_suffix_on_win()}"
            )
            await channel.send(result_msg)

            await self._post_game_logs(guild, channel, (p1, p2), winner, loser, word_history, game_no)
            await self._update_rank_message(guild)
            try:
                await self._report_pvp_result_to_admin(
                    game_no=game_no,
                    p1=p1,
                    p2=p2,
                    winner=winner,
                    loser=loser,
                    word_history=word_history,
                    start_time=start_time,
                    end_time=end_time,
                    end_reason=end_reason,
                )
            except Exception as e:
                logger.warning("[BlueWar] PVP 전적 보고 중 예외: %s", e)

    # -----------------------
    # 실제 게임 루프: 연습 모드 (user vs 유메)
    # -----------------------
    async def _run_practice_game(
        self,
        ctx: commands.Context,
        user: discord.Member,
        game_no: int,
    ):
        channel = ctx.channel
        guild = ctx.guild
        start_word = self._choose_start_word()
        used_words: Set[str] = {start_word}
        word_history: List[str] = [start_word]

        await channel.send(
            f"🔵 블루전 연습 GAME No.{game_no:02d} 시작이야.\n"
            f"시작 단어는 **{start_word}**.\n"
            f"먼저 공격하는 사람은 **{user.display_name}**, 그 다음은 유메 차례야."
        )

        current_word = start_word
        user_turn = True
        user_is_winner: bool = False
        end_reason: str = "unknown"

        start_time = datetime.now(timezone.utc)

        while True:
            if user_turn:
                required_char = current_word[-1]

                if not exists_follow_word(required_char, used_words):
                    # 유저가 아무 단어도 낼 수 없음 → 유메 승
                    user_is_winner = False
                    end_reason = "no_move_user"
                    await channel.send(
                        f"`{required_char}`(으)로 더 이상 이어지는 단어가 없네.\n"
                        f"이번 판은 유메의 승리야. 다음엔 더 어려운 단어로 막아보자, 으헤~"
                    )
                    break

                ok, word, reason = await wait_for_player_word(
                    self.bot, channel, user, required_char, used_words
                )
                if not ok:
                    user_is_winner = False
                    end_reason = reason or "user_fail"
                    if reason == "surrender":
                        await channel.send(
                            f"**{user.display_name}** 이(가) 항복했으니까, "
                            "이번 연습은 여기서 끝낼게."
                        )
                    # timeout 멘트는 위에서 이미 출력됨
                    break

                current_word = word
                used_words.add(word)
                word_history.append(word)
                user_turn = False
            else:
                required_char = current_word[-1]
                ai_word = self._choose_ai_word(required_char, used_words)
                if not ai_word:
                    # 유메가 낼 단어가 없음 → 유저 승리
                    user_is_winner = True
                    end_reason = "no_move_ai"
                    await channel.send(
                        "으으… 이어지는 단어가 더 이상 떠오르지 않아.\n"
                        f"이번 판은 **{user.display_name}** 의 승리야. 잘했어, 으헤~"
                    )
                    break

                await channel.send(f"유메: **{ai_word}**")
                current_word = ai_word
                used_words.add(ai_word)
                word_history.append(ai_word)
                user_turn = True

        end_time = datetime.now(timezone.utc)

        # 결과 코멘트 (LLM)
        try:
            comment = await self._speak_practice_result(user, user_is_winner, word_history)
            await channel.send(comment)
        except Exception as e:
            logger.warning("[BlueWar] 연습 모드 결과 코멘트 중 예외: %s", e)

        # 관리자 웹 전송
        try:
            await self._report_practice_result_to_admin(
                user=user,
                user_is_winner=user_is_winner,
                word_history=word_history,
                start_time=start_time,
                end_time=end_time,
                reason=end_reason,
            )
        except Exception as e:
            logger.warning("[BlueWar] 연습 모드 전적 보고 중 예외: %s", e)

        # 감정 엔진 이벤트
        if guild is not None:
            if user_is_winner:
                self._note_event("bluewar_practice_win", user=user, guild=guild, weight=1.2)
            else:
                self._note_event("bluewar_practice_lose", user=user, guild=guild, weight=0.8)

    # -----------------------
    # 커맨드: 블루전 시작 / 연습 / 전적 / 랭킹
    # -----------------------
    @commands.command(name="블루전시작", help="1:1 블루전 대결을 시작합니다.")
    async def cmd_bluewar_start(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("이건 서버에서만 할 수 있어. DM에서는 블루전 못 열어.", delete_after=5)
            return

        channel = ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await ctx.send("텍스트 채널에서만 블루전을 열 수 있어.", delete_after=5)
            return

        if channel.id in self.active_channels or channel.id in self.join_sessions:
            await ctx.send("이미 이 채널에서 블루전이 진행 중이거나 모집 중이야.", delete_after=5)
            return

        self.join_sessions[channel.id] = {"host_id": ctx.author.id}

        embed = discord.Embed(
            title="🔵 블루전 참가자 모집",
            description=(
                f"{ctx.author.display_name} 이(가) 블루전 1:1 대결을 신청했어.\n"
                "아래 버튼을 눌러 참가해 줘. 선착순 1명!"
            ),
            color=discord.Color.blue(),
        )
        view = BlueWarJoinView(self, channel, ctx.author)
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg

    @commands.command(name="블루전연습", help="유메와 1:1 블루전 연습을 합니다.")
    async def cmd_bluewar_practice(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("이건 서버에서만 할 수 있어. DM에서는 블루전 못 열어.", delete_after=5)
            return

        channel = ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await ctx.send("텍스트 채널에서만 블루전 연습을 할 수 있어.", delete_after=5)
            return

        if channel.id in self.active_channels:
            await ctx.send("이미 이 채널에서 블루전이 진행 중이야.", delete_after=5)
            return

        # 게임 번호 증가 및 등록
        self.game_counter += 1
        game_no = self.game_counter
        self.active_channels.add(channel.id)

        try:
            await self._run_practice_game(ctx, ctx.author, game_no)
        finally:
            self.active_channels.discard(channel.id)

    @commands.command(name="블루전전적", help="블루전 전적을 확인합니다.")
    async def cmd_bluewar_stats(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
    ):
        if ctx.guild is None:
            await ctx.send("이건 서버 안에서만 쓸 수 있어.", delete_after=5)
            return

        target = member or ctx.author
        w, l, rate, diff = self._get_stats(target.id)

        embed = discord.Embed(
            title=f"🔵 {target.display_name} 의 블루전 전적",
            color=discord.Color.blue(),
        )
        embed.add_field(name="승", value=str(w))
        embed.add_field(name="패", value=str(l))
        embed.add_field(name="승률", value=f"{rate:.1f}%")
        embed.add_field(name="승차(승-패)", value=str(diff))

        await ctx.send(embed=embed)

    @commands.command(name="블루전랭킹", help="서버의 블루전 랭킹을 보여줍니다.")
    async def cmd_bluewar_rank(self, ctx: commands.Context):
        guild = ctx.guild
        if guild is None:
            await ctx.send("이건 서버 안에서만 쓸 수 있어.", delete_after=5)
            return

        text = self._build_rank_text_for_guild(guild)
        await ctx.send(f"```{text}```")
        await self._update_rank_message(guild)



# -----------------------
# 참가 View
# -----------------------
class BlueWarJoinView(discord.ui.View):
    def __init__(
        self,
        cog: "BlueWarCog",
        channel: discord.TextChannel,
        host: discord.Member,
    ):
        super().__init__(timeout=60)
        self.cog = cog
        self.channel = channel
        self.host = host
        self.players: List[discord.Member] = [host]
        self.message: Optional[discord.Message] = None

    async def on_timeout(self) -> None:
        self.cog.join_sessions.pop(self.channel.id, None)
        if self.message:
            try:
                await self.message.edit(
                    content="⏰ 블루전 참가 모집 시간이 끝났어.",
                    view=None,
                )
            except Exception:
                pass

    @discord.ui.button(label="참가", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.channel_id != self.channel.id:
            await interaction.response.send_message(
                "이 버튼은 다른 채널 블루전용이야.",
                ephemeral=True,
            )
            return

        user = interaction.user
        if not isinstance(user, discord.Member):
            await interaction.response.send_message(
                "서버 멤버만 참가할 수 있어.",
                ephemeral=True,
            )
            return

        if user in self.players:
            await interaction.response.send_message(
                "이미 참가 신청한 상태야.",
                ephemeral=True,
            )
            return

        if len(self.players) >= 2:
            await interaction.response.send_message(
                "이미 두 명이 다 모였어.",
                ephemeral=True,
            )
            return

        self.players.append(user)

        # 임베드 갱신
        if self.message and self.message.embeds:
            embed = self.message.embeds[0]
            desc = (
                f"{self.host.display_name} 이(가) 블루전 1:1 대결을 신청했어.\n"
                f"현재 참가자:\n"
                f"- {self.host.display_name}\n"
                f"- {user.display_name}\n\n"
                "곧 게임을 시작할게."
            )
            embed.description = desc
            try:
                await self.message.edit(embed=embed, view=None)
            except Exception:
                pass

        self.cog.join_sessions.pop(self.channel.id, None)

        # 바로 게임 시작 (호스트가 선공)
        self.cog.game_counter += 1
        game_no = self.cog.game_counter
        self.cog.active_channels.add(self.channel.id)

        await interaction.response.send_message(
            f"블루전 GAME No.{game_no:02d} 을 시작할게. 채널을 봐줘!",
            ephemeral=True,
        )

        async def runner():
            try:
                await self.cog._run_pvp_game(self.channel, self.host, user, game_no)
            finally:
                self.cog.active_channels.discard(self.channel.id)

        self.cog.bot.loop.create_task(runner())

    @discord.ui.button(label="모집 취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            await interaction.response.send_message(
                "모집을 취소할 수 있는 건 방장뿐이야.",
                ephemeral=True,
            )
            return

        self.cog.join_sessions.pop(self.channel.id, None)
        if self.message:
            try:
                await self.message.edit(
                    content="블루전 모집이 취소됐어.",
                    view=None,
                    embed=None,
                )
            except Exception:
                pass

        await interaction.response.send_message(
            "모집을 취소해 뒀어.",
            ephemeral=True,
        )
        self.stop()


# =============================
# setup
# =============================
async def setup(bot: commands.Bot):
    await bot.add_cog(BlueWarCog(bot))
