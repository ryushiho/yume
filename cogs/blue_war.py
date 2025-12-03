# BlueWarCog: 블루전 PVP/연습, suggestion.txt 제시어, 전적/랭킹, 복기/결과 로그를 담당하는 Cog

from __future__ import annotations

import asyncio
import time
import random
import logging
import os
from collections import defaultdict
from typing import Dict, List, Set, Tuple, Optional

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

try:
    from config import (  # type: ignore
        TURN_TIMEOUT,
        REVIEW_CHANNEL_ID,
        RESULT_CHANNEL_ID,
        RANK_CHANNEL_ID,
    )
except Exception:
    TURN_TIMEOUT: float = 20.0
    REVIEW_CHANNEL_ID: int = 0
    RESULT_CHANNEL_ID: int = 0
    RANK_CHANNEL_ID: int = 0
    logger.warning(
        "config.py 에 TURN_TIMEOUT / REVIEW_CHANNEL_ID / RESULT_CHANNEL_ID / "
        "RANK_CHANNEL_ID 가 없어 기본값을 사용합니다."
    )

from words_core import WORDS_SET, WORDS_BY_FIRST, exists_follow_word
from records_core import load_records, save_records

SUGGESTION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "dictionary",
    "suggestion.txt",
)

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


def get_allowed_starts(ch: str) -> Set[str]:
    s: Set[str] = {ch}
    if ch in DOOUM_MAP:
        s |= DOOUM_MAP[ch]
    return s


async def wait_for_player_word(
    bot: commands.Bot,
    channel: discord.TextChannel,
    player: discord.Member,
    required_char: str,
    used_words: Set[str],
    timeout: float = TURN_TIMEOUT,
):
    def check(msg: discord.Message):
        return msg.channel == channel and msg.author == player

    deadline = time.monotonic() + timeout
    allowed_starts = get_allowed_starts(required_char)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, None, "timeout"

        try:
            msg: discord.Message = await bot.wait_for("message", check=check, timeout=remaining)
        except asyncio.TimeoutError:
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
            continue

        word = content

        if len(word) < 2:
            await channel.send("한 글자 단어는 안 돼. 두 글자 이상으로 해줘!")
            continue

        if word not in WORDS_SET:
            await channel.send(f"**{word}** 는 유메 단어 목록에 없는 단어야. 다른 걸 써봐!")
            continue

        if word in used_words:
            await channel.send(f"**{word}** 는 이미 나온 단어야. 새 걸로 도전해줘!")
            continue

        if word[0] not in allowed_starts:
            if len(allowed_starts) == 1:
                await channel.send(
                    f"**{word}** 는 `{required_char}`(으)로 시작 안 하잖아. 다시 생각해봐!"
                )
            else:
                starts_str = "/".join(sorted(allowed_starts))
                await channel.send(
                    f"**{word}** 는 `{starts_str}` 중 하나로 시작해야 해."
                )
            continue

        return True, word, None


class BlueWarCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_channels: Set[int] = set()
        self.join_sessions: Dict[int, Dict] = {}
        self.records: Dict[int, Dict] = defaultdict(lambda: {"win": 0, "loss": 0})
        self._load_records_from_file()
        self.mood: int = 0
        self.game_counter: int = 0
        self.rank_message_id: Optional[int] = None
        self.suggestion_words: List[str] = []
        self._load_suggestions()

    def _load_suggestions(self):
        try:
            with open(SUGGESTION_FILE, "r", encoding="utf-8") as f:
                words = [line.strip() for line in f if line.strip()]
            self.suggestion_words = words
            print(f"[INFO] 블루전 제시어 {len(words)}개 로드 완료 (suggestion.txt).")
        except Exception as e:
            logger.warning("suggestion.txt 로드 실패: %s", e)
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
        print(f"[INFO] 전적 {len(self.records)}명 로드 완료.")

    def _save_all_records(self):
        data: Dict[str, Dict] = {}
        for uid, rec in self.records.items():
            entry: Dict[str, object] = {
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

    def _format_record_basic(self, user: discord.Member) -> str:
        w, l, rate, _diff = self._get_stats(user.id)
        return f"{w}승 {l}패 (승률 {rate:.1f}%)"

    def _bot_name(self) -> str:
        return self.bot.user.display_name if self.bot.user else "유메"

    def _mood_suffix_on_win(self) -> str:
        if self.mood >= 2:
            return " 흐흥, 이런 건 기본이지."
        if self.mood <= -2:
            return " ...이런 거라도 이겨야지."
        return ""

    def _mood_suffix_on_lose(self) -> str:
        if self.mood >= 2:
            return " 뭐, 가끔 져주는 것도 필요하니까?"
        if self.mood <= -2:
            return " 아, 또 졌네... 오늘 컨디션 진짜 별로야."
        return ""

    def _build_rank_text_for_guild(self, guild: Optional[discord.Guild]) -> str:
        if guild is None:
            return "이건 서버에서만 쓸 수 있어."

        entries: List[Tuple[str, int, int, float, int]] = []
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

        mood = self.mood
        if mood >= 2:
            header = "랭킹이야. 잘 보고 더 높은 곳으로 올라와 봐~"
        elif mood <= -2:
            header = "솔직히 컨디션은 별로지만, 랭킹 정리 정도는 해줄게."
        else:
            header = "현재 블루전 랭킹은 이 정도야."

        lines: List[str] = [header, ""]
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

    async def _post_game_logs(
        self,
        game_no: int,
        winner: discord.Member,
        loser: discord.Member,
        word_history: List[str],
        game_channel: Optional[discord.TextChannel],
    ):
        words_line = " ".join(word_history)

        if REVIEW_CHANNEL_ID:
            review_ch = self.bot.get_channel(REVIEW_CHANNEL_ID)
            if isinstance(review_ch, discord.TextChannel):
                header1 = f"Game No.{game_no:02d} 복기"
                header2 = (
                    f"[GAME {game_no:02d} - Win : {winner.display_name} / "
                    f"Loss : {loser.display_name}]"
                )
                await review_ch.send(f"{header1}\n{header2}\n{words_line}")

        result_text = (
            f"[GAME {game_no:02d} 결과 발표!]\n"
            f"Win! : {winner.display_name} / Loss.. : {loser.display_name}"
        )

        if RESULT_CHANNEL_ID:
            result_ch = self.bot.get_channel(RESULT_CHANNEL_ID)
            if isinstance(result_ch, discord.TextChannel):
                await result_ch.send(result_text)

        if isinstance(game_channel, discord.TextChannel):
            await game_channel.send(result_text)

    async def _run_blue_pvp(
        self,
        channel: discord.TextChannel,
        p1: discord.Member,
        p2: discord.Member,
    ):
        self.active_channels.add(channel.id)

        try:
            self.game_counter += 1
            game_no = self.game_counter

            start_word = self._choose_start_word()
            used_words: Set[str] = {start_word}
            current_word = start_word
            required_char = current_word[-1]
            word_history: List[str] = [start_word]

            players = [p1, p2]
            random.shuffle(players)
            current_index = 0
            last_move = "아직 아무도 내지 않았어."

            embed = discord.Embed(
                title=f"🔵 블루전 GAME {game_no:02d} 준비",
                description=(
                    f"플레이어: **{players[0].display_name}** vs **{players[1].display_name}**\n"
                    f"이번 판 제시어는 **{start_word}** 야.\n"
                    "5초 뒤에 첫 턴을 시작할게. 숨 한 번 고르고 와."
                ),
                color=discord.Color.blue(),
            )
            await channel.send(embed=embed)
            await asyncio.sleep(5)

            status_msg = await channel.send("블루전 정보를 준비하는 중이야...")

            game_over = False
            winner: Optional[discord.Member] = None
            loser: Optional[discord.Member] = None

            while not game_over:
                player = players[current_index]
                other = players[1 - current_index]

                status_content = (
                    "🔵 **블루전 (User vs User)**\n"
                    f"플레이어: **{players[0].display_name}** vs **{players[1].display_name}**\n"
                    f"현재 제시어: **{current_word}**\n"
                    f"이어야 하는 글자: `{required_char}`\n"
                    f"이번 차례: **{player.display_name}**\n"
                    f"마지막 한 수: {last_move}\n"
                    f"턴 제한: **{TURN_TIMEOUT}초**\n"
                    f"GAME No.{game_no:02d}"
                )
                await status_msg.edit(content=status_content)

                allowed_starts = get_allowed_starts(required_char)

                if not any(exists_follow_word(ch, used_words) for ch in allowed_starts):
                    loser = player
                    winner = other
                    game_over = True
                    result_text = (
                        status_content
                        + "\n\n"
                        + f"제시어 **{current_word}** 뒤로는 "
                          f"`{required_char}`(과 두음법칙 적용 음절)으로 시작하는 단어가 더 이상 없어.\n"
                          f"**{player.display_name}** 이(가) 이어갈 수 없으니까 패배야..."
                    )
                    await status_msg.edit(content=result_text)
                    break

                success, word, reason = await wait_for_player_word(
                    self.bot, channel, player, required_char, used_words, timeout=TURN_TIMEOUT
                )

                if not success:
                    loser = player
                    winner = other
                    game_over = True

                    if reason == "surrender":
                        result_text = (
                            status_content
                            + "\n\n"
                            + f"🏳 **{player.display_name}** 이(가) 항복했어.\n"
                              f"이번 판 승자는 **{other.display_name}**!"
                        )
                    else:
                        result_text = (
                            status_content
                            + "\n\n"
                            + f"⏰ **{player.display_name}** 이(가) 시간 안에 못 썼네.\n"
                              f"**{other.display_name}** 의 승리!"
                        )

                    await status_msg.edit(content=result_text)
                    break

                prev_word = current_word
                current_word = word  # type: ignore[assignment]
                used_words.add(current_word)
                required_char = current_word[-1]
                last_move = f"{player.display_name} → **{current_word}**"
                word_history.append(current_word)

                await channel.send(
                    f"제시어: **{prev_word}** → **{current_word}** (by {player.display_name})"
                )

                current_index = 1 - current_index

            if winner is not None and loser is not None:
                self._update_record(winner, loser)
                win_rec = self._format_record_basic(winner)
                lose_rec = self._format_record_basic(loser)

                final_content = (
                    f"{status_msg.content}\n\n"
                    "⚪ **게임 끝!**\n"
                    f"Win!  : **{winner.display_name}** ({win_rec})\n"
                    f"Loss.. : **{loser.display_name}** ({lose_rec})"
                )
                await status_msg.edit(content=final_content)

                await self._post_game_logs(game_no, winner, loser, word_history, channel)
                await self._update_rank_message(channel.guild)
            else:
                await status_msg.edit(
                    content=f"{status_msg.content}\n\n결과 정리하다가 뭐가 꼬인 것 같아... 버그일지도?"
                )
        finally:
            self.active_channels.discard(channel.id)

    async def _run_blue_practice(self, channel: discord.TextChannel, user: discord.Member):
        self.active_channels.add(channel.id)

        try:
            start_word = self._choose_start_word()
            used_words: Set[str] = {start_word}
            current_word = start_word
            required_char = current_word[-1]

            bot_name = self._bot_name()

            embed = discord.Embed(
                title="🔵 블루전 연습 모드 준비",
                description=(
                    f"플레이어: **{user.display_name}** vs **{bot_name}**\n"
                    f"이번 판 제시어는 **{start_word}** 야.\n"
                    "5초 뒤에 연습을 시작할게. 전적은 안 남으니까 편하게 해도 돼~"
                ),
                color=discord.Color.blue(),
            )
            await channel.send(embed=embed)
            await asyncio.sleep(5)

            await channel.send(
                "🔵 **블루전 연습 모드 (User vs 유메)** 스타트!\n"
                f"처음 제시어는 **{start_word}**.\n"
                f"턴 제한은 **{TURN_TIMEOUT}초**이고, `!항복`이나 `gg`로 포기할 수도 있어."
            )

            players = ["user", "bot"]
            turn_index = 1

            game_over = False
            winner_name: Optional[str] = None
            loser_name: Optional[str] = None

            while not game_over:
                side = players[turn_index]
                other_side = players[1 - turn_index]

                if side == "user":
                    current_player_name = user.display_name
                else:
                    current_player_name = bot_name

                if other_side == "user":
                    other_player_name = user.display_name
                else:
                    other_player_name = bot_name

                allowed_starts = get_allowed_starts(required_char)

                if not any(exists_follow_word(ch, used_words) for ch in allowed_starts):
                    loser_name = current_player_name
                    winner_name = other_player_name
                    await channel.send(
                        f"제시어 **{current_word}** 뒤로는 "
                        f"`{required_char}`(과 두음법칙 적용 음절)으로 시작하는 단어가 더 이상 없어.\n"
                        f"**{current_player_name}** 이(가) 이어갈 수 없어서 패배야..."
                    )
                    game_over = True
                    break

                if side == "user":
                    await channel.send(
                        f"🔔 **{user.display_name}** 차례야!\n"
                        f"제시어: **{current_word}**\n"
                        f"`{required_char}`(또는 두음법칙 적용 음절)으로 시작하는 단어를 "
                        f"**{TURN_TIMEOUT}초** 안에 보내줘!\n"
                        "(포기하고 싶으면 `!항복`, `gg`, `!gg` 중 하나를 적어줘.)"
                    )

                    success, word, reason = await wait_for_player_word(
                        self.bot, channel, user, required_char, used_words, timeout=TURN_TIMEOUT
                    )

                    if not success:
                        loser_name = user.display_name
                        winner_name = bot_name
                        self.mood = min(self.mood + 1, 3)

                        if reason == "surrender":
                            extra = self._mood_suffix_on_win()
                            await channel.send(
                                f"🏳 **{user.display_name}** 이(가) 항복했네.\n"
                                f"이번 판은 **{bot_name}** 의 승리야.{extra}"
                            )
                        else:
                            extra = self._mood_suffix_on_win()
                            await channel.send(
                                f"⏰ **{user.display_name}** 이(가) 시간 초과!\n"
                                f"이번 판은 **{bot_name}** 의 승리네.{extra}"
                            )

                        game_over = True
                        break

                    prev_word = current_word
                    current_word = word  # type: ignore[assignment]
                    used_words.add(current_word)
                    required_char = current_word[-1]

                    await channel.send(
                        f"제시어: **{prev_word}** → **{current_word}** (by {user.display_name})"
                    )
                else:
                    await channel.send(
                        f"🔔 이번엔 **{bot_name}** 차례야.\n"
                        f"제시어 **{current_word}**... 유메도 한 번 이어볼게."
                    )

                    await asyncio.sleep(random.randint(5, 10))

                    candidate_words: List[str] = []
                    for ch in allowed_starts:
                        candidate_words.extend(WORDS_BY_FIRST.get(ch, []))
                    candidates = [w for w in candidate_words if w not in used_words]

                    if not candidates:
                        loser_name = bot_name
                        winner_name = user.display_name
                        self.mood = max(self.mood - 1, -3)
                        extra = self._mood_suffix_on_lose()
                        await channel.send(
                            f"제시어 **{current_word}** 뒤로는 "
                            f"`{required_char}`(과 두음법칙 적용 음절)으로 시작하는 단어가 더 이상 없네...\n"
                            f"이번엔 내가 졌어. **{user.display_name}** 승리!{extra}"
                        )
                        game_over = True
                        break

                    bot_word = random.choice(candidates)
                    prev_word = current_word
                    current_word = bot_word
                    used_words.add(bot_word)
                    required_char = current_word[-1]

                    await channel.send(
                        f"제시어: **{prev_word}** → **{current_word}** (by {bot_name})"
                    )

                turn_index = 1 - turn_index

            await channel.send("⚪ **연습 게임 끝!**")

            if winner_name and loser_name:
                await channel.send(
                    f"Win!  : **{winner_name}**\n"
                    f"Loss.. : **{loser_name}**\n"
                    "(연습이라 전적은 안 남겨 둘게.)"
                )
        finally:
            self.active_channels.discard(channel.id)

    async def _start_blue_session(self, channel: discord.TextChannel, author: discord.Member):
        if channel.id in self.active_channels:
            await channel.send(
                "여긴 이미 블루전이나 연습 중이야. 이 판 끝내고 다시 시작하자."
            )
            return

        if channel.id in self.join_sessions:
            await channel.send(
                "이 채널에서는 이미 블루전 참가자 모으는 중이야. "
                "지금 모집이랑 섞이면 유메 머리가 꼬여, 으헤에~"
            )
            return

        session = {
            "host_id": author.id,
            "participants": {author.id},
        }
        self.join_sessions[channel.id] = session

        embed = discord.Embed(
            title="🔵 블루전 참가자 모집",
            description=(
                "블루전 준비 중이야.\n"
                "아래 **참가** 버튼을 눌러서 들어와 줘.\n"
                "모든 경기는 **1:1 대전**으로 진행돼."
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="모집자",
            value=author.display_name,
            inline=True,
        )
        embed.add_field(
            name="모집 시간",
            value="최대 5분 (300초)\n※ 첫 참가자가 들어오면 바로 시작할 수도 있어.",
            inline=True,
        )
        embed.set_footer(text="참가 버튼으로 들어왔다가, 다시 누르면 취소야~")

        view = BlueJoinView(self, channel.id)
        msg = await channel.send(embed=embed, view=view)

        session["message_id"] = msg.id

    async def _finish_join_session(self, channel_id: int):
        if channel_id not in self.join_sessions:
            return

        session = self.join_sessions.pop(channel_id, None)
        channel = self.bot.get_channel(channel_id)

        if not isinstance(channel, discord.TextChannel):
            return

        message = None
        msg_id = session.get("message_id") if session else None
        if msg_id is not None:
            try:
                message = await channel.fetch_message(msg_id)
            except discord.NotFound:
                message = None

        if message is not None:
            try:
                await message.edit(view=None)
            except Exception:
                pass

        if not session:
            await channel.send("모집 정보가 사라져서, 이번 블루전은 취소할게.")
            return

        participant_ids = list(session.get("participants", set()))
        host_id = session.get("host_id")

        if len(participant_ids) < 2:
            await channel.send(
                "5분 동안 2명 이상이 모이지 않아서, 이번 블루전은 취소할게.\n"
                "다음에 여유 있을 때 다시 불러줘."
            )
            return

        guild = channel.guild
        if guild is None:
            await channel.send("여긴 서버가 아니라서 블루전을 진행할 수 없어.")
            return

        members: List[discord.Member] = []
        for uid in participant_ids:
            m = guild.get_member(uid)
            if m is not None:
                members.append(m)

        if len(members) < 2:
            await channel.send(
                "참가자 정보를 제대로 못 찾았어. 이번 판은 취소하고 다음에 다시 해보자."
            )
            return

        host_member = None
        for m in members:
            if m.id == host_id:
                host_member = m
                break

        if host_member is not None and len(members) >= 2:
            others = [m for m in members if m.id != host_id]
            opponent = random.choice(others)
            p1, p2 = host_member, opponent
        else:
            p1, p2 = random.sample(members, 2)

        await channel.send(
            "⏰ 참가자 모집 종료!\n"
            f"이번 판은 **{p1.display_name}** vs **{p2.display_name}** 로 가볼게.\n"
            "제시어는 유메가 골라둘 테니까, 준비되면 바로 시작이야."
        )

        await self._run_blue_pvp(channel, p1, p2)

    @commands.hybrid_command(name="블루전랭킹", description="현재 블루전 랭킹을 보여줄게.")
    async def blue_war_rank(self, ctx: commands.Context):
        guild = ctx.guild

        await self._update_rank_message(guild)

        rank_ch = self.bot.get_channel(RANK_CHANNEL_ID) if RANK_CHANNEL_ID else None
        if isinstance(rank_ch, discord.TextChannel):
            notice = (
                f"랭킹 채널 {rank_ch.mention} 기준으로 갱신해 뒀어.\n"
                "상세한 순위는 거기에서 확인해줘."
            )
        else:
            notice = "랭킹 채널 설정이 애매해서, 일단 내부 데이터만 갱신해 뒀어."

        if ctx.interaction:
            await ctx.send(notice, ephemeral=False)
        else:
            await ctx.send(notice)

    @commands.hybrid_command(name="블루전전적", description="블루전 전적을 보여줄게.")
    async def blue_war_record(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        target = member or ctx.author
        w, l, rate, diff = self._get_stats(target.id)

        if w + l == 0:
            if target.id == ctx.author.id:
                text = f"{target.display_name} 전적은 아직 없어. 한 판부터 찍어보자?"
            else:
                text = f"{target.display_name} 전적은 아직 없는 것 같아."
            if ctx.interaction:
                await ctx.send(text, ephemeral=True)
            else:
                await ctx.send(text)
            return

        mood = self.mood
        if target.id == ctx.author.id:
            if mood >= 2:
                flavor = "요즘 제법 하는데? 계속 이렇게만 가면 되겠다."
            elif mood <= -2:
                flavor = "음... 더 올라가고 싶으면 연습 조금 더 해야겠는걸."
            else:
                flavor = "대충 이런 느낌이야. 기분 내키면 더 올려보자."
        else:
            if mood >= 2:
                flavor = "상대가 이 정도라면, 이기는 그림도 그려지는데?"
            elif mood <= -2:
                flavor = "만만하진 않은데, 못 이길 상대도 아니야."
            else:
                flavor = "이 정도 실력이라고 보면 될 것 같아."

        text = (
            f"**{target.display_name}** 의 블루전 전적이야.\n"
            f"- 승   : {w}회\n"
            f"- 패   : {l}회\n"
            f"- 승률 : {rate:.1f}%\n"
            f"- 승차 : {diff}\n"
            f"{flavor}"
        )

        if ctx.interaction:
            await ctx.send(text, ephemeral=False)
        else:
            await ctx.send(text)

    @commands.hybrid_command(name="블루전시작", description="블루전(User vs User)을 시작할게.")
    async def blue_war_start(self, ctx: commands.Context):
        if ctx.interaction:
            await ctx.send(
                "블루전 모집 안내를 이 채널에 올려둘게. 참가자가 들어오면 5초 후 시작할 수도 있어.",
                ephemeral=True,
            )
        await self._start_blue_session(ctx.channel, ctx.author)

    @commands.hybrid_command(name="블루전연습", description="블루전 연습 모드(User vs 유메)를 시작할게.")
    async def blue_war_practice(self, ctx: commands.Context):
        channel = ctx.channel
        if channel.id in self.active_channels:
            await ctx.send("여긴 이미 블루전 중이야. 끝나고 연습하자.")
            return
        if channel.id in self.join_sessions:
            await ctx.send("지금은 블루전 참가자 모집 중이야. 모집 끝나고 연습하자.")
            return
        if ctx.interaction:
            await ctx.send("연습 모드 켤게. 채널에서 같이 놀자!", ephemeral=True)
        await self._run_blue_practice(channel, ctx.author)


class BlueJoinView(discord.ui.View):
    def __init__(self, cog: BlueWarCog, channel_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel_id = channel_id

    @discord.ui.button(label="참가", style=discord.ButtonStyle.primary)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "여긴 채널이 좀 이상해서… 유메가 참가를 처리하기 힘들어.",
                ephemeral=True,
            )
            return

        session = self.cog.join_sessions.get(self.channel_id)
        if session is None:
            await interaction.response.send_message(
                "이미 모집이 끝났거나 취소된 블루전이야.",
                ephemeral=True,
            )
            return

        user_id = interaction.user.id
        host_id = session.get("host_id")
        participants: Set[int] = session.setdefault("participants", set())

        if user_id == host_id and user_id in participants:
            await interaction.response.send_message(
                "모집자는 참가를 취소할 수 없어. 대신 게임 마감은 할 수 있어.",
                ephemeral=True,
            )
            return

        just_joined = False

        if user_id in participants:
            participants.remove(user_id)
            await interaction.response.send_message(
                "블루전 참가를 취소해 둘게.",
                ephemeral=True,
            )
        else:
            participants.add(user_id)
            just_joined = True
            await interaction.response.send_message(
                "블루전에 참가 접수해 둘게. 누구랑 붙게 될지 기대해봐, 으헤~",
                ephemeral=True,
            )

        if just_joined and len(participants) >= 2:
            guild = interaction.guild
            if guild is None:
                return

            self.cog.join_sessions.pop(self.channel_id, None)
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass

            members: List[discord.Member] = []
            for uid in participants:
                m = guild.get_member(uid)
                if m is not None:
                    members.append(m)

            if len(members) < 2:
                await channel.send(
                    "참가자 정보를 제대로 못 찾았어. 이번 판은 취소하고 다음에 다시 해보자."
                )
                return

            host_member = None
            for m in members:
                if m.id == host_id:
                    host_member = m
                    break

            if host_member is not None:
                opponent = None
                for m in members:
                    if m.id != host_id:
                        opponent = m
                        break
                if opponent is None:
                    await channel.send(
                        "참가자가 한 명뿐이라, 이번 판은 취소해야겠어."
                    )
                    return
                p1, p2 = host_member, opponent
            else:
                p1, p2 = random.sample(members, 2)

            await channel.send(
                f"첫 참가자가 들어왔으니까 바로 시작해 볼까?\n"
                f"이번 판은 **{p1.display_name}** vs **{p2.display_name}** 매치야."
            )

            await self.cog._run_blue_pvp(channel, p1, p2)

    @discord.ui.button(label="마감하기", style=discord.ButtonStyle.secondary)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "여긴 채널이 좀 이상해서… 유메가 처리를 못 하겠어.",
                ephemeral=True,
            )
            return

        session = self.cog.join_sessions.get(self.channel_id)
        if session is None:
            await interaction.response.send_message(
                "이미 모집이 끝났거나 취소된 블루전이야.",
                ephemeral=True,
            )
            return

        host_id = session.get("host_id")
        if interaction.user.id != host_id:
            await interaction.response.send_message(
                "참가 모집을 마감할 수 있는 건 모집자뿐이야.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "참가자 모집을 마감해 둘게.",
            ephemeral=True,
        )

        self.cog.join_sessions.pop(self.channel_id, None)
        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass

        participants: Set[int] = session.get("participants", set())

        await channel.send(
            f"**{interaction.user.display_name}** 이(가) 이번 블루전 참가 모집을 마감했어.\n"
            "유메가 이제부터 진행을 맡을게~"
        )

        if len(participants) < 2:
            await channel.send(
                "인원이 2명 미만이라, 이번 판은 취소할게.\n"
                "나중에 더 모였을 때 다시 불러줘."
            )
            return

        guild = channel.guild
        if guild is None:
            await channel.send("여긴 서버가 아니라서 블루전을 진행할 수 없어.")
            return

        members: List[discord.Member] = []
        for uid in participants:
            m = guild.get_member(uid)
            if m is not None:
                members.append(m)

        if len(members) < 2:
            await channel.send(
                "참가자 정보를 제대로 못 찾았어. 이번 판은 취소하고 다음에 다시 해보자."
            )
            return

        host_member = None
        for m in members:
            if m.id == host_id:
                host_member = m
                break

        if host_member is not None and len(members) >= 2:
            others = [m for m in members if m.id != host_id]
            opponent = random.choice(others)
            p1, p2 = host_member, opponent
        else:
            p1, p2 = random.sample(members, 2)

        await channel.send(
            f"이번 판은 **{p1.display_name}** vs **{p2.display_name}** 로 진행할게.\n"
            "제시어는 유메가 골라둘 테니까, 잠깐만 기다려."
        )

        await self.cog._run_blue_pvp(channel, p1, p2)

    async def on_timeout(self):
        await self.cog._finish_join_session(self.channel_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(BlueWarCog(bot))
