from __future__ import annotations

import asyncio
import ast
import logging
import json
import hashlib
import os
import urllib.request
import urllib.error
import pickle
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Callable

import discord
from discord.ext import commands

from words_core import load_words_from_file, pick_words_source
from bluewar_wordlists import sync_wordlists, load_local_meta
from config import BLUEWAR_CACHED_SUGGESTION_FILE, WORDS_FILE
from records_core import ensure_records, load_records, save_records, add_match_record, calc_rankings

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

SUGGESTION_FILE = os.path.join(BASE_DIR, "data", "dictionary", "suggestion.txt")
DOOUM_RULES_FILE = os.path.join(BASE_DIR, "data", "dictionary", "dooum_rules.txt")
GRAPH_CACHE_FILE = os.path.join(BASE_DIR, "data", "system", "bluewar_graph.pkl")

PRACTICE_TURN_TIMEOUT = 90.0
PVP_TURN_TIMEOUT = 90.0
AI_SEARCH_DEPTH = 10
AI_SEARCH_TIME_LIMIT = 60.0

# Phase 5: 웹(어드민) 단어리스트 캐시/폴백을 통해 로드된다.
# 이 모듈의 여러 함수가 참조하므로, 최신 값으로 재할당한다.
WORDS_SET: Set[str] = set()
WORDS_BY_FIRST: Dict[str, List[str]] = defaultdict(list)

try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
YUME_OPENAI_MODEL = os.getenv("YUME_OPENAI_MODEL") or "gpt-4o-mini"
YUME_BLUEWAR_USE_LLM = os.getenv("YUME_BLUEWAR_USE_LLM", "1").lower() in ("1", "true", "yes", "y", "on")

YUME_SYSTEM_PROMPT = """너는 디스코드 봇 '유메'의 말투로 말한다.
유메는 블루 아카이브의 쿠치나시 유메 모티브의 학생회장 '선배'다.
기본은 다정하고 마이페이스지만 할 일은 다 처리한다.
상대는 '후배'지만 가능하면 닉네임(표시 이름)으로 부른다.
장난스럽거나 민망할 때 '으헤~'를 섞는다.
너무 과장되게 귀엽지 말고 자연스럽게.
"""

DEFAULT_DOOUM_MAP: Dict[str, Set[str]] = {
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
    "랫": {"낫"},
    "량": {"양"},
    "략": {"약"},
    "려": {"여"},
    "력": {"역"},
    "련": {"연"},
    "렬": {"열"},
    "렴": {"염"},
    "렵": {"엽"},
    "렷": {"엿"},
    "령": {"영"},
    "로": {"노"},
    "록": {"녹"},
    "론": {"논"},
    "롤": {"놀"},
    "롬": {"놈"},
    "롭": {"놉"},
    "롯": {"놋"},
    "료": {"요"},
    "룡": {"용"},
    "루": {"누"},
    "륙": {"육"},
    "륜": {"윤"},
    "률": {"율"},
    "륭": {"융"},
    "를": {"늘"},
    "리": {"이"},
    "린": {"인"},
    "림": {"임"},
    "립": {"입"},
    "릿": {"잇"},
    "링": {"잉"},
}

def _normalize_word(w: str) -> str:
    return (w or "").strip()

def _first_char(word: str) -> str:
    return word[0] if word else ""

def _last_char(word: str) -> str:
    return word[-1] if word else ""

def post_bluewar_match_to_admin(payload: Dict[str, Any]) -> bool:
    """
    Yume Admin 웹 패널로 블루전 매치 전적을 전송한다.

    - URL: {YUME_ADMIN_URL}/bluewar/matches
    - Header: X-API-Token: {YUME_ADMIN_API_TOKEN}

    성공하면 True, 실패하면 False.
    """
    base_url = (os.getenv("YUME_ADMIN_URL") or "").rstrip("/")
    token = (os.getenv("YUME_ADMIN_API_TOKEN") or "").strip()

    if not base_url:
        return False

    try:
        send_payload = json.loads(json.dumps(payload, ensure_ascii=False))
    except Exception:
        send_payload = dict(payload)

    try:
        parts = send_payload.get("participants") or []
        fixed_parts = []
        for i, p in enumerate(parts):
            if not isinstance(p, dict):
                continue
            pp = dict(p)
            side = pp.get("side")
            if isinstance(side, str):
                s = side.lower().strip()
                if s in ("user", "human", "player", "p1"):
                    pp["side"] = 0
                elif s in ("ai", "bot", "yume", "p2"):
                    pp["side"] = 1
                else:
                    pp["side"] = i
            elif side is None:
                pp["side"] = i
            fixed_parts.append(pp)
        send_payload["participants"] = fixed_parts
    except Exception:
        pass

    url = f"{base_url}/bluewar/matches"
    data = json.dumps(send_payload, ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "yumebot-bluewar/1.0",
    }
    if token:
        headers["X-API-Token"] = token

    req = urllib.request.Request(url=url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= int(getattr(resp, "status", 0) or 0) < 300
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        logger.warning("[BlueWar->Admin] HTTPError %s: %s", getattr(e, "code", "?"), body[:500])
        return False
    except Exception as e:
        logger.warning("[BlueWar->Admin] Error: %s", e)
        return False

def _pick_suggestion_source(*, prefer_cache: bool = True) -> str:
    if prefer_cache and os.path.exists(BLUEWAR_CACHED_SUGGESTION_FILE):
        try:
            if os.path.getsize(BLUEWAR_CACHED_SUGGESTION_FILE) > 0:
                return BLUEWAR_CACHED_SUGGESTION_FILE
        except Exception:
            pass
    return SUGGESTION_FILE


def _load_suggestions() -> List[str]:
    path = _pick_suggestion_source()
    if not os.path.exists(path):
        return []
    out: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            w = _normalize_word(line)
            if w:
                out.append(w)
    return out

def _parse_dooum_text_as_lines(text: str) -> Dict[str, Set[str]]:
    m: Dict[str, Set[str]] = defaultdict(set)
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().strip('"').strip("'")
        v = v.strip()
        if not k or not v:
            continue
        parts: List[str] = []
        for token in v.replace(",", " ").split():
            t = token.strip().strip('"').strip("'")
            if t:
                parts.append(t)
        if parts:
            m[k].update(parts)
    return {k: set(v) for k, v in m.items()}

def _parse_dooum_text_as_literal(text: str) -> Dict[str, Set[str]]:
    s = text.strip()
    if not s:
        return {}
    try:
        data = ast.literal_eval(s)
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Set[str]] = {}
        for k, v in data.items():
            if not isinstance(k, str):
                continue
            if isinstance(v, (set, list, tuple)):
                vals = set(str(x) for x in v if str(x))
            elif isinstance(v, str):
                vals = {v} if v else set()
            else:
                vals = set()
            if vals:
                out[k] = vals
        return out
    except Exception:
        return {}

