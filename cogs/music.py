from __future__ import annotations

import asyncio
import base64
import logging
import os
import json
import time
import re
import bisect
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import discord
from discord.ext import commands
import yt_dlp
import aiohttp

logger = logging.getLogger(__name__)



ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STORAGE_DIR = os.path.join(ROOT_DIR, "data", "storage")
PANEL_CFG_PATH = os.path.join(STORAGE_DIR, "music_panel.json")
FX_CFG_PATH = os.path.join(STORAGE_DIR, "music_fx.json")



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



@dataclass
class _Track:
    title: str
    webpage_url: str
    requester_id: Optional[int] = None

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
    formats = entry.get("formats") or []
    audio_only = []
    for f in formats:
        try:
            if not f:
                continue
            if f.get("url") is None:
                continue
            if f.get("vcodec") != "none":
                continue
            if f.get("acodec") in (None, "none"):
                continue
            audio_only.append(f)
        except Exception:
            continue

    def _score(f: dict) -> Tuple[float, float]:
        abr = f.get("abr")
        tbr = f.get("tbr")
        bitrate = float(abr if abr is not None else (tbr if tbr is not None else 0.0))
        fs = f.get("filesize") or f.get("filesize_approx") or 0
        return (bitrate, float(fs))

    if audio_only:
        best = max(audio_only, key=_score)
        return str(best.get("url"))

    url = entry.get("url")
    if url:
        return str(url)

    return None


def _ffmpeg_source(stream_url: str, volume: float, *, af_filters: Optional[str] = None) -> discord.AudioSource:
    """FFmpeg 오디오 소스 생성.

    af_filters가 주어지면 -af로 필터 체인을 적용한다.
    (이퀄라이저/리버브 같은 FX는 여기서 처리)
    """
    options = FFMPEG_OPTIONS
    if af_filters:
        options = f"{options} -af {af_filters}"

    src = discord.FFmpegPCMAudio(
        stream_url,
        executable=FFMPEG_EXECUTABLE,
        before_options=FFMPEG_BEFORE,
        options=options,
    )
    return discord.PCMVolumeTransformer(src, volume=volume)



LRCLIB_API_BASE = "https://lrclib.net/api/get"

_TAG_LINE_RE = re.compile(r"^\s*\[(ar|ti|al|by|offset):", re.IGNORECASE)
_TS_RE = re.compile(r"\[(\d+):(\d+)(?:\.(\d+))?\]")

def _clean_title(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s*\[[^\]]+\]\s*$", "", s)
    s = re.sub(r"\s*\([^\)]+\)\s*$", "", s)
    s = re.sub(r"\s*(official|mv|m/v|audio|video|lyrics?)\s*$", "", s, flags=re.IGNORECASE)
    return s.strip()

def _guess_artist_title(raw_title: str) -> Tuple[str, Optional[str]]:
    """
    LRCLIB 검색에 쓸 (track_name, artist_name)를 최대한 그럴듯하게 뽑는다.
    - 'Artist - Title' 형태를 우선으로 본다.
    - 없으면 track_name만 반환.
    """
    t = _clean_title(raw_title)
    for sep in (" - ", " — ", " – ", " | ", " · "):
        if sep in t:
            left, right = t.split(sep, 1)
            left = left.strip()
            right = right.strip()
            if 1 <= len(left) <= 40 and len(right) >= 1:
                return (_clean_title(right), _clean_title(left) or None)
            return (_clean_title(left), _clean_title(right) or None)
    return (t, None)

def _parse_lrc(lrc_text: str) -> List[Tuple[float, str]]:
    """
    LRC 텍스트 -> [(sec, line), ...] 로 파싱.
    - 한 줄에 여러 timestamp가 있으면 각각 분해해서 동일 가사를 매핑한다.
    """
    out: List[Tuple[float, str]] = []
    if not lrc_text:
        return out

    for raw_line in lrc_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _TAG_LINE_RE.match(line):
            continue

        stamps = list(_TS_RE.finditer(line))
        if not stamps:
            continue

        lyric = _TS_RE.sub("", line).strip()
        if not lyric:
            continue

        for m in stamps:
            mm = int(m.group(1))
            ss = int(m.group(2))
            frac = m.group(3)
            ms = 0.0
            if frac:
                denom = 10 ** len(frac)
                ms = int(frac) / denom
            sec = float(mm * 60 + ss) + ms
            out.append((sec, lyric))

    out.sort(key=lambda x: x[0])
    return out


class MusicState:
    def __init__(self):
        self.queue: asyncio.Queue[_Track] = asyncio.Queue()
        self.now_playing: Optional[_Track] = None
        self.player_task: Optional[asyncio.Task] = None

        self.play_started_at: float = 0.0  # loop.time() 기준
        self.paused_at: Optional[float] = None
        self.paused_total: float = 0.0

        self.lock: asyncio.Lock = asyncio.Lock()

        self.auto_leave_task: Optional[asyncio.Task] = None

        self.volume: float = 1.0
        self.loop_all: bool = False

        self.fx_eq_enabled: bool = False
        self.fx_bass_db: float = 0.0
        self.fx_mid_db: float = 0.0
        self.fx_treble_db: float = 0.0
        self.fx_preamp_db: float = 0.0

        self.fx_reverb_enabled: bool = False
        self.fx_reverb_mix: int = 0      # 0~100
        self.fx_reverb_room: int = 50    # 0~100

        self.fx_tune_enabled: bool = False
        self.fx_tune_semitones: float = 0.0  # -12.0 ~ +12.0 (소수 허용)

        self.fx_eq_preset: str = "off"
        self.fx_reverb_level: int = 0

        self._suppress_requeue_once: bool = False

        self.last_error: Optional[str] = None
        self.last_error_at: float = 0.0

        self.temp_panel_channel_id: Optional[int] = None
        self.temp_panel_message_id: Optional[int] = None

        self.lyrics_enabled: bool = False
        self.lyrics_task: Optional[asyncio.Task] = None
        self.lyrics_channel_id: Optional[int] = None
        self.lyrics_message_id: Optional[int] = None
        self.lyrics_cache: Dict[str, List[Tuple[float, str]]] = {}
        self._lyrics_last_track_key: Optional[str] = None
        self._lyrics_last_render_key: Optional[str] = None



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
            label="Spotify 트랙/플레이리스트 URL 또는 검색어",
            placeholder="예: https://open.spotify.com/playlist/... 또는 track/...",
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




