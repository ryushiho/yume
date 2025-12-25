import os
import re
import json
import time
import math
import random
import asyncio
import logging
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, Set, List, Optional, Tuple, Any

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

WORDS_FILE = os.path.join(BASE_DIR, "data", "dictionary", "suggestion.txt")
WORDS_EXTRA_FILE = os.path.join(BASE_DIR, "data", "dictionary", "blue_archive_words.txt")
DOOUM_RULES_FILE = os.path.join(BASE_DIR, "data", "dictionary", "dooum_rules.txt")

RECORDS_FILE = os.path.join(BASE_DIR, "data", "storage", "blue_records.json")

GRAPH_CACHE_FILE = os.path.join(BASE_DIR, "data", "storage", "blue_graph_cache.pkl")

PRACTICE_TURN_TIMEOUT = 90.0
PVP_TURN_TIMEOUT = 90.0

AI_SEARCH_DEPTH = 10
AI_TIME_LIMIT_SEC = 1.2

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
    "랏": {"낫"},
    "랑": {"낭"},
    "래": {"내"},
    "랙": {"낵"},
    "랜": {"낸"},
    "랠": {"낼"},
    "램": {"냄"},
    "랩": {"냅"},
    "랫": {"냇"},
    "랭": {"냉"},
    "러": {"너"},
    "럭": {"넉"},
    "런": {"넌"},
    "럴": {"널"},
    "럼": {"넘"},
    "럽": {"넙"},
    "럿": {"넛"},
    "렁": {"넝"},
    "레": {"네"},
    "렉": {"넥"},
    "렌": {"넨"},
    "렐": {"넬"},
    "렘": {"넴"},
    "렙": {"넵"},
    "렛": {"넷"},
    "렝": {"넹"},
    "려": {"여"},
    "력": {"역"},
    "련": {"연"},
    "렬": {"열"},
    "렴": {"염"},
    "렵": {"엽"},
    "렷": {"엿"},
    "령": {"영"},
    "례": {"예"},
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
}

def _parse_dooum_text_as_lines(text: str) -> Dict[str, Set[str]]:
    m: Dict[str, Set[str]] = {}
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if "->" not in s:
            continue
        left, right = s.split("->", 1)
        left = left.strip()
        right = right.strip()
        if not left or not right:
            continue
        outs: Set[str] = set()
        for tok in re.split(r"[,\s]+", right):
            tok = tok.strip()
            if tok:
                outs.add(tok)
        if outs:
            m[left] = outs
    return m

def _parse_dooum_text_as_literal(text: str) -> Dict[str, Set[str]]:
    try:
        obj = json.loads(text)
        out: Dict[str, Set[str]] = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                if not isinstance(k, str):
                    continue
                if isinstance(v, list):
                    out[k] = {str(x) for x in v if str(x)}
                elif isinstance(v, str):
                    out[k] = {v}
        return out
    except Exception:
        return {}

def _load_dooum_map() -> Dict[str, Set[str]]:
    if not os.path.exists(DOOUM_RULES_FILE):
        return {k: set(v) for k, v in DEFAULT_DOOUM_MAP.items()}
    try:
        with open(DOOUM_RULES_FILE, "r", encoding="utf-8") as f:
            text = f.read()
        m = _parse_dooum_text_as_lines(text)
        if not m:
            m = _parse_dooum_text_as_literal(text)
        if not m:
            m = {k: set(v) for k, v in DEFAULT_DOOUM_MAP.items()}
        return m
    except Exception as e:
        logger.warning("[BlueWar] 두음법칙 파일 로드 실패: %s", e)
        return {k: set(v) for k, v in DEFAULT_DOOUM_MAP.items()}

