from __future__ import annotations

import os
import re
import json
import time
import random
import asyncio
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple, Any

import discord
from discord.ext import commands

from config import ADMIN_NOTIFY_URL, IS_ADMIN_SITE_ENABLED
from records_core import ensure_records, load_records, save_records, add_match_record
from words_core import load_words, _normalize_word, _last_char, _allowed_first_chars, _valid_follow


def _data_path(*parts: str) -> str:
    base = os.path.join(os.path.dirname(__file__), "..", "data", "dictionary")
    return os.path.normpath(os.path.join(base, *parts))


WORDS_FILE = _data_path("blue_archive_words.txt")
SUGGEST_FILE = _data_path("suggestion.txt")

WORDS_SET: Set[str] = set()
WORDS_BY_FIRST: Dict[str, List[str]] = {}

GRAPH_CACHE_FILE = _data_path("word_graph_cache.pkl")


def load_suggestions() -> List[str]:
    if os.path.exists(SUGGEST_FILE):
        with open(SUGGEST_FILE, "r", encoding="utf-8") as f:
            items = [line.strip() for line in f if line.strip()]
        return items
    return []


def _load_or_build_graph() -> Dict[str, List[str]]:
    if os.path.exists(GRAPH_CACHE_FILE):
        try:
            with open(GRAPH_CACHE_FILE, "rb") as f:
                data = pickle.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    graph: Dict[str, List[str]] = {}
    for w in WORDS_SET:
        last = _last_char(w)
        graph.setdefault(last, [])
    for w in WORDS_SET:
        last = _last_char(w)
        graph.setdefault(last, [])
        firsts = _allowed_first_chars(last)
        for ch in firsts:
            for nxt in WORDS_BY_FIRST.get(ch, []):
                graph[last].append(nxt)

    try:
        with open(GRAPH_CACHE_FILE, "wb") as f:
            pickle.dump(graph, f)
    except Exception:
        pass

    return graph


def _has_any_move_from_last(last_char: str, used: Set[str]) -> bool:
    for ch in _allowed_first_chars(last_char):
        for w in WORDS_BY_FIRST.get(ch, []):
            if w not in used:
                return True
    return False


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
    user_turn: bool
    started_at: datetime
    stop_event: Optional[asyncio.Event] = None


class BlueWarCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.suggestions = load_suggestions()
        self.sessions: Dict[Tuple[int, int], BlueWarSession] = {}
        self._sessions_lock = asyncio.Lock()
        self._base_counts_by_first = {k: len(v) for k, v in WORDS_BY_FIRST.items()}
        self.openai = None

    def _note_event(self, event: str, user: Optional[discord.Member] = None, guild: Optional[discord.Guild] = None, weight: float = 1.0):
        try:
            ai = getattr(self.bot, "yume_ai", None)
            if ai and hasattr(ai, "core"):
                core = ai.core
                if hasattr(core, "note_event"):
                    core.note_event(event, user=user, guild=guild, weight=weight)
        except Exception:
            pass

    async def _speak_practice_result(self, user: discord.Member, user_is_winner: bool, word_history: List[str]) -> str:
        try:
            ai = getattr(self.bot, "yume_ai", None)
            if not ai or not hasattr(ai, "speaker"):
                return ""
            speaker = ai.speaker
            if not hasattr(speaker, "chat"):
                return ""
            winner = user.display_name if user_is_winner else "유메"
            loser = "유메" if user_is_winner else user.display_name
            prompt = (
                "너는 블루 아카이브 모티브의 유메(학생회장 선배)처럼 말한다.\n"
                "유저를 부를 때는 기본적으로 유저의 디스코드 표시 이름을 사용한다.\n"
                "대사는 다정하지만 약간 몽롱하고, 장난스럽게 '으헤~'를 자주 붙인다.\n"
                "너무 길게 말하지 말고 1~3문단 정도로 짧고 귀엽게 마무리한다.\n\n"
                f"연습 블루전 결과를 알려줘.\n"
                f"승자: {winner}\n"
                f"패자: {loser}\n"
                f"총 턴 수: {len(word_history)}\n"
                f"마지막 단어: {word_history[-1] if word_history else ''}\n"
            )
            text = await speaker.chat(prompt)
            return text.strip() if text else ""
        except Exception:
            return ""

    async def _report_practice_result_to_admin(
        self,
        user: discord.Member,
        user_is_winner: bool,
        word_history: List[str],
        start_time: datetime,
        end_time: datetime,
        reason: str,
    ) -> None:
        if not IS_ADMIN_SITE_ENABLED or not ADMIN_NOTIFY_URL:
            return

        payload: Dict[str, Any] = {
            "mode": "practice",
            "status": "finished",
            "starter": {"discord_id": str(user.id), "name": user.display_name},
            "winner": {"discord_id": str(user.id), "name": user.display_name} if user_is_winner else {"discord_id": "yume", "name": "유메"},
            "loser": {"discord_id": "yume", "name": "유메"} if user_is_winner else {"discord_id": str(user.id), "name": user.display_name},
            "win_gap": None,
            "total_rounds": len(word_history),
            "timestamps": {"started_at": start_time.isoformat(), "ended_at": end_time.isoformat()},
            "note": f"reason={reason}",
            "review_log": "\n".join(word_history),
            "participants": [
                {"discord_id": str(user.id), "name": user.display_name, "ai_name": None, "side": "user", "is_winner": user_is_winner, "score": None, "turns": None},
                {"discord_id": "yume", "name": "유메", "ai_name": None, "side": "ai", "is_winner": (not user_is_winner), "score": None, "turns": None},
            ],
        }

        try:
            import aiohttp

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.post(ADMIN_NOTIFY_URL, json=payload) as resp:
                    _ = await resp.text()
        except Exception:
            return

    def _channel_key(self, guild: Optional[discord.Guild], channel: discord.abc.Messageable) -> Tuple[int, int]:
        gid = guild.id if guild else 0
        cid = getattr(channel, "id", 0)
        return (gid, int(cid))

    def _can_force_stop(self, author: discord.Member | discord.User, session: BlueWarSession) -> bool:
        if author.id == session.host_id:
            return True
        if isinstance(author, discord.Member):
            try:
                perms = author.guild_permissions
                return bool(perms.administrator or perms.manage_guild or perms.manage_channels)
            except Exception:
                return False
        return False

    def _approx_opp_options(self, last_char: str) -> int:
        total = 0
        for ch in _allowed_first_chars(last_char):
            total += self._base_counts_by_first.get(ch, 0)
        return total

    def _count_moves_from_last(self, last_char: str, used: Set[str], limit: Optional[int] = None) -> int:
        c = 0
        for ch in _allowed_first_chars(last_char):
            for w in WORDS_BY_FIRST.get(ch, []):
                if w not in used:
                    c += 1
                    if limit is not None and c >= limit:
                        return c
        return c

    def _select_ai_word_minimax(self, current_word: str, used: Set[str], *, depth: int = 10, time_limit_sec: float = 15.0) -> Optional[str]:
        last = _last_char(current_word)
        deadline = time.perf_counter() + max(1.0, time_limit_sec)

        def gen_moves(last_char: str) -> List[str]:
            out: List[str] = []
            for ch in _allowed_first_chars(last_char):
                out.extend(WORDS_BY_FIRST.get(ch, []))
            if used:
                out = [w for w in out if w not in used]
            return out

        def order_moves(moves: List[str], max_keep: int) -> List[str]:
            scored = [(self._approx_opp_options(_last_char(w)), w) for w in moves]
            scored.sort(key=lambda x: x[0])
            return [w for _, w in scored[:max_keep]]

        def heuristic(last_char: str) -> int:
            return self._count_moves_from_last(last_char, used, limit=200)

        def negamax(last_char: str, d: int, alpha: int, beta: int) -> int:
            if time.perf_counter() >= deadline:
                return heuristic(last_char)
            moves = gen_moves(last_char)
            if not moves:
                return -10000 - d
            if d <= 0:
                return heuristic(last_char)

            max_keep = 20
            if d >= 8:
                max_keep = 14
            moves = order_moves(moves, max_keep=max_keep)

            best = -10**9
            for w in moves:
                if time.perf_counter() >= deadline:
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

        moves0 = gen_moves(last)
        if not moves0:
            return None

        winning = []
        others = []
        for w in moves0:
            if not _has_any_move_from_last(_last_char(w), used | {w}):
                winning.append(w)
            else:
                others.append(w)
        if winning:
            winning.sort(key=lambda w: self._approx_opp_options(_last_char(w)))
            return winning[0]

        moves0 = order_moves(others, max_keep=60)
        best_word: Optional[str] = None
        best_score = -10**9
        alpha = -10**9
        beta = 10**9

        for w in moves0:
            if time.perf_counter() >= deadline:
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

    async def _run_practice_game(self, ctx: commands.Context, user: discord.Member, game_no: int, *, key: Tuple[int, int], stop_event: asyncio.Event):
        channel = ctx.channel
        guild = ctx.guild

        start_word = random.choice(self.suggestions) if self.suggestions else random.choice(list(WORDS_SET))
        used_words: Set[str] = set()
        word_history: List[str] = []

        current_word = start_word
        used_words.add(current_word)
        word_history.append(current_word)

        s = self.sessions.get(key)
        if s is not None:
            s.start_word = start_word
            s.used = set(used_words)
            s.history = list(word_history)
            s.user_turn = True

        user_turn = True
        start_time = datetime.now(timezone.utc)
        user_is_winner = False
        end_reason = "unknown"

        try:
            await channel.send(
                f"연습 모드 시작! (#{game_no})\n"
                f"첫 단어는 **{start_word}**\n"
                f"다음 단어는 `{_last_char(start_word)}`(또는 두음법칙)로 시작해야 해."
            )

            while True:
                if stop_event.is_set():
                    end_reason = "forced_stop"
                    break

                if user_turn:
                    msg_task = asyncio.create_task(
                        self.bot.wait_for(
                            "message",
                            check=lambda m: (
                                m.author.id == user.id
                                and m.channel.id == channel.id
                                and (m.content or "").strip() != ""
                            ),
                            timeout=90.0,
                        )
                    )
                    stop_task = asyncio.create_task(stop_event.wait())
                    done, pending = await asyncio.wait({msg_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

                    for t in pending:
                        t.cancel()

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
                    if stop_event.is_set():
                        end_reason = "forced_stop"
                        break

                    ai_word: Optional[str]
                    try:
                        ai_word = await asyncio.to_thread(self._select_ai_word_minimax, current_word, used_words, depth=10, time_limit_sec=15.0)
                    except Exception:
                        ai_word = None

                    if stop_event.is_set():
                        end_reason = "forced_stop"
                        break

                    if not ai_word:
                        last = _last_char(current_word)
                        candidates: List[str] = []
                        for ch in _allowed_first_chars(last):
                            candidates.extend(list(WORDS_BY_FIRST.get(ch, [])))
                        candidates = [c for c in candidates if c not in used_words]
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

                s = self.sessions.get(key)
                if s is not None:
                    s.used = set(used_words)
                    s.history = list(word_history)
                    s.user_turn = user_turn

        finally:
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

    async def cmd_bluewar_start(self, ctx: commands.Context):
        await ctx.send("PVP 블루전은 현재 비활성화 상태야. 연습은 `!블루전연습`으로 해줘. 으헤~")

    @commands.command(name="블루전연습", help="유메와 1:1 연습 블루전을 합니다.")
    async def cmd_bluewar_practice(self, ctx: commands.Context):
        key = self._channel_key(ctx.guild, ctx.channel)

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
                user_turn=True,
                started_at=datetime.now(timezone.utc),
                stop_event=stop_event,
            )

        user = ctx.author
        game_no = int(time.time()) % 100000
        await self._run_practice_game(ctx, user, game_no, key=key, stop_event=stop_event)

    @commands.command(name="연습종료", aliases=["블루전연습종료"], help="진행 중인 블루전 연습을 강제 종료합니다.")
    async def cmd_bluewar_practice_stop(self, ctx: commands.Context):
        key = self._channel_key(ctx.guild, ctx.channel)
        s = self.sessions.get(key)
        if not s or not s.is_practice:
            await ctx.send("이 채널에서 진행 중인 연습이 없어.")
            return
        if not self._can_force_stop(ctx.author, s):
            await ctx.send("연습을 종료할 권한이 없어.")
            return
        if s.stop_event:
            s.stop_event.set()
        await ctx.send("연습 종료할게. 으헤~")

    @commands.command(name="블루전전적", help="블루전 최근 전적을 보여줍니다.")
    async def cmd_bluewar_records(self, ctx: commands.Context):
        ensure_records()
        records = load_records()
        matches = records.get("matches", [])
        if not matches:
            await ctx.send("아직 전적이 없어.")
            return

        recent = matches[-5:]
        lines = []
        for m in reversed(recent):
            mode = m.get("mode", "unknown")
            winner = m.get("winner_name", "unknown")
            loser = m.get("loser_name", "unknown")
            rounds = m.get("total_rounds", 0)
            lines.append(f"- [{mode}] {winner} vs {loser} / {rounds}턴")
        await ctx.send("최근 전적:\n" + "\n".join(lines))


async def setup(bot: commands.Bot):
    global WORDS_SET, WORDS_BY_FIRST
    WORDS_SET, WORDS_BY_FIRST = load_words(WORDS_FILE)
    await bot.add_cog(BlueWarCog(bot))