def _load_dooum_map() -> Dict[str, Set[str]]:
    """두음법칙 맵을 로드한다.

    ✅ 안전장치:
    - dooum_rules.txt가 **부분 규칙만** 들어있더라도 기본(DEFAULT_DOOUM_MAP)은 항상 유지한다.
      (파일이 존재하는 순간 기본 규칙이 통째로 덮여서 '두음법칙이 하나도 적용 안 됨' 같은 현상을 방지)
    """
    base: Dict[str, Set[str]] = {k: set(v) for k, v in DEFAULT_DOOUM_MAP.items()}

    if not os.path.exists(DOOUM_RULES_FILE):
        return base

    try:
        with open(DOOUM_RULES_FILE, "r", encoding="utf-8") as f:
            text = f.read()

        extra = _parse_dooum_text_as_lines(text)
        if not extra:
            extra = _parse_dooum_text_as_literal(text)

        if extra:
            for k, vs in extra.items():
                if not k:
                    continue
                base.setdefault(k, set()).update(set(vs or []))

        return base
    except Exception as e:
        logger.warning("[BlueWar] 두음법칙 파일 로드 실패: %s", e)
        return base



def _build_equiv_map(dooum: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    adj: Dict[str, Set[str]] = defaultdict(set)
    for a, bs in dooum.items():
        if not a:
            continue
        for b in bs:
            if not b:
                continue
            adj[a].add(b)
            adj[b].add(a)
    nodes = set(adj.keys())
    for vs in adj.values():
        nodes |= set(vs)
    equiv: Dict[str, Set[str]] = {}
    visited: Set[str] = set()
    for n in nodes:
        if n in visited:
            continue
        q = deque([n])
        comp: Set[str] = set()
        visited.add(n)
        while q:
            x = q.popleft()
            comp.add(x)
            for y in adj.get(x, set()):
                if y not in visited:
                    visited.add(y)
                    q.append(y)
        for x in comp:
            equiv[x] = set(comp)
    return equiv

DOOUM_MAP: Dict[str, Set[str]] = _load_dooum_map()
DOOUM_EQUIV: Dict[str, Set[str]] = _build_equiv_map(DOOUM_MAP)

def _allowed_first_chars(last_char: str) -> Set[str]:
    if not last_char:
        return set()
    s = {last_char}
    s |= DOOUM_MAP.get(last_char, set())
    s |= DOOUM_EQUIV.get(last_char, set())
    return s

def _valid_follow(prev_word: str, next_word: str) -> bool:
    if not prev_word or not next_word:
        return False
    last = _last_char(prev_word)
    first = _first_char(next_word)
    return first in _allowed_first_chars(last)

def _has_any_move_from_last(last_char: str, used: Set[str]) -> bool:
    for ch in _allowed_first_chars(last_char):
        for cand in WORDS_BY_FIRST.get(ch, []):
            if cand not in used:
                return True
    return False

def _build_graph(words: Set[str]) -> Dict[str, Set[str]]:
    graph: Dict[str, Set[str]] = defaultdict(set)
    for w in words:
        if not w:
            continue
        first = _first_char(w)
        last = _last_char(w)
        graph[first].add(w)
        # last char도 key는 만들어둔다
        graph[last]
    return graph


def _load_or_build_graph(words: Set[str], signature: str) -> Dict[str, Set[str]]:
    """단어 그래프(캐시)를 로드하거나 빌드한다.

    signature(예: sha256)가 바뀌면 캐시를 무조건 재생성한다.
    """
    try:
        if os.path.exists(GRAPH_CACHE_FILE):
            with open(GRAPH_CACHE_FILE, "rb") as f:
                data = pickle.load(f)

            # 새 포맷: {"signature": str, "graph": dict}
            if isinstance(data, dict) and "signature" in data and "graph" in data:
                sig = data.get("signature")
                g = data.get("graph")
                if isinstance(sig, str) and sig == signature and isinstance(g, dict):
                    return g  # type: ignore[return-value]

            # 구 포맷: 그래프 dict만 저장되어 있던 경우 -> words 변경 가능성이 있어 재빌드
    except Exception as e:
        logger.warning("[BlueWar] 그래프 캐시 로드 실패: %s", e)

    graph = _build_graph(words)
    try:
        os.makedirs(os.path.dirname(GRAPH_CACHE_FILE), exist_ok=True)
        with open(GRAPH_CACHE_FILE, "wb") as f:
            pickle.dump({"signature": signature, "graph": graph}, f)
    except Exception as e:
        logger.warning("[BlueWar] 그래프 캐시 저장 실패: %s", e)
    return graph

def _gen_candidates_from_last(last_char: str, used: Set[str]) -> List[str]:
    candidates: List[str] = []
    for ch in _allowed_first_chars(last_char):
        candidates.extend(WORDS_BY_FIRST.get(ch, []))
    if used:
        candidates = [c for c in candidates if c not in used]
    return candidates

def _count_moves_from_last(last_char: str, used: Set[str], limit: int = 400) -> int:
    c = 0
    for ch in _allowed_first_chars(last_char):
        for w in WORDS_BY_FIRST.get(ch, []):
            if w not in used:
                c += 1
                if c >= limit:
                    return c
    return c

def _select_ai_word_minimax(current_word: str, used: Set[str], *, depth: int = AI_SEARCH_DEPTH, time_limit: Optional[float] = AI_SEARCH_TIME_LIMIT, abort_check: Optional[Callable[[], bool]] = None) -> Optional[str]:
    last0 = _last_char(current_word)
    deadline: Optional[float] = None
    try:
        if time_limit is not None and float(time_limit) > 0:
            deadline = time.perf_counter() + float(time_limit)
    except Exception:
        deadline = None

    def heuristic(last_char: str) -> int:
        return _count_moves_from_last(last_char, used)

    def order_moves(moves: List[str], keep: int) -> List[str]:
        scored: List[Tuple[int, str]] = []
        for w in moves:
            scored.append((_count_moves_from_last(_last_char(w), used | {w}, limit=200), w))
        scored.sort(key=lambda x: x[0])
        return [w for _, w in scored[:keep]]

    def negamax(last_char: str, d: int, alpha: int, beta: int) -> int:
        if abort_check and abort_check():
            return heuristic(last_char)
        if deadline is not None and time.perf_counter() >= deadline:
            return heuristic(last_char)
        moves = _gen_candidates_from_last(last_char, used)
        if not moves:
            return -10000 - d
        if d <= 0:
            return heuristic(last_char)

        keep = 18 if d >= 8 else 28
        moves = order_moves(moves, keep=keep)

        best = -10**9
        for w in moves:
            if abort_check and abort_check():
                break
            if deadline is not None and time.perf_counter() >= deadline:
                break
            used.add(w)
            score = -negamax(_last_char(w), d - 1, -beta, -alpha)
            used.remove(w)

            if score > best:
                best = score
            if score > alpha:
                alpha = score
            if alpha >= beta:
                break
        return best

    moves0 = _gen_candidates_from_last(last0, used)
    if not moves0:
        return None

    immediate_win: List[str] = []
    others: List[str] = []
    for w in moves0:
        if not _has_any_move_from_last(_last_char(w), used | {w}):
            immediate_win.append(w)
        else:
            others.append(w)
    if immediate_win:
        immediate_win.sort(key=lambda w: _count_moves_from_last(_last_char(w), used | {w}, limit=200))
        return immediate_win[0]

    moves0 = order_moves(others, keep=70)

    best_word: Optional[str] = None
    best_score = -10**9
    alpha = -10**9
    beta = 10**9

    for w in moves0:
        if abort_check and abort_check():
            break
        if deadline is not None and time.perf_counter() >= deadline:
            break
        used.add(w)
        score = -negamax(_last_char(w), depth - 1, -beta, -alpha)
        used.remove(w)

        if score > best_score:
            best_score = score
            best_word = w
        if score > alpha:
            alpha = score

    return best_word

@dataclass
class BlueWarSession:
    guild_id: int
    channel_id: int
    host_id: int
    opponent_id: Optional[int]
    is_practice: bool
    start_word: str
    used: Set[str]
    history: List[str]
    started_at: datetime
    stop_event: Optional[asyncio.Event] = None

class BlueWarJoinView(discord.ui.View):
    def __init__(self, host: discord.Member, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.host = host
        self.opponent: Optional[discord.Member] = None
        self.closed = False

    @discord.ui.button(label="참가", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.closed:
            await interaction.response.send_message("이미 모집이 닫혔어.", ephemeral=True)
            return
        if self.opponent is not None:
            await interaction.response.send_message("이미 참가자가 있어.", ephemeral=True)
            return
        if interaction.user.id == self.host.id:
            await interaction.response.send_message("호스트는 참가할 수 없어.", ephemeral=True)
            return
        self.opponent = interaction.user
        self.closed = True
        for child in self.children:
            try:
                child.disabled = True
            except Exception:
                pass
        await interaction.response.send_message(f"{interaction.user.display_name} 참가 완료!", ephemeral=True)
        self.stop()

    @discord.ui.button(label="닫기", style=discord.ButtonStyle.secondary)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("호스트만 닫을 수 있어.", ephemeral=True)
            return
        self.closed = True
        self.stop()
        await interaction.response.send_message("모집을 닫았어.", ephemeral=True)



class BlueWarPracticeDifficultyView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.selected: Optional[str] = None
        self.depth: int = AI_SEARCH_DEPTH

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        try:
            await interaction.response.send_message("이 선택은 명령을 실행한 사람만 할 수 있어.", ephemeral=True)
        except Exception:
            pass
        return False

    async def _select(self, interaction: discord.Interaction, label: str, depth: int) -> None:
        self.selected = label
        self.depth = depth

        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if child.label == label:
                child.style = discord.ButtonStyle.success
            else:
                child.style = discord.ButtonStyle.secondary
            child.disabled = True

        try:
            await interaction.response.edit_message(content=f"연습 난이도: **{label}**", view=self)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="Easy", style=discord.ButtonStyle.secondary)
    async def easy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._select(interaction, "Easy", 4)

    @discord.ui.button(label="Normal", style=discord.ButtonStyle.primary)
    async def normal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._select(interaction, "Normal", 10)

    @discord.ui.button(label="Hard", style=discord.ButtonStyle.danger)
    async def hard(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._select(interaction, "Hard", 20)


class BlueWarPvpStartModeView(discord.ui.View):
    """PVP 시작 방식 선택 View.

    - 랜덤 제시어(기존): 유메가 제시어를 랜덤으로 뽑고, 선플레이어부터 시작.
    - 선플레이어 제시어: 선플레이어가 첫 단어(=제시어)를 직접 입력하고, 그 다음 턴부터 진행.
    """

    def __init__(self, author_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.mode: Optional[str] = None  # "random" | "starter_word"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        try:
            await interaction.response.send_message("이 선택은 명령을 실행한 사람만 할 수 있어.", ephemeral=True)
        except Exception:
            pass
        return False

    async def _select(self, interaction: discord.Interaction, label: str, mode: str) -> None:
        self.mode = mode
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if child.label == label:
                child.style = discord.ButtonStyle.success
            else:
                child.style = discord.ButtonStyle.secondary
            child.disabled = True

        try:
            await interaction.response.edit_message(content=f"PVP 시작: **{label}**", view=self)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="랜덤 제시어", style=discord.ButtonStyle.primary)
    async def random_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._select(interaction, "랜덤 제시어", "random")

    @discord.ui.button(label="선플레이어 제시어", style=discord.ButtonStyle.secondary)
    async def starter_word_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._select(interaction, "선플레이어 제시어", "starter_word")

class BlueWarCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions: Dict[Tuple[int, int], BlueWarSession] = {}
        self._sessions_lock = asyncio.Lock()

        # Phase 5: 단어리스트는 웹(어드민) -> 로컬 캐시 -> 로컬 파일 순으로 로드한다.
        self._wordlists_signature: str = ""
        self._wordlists_source: str = ""
        self._wordlists_last_sync_reason: str = ""

        self._load_wordlists_initial()

        self.openai = AsyncOpenAI(api_key=OPENAI_API_KEY) if (AsyncOpenAI and OPENAI_API_KEY) else None


    def _compute_file_sha256(self, path: str) -> str:
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return ""

    def _get_words_signature(self, words_path: str) -> str:
        """웹 meta(sha256) -> 파일 sha256 순으로 시그니처를 만든다."""
        try:
            meta = load_local_meta()
            m = meta.get("blue_archive_words") if isinstance(meta, dict) else None
            if isinstance(m, dict):
                sha = (m.get("sha256") or "").strip()
                if sha:
                    return sha
        except Exception:
            pass
        if words_path and os.path.exists(words_path):
            return self._compute_file_sha256(words_path)
        return ""

    def _reload_words_and_graph(self) -> None:
        global WORDS_SET, WORDS_BY_FIRST
        words_path = pick_words_source(prefer_cache=True)
        self._wordlists_source = words_path
        WORDS_SET, WORDS_BY_FIRST = load_words_from_file(words_path)
        self._wordlists_signature = self._get_words_signature(words_path)
        self.graph = _load_or_build_graph(WORDS_SET, self._wordlists_signature)

    def _load_wordlists_initial(self) -> None:
        """봇 시작 시 단어리스트를 로드한다.

        - 캐시가 없고 로컬 파일도 없으면 1회 동기화를 시도한다.
        - 네트워크가 실패해도 예외로 죽지 않고(가능한 폴백),
          최종적으로 로드 실패 시 WORDS_SET이 비게 된다.
        """
        # 1) 서버에 로컬 파일을 안 둘 수도 있으니, 캐시/로컬 둘 다 없으면 동기화 1회 시도
        try:
            words_candidate = pick_words_source(prefer_cache=True)
            sugg_candidate = _pick_suggestion_source(prefer_cache=True)
            need_sync = (not os.path.exists(words_candidate)) or (not os.path.exists(sugg_candidate))
            if need_sync:
                res = sync_wordlists(force=False)
                self._wordlists_last_sync_reason = res.reason
        except Exception as e:
            self._wordlists_last_sync_reason = f"initial sync failed: {e}"

        # 2) 실제 로드
        try:
            self._reload_words_and_graph()
        except Exception as e:
            logger.error("[BlueWar] 단어 로드 실패: %s", e)
            WORDS_SET = set()
            WORDS_BY_FIRST = defaultdict(list)
            self.graph = {}
            self._wordlists_signature = ""
            self._wordlists_source = ""

        try:
            self.suggestions = _load_suggestions()
        except Exception:
            self.suggestions = []

    async def _ensure_wordlists_ready(self, *, force: bool = False) -> bool:
        """게임 시작 직전에 웹 캐시를 확인하고, 변경되었으면 리로드한다."""
        try:
            res = await asyncio.to_thread(sync_wordlists, force=force)
            self._wordlists_last_sync_reason = res.reason
            if not res.synced:
                return False
            if not res.changed:
                return False

            changed = False
            if "blue_archive_words" in res.changed_lists:
                self._reload_words_and_graph()
                changed = True
            if "suggestion" in res.changed_lists:
                self.suggestions = _load_suggestions()
                changed = True

            if changed:
                logger.info("[BlueWar] wordlists updated: %s", ",".join(res.changed_lists))
            return changed
        except Exception as e:
            logger.warning("[BlueWar] wordlists ensure failed: %s", e)
            return False

    def _key(self, guild: Optional[discord.Guild], channel: discord.abc.GuildChannel | discord.abc.PrivateChannel) -> Tuple[int, int]:
        gid = guild.id if guild else 0
        cid = getattr(channel, "id", 0)
        return (gid, int(cid))

    def _can_stop(self, author: discord.abc.User, session: BlueWarSession, guild: Optional[discord.Guild]) -> bool:
        if author.id == session.host_id:
            return True
        if guild is None:
            return False
        member = guild.get_member(author.id)
        if not member:
            return False
        perms = member.guild_permissions
        return bool(perms.administrator or perms.manage_channels or perms.manage_guild)

    def _note_event(self, event_name: str, *, user: Optional[discord.Member] = None, guild: Optional[discord.Guild] = None, weight: float = 1.0):
        try:
            core = getattr(self.bot, "yume_core", None)
            if core and hasattr(core, "note_event"):
                core.note_event(event_name, user=user, guild=guild, weight=weight)
        except Exception:
            pass

    def _build_review_log_text(self, word_history: List[str]) -> str:
        if not word_history:
            return ""
        return " → ".join(word_history)

    async def _warn_10s_left(
        self,
        channel: discord.abc.Messageable,
        member: discord.abc.User,
        wait_task: asyncio.Task,
        *,
        total_timeout: float,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        try:
            wait_time = max(0.0, float(total_timeout) - 10.0)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            if wait_task.done():
                return
            if stop_event is not None and stop_event.is_set():
                return
            await channel.send(f"{member.mention} 후배, 10초 남았어.")
        except asyncio.CancelledError:
            return
        except Exception:
            return


    async def _report_match_to_admin(
        self,
        *,
        mode: str,
        starter: discord.Member,
        winner: discord.Member,
        loser: discord.Member,
        word_history: List[str],
        start_time: datetime,
        end_time: datetime,
        reason: str,
    ) -> None:
        total_rounds = len(word_history)
        review_log = self._build_review_log_text(word_history)
        payload: Dict[str, Any] = {
            "mode": mode,
            "status": "finished",
            "starter_discord_id": str(starter.id),
            "winner_discord_id": str(winner.id),
            "loser_discord_id": str(loser.id),
            "win_gap": None,
            "total_rounds": total_rounds,
            "started_at": start_time.isoformat(),
            "finished_at": end_time.isoformat(),
            "note": f"{mode}, reason={reason}",
            "review_log": review_log,
            "participants": [
                {
                    "discord_id": str(winner.id),
                    "name": winner.display_name,
                    "ai_name": None,
                    "side": 0,
                    "is_winner": True,
                    "score": None,
                    "turns": None,
                },
                {
                    "discord_id": str(loser.id),
                    "name": loser.display_name,
                    "ai_name": None,
                    "side": 1,
                    "is_winner": False,
                    "score": None,
                    "turns": None,
                },
            ],
        }
        try:
            sender = getattr(self.bot, "admin_sender", None)
            if sender and hasattr(sender, "send_bluewar_match"):
                await sender.send_bluewar_match(payload)
            else:
                ok = await asyncio.to_thread(post_bluewar_match_to_admin, payload)
                if not ok:
                    logger.warning("[BlueWar] 관리자 웹 전송 실패: upload failed")
        except Exception as e:
            logger.warning("[BlueWar] 관리자 웹 전송 실패: %s", e)

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
        winner_discord_id: Optional[str] = str(user.id) if user_is_winner else None
        loser_discord_id: Optional[str] = str(user.id) if (not user_is_winner) else None

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
                    "side": 0,
                    "is_winner": user_is_winner,
                    "score": None,
                    "turns": None,
                },
                {
                    "discord_id": None,
                    "name": "유메",
                    "ai_name": "yume",
                    "side": 1,
                    "is_winner": (not user_is_winner),
                    "score": None,
                    "turns": None,
                },
            ],
        }

        try:
            sender = getattr(self.bot, "admin_sender", None)
            if sender and hasattr(sender, "send_bluewar_match"):
                await sender.send_bluewar_match(payload)
            else:
                ok = await asyncio.to_thread(post_bluewar_match_to_admin, payload)
                if not ok:
                    logger.warning("[BlueWar] practice 관리자 웹 전송 실패: upload failed")
        except Exception as e:
            logger.warning("[BlueWar] practice 관리자 웹 전송 실패: %s", e)

    async def _speak_practice_result(self, user: discord.Member, user_is_winner: bool, word_history: List[str]) -> str:
        if not (self.openai and YUME_BLUEWAR_USE_LLM):
            if user_is_winner:
                return f"{user.display_name}, 오늘은 네가 이겼네. 잘했어, 으헤~"
            return f"{user.display_name}, 다음엔 더 잘할 수 있을 거야. 유메가 응원할게, 으헤~"

        tone = "neutral"
        try:
            core = getattr(self.bot, "yume_core", None)
            if core and hasattr(core, "get_tone_for_user"):
                tone = core.get_tone_for_user(user)
        except Exception:
            tone = "neutral"

        prompt = f"""
상황: 디스코드 끝말잇기 연습 모드(블루전). 유저 vs 유메(AI).
유저: {user.display_name}
결과: {"유저 승리" if user_is_winner else "유메 승리"}
진행 단어 기록: {" / ".join(word_history)}

요청: 유메 말투로, 결과를 짧게 코멘트해줘. 1~2문장.
톤: {tone}
"""
        try:
            resp = await self.openai.chat.completions.create(
                model=YUME_OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": YUME_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt.strip()},
                ],
                temperature=0.7,
            )
            text_out = (resp.choices[0].message.content or "").strip()
            return text_out if text_out else (f"{user.display_name}, 수고했어. 으헤~")
        except Exception as e:
            logger.warning("[BlueWar] LLM practice result 실패: %s", e)
            if user_is_winner:
                return f"{user.display_name}, 오늘은 네가 이겼네. 잘했어, 으헤~"
            return f"{user.display_name}, 다음엔 더 잘할 수 있을 거야. 유메가 응원할게, 으헤~"

    async def _run_practice_game(self, ctx: commands.Context, user: discord.Member, game_no: int, *, key: Tuple[int, int], stop_event: asyncio.Event, ai_depth: int = AI_SEARCH_DEPTH):
        channel = ctx.channel
        guild = ctx.guild

        start_word = random.choice(self.suggestions) if self.suggestions else random.choice(list(WORDS_SET))
        used_words: Set[str] = set()
        word_history: List[str] = []

        current_word = start_word
        used_words.add(current_word)
        word_history.append(current_word)

        user_turn = bool(random.getrandbits(1))
        first_turn = user.mention if user_turn else "유메"

        await channel.send(
            f"연습 모드 시작! (#{game_no})\n"
            f"첫 단어는 **{start_word}**\n"
            f"첫 턴: {first_turn}\n"
            f"다음 단어는 `{_last_char(start_word)}`(또는 두음법칙)로 시작해야 해."
        )

        start_time = datetime.now(timezone.utc)
        user_is_winner = False
        end_reason = "unknown"

        try:
            while True:
                if stop_event.is_set():
                    end_reason = "forced_stop"
                    break

                if user_turn:
                    msg_task = asyncio.create_task(
                        self.bot.wait_for(
                            "message",
                            timeout=PRACTICE_TURN_TIMEOUT,
                            check=lambda m: (
                                m.author.id == user.id
                                and m.channel.id == channel.id
                                and (m.content or "").strip() != ""
                            ),
                        )
                    )
                    stop_task = asyncio.create_task(stop_event.wait())
                    warn_task = asyncio.create_task(
                        self._warn_10s_left(
                            channel,
                            user,
                            msg_task,
                            total_timeout=PRACTICE_TURN_TIMEOUT,
                            stop_event=stop_event,
                        )
                    )
                    done, pending = await asyncio.wait({msg_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    warn_task.cancel()

                    if stop_task in done and stop_event.is_set():
                        end_reason = "forced_stop"
                        break

                    try:
                        msg: discord.Message = msg_task.result()
                    except asyncio.TimeoutError:
                        await channel.send(
                            f"{user.display_name}, 시간이 초과됐어.\n"
                            "이번 판은 유메가 이긴 걸로 할게… 으헤~"
                        )
                        user_is_winner = False
                        end_reason = "timeout"
                        break
                    except Exception:
                        end_reason = "forced_stop"
                        break

                    w = _normalize_word(msg.content)
                    if w in used_words:
                        await channel.send("이미 나온 단어야. 다른 단어로 해줘.")
                        continue
                    if w not in WORDS_SET:
                        await channel.send("그 단어는 유메 사전에 없네… 다른 걸로 해줘.")
                        continue
                    if not _valid_follow(current_word, w):
                        last = _last_char(current_word)
                        allowed = _allowed_first_chars(last)
                        await channel.send(f"규칙 위반! `{current_word}` 다음은 `{', '.join(sorted(allowed))}` 로 시작해야 해.")
                        continue

                    current_word = w
                    used_words.add(w)
                    word_history.append(w)

                    if not _has_any_move_from_last(_last_char(current_word), used_words):
                        await channel.send(
                            "으으… 다음으로 이어질 단어가 없어졌네.\n"
                            f"이번 판은 **{user.display_name}** 의 승리야. 잘했어, 으헤~"
                        )
                        user_is_winner = True
                        end_reason = "ai_no_move"
                        break

                    user_turn = False

                else:
                    ai_word: Optional[str] = None
                    try:
                        ai_word = await asyncio.to_thread(
                            _select_ai_word_minimax,
                            current_word,
                            used_words,
                            depth=ai_depth,
                            time_limit=None,
                            abort_check=stop_event.is_set,
                        )
                    except Exception:
                        ai_word = None

                    if stop_event.is_set():
                        end_reason = "forced_stop"
                        break

                    if not ai_word:
                        last = _last_char(current_word)
                        candidates = _gen_candidates_from_last(last, used_words)
                        random.shuffle(candidates)
                        ai_word = candidates[0] if candidates else None

                    if not ai_word:
                        await channel.send(
                            "으으… 이어지는 단어가 더 이상 떠오르지 않아.\n"
                            f"이번 판은 **{user.display_name}** 의 승리야. 잘했어, 으헤~"
                        )
                        user_is_winner = True
                        end_reason = "ai_no_word"
                        break

                    await channel.send(f"**{ai_word}**")
                    current_word = ai_word
                    used_words.add(ai_word)
                    word_history.append(ai_word)

                    if not _has_any_move_from_last(_last_char(current_word), used_words):
                        await channel.send(
                            f"다음으로 이어질 단어가 없네.\n"
                            "이번 판은 유메가 이긴 걸로 할게… 으헤~"
                        )
                        user_is_winner = False
                        end_reason = "user_no_move"
                        break

                    user_turn = True

        finally:
            async with self._sessions_lock:
                self.sessions.pop(key, None)

        if end_reason == "forced_stop":
            try:
                await channel.send("연습을 종료했어. 으헤~")
            except Exception:
                pass
            return

        end_time = datetime.now(timezone.utc)

        ensure_records()
        records = load_records()
        add_match_record(
            records,
            mode="practice",
            winner_id=str(user.id) if user_is_winner else "yume",
            loser_id="yume" if user_is_winner else str(user.id),
            winner_name=user.display_name if user_is_winner else "유메",
            loser_name="유메" if user_is_winner else user.display_name,
            win_gap=None,
            total_rounds=len(word_history),
            history=word_history,
        )
        save_records(records)

        try:
            if user_is_winner:
                self._note_event("bluewar_practice_win", user=user, guild=guild, weight=1.2 if guild else 1.0)
            else:
                self._note_event("bluewar_practice_lose", user=user, guild=guild, weight=0.8 if guild else 1.0)
        except Exception:
            pass

        try:
            await self._report_practice_result_to_admin(
                user=user,
                user_is_winner=user_is_winner,
                word_history=word_history,
                start_time=start_time,
                end_time=end_time,
                reason=end_reason,
            )
        except Exception:
            pass

        try:
            result_text = await self._speak_practice_result(user, user_is_winner, word_history)
            if result_text:
                await channel.send(result_text)
        except Exception:
            pass

    async def _run_pvp_game(
        self,
        ctx: commands.Context,
        host: discord.Member,
        opponent: discord.Member,
        *,
        key: Tuple[int, int],
        start_mode: str = "random",  # "random" | "starter_word"
    ):
        channel = ctx.channel
        guild = ctx.guild

        starter = random.choice([host, opponent])

        async def _ask_starter_word(timeout: float = 60.0) -> Optional[str]:
            await channel.send(
                f"제시어 선택 모드야. {starter.mention} 선플레이어가 첫 단어(제시어)를 입력해줘."
                f" (제한 {int(timeout)}초)"
            )
            while True:
                try:
                    msg: discord.Message = await self.bot.wait_for(
                        "message",
                        timeout=timeout,
                        check=lambda m: (
                            m.channel.id == channel.id
                            and m.author.id == starter.id
                            and (m.content or "").strip() != ""
                        ),
                    )
                except asyncio.TimeoutError:
                    return None

                w = _normalize_word(msg.content)
                if not w:
                    continue
                if w.lower() in ("gg", "기권", "항복", "포기", "취소"):
                    return None
                if w not in WORDS_SET:
                    await channel.send("그 단어는 유메 사전에 없네… 다른 걸로 해줘.")
                    continue
                return w

        start_word: str
        used_words: Set[str]
        word_history: List[str]
        current_word: str

        start_mode = (start_mode or "random").strip().lower()
        if start_mode == "starter_word":
            chosen = await _ask_starter_word(timeout=60.0)
            if not chosen:
                await channel.send("제시어 입력이 없어서… 이번엔 랜덤 제시어로 갈게. 으헤~")
                start_mode = "random"
            else:
                start_word = chosen
                used_words = {start_word}
                word_history = [start_word]
                current_word = start_word
        if start_mode != "starter_word":
            start_word = random.choice(self.suggestions) if self.suggestions else random.choice(list(WORDS_SET))
            used_words = {start_word}
            word_history = [start_word]
            current_word = start_word

        if start_mode == "starter_word":
            turn = opponent if starter.id == host.id else host
        else:
            turn = starter

        await channel.send(
            f"PVP 블루전 시작!\n"
            f"{host.mention} vs {opponent.mention}\n"
            f"선플레이어: {starter.mention}\n"
            f"첫 단어는 **{start_word}**\n"
            f"첫 턴: {turn.mention}\n"
            f"다음 단어는 `{_last_char(start_word)}`(또는 두음법칙)로 시작해야 해."
        )

        start_time = datetime.now(timezone.utc)
        winner: Optional[discord.Member] = None
        loser: Optional[discord.Member] = None
        end_reason = "unknown"

        try:
            while True:
                msg_task = asyncio.create_task(
                    self.bot.wait_for(
                        "message",
                        timeout=PVP_TURN_TIMEOUT,
                        check=lambda m: (
                            m.channel.id == channel.id
                            and m.author.id in (host.id, opponent.id)
                            and (m.content or "").strip() != ""
                        ),
                    )
                )
                warn_task = asyncio.create_task(
                    self._warn_10s_left(
                        channel,
                        turn,
                        msg_task,
                        total_timeout=PVP_TURN_TIMEOUT,
                    )
                )
                try:
                    msg: discord.Message = await msg_task
                except asyncio.TimeoutError:
                    winner = opponent if turn.id == host.id else host
                    loser = turn
                    end_reason = "timeout"
                    await channel.send(f"{loser.display_name}, 시간이 초과됐어.\n이번 판 승자는 **{winner.display_name}**!")
                    break
                finally:
                    warn_task.cancel()

                if msg.author.id != turn.id:
                    continue

                raw = _normalize_word(msg.content)
                if raw.lower() in ("gg", "기권", "항복", "포기"):
                    winner = opponent if turn.id == host.id else host
                    loser = turn
                    end_reason = "forfeit"
                    await channel.send(f"{loser.display_name} 기권!\n이번 판 승자는 **{winner.display_name}**!")
                    break

                w = raw
                if w in used_words:
                    await channel.send("이미 나온 단어야. 다른 단어로 해줘.")
                    continue
                if w not in WORDS_SET:
                    await channel.send("그 단어는 유메 사전에 없네… 다른 걸로 해줘.")
                    continue
                if not _valid_follow(current_word, w):
                    last = _last_char(current_word)
                    allowed = _allowed_first_chars(last)
                    await channel.send(f"규칙 위반! `{current_word}` 다음은 `{', '.join(sorted(allowed))}` 로 시작해야 해.")
                    continue

                current_word = w
                used_words.add(w)
                word_history.append(w)

                if not _has_any_move_from_last(_last_char(current_word), used_words):
                    winner = turn
                    loser = opponent if turn.id == host.id else host
                    end_reason = "no_move"
                    await channel.send(f"다음으로 이어질 단어가 없어졌어.\n이번 판 승자는 **{winner.display_name}**!")
                    break

                turn = opponent if turn.id == host.id else host
                await channel.send(f"다음 턴: {turn.mention}")

        finally:
            async with self._sessions_lock:
                self.sessions.pop(key, None)

        end_time = datetime.now(timezone.utc)

        if winner and loser:
            ensure_records()
            records = load_records()
            add_match_record(
                records,
                mode="pvp",
                winner_id=str(winner.id),
                loser_id=str(loser.id),
                winner_name=winner.display_name,
                loser_name=loser.display_name,
                win_gap=None,
                total_rounds=len(word_history),
                history=word_history,
            )
            save_records(records)

            try:
                if guild is not None:
                    self._note_event("bluewar_pvp_win", user=winner, guild=guild, weight=1.2)
                    self._note_event("bluewar_pvp_lose", user=loser, guild=guild, weight=0.8)
            except Exception:
                pass

            try:
                await self._report_match_to_admin(
                    mode="pvp",
                    starter=starter,
                    winner=winner,
                    loser=loser,
                    word_history=word_history,
                    start_time=start_time,
                    end_time=end_time,
                    reason=end_reason,
                )
            except Exception:
                pass

    @commands.command(name="블루전", aliases=["블루전시작", "블루전대전"], help="1:1 블루전 대결을 시작합니다.")
    async def cmd_bluewar_start(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("PVP 블루전은 서버 채널에서만 할 수 있어.")
            return

        # Phase 5: 게임 시작 전 웹 단어리스트 캐시를 한 번 확인한다.
        await self._ensure_wordlists_ready()
        if not WORDS_SET:
            await ctx.send("단어 DB를 아직 못 불러왔어... 웹 단어리스트 설정/상태를 확인해 줘.")
            return

        key = self._key(ctx.guild, ctx.channel)

        async with self._sessions_lock:
            if key in self.sessions:
                await ctx.send("이 채널에서 이미 블루전이 진행 중이야.")
                return

        host = ctx.author
        view = BlueWarJoinView(host=host, timeout=60.0)
        msg = await ctx.send(f"{host.display_name}의 PVP 블루전 모집!\n버튼으로 참가하면 5초 뒤에 시작해.", view=view)

        await view.wait()

        try:
            await msg.edit(view=None)
        except Exception:
            pass

        if view.opponent is None:
            await ctx.send("모집이 종료됐어. 참가자가 없어서 취소할게.")
            return

        opponent = view.opponent
        if opponent.bot:
            await ctx.send("봇은 참가할 수 없어.")
            return

        start_mode_view = BlueWarPvpStartModeView(author_id=host.id, timeout=30.0)
        mode_msg = await ctx.send("PVP 시작 방식을 골라줘.", view=start_mode_view)
        await start_mode_view.wait()

        start_mode = start_mode_view.mode or "random"  # "random" | "starter_word"
        try:
            await mode_msg.edit(view=None)
        except Exception:
            pass

        async with self._sessions_lock:
            if key in self.sessions:
                await ctx.send("이 채널에서 이미 블루전이 진행 중이야.")
                return
            self.sessions[key] = BlueWarSession(
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                host_id=host.id,
                opponent_id=opponent.id,
                is_practice=False,
                start_word="",
                used=set(),
                history=[],
                started_at=datetime.now(timezone.utc),
                stop_event=None,
            )

        await ctx.send(f"{opponent.mention} 후배가 참여했어.")
        await asyncio.sleep(5)

        await self._run_pvp_game(ctx, host, opponent, key=key, start_mode=start_mode)

    @commands.command(name="블루전연습", help="유메와 1:1 연습 블루전을 합니다.")
    async def cmd_bluewar_practice(self, ctx: commands.Context):
        # Phase 5: 게임 시작 전 웹 단어리스트 캐시를 한 번 확인한다.
        await self._ensure_wordlists_ready()
        if not WORDS_SET:
            await ctx.send("단어 DB를 아직 못 불러왔어... 웹 단어리스트 설정/상태를 확인해 줘.")
            return

        key = self._key(ctx.guild, ctx.channel)

        async with self._sessions_lock:
            if key in self.sessions:
                s = self.sessions[key]
                if s.is_practice:
                    await ctx.send("이 채널에서 이미 연습이 진행 중이야. `!연습종료`로 끝내고 다시 해줘.")
                else:
                    await ctx.send("이 채널에서 이미 블루전이 진행 중이야.")
                return
            stop_event = asyncio.Event()
            self.sessions[key] = BlueWarSession(
                guild_id=ctx.guild.id if ctx.guild else 0,
                channel_id=ctx.channel.id,
                host_id=ctx.author.id,
                opponent_id=None,
                is_practice=True,
                start_word="",
                used=set(),
                history=[],
                started_at=datetime.now(timezone.utc),
                stop_event=stop_event,
            )

        user = ctx.author

        view = BlueWarPracticeDifficultyView(author_id=user.id, timeout=30.0)
        select_msg = await ctx.send("연습 난이도를 선택해줘. (30초 안에 선택 안 하면 Normal로 시작해.)", view=view)
        await view.wait()
        try:
            await select_msg.edit(view=None)
        except Exception:
            pass

        if stop_event.is_set():
            async with self._sessions_lock:
                self.sessions.pop(key, None)
            return

        depth = view.depth if view.selected else AI_SEARCH_DEPTH
        label = view.selected or "Normal"

        await ctx.send(f"연습 난이도: **{label}**. 5초 뒤에 시작할게.")
        await asyncio.sleep(5)

        if stop_event.is_set():
            async with self._sessions_lock:
                self.sessions.pop(key, None)
            return

        game_no = int(time.time()) % 100000
        await self._run_practice_game(ctx, user, game_no, key=key, stop_event=stop_event, ai_depth=depth)

    @commands.command(name="연습종료", aliases=["블루전연습종료"], help="진행 중인 블루전 연습을 강제 종료합니다.")
    async def cmd_bluewar_practice_stop(self, ctx: commands.Context):
        key = self._key(ctx.guild, ctx.channel)
        session = self.sessions.get(key)
        if not session or not session.is_practice:
            await ctx.send("이 채널에서 진행 중인 연습이 없어.")
            return
        if not self._can_stop(ctx.author, session, ctx.guild):
            await ctx.send("연습을 종료할 권한이 없어.")
            return
        if session.stop_event:
            session.stop_event.set()
        await ctx.send("연습 종료할게. 으헤~")

    @commands.command(name="블루전전적", help="블루전 전적을 확인합니다.")
    async def cmd_bluewar_records(self, ctx: commands.Context):
        ensure_records()
        records = load_records()
        user_id = str(ctx.author.id)
        user_data = records.get("users", {}).get(user_id)
        if not user_data:
            await ctx.send("전적이 아직 없어.")
            return
        wins = user_data.get("wins", 0)
        losses = user_data.get("losses", 0)
        await ctx.send(f"{ctx.author.display_name} 전적: {wins}승 {losses}패")

    @commands.command(name="블루전랭킹", help="블루전 랭킹을 확인합니다.")
    async def cmd_bluewar_ranking(self, ctx: commands.Context):
        ensure_records()
        records = load_records()
        rankings = calc_rankings(records)
        if not rankings:
            await ctx.send("랭킹 데이터가 없어.")
            return
        lines_out = []
        for i, r in enumerate(rankings[:10], 1):
            lines_out.append(f"{i}. {r['name']} - {r['wins']}승 {r['losses']}패")
        await ctx.send("블루전 랭킹 TOP10\n" + "\n".join(lines_out))

async def setup(bot: commands.Bot):
    await bot.add_cog(BlueWarCog(bot))