def _build_equiv_map(m: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    equiv: Dict[str, Set[str]] = defaultdict(set)
    for k, outs in m.items():
        for o in outs:
            equiv[o].add(k)
    return {k: set(v) for k, v in equiv.items()}

DOOUM_MAP: Dict[str, Set[str]] = _load_dooum_map()
DOOUM_EQUIV: Dict[str, Set[str]] = _build_equiv_map(DOOUM_MAP)

def _allowed_first_chars(last_char: str) -> Set[str]:
    if not last_char:
        return set()
    s = {last_char}
    s |= DOOUM_MAP.get(last_char, set())
    s |= DOOUM_EQUIV.get(last_char, set())
    return s

def _first_char(word: str) -> str:
    return word[0] if word else ""

def _last_char(word: str) -> str:
    return word[-1] if word else ""

def _valid_follow(prev_word: str, next_word: str) -> bool:
    if not prev_word or not next_word:
        return False
    last = _last_char(prev_word)
    first = _first_char(next_word)
    return first in _allowed_first_chars(last)

def _normalize_word(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "", s)
    return s

def ensure_records():
    os.makedirs(os.path.dirname(RECORDS_FILE), exist_ok=True)
    if not os.path.exists(RECORDS_FILE):
        with open(RECORDS_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": {}}, f, ensure_ascii=False, indent=2)

def load_records() -> Dict[str, Any]:
    ensure_records()
    try:
        with open(RECORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}

def save_records(data: Dict[str, Any]) -> None:
    ensure_records()
    with open(RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _read_words_file(path: str) -> Set[str]:
    out: Set[str] = set()
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            w = raw.strip()
            if not w:
                continue
            out.add(w)
    return out

WORDS_SET: Set[str] = set()
WORDS_BY_FIRST: Dict[str, List[str]] = defaultdict(list)

def _build_words_index(words: Set[str]) -> None:
    global WORDS_SET, WORDS_BY_FIRST
    WORDS_SET = set(words)
    by_first: Dict[str, List[str]] = defaultdict(list)
    for w in WORDS_SET:
        if not w:
            continue
        by_first[_first_char(w)].append(w)
    for k in by_first:
        by_first[k].sort(key=lambda x: (-len(x), x))
    WORDS_BY_FIRST = by_first

def _load_words() -> Set[str]:
    words = set()
    words |= _read_words_file(WORDS_FILE)
    words |= _read_words_file(WORDS_EXTRA_FILE)
    return {w for w in words if w}

def _gen_candidates_from_last(last_char: str, used: Set[str]) -> List[str]:
    outs: List[str] = []
    for ch in _allowed_first_chars(last_char):
        for cand in WORDS_BY_FIRST.get(ch, []):
            if cand in used:
                continue
            outs.append(cand)
    outs.sort(key=lambda x: (-len(x), x))
    return outs

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
        graph[first].add(last)
    return {k: set(v) for k, v in graph.items()}

def _load_graph_cache() -> Optional[Dict[str, Set[str]]]:
    if not os.path.exists(GRAPH_CACHE_FILE):
        return None
    try:
        with open(GRAPH_CACHE_FILE, "rb") as f:
            obj = pickle.load(f)
        if isinstance(obj, dict):
            out: Dict[str, Set[str]] = {}
            for k, v in obj.items():
                if isinstance(k, str) and isinstance(v, set):
                    out[k] = set(v)
            return out
    except Exception:
        return None
    return None

def _save_graph_cache(graph: Dict[str, Set[str]]) -> None:
    try:
        os.makedirs(os.path.dirname(GRAPH_CACHE_FILE), exist_ok=True)
        with open(GRAPH_CACHE_FILE, "wb") as f:
            pickle.dump(graph, f)
    except Exception:
        pass

GRAPH_CACHE: Optional[Dict[str, Set[str]]] = None

def _ensure_graph_cache() -> Dict[str, Set[str]]:
    global GRAPH_CACHE
    if GRAPH_CACHE is not None:
        return GRAPH_CACHE
    cached = _load_graph_cache()
    if cached is not None:
        GRAPH_CACHE = cached
        return GRAPH_CACHE
    g = _build_graph(WORDS_SET)
    GRAPH_CACHE = g
    _save_graph_cache(g)
    return GRAPH_CACHE

def _evaluate_leaf(last_char: str, used: Set[str]) -> int:
    if not _has_any_move_from_last(last_char, used):
        return -9999
    return 0

def _minimax(last_char: str, used: Set[str], depth: int, alpha: int, beta: int, maximizing: bool, start_time: float, time_limit: float) -> int:
    if time.time() - start_time > time_limit:
        return 0
    if depth <= 0:
        return _evaluate_leaf(last_char, used)

    moves = _gen_candidates_from_last(last_char, used)
    if not moves:
        return -9999 if maximizing else 9999

    if maximizing:
        best = -999999
        for w in moves:
            used.add(w)
            score = _minimax(_last_char(w), used, depth - 1, alpha, beta, False, start_time, time_limit)
            used.remove(w)
            if score > best:
                best = score
            if score > alpha:
                alpha = score
            if alpha >= beta:
                break
        return best
    else:
        best = 999999
        for w in moves:
            used.add(w)
            score = _minimax(_last_char(w), used, depth - 1, alpha, beta, True, start_time, time_limit)
            used.remove(w)
            if score < best:
                best = score
            if score < beta:
                beta = score
            if alpha >= beta:
                break
        return best

def _select_ai_word_minimax(current_word: str, used: Set[str], depth: int, time_limit: float) -> Optional[str]:
    start_time = time.time()
    last0 = _last_char(current_word)
    moves0 = _gen_candidates_from_last(last0, used)
    if not moves0:
        return None

    immediate_win: List[str] = []
    for w in moves0[:30]:
        last = _last_char(w)
        used.add(w)
        if not _has_any_move_from_last(last, used):
            immediate_win.append(w)
        used.remove(w)
    if immediate_win:
        immediate_win.sort(key=lambda x: (-len(x), x))
        return immediate_win[0]

    best_score = -999999
    best_move = None
    for w in moves0[:60]:
        if time.time() - start_time > time_limit:
            break
        used.add(w)
        score = _minimax(_last_char(w), used, depth - 1, -999999, 999999, False, start_time, time_limit)
        used.remove(w)
        if score > best_score:
            best_score = score
            best_move = w

    if best_move is None:
        return moves0[0]
    return best_move

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

        # 버튼 토글 표시
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

YUME_OPENAI_MODEL = os.getenv("YUME_OPENAI_MODEL", "gpt-4o-mini")

YUME_SYSTEM_PROMPT = (
    "너는 블루 아카이브의 쿠치나시 유메 모티브 학생회장 선배 캐릭터 '유메'야. "
    "말투는 다정하고 마이페이스지만 가끔 장난스럽고, 문장 끝에 '으헤~'를 자주 붙여. "
    "상대방은 '후배'로 부르되 가능하면 멘션으로 부르고, 너무 과장되지 않게 자연스럽게 말해."
)

class BlueWarCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions: Dict[Tuple[int, int], BlueWarSession] = {}
        self._sessions_lock = asyncio.Lock()
        self._records_lock = asyncio.Lock()

        self.words = _load_words()
        _build_words_index(self.words)
        _ensure_graph_cache()

        self.suggestions: List[str] = []
        try:
            if os.path.exists(WORDS_FILE):
                with open(WORDS_FILE, "r", encoding="utf-8") as f:
                    self.suggestions = [x.strip() for x in f if x.strip()]
        except Exception:
            self.suggestions = []

    def _key(self, guild: Optional[discord.Guild], channel: discord.abc.GuildChannel) -> Tuple[int, int]:
        gid = guild.id if guild else 0
        return (gid, channel.id)

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
        core = getattr(self.bot, "yume_admin_client", None)
        if not core:
            return
        review_log = self._build_review_log_text(word_history)
        payload: Dict[str, Any] = {
            "mode": mode,
            "status": "finished",
            "starter_discord_id": str(starter.id),
            "winner_discord_id": str(winner.id),
            "loser_discord_id": str(loser.id),
            "win_gap": None,
            "total_rounds": len(word_history),
            "started_at": start_time.isoformat(),
            "finished_at": end_time.isoformat(),
            "note": "",
            "review_log": review_log,
            "participants": [
                {
                    "discord_id": str(starter.id),
                    "name": starter.display_name,
                    "side": "starter",
                    "is_winner": starter.id == winner.id,
                    "score": None,
                    "turns": None,
                },
                {
                    "discord_id": str(loser.id),
                    "name": loser.display_name,
                    "side": "opponent",
                    "is_winner": loser.id == winner.id,
                    "score": None,
                    "turns": None,
                },
            ],
        }
        await core.send_bluewar_match(payload)

    def _build_review_log_text(self, word_history: List[str]) -> str:
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

    def _note_event(self, event_name: str, *, user: Optional[discord.abc.User] = None, guild: Optional[discord.Guild] = None, weight: float = 1.0):
        try:
            core = getattr(self.bot, "yume_core", None)
            if core and user:
                core.note_event(user_id=str(user.id), name=event_name, weight=weight, guild_id=str(guild.id) if guild else None)
        except Exception:
            pass

    async def _llm_practice_result(self, user: discord.Member, user_is_winner: bool, end_reason: str) -> str:
        openai = getattr(self.bot, "openai", None)
        if not openai:
            if user_is_winner:
                return f"{user.display_name}, 오늘은 네가 이겼네. 잘했어, 으헤~"
            return f"{user.display_name}, 다음엔 더 잘할 수 있을 거야. 유메가 응원할게, 으헤~"

        prompt = (
            "블루전(끝말잇기) 연습 결과 멘트를 짧게 만들어줘.\n"
            f"- 상대: {user.display_name}\n"
            f"- 결과: {'유저 승리' if user_is_winner else '유메 승리'}\n"
            f"- 종료 사유: {end_reason}\n"
            "조건: 한두 문장, 너무 길지 않게."
        )
        try:
            resp = await openai.chat.completions.create(
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
                    if w.lower() in ("gg", "기권", "항복", "포기"):
                        await channel.send(f"{user.display_name} 기권!\n이번 판은 유메 승리야. 으헤~")
                        user_is_winner = False
                        end_reason = "forfeit"
                        break

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

                    last = _last_char(current_word)
                    if not _has_any_move_from_last(last, used_words):
                        await channel.send("더 이상 이을 수 있는 단어가 없어.\n이번 판은 네가 이겼어! 으헤~")
                        user_is_winner = True
                        end_reason = "no_moves"
                        break

                    user_turn = False
                else:
                    last = _last_char(current_word)

                    candidates = _gen_candidates_from_last(last, used_words)
                    if not candidates:
                        await channel.send(
                            "유메가 낼 단어가 없어…\n"
                            f"이번 판은 **{user.display_name}** 승리야! 으헤~"
                        )
                        user_is_winner = True
                        end_reason = "no_moves"
                        break

                    ai_word: Optional[str] = None
                    try:
                        ai_word = await asyncio.to_thread(
                            _select_ai_word_minimax,
                            current_word,
                            used_words.copy(),
                            ai_depth,
                            AI_TIME_LIMIT_SEC,
                        )
                    except Exception:
                        ai_word = None

                    if not ai_word:
                        ai_word = candidates[0] if candidates else None

                    if not ai_word:
                        await channel.send(
                            "유메가 낼 단어를 못 찾았어…\n"
                            f"이번 판은 **{user.display_name}** 승리로 할게. 으헤~"
                        )
                        user_is_winner = True
                        end_reason = "ai_fail"
                        break

                    await channel.send(f"**{ai_word}**")
                    current_word = ai_word
                    used_words.add(ai_word)
                    word_history.append(ai_word)

                    last = _last_char(current_word)
                    if not _has_any_move_from_last(last, used_words):
                        await channel.send(
                            "더 이상 이을 수 있는 단어가 없어.\n"
                            "이번 판은 유메가 이겼어. 으헤~"
                        )
                        user_is_winner = False
                        end_reason = "no_moves"
                        break

                    user_turn = True
        finally:
            async with self._sessions_lock:
                self.sessions.pop(key, None)

        end_time = datetime.now(timezone.utc)
        result_text = await self._llm_practice_result(user, user_is_winner, end_reason)
        await channel.send(result_text)

        try:
            winner = user if user_is_winner else (ctx.guild.me if ctx.guild and ctx.guild.me else user)
            loser = (ctx.guild.me if ctx.guild and ctx.guild.me else user) if user_is_winner else user
            starter = user
            try:
                await self._report_match_to_admin(
                    mode="practice",
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
        except Exception:
            pass

    async def _run_pvp_game(self, ctx: commands.Context, host: discord.Member, opponent: discord.Member, *, key: Tuple[int, int]):
        channel = ctx.channel

        start_word = random.choice(self.suggestions) if self.suggestions else random.choice(list(WORDS_SET))
        used_words: Set[str] = set()
        word_history: List[str] = []
        current_word = start_word
        used_words.add(current_word)
        word_history.append(current_word)

        starter = host
        turn = host

        await channel.send(
            f"PVP 블루전 시작!\n"
            f"{host.mention} vs {opponent.mention}\n"
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

                last = _last_char(current_word)
                if not _has_any_move_from_last(last, used_words):
                    winner = turn
                    loser = opponent if turn.id == host.id else host
                    end_reason = "no_moves"
                    await channel.send(f"더 이상 이을 수 있는 단어가 없어.\n이번 판 승자는 **{winner.display_name}**!")
                    break

                turn = opponent if turn.id == host.id else host
        finally:
            async with self._sessions_lock:
                self.sessions.pop(key, None)

        if winner and loser:
            end_time = datetime.now(timezone.utc)
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

        await self._run_pvp_game(ctx, host, opponent, key=key)

    @commands.command(name="블루전연습", help="유메와 1:1 연습 블루전을 합니다.")
    async def cmd_bluewar_practice(self, ctx: commands.Context):
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

async def setup(bot: commands.Bot):
    await bot.add_cog(BlueWarCog(bot))
