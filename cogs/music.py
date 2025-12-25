from __future__ import annotations

import asyncio
import logging
import os
import json
import time
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import discord
from discord.ext import commands
import yt_dlp
import aiohttp

logger = logging.getLogger(__name__)


# ==============================
# 패널 고정 설정 저장소
# ==============================

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STORAGE_DIR = os.path.join(ROOT_DIR, "data", "storage")
PANEL_CFG_PATH = os.path.join(STORAGE_DIR, "music_panel.json")


# ==============================
# YouTube / ffmpeg 설정
# ==============================
#
# ⚠️ 재생이 안 되는 주요 원인:
# - yt_dlp 검색 결과(entry)에서 entry["url"]을 바로 쓰면 "직접 스트림 URL"이 아닌 경우가 많다.
# - 그래서 "검색/추가 단계"에서는 webpage_url만 확보하고,
#   "재생 직전"에 webpage_url로 다시 extract 해서 bestaudio 스트림 URL을 해상(resolution)한다.
#
# 이 구조가 실서버에서 가장 안정적이다.

YTDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "default_search": "ytsearch",
    "noplaylist": True,
    "nocheckcertificate": True,
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"
FFMPEG_EXECUTABLE = os.getenv("YUME_FFMPEG_PATH", "ffmpeg")

_ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)


# ==============================
# 내부 데이터 구조
# ==============================

@dataclass
class _Track:
    title: str
    webpage_url: str
    requester_id: Optional[int] = None

    # 재생 직전에 해상한 실제 스트림 URL (짧은 시간만 유효할 수 있어서 캐시하되 과신 금지)
    _resolved_stream_url: Optional[str] = None
    _resolved_at: float = 0.0


async def _extract_info(query: str) -> dict:
    loop = asyncio.get_running_loop()

    def _run():
        return _ytdl.extract_info(query, download=False)

    return await loop.run_in_executor(None, _run)


def _pick_entry(info: dict) -> dict:
    if not info:
        return {}
    if "entries" in info and isinstance(info["entries"], list):
        for e in info["entries"]:
            if e:
                return e
        return {}
    return info


def _select_best_audio_url(entry: dict) -> Optional[str]:
    """
    yt_dlp 결과(entry)에서 ffmpeg가 재생 가능한 bestaudio URL을 고른다.
    """
    # 1) formats에서 audio-only 후보 선별
    formats = entry.get("formats") or []
    audio_only = []
    for f in formats:
        try:
            if not f:
                continue
            if f.get("url") is None:
                continue
            # audio-only
            if f.get("vcodec") != "none":
                continue
            if f.get("acodec") in (None, "none"):
                continue
            audio_only.append(f)
        except Exception:
            continue

    # 2) 품질(abr/tbr)을 기준으로 best 선택
    def _score(f: dict) -> Tuple[float, float]:
        abr = f.get("abr")
        tbr = f.get("tbr")
        bitrate = float(abr if abr is not None else (tbr if tbr is not None else 0.0))
        fs = f.get("filesize") or f.get("filesize_approx") or 0
        return (bitrate, float(fs))

    if audio_only:
        best = max(audio_only, key=_score)
        return str(best.get("url"))

    # 3) fallback: entry["url"] (가끔 여기만 있는 경우)
    url = entry.get("url")
    if url:
        return str(url)

    return None


def _ffmpeg_source(stream_url: str, volume: float) -> discord.AudioSource:
    src = discord.FFmpegPCMAudio(
        stream_url,
        executable=FFMPEG_EXECUTABLE,
        before_options=FFMPEG_BEFORE,
        options=FFMPEG_OPTIONS,
    )
    return discord.PCMVolumeTransformer(src, volume=volume)


class MusicState:
    def __init__(self):
        self.queue: asyncio.Queue[_Track] = asyncio.Queue()
        self.now_playing: Optional[_Track] = None
        self.player_task: Optional[asyncio.Task] = None

        # 길드별 큐/상태 조작 보호
        self.lock: asyncio.Lock = asyncio.Lock()

        # 자동 퇴장(유메만 남았을 때) 예약 태스크
        self.auto_leave_task: Optional[asyncio.Task] = None

        # 0~2.0 (0~200%)
        self.volume: float = 1.0
        self.loop_all: bool = False

        # 버튼 액션으로 트랙을 멈췄을 때(스킵/정지) 루프 재큐잉을 한 번 막는다.
        self._suppress_requeue_once: bool = False

        # 마지막 오류(패널에 짧게 표시)
        self.last_error: Optional[str] = None
        self.last_error_at: float = 0.0

        # 패널 메시지(서버 설정이 없을 때 임시로 사용)
        self.temp_panel_channel_id: Optional[int] = None
        self.temp_panel_message_id: Optional[int] = None


# ==============================
# UI (패널 / 버튼)
# ==============================