def _clamp_float(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _parse_float(s: str, *, default: float, lo: float, hi: float) -> float:
    try:
        v = float(str(s).strip())
    except Exception:
        v = float(default)
    return _clamp_float(v, lo, hi)


def _parse_int(s: str, *, default: int, lo: int, hi: int) -> int:
    try:
        v = int(float(str(s).strip()))
    except Exception:
        v = int(default)
    return _clamp_int(v, lo, hi)


class EQSettingsModal(discord.ui.Modal):
    title = "EQ 설정"

    def __init__(self, cog: "MusicCog", guild_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id

        st = cog._state(guild_id)
        self.bass = discord.ui.TextInput(
            label="Bass dB (-12 ~ +12)",
            placeholder="예) 6",
            required=False,
            default=str(st.fx_bass_db),
            max_length=16,
        )
        self.mid = discord.ui.TextInput(
            label="Mid dB (-12 ~ +12)",
            placeholder="예) 0",
            required=False,
            default=str(st.fx_mid_db),
            max_length=16,
        )
        self.treble = discord.ui.TextInput(
            label="Treble dB (-12 ~ +12)",
            placeholder="예) 2",
            required=False,
            default=str(st.fx_treble_db),
            max_length=16,
        )
        self.preamp = discord.ui.TextInput(
            label="Preamp dB (-12 ~ +12)",
            placeholder="예) -1",
            required=False,
            default=str(st.fx_preamp_db),
            max_length=16,
        )
        self.add_item(self.bass)
        self.add_item(self.mid)
        self.add_item(self.treble)
        self.add_item(self.preamp)

    async def on_submit(self, interaction: discord.Interaction):
        bass = _parse_float(self.bass.value, default=0.0, lo=-12.0, hi=12.0)
        mid = _parse_float(self.mid.value, default=0.0, lo=-12.0, hi=12.0)
        treble = _parse_float(self.treble.value, default=0.0, lo=-12.0, hi=12.0)
        preamp = _parse_float(self.preamp.value, default=0.0, lo=-12.0, hi=12.0)
        await self.cog._set_eq_settings(interaction, guild_id=self.guild_id, bass=bass, mid=mid, treble=treble, preamp=preamp)


class ReverbSettingsModal(discord.ui.Modal):
    title = "리버브 설정"

    def __init__(self, cog: "MusicCog", guild_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id

        st = cog._state(guild_id)
        self.mix = discord.ui.TextInput(
            label="Mix (0~100)",
            placeholder="예) 20",
            required=False,
            default=str(st.fx_reverb_mix),
            max_length=16,
        )
        self.room = discord.ui.TextInput(
            label="Room (0~100)",
            placeholder="예) 60",
            required=False,
            default=str(st.fx_reverb_room),
            max_length=16,
        )
        self.add_item(self.mix)
        self.add_item(self.room)

    async def on_submit(self, interaction: discord.Interaction):
        mix = _parse_int(self.mix.value, default=0, lo=0, hi=100)
        room = _parse_int(self.room.value, default=50, lo=0, hi=100)
        await self.cog._set_reverb_settings(interaction, guild_id=self.guild_id, mix=mix, room=room)


class TuneSettingsModal(discord.ui.Modal):
    title = "튠(피치) 설정"

    def __init__(self, cog: "MusicCog", guild_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id

        st = cog._state(guild_id)
        self.semi = discord.ui.TextInput(
            label="Semitone (-12.0 ~ +12.0)",
            placeholder="예) 2  /  -3.5",
            required=False,
            default=str(st.fx_tune_semitones),
            max_length=16,
        )
        self.add_item(self.semi)

    async def on_submit(self, interaction: discord.Interaction):
        semi = _parse_float(self.semi.value, default=0.0, lo=-12.0, hi=12.0)
        await self.cog._set_tune_settings(interaction, guild_id=self.guild_id, semitones=semi)
class MusicPanelView(discord.ui.View):
    """패널은 재부팅 이후에도 버튼이 살아있도록(퍼시스턴트) timeout=None로 유지."""

    def __init__(self, cog: "MusicCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="YouTube",
        style=discord.ButtonStyle.danger,
        emoji="🔴",
        custom_id="yume_music_add_yt",
        row=0,
    )
    async def youtube_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await interaction.response.send_modal(YouTubeAddModal(self.cog))

    @discord.ui.button(
        label="Spotify",
        style=discord.ButtonStyle.success,
        emoji="🟢",
        custom_id="yume_music_add_sp",
        row=0,
    )
    async def spotify_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await interaction.response.send_modal(SpotifyAddModal(self.cog))

    @discord.ui.button(
        label="재생/일시정지",
        style=discord.ButtonStyle.secondary,
        emoji="⏯",
        custom_id="yume_music_toggle",
        row=0,
    )
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_pause(interaction)

    @discord.ui.button(
        label="스킵",
        style=discord.ButtonStyle.secondary,
        emoji="⏭",
        custom_id="yume_music_skip",
        row=0,
    )
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._skip(interaction)

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

    @discord.ui.button(
        label="반복",
        style=discord.ButtonStyle.secondary,
        emoji="🔁",
        custom_id="yume_music_loop",
        row=1,
    )
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_loop(interaction)

    @discord.ui.button(
        label="셔플",
        style=discord.ButtonStyle.secondary,
        emoji="🔀",
        custom_id="yume_music_shuffle",
        row=1,
    )
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._shuffle(interaction)

    @discord.ui.button(
        label="정지",
        style=discord.ButtonStyle.danger,
        emoji="⏹",
        custom_id="yume_music_stop",
        row=1,
    )
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._stop(interaction)

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

    @discord.ui.button(
        label="큐 셔플",
        style=discord.ButtonStyle.secondary,
        emoji="🔀",
        custom_id="yume_music_q_shuffle",
        row=0,
    )
    async def q_shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._queue_manage_shuffle(interaction)

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
            try:
                await interaction.response.send_message("지금은 입력창을 열 수 없어…", ephemeral=True)
            except Exception:
                pass

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

    @discord.ui.button(
        label="중복정리",
        style=discord.ButtonStyle.secondary,
        emoji="🧹",
        custom_id="yume_music_q_dedupe",
        row=0,
    )
    async def q_dedupe(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._queue_dedupe(interaction)

    @discord.ui.button(
        label="가사",
        style=discord.ButtonStyle.secondary,
        emoji="🎤",
        custom_id="yume_music_lyrics",
        row=1,
    )
    async def lyrics_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_lyrics(interaction)

    @discord.ui.button(
        label="EQ",
        style=discord.ButtonStyle.secondary,
        emoji="🎚️",
        custom_id="yume_music_fx",
        row=1,
    )
    async def fx_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_eq(interaction)

    @discord.ui.button(
        label="EQ 설정",
        style=discord.ButtonStyle.secondary,
        emoji="⚙️",
        custom_id="yume_music_eq_settings",
        row=1,
    )
    async def eq_settings_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        guild = interaction.guild
        if guild is None:
            try:
                await interaction.response.send_message("서버에서만 사용할 수 있어.", ephemeral=True)
            except Exception:
                pass
            return
        try:
            await interaction.response.send_modal(EQSettingsModal(self.cog, guild.id))
        except Exception:
            try:
                await interaction.followup.send("지금은 입력창을 열 수 없어…", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(
        label="리버브",
        style=discord.ButtonStyle.secondary,
        emoji="🌊",
        custom_id="yume_music_reverb",
        row=1,
    )
    async def reverb_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_reverb(interaction)

    @discord.ui.button(
        label="리버브 설정",
        style=discord.ButtonStyle.secondary,
        emoji="⚙️",
        custom_id="yume_music_reverb_settings",
        row=1,
    )
    async def reverb_settings_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        guild = interaction.guild
        if guild is None:
            try:
                await interaction.response.send_message("서버에서만 사용할 수 있어.", ephemeral=True)
            except Exception:
                pass
            return
        try:
            await interaction.response.send_modal(ReverbSettingsModal(self.cog, guild.id))
        except Exception:
            try:
                await interaction.followup.send("지금은 입력창을 열 수 없어…", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(
        label="튠",
        style=discord.ButtonStyle.secondary,
        emoji="🎛️",
        custom_id="yume_music_tune",
        row=2,
    )
    async def tune_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_tune(interaction)

    @discord.ui.button(
        label="튠 설정",
        style=discord.ButtonStyle.secondary,
        emoji="⚙️",
        custom_id="yume_music_tune_settings",
        row=2,
    )
    async def tune_settings_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        guild = interaction.guild
        if guild is None:
            try:
                await interaction.response.send_message("서버에서만 사용할 수 있어.", ephemeral=True)
            except Exception:
                pass
            return
        try:
            await interaction.response.send_modal(TuneSettingsModal(self.cog, guild.id))
        except Exception:
            try:
                await interaction.followup.send("지금은 입력창을 열 수 없어…", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(
        label="FX 초기화",
        style=discord.ButtonStyle.danger,
        emoji="🧼",
        custom_id="yume_music_fx_reset",
        row=2,
    )
    async def fx_reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._reset_fx(interaction)
    @discord.ui.button(
        label="돌아가기",
        style=discord.ButtonStyle.primary,
        emoji="↩️",
        custom_id="yume_music_q_back",
        row=0,
    )
    async def q_back(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._back_to_main_panel(interaction)



class MusicCog(commands.Cog):
    """
    음악은 **!음악** 하나로만 연다.
    - !음악: 유메 음성채널 입장 + 음악 패널(임베드 + 버튼) 표시
    - 노래 추가/컨트롤은 전부 패널 버튼으로 처리
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._states: Dict[int, MusicState] = {}

        self._panel_cfg: Dict[str, Dict[str, int]] = self._load_panel_config()
        self._panel_cfg_lock = asyncio.Lock()
        self._fx_cfg = self._load_fx_cfg()
        self._fx_cfg_lock = asyncio.Lock()
        self._ffmpeg_filters = self._detect_ffmpeg_filters()
        self._restore_task: Optional[asyncio.Task] = None

        self.panel_view = MusicPanelView(self)
        self.queue_view = QueueManageView(self)

        self._spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        self._spotify_client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
        self._spotify_token: Optional[str] = None
        self._spotify_token_exp: float = 0.0
        self._spotify_token_lock: asyncio.Lock = asyncio.Lock()

    async def cog_load(self):
        self._restore_task = asyncio.create_task(self._restore_fixed_panels())

    async def cog_unload(self):
        if self._restore_task and not self._restore_task.done():
            self._restore_task.cancel()

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

    def _state(self, guild_id: int) -> MusicState:
        st = self._states.get(guild_id)
        if st is None:
            st = MusicState()
            self._apply_fx_cfg_to_state(guild_id, st)
            self._states[guild_id] = st
        return st

    def _set_error(self, guild_id: int, msg: str):
        st = self._state(guild_id)
        st.last_error = msg[:160]
        st.last_error_at = time.time()

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


    # =========================
    # FX config (per-guild)
    # =========================

    def _load_fx_cfg(self) -> Dict[str, Dict[str, object]]:
        """
        data/storage/music_fx.json 에서 길드별 FX 설정을 읽어온다.
        - 파일이 없거나 깨져 있으면 빈 dict를 반환한다.
        - 값은 최소한으로 검증/클램프해서 저장한다.
        """
        try:
            if not os.path.exists(FX_CFG_PATH):
                return {}
            with open(FX_CFG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}

            out: Dict[str, Dict[str, object]] = {}
            for k, v in data.items():
                if not isinstance(k, str) or not isinstance(v, dict):
                    continue
                try:
                    gid = int(k)
                except Exception:
                    continue
                if gid <= 0:
                    continue

                def _bf(x: object, default: bool) -> bool:
                    try:
                        return bool(x)
                    except Exception:
                        return default

                def _ff(x: object, default: float, lo: float, hi: float) -> float:
                    try:
                        return _clamp_float(float(x), lo, hi)
                    except Exception:
                        return default

                def _ii(x: object, default: int, lo: int, hi: int) -> int:
                    try:
                        return _clamp_int(int(x), lo, hi)
                    except Exception:
                        return default

                out[str(gid)] = {
                    'eq_enabled': _bf(v.get('eq_enabled', False), False),
                    'bass_db': _ff(v.get('bass_db', 0.0), 0.0, -24.0, 24.0),
                    'mid_db': _ff(v.get('mid_db', 0.0), 0.0, -24.0, 24.0),
                    'treble_db': _ff(v.get('treble_db', 0.0), 0.0, -24.0, 24.0),
                    'preamp_db': _ff(v.get('preamp_db', 0.0), 0.0, -24.0, 24.0),

                    'reverb_enabled': _bf(v.get('reverb_enabled', False), False),
                    'reverb_mix': _ii(v.get('reverb_mix', 0), 0, 0, 100),
                    'reverb_room': _ii(v.get('reverb_room', 50), 50, 0, 100),

                    'tune_enabled': _bf(v.get('tune_enabled', False), False),
                    'tune_semitones': _ff(v.get('tune_semitones', 0.0), 0.0, -12.0, 12.0),
                }

            return out
        except Exception:
            return {}

    def _save_fx_cfg_unlocked(self) -> None:
        try:
            os.makedirs(STORAGE_DIR, exist_ok=True)
            tmp = FX_CFG_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._fx_cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, FX_CFG_PATH)
        except Exception as e:
            logger.warning('[Music] failed to save fx cfg: %s', e)

    def _apply_fx_cfg_to_state(self, guild_id: int, st: MusicState) -> None:
        cfg = self._fx_cfg.get(str(guild_id))
        if not cfg:
            return
        try:
            st.fx_eq_enabled = bool(cfg.get('eq_enabled', False))
            st.fx_bass_db = float(cfg.get('bass_db', 0.0))
            st.fx_mid_db = float(cfg.get('mid_db', 0.0))
            st.fx_treble_db = float(cfg.get('treble_db', 0.0))
            st.fx_preamp_db = float(cfg.get('preamp_db', 0.0))

            st.fx_reverb_enabled = bool(cfg.get('reverb_enabled', False))
            st.fx_reverb_mix = int(cfg.get('reverb_mix', 0))
            st.fx_reverb_room = int(cfg.get('reverb_room', 50))

            st.fx_tune_enabled = bool(cfg.get('tune_enabled', False))
            st.fx_tune_semitones = float(cfg.get('tune_semitones', 0.0))
        except Exception:
            # 값이 깨져있어도 봇이 죽으면 안 된다.
            return

    async def _persist_fx_cfg_from_state(self, guild_id: int, st: MusicState) -> None:
        async with self._fx_cfg_lock:
            self._fx_cfg[str(guild_id)] = {
                'eq_enabled': bool(st.fx_eq_enabled),
                'bass_db': float(_clamp_float(st.fx_bass_db, -24.0, 24.0)),
                'mid_db': float(_clamp_float(st.fx_mid_db, -24.0, 24.0)),
                'treble_db': float(_clamp_float(st.fx_treble_db, -24.0, 24.0)),
                'preamp_db': float(_clamp_float(st.fx_preamp_db, -24.0, 24.0)),

                'reverb_enabled': bool(st.fx_reverb_enabled),
                'reverb_mix': int(_clamp_int(int(st.fx_reverb_mix), 0, 100)),
                'reverb_room': int(_clamp_int(int(st.fx_reverb_room), 0, 100)),

                'tune_enabled': bool(st.fx_tune_enabled),
                'tune_semitones': float(_clamp_float(float(st.fx_tune_semitones), -12.0, 12.0)),
            }
            self._save_fx_cfg_unlocked()

    def _detect_ffmpeg_filters(self) -> Optional[set[str]]:
        """
        ffmpeg -filters 결과에서 필터 이름을 추출한다.
        실패하면 None을 반환한다. (보수적으로 동작하도록)
        """
        try:
            import subprocess

            proc = subprocess.run(
                [FFMPEG_EXECUTABLE, "-hide_banner", "-filters"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            blob = (proc.stdout or "") + "\n" + (proc.stderr or "")

            names: set[str] = set()
            for ln in blob.splitlines():
                ln = ln.strip()
                if (not ln) or ln.startswith("Filters:") or ln.startswith("---"):
                    continue
                # 보통: " T.. equalizer         A->A       Apply two-pole ..."
                parts = ln.split()
                if len(parts) >= 2 and len(parts[0]) >= 3:
                    cand = parts[1].strip()
                    if re.match(r"^[A-Za-z0-9_]+$", cand):
                        names.add(cand)

            return names if names else None
        except Exception:
            return None

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
                if vc.channel and vc.channel.id != interaction.user.voice.channel.id:
                    await vc.move_to(interaction.user.voice.channel)
            else:
                vc = await interaction.user.voice.channel.connect()
        except Exception as e:
            logger.warning("[Music] voice connect error: %s", e)
            return None

        return vc

    def _parse_spotify(self, s: str) -> Tuple[Optional[str], Optional[str]]:
        """
        return: (kind, id)  kind in {"track","playlist"}.
        지원:
          - https://open.spotify.com/track/{id}
          - https://open.spotify.com/playlist/{id}
          - spotify:track:{id}
          - spotify:playlist:{id}
        """
        s = (s or "").strip()
        if not s:
            return (None, None)

        if s.startswith("spotify:track:"):
            return ("track", s.split(":")[-1].strip() or None)
        if s.startswith("spotify:playlist:"):
            return ("playlist", s.split(":")[-1].strip() or None)

        m = re.search(r"open\.spotify\.com/(track|playlist)/([A-Za-z0-9]+)", s)
        if not m:
            return (None, None)
        return (m.group(1), m.group(2))

    def _spotify_enabled(self) -> bool:
        return bool(self._spotify_client_id and self._spotify_client_secret)

    async def _spotify_get_token(self, session: aiohttp.ClientSession) -> Optional[str]:
        now = time.time()
        if self._spotify_token and now < (self._spotify_token_exp - 30):
            return self._spotify_token

        async with self._spotify_token_lock:
            now = time.time()
            if self._spotify_token and now < (self._spotify_token_exp - 30):
                return self._spotify_token

            if not self._spotify_enabled():
                return None

            basic = base64.b64encode(f"{self._spotify_client_id}:{self._spotify_client_secret}".encode("utf-8")).decode("ascii")
            url = "https://accounts.spotify.com/api/token"
            data = {"grant_type": "client_credentials"}

            try:
                async with session.post(url, data=data, headers={"Authorization": f"Basic {basic}"}) as r:
                    if r.status != 200:
                        return None
                    js = await r.json()
            except Exception:
                return None

            access = str(js.get("access_token") or "")
            expires_in = int(js.get("expires_in") or 0)
            if not access or expires_in <= 0:
                return None

            self._spotify_token = access
            self._spotify_token_exp = time.time() + expires_in
            return access

    async def _spotify_api_get(self, session: aiohttp.ClientSession, url: str) -> Optional[dict]:
        tok = await self._spotify_get_token(session)
        if not tok:
            return None
        try:
            async with session.get(url, headers={"Authorization": f"Bearer {tok}"}) as r:
                if r.status != 200:
                    return None
                return await r.json()
        except Exception:
            return None

    async def _spotify_track_query(self, session: aiohttp.ClientSession, track_id: str, fallback_url: str) -> str:
        """
        트랙을 (곡명 + 아티스트) 검색어로 변환.
        API가 있으면 API 우선, 없으면 oEmbed로 best-effort.
        """
        if self._spotify_enabled():
            js = await self._spotify_api_get(session, f"https://api.spotify.com/v1/tracks/{track_id}")
            if js:
                name = str(js.get("name") or "").strip()
                artists = js.get("artists") or []
                artist = str(artists[0].get("name") or "").strip() if artists else ""
                q = f"{name} {artist}".strip()
                return q or fallback_url

        oembed = f"https://open.spotify.com/oembed?url={quote(fallback_url, safe='')}"
        try:
            async with session.get(oembed, headers={"User-Agent": "YumeBot"}) as r:
                if r.status != 200:
                    return fallback_url
                data = await r.json()
        except Exception:
            return fallback_url

        title = str(data.get("title") or "").strip()
        author = str(data.get("author_name") or "").strip()
        if not title:
            return fallback_url
        if author and author.lower() not in title.lower():
            return f"{title} {author}"
        return title

    async def _spotify_playlist_queries(self, session: aiohttp.ClientSession, playlist_id: str) -> Optional[List[str]]:
        """
        Spotify 플레이리스트 -> ['곡명 아티스트', ...]
        API 없으면 None 반환(안정성 위해).
        """
        if not self._spotify_enabled():
            return None

        try:
            max_n = int(os.getenv("YUME_SPOTIFY_IMPORT_MAX", "50"))
        except Exception:
            max_n = 50
        max_n = max(1, min(200, max_n))

        out: List[str] = []
        url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=100"

        while url and len(out) < max_n:
            js = await self._spotify_api_get(session, url)
            if not js:
                break
            items = js.get("items") or []
            for it in items:
                tr = (it or {}).get("track") or {}
                name = str(tr.get("name") or "").strip()
                artists = tr.get("artists") or []
                artist = str(artists[0].get("name") or "").strip() if artists else ""
                q = f"{name} {artist}".strip()
                if q:
                    out.append(q)
                if len(out) >= max_n:
                    break
            url = js.get("next")

        return out

    async def _resolve_stream_url(self, track: _Track) -> Optional[str]:
        """
        track.webpage_url(유튜브 URL 또는 ytsearch1:...)로 yt_dlp를 돌려
        "진짜 ffmpeg가 재생 가능한 오디오 스트림 URL"을 얻는다.

        Phase2: playlist는 ytsearch1:로 큐에 들어갈 수 있으므로,
        1차 extract에서 URL이 http(s)가 아니면(=id 등) 2차 extract로 formats 확보한다.
        """
        if track._resolved_stream_url and (time.time() - track._resolved_at) < 30:
            return track._resolved_stream_url

        try:
            info = await _extract_info(track.webpage_url)
            entry = _pick_entry(info)
            if not entry:
                return None

            try:
                real_title = entry.get("title")
                real_page = entry.get("webpage_url") or entry.get("original_url")
                if real_title and isinstance(real_title, str):
                    track.title = real_title
                if real_page and isinstance(real_page, str) and real_page.startswith("http"):
                    track.webpage_url = real_page
            except Exception:
                pass

            url = _select_best_audio_url(entry)

            if not url or not re.match(r"^https?://", str(url)):
                page = entry.get("webpage_url") or entry.get("original_url")
                if page and page != track.webpage_url:
                    info2 = await _extract_info(str(page))
                    entry2 = _pick_entry(info2) or info2
                    url = _select_best_audio_url(entry2)

            if not url or not re.match(r"^https?://", str(url)):
                return None

            track._resolved_stream_url = str(url)
            track._resolved_at = time.time()
            return track._resolved_stream_url

        except Exception as e:
            logger.warning("[Music] resolve error: %s", e)
            return None

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

            if vc is None or not vc.is_connected():
                st.now_playing = None
                continue

            stream_url = await self._resolve_stream_url(track)
            if not stream_url:
                self._set_error(guild_id, "재생 URL을 해상하지 못했어(yt-dlp).")
                st.now_playing = None
                await self._refresh_panel(guild_id)
                continue

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
                src = _ffmpeg_source(stream_url, volume=st.volume, af_filters=self._build_af_filters(st))

                st.play_started_at = self.bot.loop.time()
                st.paused_at = None
                st.paused_total = 0.0

                await self._lyrics_on_track_start(guild_id)

                vc.play(src, after=_after)
                await done.wait()

            except Exception as e:
                logger.warning("[Music] play error: %s", e)
                self._set_error(guild_id, f"재생 예외: {e}")

            finally:
                finished = st.now_playing
                st.now_playing = None

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



    def _fx_summary(self, st: MusicState) -> str:
        parts = []
        if st.fx_eq_enabled and (abs(st.fx_bass_db) > 0.01 or abs(st.fx_mid_db) > 0.01 or abs(st.fx_treble_db) > 0.01 or abs(st.fx_preamp_db) > 0.01):
            parts.append(f"EQ ON (B{st.fx_bass_db:+.0f} M{st.fx_mid_db:+.0f} T{st.fx_treble_db:+.0f} P{st.fx_preamp_db:+.0f})")
        else:
            parts.append("EQ OFF")

        if st.fx_reverb_enabled and int(st.fx_reverb_mix) > 0:
            parts.append(f"RVB {int(st.fx_reverb_mix)}%/{int(st.fx_reverb_room)}%")
        else:
            parts.append("RVB OFF")

        if st.fx_tune_enabled and abs(st.fx_tune_semitones) > 0.01:
            parts.append(f"TUNE {st.fx_tune_semitones:+.1f}")
        else:
            parts.append("TUNE 0")

        return " | ".join(parts)
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

        if now_url and isinstance(now_url, str) and now_url.startswith("http"):
            embed.add_field(name="🎧 지금 재생", value=f"[{now_title}]({now_url})", inline=False)
        else:
            embed.add_field(name="🎧 지금 재생", value=now_title, inline=False)

        embed.add_field(name="📃 큐", value=f"{st.queue.qsize()}곡", inline=True)
        embed.add_field(name="🔁 반복", value="ON" if st.loop_all else "OFF", inline=True)
        embed.add_field(name="🔊 볼륨", value=f"{int(st.volume * 100)}%", inline=True)
        embed.add_field(name="🎚️ FX", value=self._fx_summary(st), inline=True)

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
        fixed_channel_id, _ = self._fixed_panel(guild_id)
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
        if interaction.guild is None:
            return
        await self._refresh_panel(interaction.guild.id, hint_channel_id=interaction.channel_id)

    async def _enqueue_from_interaction(self, interaction: discord.Interaction, query: str):
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

    async def _enqueue_spotify_from_interaction(self, interaction: discord.Interaction, query: str):
        """
        Phase2:
        - Spotify track: 제목/아티스트 -> 유튜브 검색으로 1곡 추가
        - Spotify playlist: (API 필요) 트랙들 -> ytsearch1:... 로 대량 큐 적재 (재생 직전 해상)
        """
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        if interaction.guild is None:
            return

        raw = (query or "").strip()
        if not raw:
            try:
                await interaction.followup.send("검색어/URL이 비어있어.", ephemeral=True)
            except Exception:
                pass
            return

        vc = await self._ensure_voice_interaction(interaction)
        if not vc:
            in_voice = isinstance(interaction.user, discord.Member) and interaction.user.voice and interaction.user.voice.channel
            msg = "먼저 음성 채널에 들어가줘." if not in_voice else "음성 채널에 연결하지 못했어."
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass
            return

        kind, sid = self._parse_spotify(raw)

        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if kind == "playlist" and sid:
                qs = await self._spotify_playlist_queries(session, sid)
                if not qs:
                    msg = (
                        "플레이리스트를 가져오려면 Spotify API 키가 필요해.\n"
                        "서버 .env에 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET 넣고 재시작해줘."
                    )
                    try:
                        await interaction.followup.send(msg, ephemeral=True)
                    except Exception:
                        pass
                    return

                st = self._state(interaction.guild.id)
                added = 0
                for q in qs:
                    t = _Track(
                        title=q,
                        webpage_url=f"ytsearch1:{q}",
                        requester_id=interaction.user.id,
                    )
                    await st.queue.put(t)
                    added += 1

                self._start_player_if_needed(interaction.guild.id)
                await self._refresh_from_interaction(interaction)
                try:
                    await interaction.followup.send(f"플레이리스트에서 **{added}곡** 큐에 추가했어.", ephemeral=True)
                except Exception:
                    pass
                return

            if kind == "track" and sid:
                url = f"https://open.spotify.com/track/{sid}"
                q = await self._spotify_track_query(session, sid, url)
                await self._enqueue_from_interaction(interaction, q)
                return

            await self._enqueue_from_interaction(interaction, raw)

    async def _set_volume_from_interaction(self, interaction: discord.Interaction, raw: str):
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

    async def _toggle_pause(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        vc = interaction.guild.voice_client
        try:
            st = self._state(interaction.guild.id)
            if vc and vc.is_connected() and vc.is_playing():
                if st.paused_at is None:
                    st.paused_at = self.bot.loop.time()
                vc.pause()
                await interaction.response.send_message("잠깐 멈출게.", ephemeral=True)
            elif vc and vc.is_connected() and vc.is_paused():
                if st.paused_at is not None:
                    st.paused_total += max(0.0, self.bot.loop.time() - st.paused_at)
                    st.paused_at = None
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


    def _build_af_filters(self, st: MusicState) -> Optional[str]:
        chain: List[str] = []

        if st.fx_tune_enabled and abs(st.fx_tune_semitones) > 0.01:
            factor = 2 ** (float(st.fx_tune_semitones) / 12.0)
            inv = 1.0 / factor
            chain.append(f"asetrate=48000*{factor}")
            chain.append("aresample=48000")
            chain.append(f"atempo={inv}")

        if abs(st.fx_preamp_db) > 0.01:
            chain.append(f"volume={float(st.fx_preamp_db)}dB")

        if st.fx_eq_enabled:
            if abs(st.fx_bass_db) > 0.01:
                chain.append(f"bass=g={float(st.fx_bass_db)}:f=100:w=0.5")
            if abs(st.fx_mid_db) > 0.01 and (self._ffmpeg_filters is not None and 'equalizer' in self._ffmpeg_filters):
                chain.append(f"equalizer=f=1000:t=q:w=1:g={float(st.fx_mid_db)}")
            if abs(st.fx_treble_db) > 0.01:
                chain.append(f"treble=g={float(st.fx_treble_db)}:f=3500:w=0.5")

        if st.fx_reverb_enabled and int(st.fx_reverb_mix) > 0:
            mix = max(0, min(100, int(st.fx_reverb_mix))) / 100.0
            room = max(0, min(100, int(st.fx_reverb_room))) / 100.0
            d1 = int(60 + 200 * room)
            d2 = int(d1 * 2)
            decay1 = min(0.95, 0.20 + 0.60 * mix)
            decay2 = min(0.95, 0.12 + 0.45 * mix)
            chain.append(f"aecho=0.8:0.9:{d1}|{d2}:{decay1}|{decay2}")

        return ",".join(chain) if chain else None
    async def _replay_current_from_start(self, guild_id: int) -> bool:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        st = self._state(guild_id)
        vc = guild.voice_client
        if vc is None or (not vc.is_connected()):
            return False
        if st.now_playing is None:
            return False

        cur = st.now_playing

        items: List[_Track] = []
        try:
            while not st.queue.empty():
                items.append(st.queue.get_nowait())
        except Exception:
            pass

        try:
            st.queue.put_nowait(cur)
        except Exception:
            pass
        for t in items:
            try:
                st.queue.put_nowait(t)
            except Exception:
                pass

        st._suppress_requeue_once = True
        try:
            vc.stop()
        except Exception:
            return False
        return True


    async def _toggle_eq(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)
        async with st.lock:
            st.fx_eq_enabled = not bool(st.fx_eq_enabled)
            await self._persist_fx_cfg_from_state(gid, st)
            restarted = await self._replay_current_from_start(gid)

        msg = f"EQ: {'ON' if st.fx_eq_enabled else 'OFF'}" + (" (현재 곡 재시작)" if restarted else " (다음 곡부터)")
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass
        await self._refresh_from_interaction(interaction)

    async def _toggle_reverb(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)
        async with st.lock:
            st.fx_reverb_enabled = not bool(st.fx_reverb_enabled)
            await self._persist_fx_cfg_from_state(gid, st)
            restarted = await self._replay_current_from_start(gid)

        msg = f"리버브: {'ON' if st.fx_reverb_enabled else 'OFF'}" + (" (현재 곡 재시작)" if restarted else " (다음 곡부터)")
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass
        await self._refresh_from_interaction(interaction)

    async def _toggle_tune(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)
        async with st.lock:
            st.fx_tune_enabled = not bool(st.fx_tune_enabled)
            await self._persist_fx_cfg_from_state(gid, st)
            restarted = await self._replay_current_from_start(gid)

        msg = f"튠: {'ON' if st.fx_tune_enabled else 'OFF'}" + (" (현재 곡 재시작)" if restarted else " (다음 곡부터)")
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass
        await self._refresh_from_interaction(interaction)

    async def _set_eq_settings(self, interaction: discord.Interaction, *, guild_id: int, bass: float, mid: float, treble: float, preamp: float):
        st = self._state(guild_id)
        async with st.lock:
            st.fx_bass_db = float(bass)
            st.fx_mid_db = float(mid)
            st.fx_treble_db = float(treble)
            st.fx_preamp_db = float(preamp)
            st.fx_eq_enabled = True
            await self._persist_fx_cfg_from_state(guild_id, st)
            restarted = await self._replay_current_from_start(guild_id)

        msg = f"EQ 설정 저장: B{bass:+.1f} M{mid:+.1f} T{treble:+.1f} P{preamp:+.1f}" + (" (현재 곡 재시작)" if restarted else " (다음 곡부터)")
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass
        await self._refresh_from_interaction(interaction)

    async def _set_reverb_settings(self, interaction: discord.Interaction, *, guild_id: int, mix: int, room: int):
        st = self._state(guild_id)
        async with st.lock:
            st.fx_reverb_mix = int(mix)
            st.fx_reverb_room = int(room)
            st.fx_reverb_enabled = True
            await self._persist_fx_cfg_from_state(guild_id, st)
            restarted = await self._replay_current_from_start(guild_id)

        msg = f"리버브 설정 저장: mix {mix}% / room {room}%" + (" (현재 곡 재시작)" if restarted else " (다음 곡부터)")
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass
        await self._refresh_from_interaction(interaction)

    async def _set_tune_settings(self, interaction: discord.Interaction, *, guild_id: int, semitones: float):
        st = self._state(guild_id)
        async with st.lock:
            st.fx_tune_semitones = float(semitones)
            st.fx_tune_enabled = True
            await self._persist_fx_cfg_from_state(guild_id, st)
            restarted = await self._replay_current_from_start(guild_id)

        msg = f"튠 설정 저장: {semitones:+.2f} semitone" + (" (현재 곡 재시작)" if restarted else " (다음 곡부터)")
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass
        await self._refresh_from_interaction(interaction)

    async def _reset_fx(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)
        async with st.lock:
            st.fx_eq_enabled = False
            st.fx_bass_db = 0.0
            st.fx_mid_db = 0.0
            st.fx_treble_db = 0.0
            st.fx_preamp_db = 0.0

            st.fx_reverb_enabled = False
            st.fx_reverb_mix = 0
            st.fx_reverb_room = 50

            st.fx_tune_enabled = False
            st.fx_tune_semitones = 0.0

            await self._persist_fx_cfg_from_state(gid, st)
            restarted = await self._replay_current_from_start(gid)

        msg = "FX 초기화 완료" + (" (현재 곡 재시작)" if restarted else "")
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass
        await self._refresh_from_interaction(interaction)

    async def _cycle_fx_preset(self, interaction: discord.Interaction):
        await self._toggle_eq(interaction)

    async def _cycle_reverb_level(self, interaction: discord.Interaction):
        await self._toggle_reverb(interaction)
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

        self._cancel_auto_leave(guild_id)

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

            try:
                while not st.queue.empty():
                    st.queue.get_nowait()
            except Exception:
                pass

            st.now_playing = None

            if reason:
                self._set_error(guild_id, reason)

        try:
            if vc and vc.is_connected():
                await vc.disconnect()
        except Exception:
            pass

        try:
            await self._refresh_panel(guild_id)
        except Exception:
            pass

    def _build_queue_embed(self, guild: discord.Guild) -> discord.Embed:
        st = self._state(guild.id)
        vc = guild.voice_client

        embed = discord.Embed(
            title="유메 - 큐 관리",
            description="번호로 삭제/정리할 수 있어. (예: 3,5,7 / 2-6)",
            color=discord.Color.blurple(),
        )

        if st.now_playing and st.now_playing.webpage_url and st.now_playing.webpage_url.startswith("http"):
            embed.add_field(
                name="🎧 지금 재생",
                value=f"[{st.now_playing.title}]({st.now_playing.webpage_url})",
                inline=False,
            )
        elif st.now_playing:
            embed.add_field(name="🎧 지금 재생", value=st.now_playing.title, inline=False)
        else:
            embed.add_field(name="🎧 지금 재생", value="없음", inline=False)

        items: List[_Track] = []
        try:
            items = list(getattr(st.queue, "_queue", []))  # type: ignore[arg-type]
        except Exception:
            items = []

        total = len(items)
        if total <= 0:
            q_text = "비어있음"
        else:
            lines: List[str] = []
            for i, t in enumerate(items[:15], start=1):
                if t.webpage_url and t.webpage_url.startswith("http"):
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
        if interaction is not None and getattr(interaction, "message", None) is not None:
            try:
                await interaction.response.edit_message(embed=embed, view=view)
                return True
            except Exception:
                pass

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



    def _lyrics_cache_key(self, track: _Track) -> str:
        tn, ar = _guess_artist_title(track.title)
        ar = ar or track.artist or ""
        return f"{tn}|||{ar}".strip()

    def _current_pos(self, st: MusicState) -> float:
        if st.play_started_at <= 0:
            return 0.0
        now = self.bot.loop.time()
        if st.paused_at is not None:
            now = st.paused_at
        pos = now - st.play_started_at - st.paused_total
        if pos < 0:
            pos = 0.0
        return float(pos)

    async def _disable_lyrics(self, guild_id: int, *, delete_message: bool):
        st = self._state(guild_id)
        st.lyrics_enabled = False

        if st.lyrics_task and not st.lyrics_task.done():
            try:
                st.lyrics_task.cancel()
            except Exception:
                pass
        st.lyrics_task = None

        if delete_message and st.lyrics_channel_id and st.lyrics_message_id:
            ch = self.bot.get_channel(st.lyrics_channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    msg = await ch.fetch_message(st.lyrics_message_id)
                    await msg.delete()
                except Exception:
                    pass

        st.lyrics_channel_id = None
        st.lyrics_message_id = None
        st._lyrics_last_track_key = None
        st._lyrics_last_render_key = None

    async def _toggle_lyrics(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)

        if st.lyrics_enabled:
            await self._disable_lyrics(gid, delete_message=True)
            try:
                await interaction.response.send_message("가사 표시를 껐어.", ephemeral=True)
            except Exception:
                pass
            return

        st.lyrics_enabled = True

        cid, _mid = self._fixed_panel(gid)
        if not cid:
            cid = interaction.channel_id
        st.lyrics_channel_id = cid

        if st.lyrics_task is None or st.lyrics_task.done():
            st.lyrics_task = asyncio.create_task(self._lyrics_loop(gid))

        try:
            await interaction.response.send_message("가사 표시를 켰어. 🎤", ephemeral=True)
        except Exception:
            pass

    async def _lyrics_on_track_start(self, guild_id: int):
        st = self._state(guild_id)
        if not st.lyrics_enabled:
            return
        if st.lyrics_task is None or st.lyrics_task.done():
            st.lyrics_task = asyncio.create_task(self._lyrics_loop(guild_id))

        if not st.lyrics_channel_id:
            cid, _ = self._fixed_panel(guild_id)
            if cid:
                st.lyrics_channel_id = cid


    async def _fetch_lrclib(self, track_name: str, artist_name: Optional[str]) -> Optional[str]:
        params = {"track_name": track_name}
        if artist_name:
            params["artist_name"] = artist_name

        timeout = aiohttp.ClientTimeout(total=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(LRCLIB_API_BASE, params=params) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
        except Exception:
            return None

        lrc = data.get("syncedLyrics") or data.get("synced_lyrics")
        if isinstance(lrc, str) and lrc.strip():
            return lrc

        return None

    def _build_lyrics_embed(
        self,
        guild: discord.Guild,
        track: Optional[_Track],
        lines: List[Tuple[float, str]],
        pos: float,
    ) -> discord.Embed:
        embed = discord.Embed(title="🎤 유메 - 가사")
        if track:
            if track.webpage_url:
                embed.description = f"[{track.title}]({track.webpage_url})"
            else:
                embed.description = track.title

        if not track:
            embed.add_field(name="상태", value="재생 중인 곡이 없어.", inline=False)
            return embed

        if not lines:
            embed.add_field(name="가사", value="`가사를 찾지 못했어.`", inline=False)
            return embed

        times = [t for t, _ in lines]
        idx = bisect.bisect_right(times, pos) - 1
        idx = max(0, min(idx, len(lines) - 1))

        prev_txt = lines[idx - 1][1] if idx - 1 >= 0 else ""
        cur_txt = lines[idx][1]
        next_txt = lines[idx + 1][1] if idx + 1 < len(lines) else ""

        desc = ""
        if prev_txt:
            desc += f"_{prev_txt}_\n"
        desc += f"**{cur_txt}**\n"
        if next_txt:
            desc += f"_{next_txt}_\n"

        mm = int(pos // 60)
        ss = int(pos % 60)
        embed.add_field(name=f"⏱ {mm:02d}:{ss:02d}", value=desc[:1024] or " ", inline=False)
        embed.set_footer(text="가사 데이터: LRCLIB (가능한 곡만 제공돼)")
        return embed

    async def _lyrics_loop(self, guild_id: int):
        last_embed_key: Optional[str] = None
        while True:
            st = self._state(guild_id)
            if not st.lyrics_enabled:
                break

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                await asyncio.sleep(2.0)
                continue

            if not st.lyrics_channel_id:
                cid, _ = self._fixed_panel(guild_id)
                if cid:
                    st.lyrics_channel_id = cid

            ch = self.bot.get_channel(st.lyrics_channel_id) if st.lyrics_channel_id else None
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                await asyncio.sleep(2.0)
                continue

            msg = None
            if st.lyrics_message_id:
                try:
                    msg = await ch.fetch_message(st.lyrics_message_id)
                except Exception:
                    st.lyrics_message_id = None
                    msg = None

            if msg is None:
                try:
                    m = await ch.send(embed=discord.Embed(title="🎤 유메 - 가사", description="가사를 준비하는 중..."))
                    st.lyrics_message_id = m.id
                    msg = m
                except Exception:
                    await asyncio.sleep(2.0)
                    continue

            track = st.now_playing
            pos = self._current_pos(st)

            track_key = self._lyrics_cache_key(track) if track else None
            if track_key != st._lyrics_last_track_key:
                st._lyrics_last_track_key = track_key
                st._lyrics_last_render_key = None  # 강제 갱신

            lines_lrc: List[Tuple[float, str]] = []
            if track:
                key = self._lyrics_cache_key(track)
                if key in st.lyrics_cache:
                    lines_lrc = st.lyrics_cache[key]
                else:
                    tn, ar = _guess_artist_title(track.title)
                    ar = ar or track.artist
                    lrc = await self._fetch_lrclib(tn, ar)
                    lines_lrc = _parse_lrc(lrc or "")
                    st.lyrics_cache[key] = lines_lrc

            embed = self._build_lyrics_embed(guild, track, lines_lrc, pos)
            try:
                embed_key = json.dumps(embed.to_dict(), ensure_ascii=False)
            except Exception:
                embed_key = None

            if embed_key and embed_key == last_embed_key:
                await asyncio.sleep(2.5)
                continue
            last_embed_key = embed_key

            try:
                await msg.edit(embed=embed)
            except Exception:
                st.lyrics_message_id = None

            await asyncio.sleep(2.5)

    def _parse_index_spec(self, spec: str, *, max_n: int) -> List[int]:
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

    @commands.command(name="음악채널지정")
    @commands.has_permissions(manage_guild=True)
    async def set_music_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """!음악채널지정 <채널ID>: 지정한 채널에 음악 패널을 항상 고정한다."""
        if ctx.guild is None:
            await ctx.send("서버 채널에서만 쓸 수 있어.")
            return

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
            await self._ensure_panel_message(ctx.guild.id, fixed_channel_id, fixed=True)
            await self._refresh_panel(ctx.guild.id)
            try:
                await ctx.send(f"패널은 <#{fixed_channel_id}>에 있어.", delete_after=5)
            except Exception:
                pass
            return

        embed = self._build_embed(ctx.guild)
        msg = await ctx.send(embed=embed, view=self.panel_view)
        st = self._state(ctx.guild.id)
        st.temp_panel_channel_id = ctx.channel.id
        st.temp_panel_message_id = msg.id


async def setup(bot: commands.Bot):
    cog = MusicCog(bot)
    await bot.add_cog(cog)

    try:
        bot.add_view(cog.panel_view)
        bot.add_view(cog.queue_view)
    except Exception:
        pass