class YouTubeAddModal(discord.ui.Modal):
    def __init__(self, cog: "MusicCog"):
        super().__init__(title="🔴 YouTube 추가")
        self.cog = cog

        self.query = discord.ui.TextInput(
            label="검색어 또는 URL",
            placeholder="예: Blue Archive OST / https://youtu.be/...",
            required=True,
            max_length=200,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        q = (self.query.value or "").strip()
        await self.cog._enqueue_from_interaction(interaction, q)


class SpotifyAddModal(discord.ui.Modal):
    def __init__(self, cog: "MusicCog"):
        super().__init__(title="🟢 Spotify 추가")
        self.cog = cog

        self.query = discord.ui.TextInput(
            label="Spotify 트랙 URL 또는 검색어",
            placeholder="예: https://open.spotify.com/track/...",
            required=True,
            max_length=200,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        q = (self.query.value or "").strip()
        await self.cog._enqueue_spotify_from_interaction(interaction, q)


class VolumeModal(discord.ui.Modal):
    def __init__(self, cog: "MusicCog", current_percent: int):
        super().__init__(title="🔊 음량 설정")
        self.cog = cog

        self.value = discord.ui.TextInput(
            label="0~200 (기본 100)",
            placeholder=str(current_percent),
            required=True,
            max_length=3,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.value.value or "").strip()
        await self.cog._set_volume_from_interaction(interaction, raw)


class MusicPanelView(discord.ui.View):
    """패널은 재부팅 이후에도 버튼이 살아있도록(퍼시스턴트) timeout=None로 유지."""

    def __init__(self, cog: "MusicCog"):
        super().__init__(timeout=None)
        self.cog = cog

    # 🔴 YouTube 추가 (빨간색)
    @discord.ui.button(
        label="YouTube",
        style=discord.ButtonStyle.danger,
        emoji="🔴",
        custom_id="yume_music_add_yt",
        row=0,
    )
    async def youtube_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await interaction.response.send_modal(YouTubeAddModal(self.cog))

    # 🟢 Spotify 추가 (연두색)
    @discord.ui.button(
        label="Spotify",
        style=discord.ButtonStyle.success,
        emoji="🟢",
        custom_id="yume_music_add_sp",
        row=0,
    )
    async def spotify_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await interaction.response.send_modal(SpotifyAddModal(self.cog))

    # ⏯ 재생/일시정지
    @discord.ui.button(
        label="재생/일시정지",
        style=discord.ButtonStyle.secondary,
        emoji="⏯",
        custom_id="yume_music_toggle",
        row=0,
    )
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_pause(interaction)

    # ⏭ 스킵
    @discord.ui.button(
        label="스킵",
        style=discord.ButtonStyle.secondary,
        emoji="⏭",
        custom_id="yume_music_skip",
        row=0,
    )
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._skip(interaction)

    # 🔊 음량 모달
    @discord.ui.button(
        label="음량",
        style=discord.ButtonStyle.secondary,
        emoji="🔊",
        custom_id="yume_music_volume",
        row=0,
    )
    async def volume_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        if interaction.guild is None:
            return
        st = self.cog._state(interaction.guild.id)
        await interaction.response.send_modal(VolumeModal(self.cog, int(st.volume * 100)))

    # 🔁 반복 토글
    @discord.ui.button(
        label="반복",
        style=discord.ButtonStyle.secondary,
        emoji="🔁",
        custom_id="yume_music_loop",
        row=1,
    )
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_loop(interaction)

    # 🔀 셔플
    @discord.ui.button(
        label="셔플",
        style=discord.ButtonStyle.secondary,
        emoji="🔀",
        custom_id="yume_music_shuffle",
        row=1,
    )
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._shuffle(interaction)

    # ⏹ 정지
    @discord.ui.button(
        label="정지",
        style=discord.ButtonStyle.danger,
        emoji="⏹",
        custom_id="yume_music_stop",
        row=1,
    )
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._stop(interaction)

    # 🧰 큐 관리
    @discord.ui.button(
        label="큐 관리",
        style=discord.ButtonStyle.secondary,
        emoji="🧰",
        custom_id="yume_music_queue",
        row=1,
    )
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._open_queue_manage(interaction)



class QueueDeleteModal(discord.ui.Modal):
    title = "큐 삭제"

    def __init__(self, cog: "MusicCog"):
        super().__init__(timeout=180)
        self.cog = cog
        self.target = discord.ui.TextInput(
            label="삭제할 번호(들)",
            placeholder="예) 3  |  3,5,7  |  2-6",
            required=True,
            max_length=100,
        )
        self.add_item(self.target)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._queue_delete_from_modal(interaction, str(self.target.value))


class QueuePriorityModal(discord.ui.Modal):
    title = "맨 위로 올리기"

    def __init__(self, cog: "MusicCog"):
        super().__init__(timeout=180)
        self.cog = cog
        self.target = discord.ui.TextInput(
            label="맨 위로 올릴 번호",
            placeholder="예) 2",
            required=True,
            max_length=10,
        )
        self.add_item(self.target)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._queue_priority_from_modal(interaction, str(self.target.value))


class QueueManageView(discord.ui.View):
    """큐 관리(토글 메뉴)."""

    def __init__(self, cog: "MusicCog"):
        super().__init__(timeout=None)
        self.cog = cog

    # 🔀 큐 셔플
    @discord.ui.button(
        label="큐 셔플",
        style=discord.ButtonStyle.secondary,
        emoji="🔀",
        custom_id="yume_music_q_shuffle",
        row=0,
    )
    async def q_shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._queue_manage_shuffle(interaction)

    # 🗑️ 큐 삭제(번호 입력)
    @discord.ui.button(
        label="큐 삭제",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="yume_music_q_delete",
        row=0,
    )
    async def q_delete(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        try:
            await interaction.response.send_modal(QueueDeleteModal(self.cog))
        except Exception:
            # modal 실패 시 안내
            try:
                await interaction.response.send_message("지금은 입력창을 열 수 없어…", ephemeral=True)
            except Exception:
                pass

    # ⏫ 맨 위로
    @discord.ui.button(
        label="맨 위로",
        style=discord.ButtonStyle.secondary,
        emoji="⏫",
        custom_id="yume_music_q_priority",
        row=0,
    )
    async def q_priority(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        try:
            await interaction.response.send_modal(QueuePriorityModal(self.cog))
        except Exception:
            try:
                await interaction.response.send_message("지금은 입력창을 열 수 없어…", ephemeral=True)
            except Exception:
                pass

    # 🧹 중복 정리
    @discord.ui.button(
        label="중복정리",
        style=discord.ButtonStyle.secondary,
        emoji="🧹",
        custom_id="yume_music_q_dedupe",
        row=0,
    )
    async def q_dedupe(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._queue_dedupe(interaction)

    # ↩️ 돌아가기
    @discord.ui.button(
        label="돌아가기",
        style=discord.ButtonStyle.primary,
        emoji="↩️",
        custom_id="yume_music_q_back",
        row=0,
    )
    async def q_back(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._back_to_main_panel(interaction)


# ==============================
# Cog
# ==============================

class MusicCog(commands.Cog):
    """
    음악은 **!음악** 하나로만 연다.
    - !음악: 유메 음성채널 입장 + 음악 패널(임베드 + 버튼) 표시
    - 노래 추가/컨트롤은 전부 패널 버튼으로 처리
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._states: Dict[int, MusicState] = {}

        # 길드별 패널 고정 설정(guild_id -> {channel_id, message_id})
        self._panel_cfg: Dict[str, Dict[str, int]] = self._load_panel_config()
        self._panel_cfg_lock = asyncio.Lock()
        self._restore_task: Optional[asyncio.Task] = None

        # 재부팅 후에도 버튼이 살아있도록 등록할 퍼시스턴트 뷰
        self.panel_view = MusicPanelView(self)
        self.queue_view = QueueManageView(self)

    async def cog_load(self):
        # 봇이 준비된 뒤, 지정된 음악 채널에 패널을 복구한다.
        self._restore_task = asyncio.create_task(self._restore_fixed_panels())

    async def cog_unload(self):
        if self._restore_task and not self._restore_task.done():
            self._restore_task.cancel()

        # 남아있는 자동퇴장/플레이어 태스크 정리
        for st in self._states.values():
            try:
                if st.auto_leave_task and not st.auto_leave_task.done():
                    st.auto_leave_task.cancel()
            except Exception:
                pass
            try:
                if st.player_task and not st.player_task.done():
                    st.player_task.cancel()
            except Exception:
                pass

    # -------------------------------
    # State
    # -------------------------------
    def _state(self, guild_id: int) -> MusicState:
        st = self._states.get(guild_id)
        if st is None:
            st = MusicState()
            self._states[guild_id] = st
        return st

    def _set_error(self, guild_id: int, msg: str):
        st = self._state(guild_id)
        st.last_error = msg[:160]
        st.last_error_at = time.time()

    # -------------------------------
    # Fixed panel config (guild-level)
    # -------------------------------
    def _load_panel_config(self) -> Dict[str, Dict[str, int]]:
        try:
            if not os.path.exists(PANEL_CFG_PATH):
                return {}
            with open(PANEL_CFG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            out: Dict[str, Dict[str, int]] = {}
            for k, v in data.items():
                if not isinstance(k, str) or not isinstance(v, dict):
                    continue
                try:
                    gid = int(k)
                    ch = int(v.get("channel_id", 0))
                    mid = int(v.get("message_id", 0))
                except Exception:
                    continue
                if gid <= 0 or ch <= 0:
                    continue
                out[str(gid)] = {"channel_id": ch, "message_id": max(0, mid)}
            return out
        except Exception:
            return {}

    def _save_panel_config_unlocked(self) -> None:
        try:
            os.makedirs(STORAGE_DIR, exist_ok=True)
            tmp = PANEL_CFG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._panel_cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, PANEL_CFG_PATH)
        except Exception as e:
            logger.warning("[Music] failed to save panel cfg: %s", e)

    def _fixed_panel(self, guild_id: int) -> Tuple[Optional[int], Optional[int]]:
        v = self._panel_cfg.get(str(guild_id))
        if not v:
            return (None, None)
        try:
            return (int(v.get("channel_id", 0)) or None, int(v.get("message_id", 0)) or None)
        except Exception:
            return (None, None)

    async def _set_fixed_panel(self, guild_id: int, channel_id: int, message_id: int):
        async with self._panel_cfg_lock:
            self._panel_cfg[str(guild_id)] = {
                "channel_id": int(channel_id),
                "message_id": int(message_id),
            }
            self._save_panel_config_unlocked()

    async def _clear_fixed_panel(self, guild_id: int):
        async with self._panel_cfg_lock:
            self._panel_cfg.pop(str(guild_id), None)
            self._save_panel_config_unlocked()

    async def _restore_fixed_panels(self):
        await self.bot.wait_until_ready()
        # 캐시가 안정될 시간을 살짝 준다.
        await asyncio.sleep(1)

        for gid_str, v in list(self._panel_cfg.items()):
            try:
                gid = int(gid_str)
                channel_id = int(v.get("channel_id", 0))
                message_id = int(v.get("message_id", 0))
            except Exception:
                continue

            guild = self.bot.get_guild(gid)
            if not guild:
                continue

            ch = guild.get_channel(channel_id)
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                continue

            embed = self._build_embed(guild)

            msg: Optional[discord.Message] = None
            if message_id:
                try:
                    msg = await ch.fetch_message(message_id)
                except discord.NotFound:
                    msg = None
                except Exception as e:
                    logger.warning("[Music] panel fetch error: %s", e)
                    msg = None

            try:
                if msg:
                    await msg.edit(embed=embed, view=self.panel_view)
                else:
                    msg = await ch.send(embed=embed, view=self.panel_view)
                    await self._set_fixed_panel(gid, channel_id, msg.id)
            except Exception as e:
                logger.warning("[Music] panel restore error: %s", e)

    # -------------------------------
    # Voice connect helpers
    # -------------------------------
    async def _ensure_voice_ctx(self, ctx: commands.Context) -> Optional[discord.VoiceClient]:
        if ctx.guild is None:
            await ctx.send("서버 채널에서만 쓸 수 있어.")
            return None
        if not isinstance(ctx.author, discord.Member) or ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("먼저 음성 채널에 들어가줘.")
            return None

        vc = ctx.guild.voice_client
        try:
            if vc and vc.is_connected():
                if vc.channel and vc.channel.id != ctx.author.voice.channel.id:
                    await vc.move_to(ctx.author.voice.channel)
            else:
                vc = await ctx.author.voice.channel.connect()
        except Exception as e:
            logger.warning("[Music] voice connect error: %s", e)
            await ctx.send("음성 채널에 연결하지 못했어.")
            return None

        return vc

    async def _ensure_voice_interaction(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        if interaction.guild is None:
            return None

        if not isinstance(interaction.user, discord.Member) or interaction.user.voice is None or interaction.user.voice.channel is None:
            return None

        vc = interaction.guild.voice_client
        try:
            if vc and vc.is_connected():
                # 버튼 누른 사람이 다른 채널이면 그 채널로 이동
                if vc.channel and vc.channel.id != interaction.user.voice.channel.id:
                    await vc.move_to(interaction.user.voice.channel)
            else:
                vc = await interaction.user.voice.channel.connect()
        except Exception as e:
            logger.warning("[Music] voice connect error: %s", e)
            return None

        return vc

    # -------------------------------
    # Stream resolve (핵심)
    # -------------------------------
    async def _resolve_stream_url(self, track: _Track) -> Optional[str]:
        """
        track.webpage_url로 yt_dlp를 다시 돌려 "진짜 재생 가능한 오디오 스트림 URL"을 얻는다.
        """
        # 짧은 캐시(30초): 스킵/재시작 같은 경우만 이득. 너무 길게 잡으면 URL 만료 위험.
        if track._resolved_stream_url and (time.time() - track._resolved_at) < 30:
            return track._resolved_stream_url

        try:
            info = await _extract_info(track.webpage_url)
            entry = _pick_entry(info)
            if not entry:
                return None
            url = _select_best_audio_url(entry)
            if not url:
                return None
            track._resolved_stream_url = url
            track._resolved_at = time.time()
            return url
        except Exception as e:
            logger.warning("[Music] resolve error: %s", e)
            return None

    # -------------------------------
    # Player loop
    # -------------------------------
    async def _player_loop(self, guild_id: int):
        st = self._state(guild_id)

        while True:
            try:
                track = await st.queue.get()
            except asyncio.CancelledError:
                return

            st.now_playing = track
            st.last_error = None

            guild = self.bot.get_guild(guild_id)
            vc = guild.voice_client if guild else None

            # 보이스가 없으면 트랙 버리고 다음
            if vc is None or not vc.is_connected():
                st.now_playing = None
                continue

            # 재생 직전: 스트림 URL 해상
            stream_url = await self._resolve_stream_url(track)
            if not stream_url:
                self._set_error(guild_id, "재생 URL을 해상하지 못했어(yt-dlp).")
                st.now_playing = None
                await self._refresh_panel(guild_id)
                continue

            # 패널 업데이트(재생 시작)
            await self._refresh_panel(guild_id)

            done = asyncio.Event()

            def _after(err: Optional[Exception]):
                if err:
                    logger.warning("[Music] playback error: %s", err)
                    self._set_error(guild_id, f"ffmpeg 재생 오류: {err}")
                try:
                    self.bot.loop.call_soon_threadsafe(done.set)
                except Exception:
                    pass

            try:
                src = _ffmpeg_source(stream_url, volume=st.volume)
                vc.play(src, after=_after)
                await done.wait()

            except Exception as e:
                logger.warning("[Music] play error: %s", e)
                self._set_error(guild_id, f"재생 예외: {e}")

            finally:
                finished = st.now_playing
                st.now_playing = None

                # 루프(큐 반복) 옵션: 스킵/정지로 멈춘 경우엔 한 번 재큐잉을 막는다.
                if st.loop_all and finished is not None and not st._suppress_requeue_once:
                    try:
                        await st.queue.put(finished)
                    except Exception:
                        pass
                st._suppress_requeue_once = False

                await self._refresh_panel(guild_id)

    def _start_player_if_needed(self, guild_id: int):
        st = self._state(guild_id)
        if st.player_task and not st.player_task.done():
            return
        st.player_task = asyncio.create_task(self._player_loop(guild_id))

    # -------------------------------
    # Panel render/update
    # -------------------------------
    def _build_embed(self, guild: discord.Guild) -> discord.Embed:
        """음악 패널 임베드(깔끔/고정용)."""
        st = self._state(guild.id)
        vc = guild.voice_client

        now_title = st.now_playing.title if st.now_playing else "없음"
        now_url = st.now_playing.webpage_url if st.now_playing else None

        embed = discord.Embed(
            title="유메 - 음악채널",
            description="🔴 YouTube / 🟢 Spotify 버튼으로 곡을 추가해줘.",
            color=discord.Color.blurple(),
        )

        if now_url:
            embed.add_field(name="🎧 지금 재생", value=f"[{now_title}]({now_url})", inline=False)
        else:
            embed.add_field(name="🎧 지금 재생", value=now_title, inline=False)

        embed.add_field(name="📃 큐", value=f"{st.queue.qsize()}곡", inline=True)
        embed.add_field(name="🔁 반복", value="ON" if st.loop_all else "OFF", inline=True)
        embed.add_field(name="🔊 볼륨", value=f"{int(st.volume * 100)}%", inline=True)

        if vc and vc.is_connected() and vc.channel:
            embed.add_field(name="🔊 음성 채널", value=vc.channel.name, inline=False)
        else:
            embed.add_field(name="🔊 음성 채널", value="(연결 안 됨)", inline=False)

        if st.last_error and (time.time() - st.last_error_at) < 300:
            embed.add_field(name="⚠️ 상태", value=st.last_error, inline=False)

        embed.set_footer(text="버튼으로 조작해줘. 으헤~")
        return embed

    async def _ensure_panel_message(
        self,
        guild_id: int,
        channel_id: int,
        *,
        fixed: bool,
    ) -> Tuple[Optional[int], Optional[int]]:
        """패널 메시지가 없으면 생성하고 (channel_id, message_id)를 돌려준다."""
        ch = self.bot.get_channel(channel_id)
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return (None, None)

        guild = ch.guild
        embed = self._build_embed(guild)

        # 현재 저장된 message_id
        msg_id: Optional[int] = None
        if fixed:
            _, msg_id = self._fixed_panel(guild_id)
        else:
            st = self._state(guild_id)
            msg_id = st.temp_panel_message_id

        msg: Optional[discord.Message] = None
        if msg_id:
            try:
                msg = await ch.fetch_message(msg_id)
            except discord.NotFound:
                msg = None
            except Exception:
                msg = None

        try:
            if msg:
                await msg.edit(embed=embed, view=self.panel_view)
                return (channel_id, msg.id)

            msg = await ch.send(embed=embed, view=self.panel_view)
            if fixed:
                await self._set_fixed_panel(guild_id, channel_id, msg.id)
            else:
                st = self._state(guild_id)
                st.temp_panel_channel_id = channel_id
                st.temp_panel_message_id = msg.id
            return (channel_id, msg.id)
        except Exception:
            return (None, None)

    async def _refresh_panel(
        self,
        guild_id: int,
        *,
        hint_channel_id: Optional[int] = None,
        force_create_when_transient: bool = False,
    ):
        """고정 패널이 있으면 그걸 갱신, 없으면 힌트/임시 패널을 갱신."""
        fixed_channel_id, fixed_msg_id = self._fixed_panel(guild_id)
        if fixed_channel_id:
            await self._ensure_panel_message(guild_id, fixed_channel_id, fixed=True)
            return

        st = self._state(guild_id)
        channel_id = st.temp_panel_channel_id or hint_channel_id
        if not channel_id:
            return

        if not st.temp_panel_message_id and not force_create_when_transient:
            return

        await self._ensure_panel_message(guild_id, channel_id, fixed=False)

    async def _refresh_from_interaction(self, interaction: discord.Interaction):
        """예전 코드 호환용: 버튼/모달에서 패널 갱신."""
        if interaction.guild is None:
            return
        await self._refresh_panel(interaction.guild.id, hint_channel_id=interaction.channel_id)

    # -------------------------------
    # Queue operations
    # -------------------------------
    async def _enqueue_from_interaction(self, interaction: discord.Interaction, query: str):
        # 모달 제출에서 호출될 수 있으므로 우선 defer 후 followup로 처리한다.
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        if interaction.guild is None:
            return

        q = (query or "").strip()
        if not q:
            try:
                await interaction.followup.send("검색어/URL이 비어있어.", ephemeral=True)
            except Exception:
                pass
            return

        vc = await self._ensure_voice_interaction(interaction)
        if not vc:
            # 유저가 음성에 없거나 / 연결 실패
            in_voice = isinstance(interaction.user, discord.Member) and interaction.user.voice and interaction.user.voice.channel
            msg = "먼저 음성 채널에 들어가줘." if not in_voice else "음성 채널에 연결하지 못했어."
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass
            return

        try:
            info = await _extract_info(q)
            entry = _pick_entry(info)
            if not entry:
                await interaction.followup.send("검색 결과가 없네.", ephemeral=True)
                return

            title = str(entry.get("title") or "제목 없음")
            webpage_url = str(entry.get("webpage_url") or entry.get("original_url") or q)

            # 핵심: 여기서는 stream_url을 저장하지 않는다(불안정).
            track = _Track(title=title, webpage_url=webpage_url, requester_id=interaction.user.id)

            st = self._state(interaction.guild.id)
            await st.queue.put(track)
            self._start_player_if_needed(interaction.guild.id)

            await interaction.followup.send(f"큐에 추가: **{title}**", ephemeral=True)
            await self._refresh_from_interaction(interaction)
        except Exception as e:
            logger.warning("[Music] extract error: %s", e)
            self._set_error(interaction.guild.id, f"추가 실패: {e}")
            try:
                await interaction.followup.send("그건 재생하기가 어려워…", ephemeral=True)
            except Exception:
                pass

    async def _resolve_spotify_to_query(self, q: str) -> str:
        """Spotify 트랙 URL이면 oEmbed로 제목을 가져와 YouTube 검색어로 변환한다.

        - Spotify API 키 없이도 되는 방식(oEmbed)이라 운영이 간단하다.
        - 실패하면 원문(q)을 그대로 반환해서 ytsearch에 태운다.
        """
        s = (q or "").strip()
        if not s:
            return s

        # spotify:track:ID -> https://open.spotify.com/track/ID
        if s.startswith("spotify:track:"):
            tid = s.split(":")[-1].strip()
            if tid:
                s = f"https://open.spotify.com/track/{tid}"

        if "open.spotify.com/track/" not in s:
            return s

        oembed = f"https://open.spotify.com/oembed?url={quote(s, safe='')}"
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(oembed, headers={"User-Agent": "YumeBot"}) as r:
                    if r.status != 200:
                        return s
                    data = await r.json()
        except Exception:
            return s

        title = str(data.get("title") or "").strip()
        author = str(data.get("author_name") or "").strip()
        if not title:
            return s

        # title에 이미 아티스트가 들어있을 때가 많아서, author는 보조로만.
        if author and author.lower() not in title.lower():
            return f"{title} {author}"
        return title

    async def _enqueue_spotify_from_interaction(self, interaction: discord.Interaction, query: str):
        # Spotify URL -> (가능하면) 제목 추출 -> YouTube 검색으로 큐 추가
        resolved = await self._resolve_spotify_to_query(query)
        await self._enqueue_from_interaction(interaction, resolved)

    async def _set_volume_from_interaction(self, interaction: discord.Interaction, raw: str):
        # 모달 제출이므로 defer 후 followup
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        if interaction.guild is None:
            return

        s = (raw or "").strip()
        try:
            value = int(s)
        except Exception:
            try:
                await interaction.followup.send("숫자(0~200)로 입력해줘.", ephemeral=True)
            except Exception:
                pass
            return

        value = max(0, min(200, value))
        st = self._state(interaction.guild.id)
        st.volume = value / 100.0

        vc = interaction.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            try:
                vc.source.volume = st.volume
            except Exception:
                pass

        try:
            await interaction.followup.send(f"볼륨을 {value}%로 맞췄어.", ephemeral=True)
        except Exception:
            pass

        await self._refresh_from_interaction(interaction)

    # -------------------------------
    # Button actions
    # -------------------------------
    async def _toggle_pause(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        vc = interaction.guild.voice_client
        try:
            if vc and vc.is_connected() and vc.is_playing():
                vc.pause()
                await interaction.response.send_message("잠깐 멈출게.", ephemeral=True)
            elif vc and vc.is_connected() and vc.is_paused():
                vc.resume()
                await interaction.response.send_message("다시 재생할게. 으헤~", ephemeral=True)
            else:
                await interaction.response.send_message("지금 재생 중이 아니야.", ephemeral=True)
        except Exception as e:
            self._set_error(interaction.guild.id, f"토글 오류: {e}")
            try:
                await interaction.response.send_message("지금은 조작이 잘 안 돼…", ephemeral=True)
            except Exception:
                pass
        await self._refresh_from_interaction(interaction)

    async def _skip(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        vc = interaction.guild.voice_client
        st = self._state(interaction.guild.id)

        if not vc or not vc.is_connected() or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("넘길 곡이 없어.", ephemeral=True)
            return

        st._suppress_requeue_once = True
        try:
            vc.stop()
        except Exception:
            pass

        try:
            await interaction.response.send_message("넘길게. 으헤~", ephemeral=True)
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    async def _stop(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return

        st = self._state(interaction.guild.id)
        st._suppress_requeue_once = True

        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            try:
                vc.stop()
            except Exception:
                pass

        # 큐 비우기
        try:
            while not st.queue.empty():
                st.queue.get_nowait()
        except Exception:
            pass

        st.now_playing = None

        try:
            await interaction.response.send_message("멈췄어. 으헤~", ephemeral=True)
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    async def _toggle_loop(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        st = self._state(interaction.guild.id)
        st.loop_all = not st.loop_all
        try:
            await interaction.response.send_message(
                f"반복: {'ON' if st.loop_all else 'OFF'}",
                ephemeral=True,
            )
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    async def _shuffle(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        st = self._state(interaction.guild.id)

        items: List[_Track] = []
        try:
            while not st.queue.empty():
                items.append(st.queue.get_nowait())
        except Exception:
            pass

        if not items:
            await interaction.response.send_message("셔플할 큐가 비어있어.", ephemeral=True)
            return

        import random
        random.shuffle(items)
        for t in items:
            await st.queue.put(t)

        try:
            await interaction.response.send_message("큐를 섞었어.", ephemeral=True)
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    async def _change_volume(self, interaction: discord.Interaction, *, delta: float):
        if interaction.guild is None:
            return
        st = self._state(interaction.guild.id)
        st.volume = max(0.0, min(2.0, st.volume + delta))

        vc = interaction.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = st.volume

        try:
            await interaction.response.send_message(f"볼륨: {int(st.volume * 100)}%", ephemeral=True)
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    async def _leave(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return

        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            await interaction.response.send_message("이미 나가있어.", ephemeral=True)
            return

        st = self._state(interaction.guild.id)
        st._suppress_requeue_once = True

        try:
            vc.stop()
        except Exception:
            pass

        try:
            await vc.disconnect(force=True)
        except Exception:
            pass

        if st.player_task and not st.player_task.done():
            st.player_task.cancel()

        # 큐 비우기
        try:
            while not st.queue.empty():
                st.queue.get_nowait()
        except Exception:
            pass

        st.now_playing = None

        try:
            await interaction.response.send_message("나갈게. 으헤~", ephemeral=True)
        except Exception:
            pass
        await self._refresh_from_interaction(interaction)

    # -------------------------------
    # Auto leave (유메만 남았을 때 자동 퇴장 + 큐 정리)
    # -------------------------------
    def _human_count(self, channel: Optional[discord.VoiceChannel]) -> int:
        if not channel:
            return 0
        try:
            return sum(1 for m in channel.members if not getattr(m, "bot", False))
        except Exception:
            return 0

    def _cancel_auto_leave(self, guild_id: int):
        st = self._state(guild_id)
        if st.auto_leave_task and not st.auto_leave_task.done():
            st.auto_leave_task.cancel()
        st.auto_leave_task = None

    def _schedule_auto_leave(self, guild_id: int, *, delay: float = 8.0):
        st = self._state(guild_id)
        # 이미 예약돼 있으면 그대로 둔다.
        if st.auto_leave_task and not st.auto_leave_task.done():
            return
        st.auto_leave_task = asyncio.create_task(self._auto_leave_runner(guild_id, delay))

    async def _auto_leave_runner(self, guild_id: int, delay: float):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return

        channel = getattr(vc, "channel", None)
        if self._human_count(channel) > 0:
            return

        await self._disconnect_and_cleanup(guild_id, reason="아무도 없어서 유메가 나갈게. 큐도 정리했어. 으헤~")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        guild = member.guild
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return

        channel = getattr(vc, "channel", None)
        if not channel:
            return

        # 이 채널과 무관한 이동은 무시
        if before.channel != channel and after.channel != channel:
            return

        humans = self._human_count(channel)
        if humans <= 0:
            self._schedule_auto_leave(guild.id, delay=8.0)
        else:
            self._cancel_auto_leave(guild.id)

    async def _disconnect_and_cleanup(self, guild_id: int, *, reason: Optional[str] = None):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        vc = guild.voice_client
        st = self._state(guild_id)

        # 자동퇴장 예약은 여기서 끝낸다.
        self._cancel_auto_leave(guild_id)

        # 재생/큐 정리
        async with st.lock:
            st._suppress_requeue_once = True

            try:
                if vc and vc.is_connected():
                    vc.stop()
            except Exception:
                pass

            if st.player_task and not st.player_task.done():
                try:
                    st.player_task.cancel()
                except Exception:
                    pass
            st.player_task = None

            # 큐 비우기
            try:
                while not st.queue.empty():
                    st.queue.get_nowait()
            except Exception:
                pass

            st.now_playing = None

            if reason:
                self._set_error(guild_id, reason)

        # 보이스 나가기
        try:
            if vc and vc.is_connected():
                await vc.disconnect()
        except Exception:
            pass

        # 패널 갱신(고정 패널이 있으면 거기로)
        try:
            await self._refresh_panel(guild_id)
        except Exception:
            pass

    # -------------------------------
    # Queue manage (토글 메뉴)
    # -------------------------------
    def _build_queue_embed(self, guild: discord.Guild) -> discord.Embed:
        st = self._state(guild.id)
        vc = guild.voice_client

        embed = discord.Embed(
            title="유메 - 큐 관리",
            description="번호로 삭제/정리할 수 있어. (예: 3,5,7 / 2-6)",
            color=discord.Color.blurple(),
        )

        if st.now_playing and st.now_playing.webpage_url:
            embed.add_field(
                name="🎧 지금 재생",
                value=f"[{st.now_playing.title}]({st.now_playing.webpage_url})",
                inline=False,
            )
        elif st.now_playing:
            embed.add_field(name="🎧 지금 재생", value=st.now_playing.title, inline=False)
        else:
            embed.add_field(name="🎧 지금 재생", value="없음", inline=False)

        # 큐 미리보기
        items: List[_Track] = []
        try:
            # asyncio.Queue 내부는 deque라 보통 _queue가 존재한다(읽기만)
            items = list(getattr(st.queue, "_queue", []))  # type: ignore[arg-type]
        except Exception:
            items = []

        total = len(items)
        if total <= 0:
            q_text = "비어있음"
        else:
            lines: List[str] = []
            for i, t in enumerate(items[:15], start=1):
                if t.webpage_url:
                    lines.append(f"{i}. [{t.title}]({t.webpage_url})")
                else:
                    lines.append(f"{i}. {t.title}")
            if total > 15:
                lines.append(f"... (+{total-15}곡 더)")
            q_text = "\n".join(lines)

        embed.add_field(name=f"📜 대기열 (총 {total}곡)", value=q_text, inline=False)

        if vc and vc.is_connected() and getattr(vc, "channel", None):
            embed.add_field(name="🔊 음성 채널", value=vc.channel.name, inline=False)
        else:
            embed.add_field(name="🔊 음성 채널", value="(연결 안 됨)", inline=False)

        if st.last_error and (time.time() - st.last_error_at) < 300:
            embed.add_field(name="⚠️ 상태", value=st.last_error, inline=False)

        embed.set_footer(text="큐 관리는 여기서. ↩️ 돌아가기 누르면 메인 패널로 돌아가.")
        return embed

    async def _edit_panel_message(
        self,
        guild_id: int,
        *,
        embed: discord.Embed,
        view: discord.ui.View,
        interaction: Optional[discord.Interaction] = None,
    ) -> bool:
        # 버튼 인터랙션이면 그 메시지를 바로 수정
        if interaction is not None and getattr(interaction, "message", None) is not None:
            try:
                await interaction.response.edit_message(embed=embed, view=view)
                return True
            except Exception:
                pass

        # 모달 제출 등: 저장된 패널 메시지를 찾아 편집
        fixed_ch, fixed_mid = self._fixed_panel(guild_id)
        st = self._state(guild_id)
        ch_id = fixed_ch or st.temp_panel_channel_id
        mid = fixed_mid or st.temp_panel_message_id
        if not ch_id or not mid:
            return False

        ch = self.bot.get_channel(int(ch_id))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return False
        try:
            msg = await ch.fetch_message(int(mid))
            await msg.edit(embed=embed, view=view)
            return True
        except Exception:
            return False

    async def _open_queue_manage(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        embed = self._build_queue_embed(interaction.guild)
        await self._edit_panel_message(gid, embed=embed, view=self.queue_view, interaction=interaction)

    async def _back_to_main_panel(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        embed = self._build_embed(interaction.guild)
        await self._edit_panel_message(gid, embed=embed, view=self.panel_view, interaction=interaction)

    def _parse_index_spec(self, spec: str, *, max_n: int) -> List[int]:
        """'3', '3,5,7', '2-6' 같은 입력을 0-based 인덱스 리스트로 변환."""
        s = (spec or "").strip()
        if not s or max_n <= 0:
            return []
        out: List[int] = []
        parts = re.split(r"[\s,]+", s)
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if "-" in p:
                a, b = p.split("-", 1)
                try:
                    ia = int(a)
                    ib = int(b)
                except Exception:
                    continue
                if ia > ib:
                    ia, ib = ib, ia
                for k in range(ia, ib + 1):
                    if 1 <= k <= max_n:
                        out.append(k - 1)
            else:
                try:
                    k = int(p)
                except Exception:
                    continue
                if 1 <= k <= max_n:
                    out.append(k - 1)
        # 중복 제거 + 정렬
        return sorted(set(out))

    async def _queue_manage_shuffle(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)

        async with st.lock:
            items: List[_Track] = []
            try:
                while not st.queue.empty():
                    items.append(st.queue.get_nowait())
            except Exception:
                pass

            if not items:
                try:
                    await interaction.response.send_message("셔플할 큐가 비어있어.", ephemeral=True)
                except Exception:
                    pass
                return

            import random
            random.shuffle(items)
            for t in items:
                try:
                    st.queue.put_nowait(t)
                except Exception:
                    pass

        # 큐 화면 갱신
        embed = self._build_queue_embed(interaction.guild)
        await self._edit_panel_message(gid, embed=embed, view=self.queue_view, interaction=interaction)

    async def _queue_delete_from_modal(self, interaction: discord.Interaction, spec: str):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)

        removed = 0
        async with st.lock:
            items: List[_Track] = []
            try:
                while not st.queue.empty():
                    items.append(st.queue.get_nowait())
            except Exception:
                pass

            idxs = self._parse_index_spec(spec, max_n=len(items))
            if idxs:
                keep: List[_Track] = [t for i, t in enumerate(items) if i not in set(idxs)]
                removed = len(items) - len(keep)
                for t in keep:
                    try:
                        st.queue.put_nowait(t)
                    except Exception:
                        pass
            else:
                # 원복
                for t in items:
                    try:
                        st.queue.put_nowait(t)
                    except Exception:
                        pass

        try:
            await interaction.response.send_message(
                "삭제할 번호를 제대로 못 읽었어…" if removed == 0 else f"큐에서 {removed}곡을 삭제했어.",
                ephemeral=True,
            )
        except Exception:
            pass

        # 패널(큐화면) 갱신
        try:
            await self._edit_panel_message(gid, embed=self._build_queue_embed(interaction.guild), view=self.queue_view)
        except Exception:
            pass

    async def _queue_priority_from_modal(self, interaction: discord.Interaction, spec: str):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)

        moved = False
        async with st.lock:
            items: List[_Track] = []
            try:
                while not st.queue.empty():
                    items.append(st.queue.get_nowait())
            except Exception:
                pass

            idxs = self._parse_index_spec(spec, max_n=len(items))
            if idxs:
                i = idxs[0]
                t = items.pop(i)
                items.insert(0, t)
                moved = True

            for t in items:
                try:
                    st.queue.put_nowait(t)
                except Exception:
                    pass

        try:
            await interaction.response.send_message(
                "맨 위로 올릴 번호가 없었어…" if not moved else "맨 위로 올렸어.",
                ephemeral=True,
            )
        except Exception:
            pass

        try:
            await self._edit_panel_message(gid, embed=self._build_queue_embed(interaction.guild), view=self.queue_view)
        except Exception:
            pass

    async def _queue_dedupe(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)

        removed = 0
        async with st.lock:
            items: List[_Track] = []
            try:
                while not st.queue.empty():
                    items.append(st.queue.get_nowait())
            except Exception:
                pass

            seen: set[str] = set()
            keep: List[_Track] = []
            for t in items:
                key = (t.webpage_url or t.title).strip()
                if key in seen:
                    removed += 1
                    continue
                seen.add(key)
                keep.append(t)

            for t in keep:
                try:
                    st.queue.put_nowait(t)
                except Exception:
                    pass

        try:
            await interaction.response.send_message(
                f"중복 {removed}곡을 정리했어." if removed > 0 else "중복이 없었어.",
                ephemeral=True,
            )
        except Exception:
            pass

        embed = self._build_queue_embed(interaction.guild)
        await self._edit_panel_message(gid, embed=embed, view=self.queue_view, interaction=interaction)



    # -------------------------------
    # Command
    # -------------------------------
    @commands.command(name="음악채널지정")
    @commands.has_permissions(manage_guild=True)
    async def set_music_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """!음악채널지정 <채널ID>: 지정한 채널에 음악 패널을 항상 고정한다."""
        if ctx.guild is None:
            await ctx.send("서버 채널에서만 쓸 수 있어.")
            return

        # 패널 생성/복구
        cid, mid = await self._ensure_panel_message(ctx.guild.id, channel.id, fixed=True)
        if not cid or not mid:
            await ctx.send("그 채널에 패널을 만들 수 없었어(권한을 확인해줘).")
            return

        await ctx.send(f"음악 패널 채널을 {channel.mention}로 지정했어. 이제 여기만 갱신할게.")

    @set_music_channel.error
    async def set_music_channel_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("이건 서버 관리 권한(서버 관리)이 필요해.")
            return
        await ctx.send("사용법: `!음악채널지정 <채널ID>`")

    @commands.command(name="음악채널해제")
    @commands.has_permissions(manage_guild=True)
    async def clear_music_channel(self, ctx: commands.Context):
        """!음악채널해제: 고정 패널 설정을 지운다."""
        if ctx.guild is None:
            await ctx.send("서버 채널에서만 쓸 수 있어.")
            return
        await self._clear_fixed_panel(ctx.guild.id)
        await ctx.send("고정 음악 패널 설정을 지웠어. 이제 `!음악`을 누른 채널에 임시 패널이 떠.")

    @commands.command(name="음악")
    async def music_panel(self, ctx: commands.Context):
        """!음악: 유메를 음성 채널로 부르고 음악 패널을 띄운다."""
        vc = await self._ensure_voice_ctx(ctx)
        if not vc or ctx.guild is None:
            return

        self._start_player_if_needed(ctx.guild.id)

        fixed_channel_id, _ = self._fixed_panel(ctx.guild.id)
        if fixed_channel_id:
            # 고정 패널이 있으면 그 채널만 갱신한다.
            await self._ensure_panel_message(ctx.guild.id, fixed_channel_id, fixed=True)
            await self._refresh_panel(ctx.guild.id)
            try:
                await ctx.send(f"패널은 <#{fixed_channel_id}>에 있어.", delete_after=5)
            except Exception:
                pass
            return

        # 고정이 없으면 현재 채널에 임시 패널을 띄워둔다.
        embed = self._build_embed(ctx.guild)
        msg = await ctx.send(embed=embed, view=self.panel_view)
        st = self._state(ctx.guild.id)
        st.temp_panel_channel_id = ctx.channel.id
        st.temp_panel_message_id = msg.id


async def setup(bot: commands.Bot):
    cog = MusicCog(bot)
    await bot.add_cog(cog)

    # 퍼시스턴트 뷰 등록 (재부팅 후에도 버튼이 동작)
    try:
        bot.add_view(cog.panel_view)
        bot.add_view(cog.queue_view)
    except Exception:
        pass
