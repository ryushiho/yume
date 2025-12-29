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
CACHE_CFG_PATH = os.path.join(STORAGE_DIR, "music_cache.json")



YTDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "default_search": "ytsearch",
    "noplaylist": True,
    "nocheckcertificate": True,
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
# ffmpeg는 스트림 연결이 흔들릴 때 warning 로그를 많이 뿜는다.
# (예: "Connection reset by peer")
# 서비스 재시작/음악 스킵 때마다 journalctl이 지저분해지니,
# 기본 로그 레벨을 error로 낮춰서 '정말 중요한 오류'만 남긴다.
FFMPEG_OPTIONS = "-vn -hide_banner -loglevel error"
FFMPEG_EXECUTABLE = os.getenv("YUME_FFMPEG_PATH", "ffmpeg")

_ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)



@dataclass
class _Track:
    title: str
    webpage_url: str
    requester_id: Optional[int] = None

    duration_sec: Optional[int] = None
    is_live: bool = False


    # Phase1: Spotify 등 외부 소스 메타(가사 검색/표시 정확도)
    meta_track: Optional[str] = None
    meta_artist: Optional[str] = None
    spotify_track_id: Optional[str] = None
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



# =========================
# Phase2: ytsearch 후보 채점 선택
# =========================

_WORD_RE = re.compile(r"[^0-9a-zA-Z가-힣\s]+")

def _norm_for_match(s: str) -> str:
    s = (s or "").lower()
    # 구분자/괄호류는 공백으로
    s = s.replace("—", " ").replace("–", " ").replace("-", " ").replace("|", " ")
    s = re.sub(r"[\[\]\(\)\{\}<>]", " ", s)
    s = _WORD_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _tokens(s: str) -> List[str]:
    s = _norm_for_match(s)
    toks = [t for t in s.split() if len(t) >= 2]
    # 중복 제거(순서 유지)
    seen = set()
    out = []
    for t in toks:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out

def _wanted_meta(track: "_Track", query_text: Optional[str]) -> Tuple[str, str]:
    """후보 선택에 쓸 (want_title, want_artist)를 만든다."""
    want_title = (track.meta_track or "").strip()
    want_artist = (track.meta_artist or "").strip()

    if not want_title:
        gt, ga = _guess_artist_title(track.title or "")
        want_title = (gt or "").strip()
        if not want_artist:
            want_artist = (ga or "").strip()

    if not want_title and query_text:
        want_title = str(query_text).strip()

    return (want_title, want_artist)

def _score_yt_candidate(entry: dict, want_title: str, want_artist: str) -> float:
    title = str(entry.get("title") or "")
    uploader = str(entry.get("uploader") or entry.get("channel") or entry.get("uploader_id") or "")
    t = _norm_for_match(title)
    u = _norm_for_match(uploader)
    full = f"{t} {u}".strip()

    score = 0.0

    # 라이브/스트림은 웬만하면 제외
    try:
        if entry.get("is_live") or str(entry.get("live_status") or "").lower() in {"is_live", "live"}:
            score -= 120.0
    except Exception:
        pass

    # 제목/아티스트 매칭 가점
    if want_title:
        wt = _norm_for_match(want_title)
        if wt and wt in t:
            score += 30.0
        for tok in _tokens(wt):
            score += 6.0 if tok in t else -2.0

    if want_artist:
        wa = _norm_for_match(want_artist)
        if wa and wa in full:
            score += 20.0
        for tok in _tokens(wa):
            score += 4.0 if tok in full else -1.0

    # 좋은 신호
    if "topic" in u:
        score += 10.0
    if "official" in u or "official" in t:
        score += 4.0
    if "official audio" in t:
        score += 8.0

    # 나쁜 신호(강한 페널티)
    bad_kw = [
        "cover", "karaoke", "instrumental", "inst", "remix", "nightcore",
        "8d", "sped up", "slowed", "teaser", "shorts", "fanmade", "edit",
        "reaction", "compilation", "mix",
    ]
    for kw in bad_kw:
        if kw in t:
            score -= 18.0

    # 컴필/모음집 류는 큰 페널티(지금처럼 'Greatest Hits' 문제 방지)
    comp_kw = ["greatest hits", "best of", "the best", "hits", "collection"]
    for kw in comp_kw:
        if kw in t:
            score -= 25.0

    # lyric video는 완전 배제까진 하지 않되, 살짝만 감점
    if ("lyric" in t) or ("lyrics" in t) or ("가사" in t) or ("자막" in t):
        score -= 3.0

    # 길이 sanity check(너무 짧거나 너무 길면 감점)
    try:
        dur = entry.get("duration")
        if isinstance(dur, (int, float)):
            d = int(dur)
            if d < 60 or d > 900:
                score -= 10.0
    except Exception:
        pass

    return score

def _pick_best_ytsearch_entry(entries: List[dict], track: "_Track", query_text: Optional[str]) -> dict:
    clean = [e for e in (entries or []) if e]
    if not clean:
        return {}

    want_title, want_artist = _wanted_meta(track, query_text)

    scored: List[Tuple[float, dict]] = []
    for e in clean:
        try:
            s = _score_yt_candidate(e, want_title, want_artist)
        except Exception:
            s = -9999.0
        scored.append((s, e))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]

    if logger.isEnabledFor(logging.DEBUG):
        try:
            top = scored[:3]
            dbg = ", ".join([f"{sc:.1f}:{str(en.get('title') or '')[:40]}" for sc, en in top])
            logger.debug("[Music][Phase2] ytsearch pick=%.1f want=(%s/%s) top=%s", best_score, want_title, want_artist, dbg)
        except Exception:
            pass

    # 점수가 너무 낮으면(모두 엉망) 첫 번째 fallback
    if best_score < -50.0:
        for e in clean:
            if e:
                return e
    return best


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


def _ffmpeg_source(
    stream_url: str,
    volume: float,
    *,
    af_filters: Optional[str] = None,
    seek_sec: Optional[float] = None,
    limit_sec: Optional[float] = None,
) -> discord.AudioSource:
    """ffmpeg 오디오 소스 생성.

    Phase3:
    - seek_sec: -ss (입력 앞, before_options)
    - limit_sec: -t (출력 옵션)
    """

    before = FFMPEG_BEFORE
    try:
        if seek_sec is not None and float(seek_sec) > 0:
            before = f"{before} -ss {float(seek_sec):.3f}"
    except Exception:
        pass

    opts = FFMPEG_OPTIONS
    try:
        if limit_sec is not None and float(limit_sec) > 0:
            opts = f"{opts} -t {float(limit_sec):.3f}"
    except Exception:
        pass

    if af_filters:
        opts = f"{opts} -af {af_filters}"

    src = discord.FFmpegPCMAudio(
        stream_url,
        before_options=before,
        options=opts,
        executable=FFMPEG_EXECUTABLE,
    )
    return discord.PCMVolumeTransformer(src, volume=volume)



LRCLIB_API_BASE = "https://lrclib.net/api/get"

_TAG_LINE_RE = re.compile(r"^\s*\[(ar|ti|al|by|offset):", re.IGNORECASE)
_TS_RE = re.compile(r"\[(\d+):(\d+)(?:\.(\d+))?\]")

def _clean_title(s: str) -> str:
    """YouTube 제목을 가사 검색용 키워드로 최대한 '깨끗하게' 만든다.

    흔히 붙는 꼬리표([Official Video], (MV), | ... , feat. ... 등)를 제거해서
    LRCLIB 검색 성공률을 올린다.
    """
    s = (s or "").strip()
    if not s:
        return ""

    # 유니코드 구분자 정리
    s = s.replace("｜", "|").replace("—", "-").replace("–", "-").replace("·", "-")
    s = re.sub(r"\s+", " ", s).strip()

    # '|' 뒤는 보통 꼬리표(Official Video 등)인 경우가 많아서 우선 잘라낸다.
    if "|" in s:
        s = s.split("|", 1)[0].strip()

    # 특정 키워드가 들어있는 괄호/대괄호 구간 제거
    noise_kw = re.compile(
        r"(official|music\s*video|mv|m/v|lyric|lyrics|audio|video|performance|live|hd|4k|visualizer|karaoke)",
        re.IGNORECASE,
    )

    def _strip_bracketed(text: str, open_ch: str, close_ch: str) -> str:
        # 반복 제거(중첩/여러개 대응)
        while True:
            m = re.search(rf"\{open_ch}([^\{close_ch}]*)\{close_ch}", text)
            if not m:
                break
            inner = (m.group(1) or "").strip()
            # feat/ft도 꼬리표로 취급
            if noise_kw.search(inner) or re.search(r"\b(feat\.?|ft\.?|featuring)\b", inner, re.IGNORECASE):
                text = (text[: m.start()] + " " + text[m.end() :]).strip()
            else:
                # 의미있는 괄호는 남긴다(예: (Japanese Ver.))
                break
        return text

    s = _strip_bracketed(s, "[", "]")
    s = _strip_bracketed(s, "(", ")")

    # 뒤쪽에 붙는 ' - Official Video' 같은 꼬리표 제거(여러 번 반복)
    tail_noise = re.compile(
        r"^(official|music\s*video|mv|m/v|lyric(s)?|audio|video|performance|live|hd|4k|visualizer|karaoke)$",
        re.IGNORECASE,
    )
    while True:
        parts = [p.strip() for p in s.split("-") if p.strip()]
        if len(parts) >= 2 and tail_noise.match(parts[-1]):
            s = " - ".join(parts[:-1]).strip()
            continue
        break

    # feat / featuring 꼬리표 제거 (끝부분 위주)
    s = re.sub(r"\s*\b(feat\.?|ft\.?|featuring)\b\s+.*$", "", s, flags=re.IGNORECASE).strip()

    # 끝 장식 문자 정리
    s = s.strip("-–—| ").strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s

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



def _normalize_lyric_term(s: str) -> str:
    """LRCLIB 질의용 문자열 정규화(트랙/아티스트 공용)."""
    s = _clean_title(s or "")
    if not s:
        return ""
    # 따옴표/장식 제거
    s = re.sub(r"[\"\'’`]", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _split_bracket_variants(s: str) -> List[str]:
    """괄호/대괄호에 들어있는 별칭까지 포함해서 여러 후보를 만든다.

    예) '비밀정원 (Secret Garden)' -> ['비밀정원 (Secret Garden)', '비밀정원', 'Secret Garden']
    예) '오마이걸 (OH MY GIRL)' -> ['오마이걸 (OH MY GIRL)', '오마이걸', 'OH MY GIRL']
    """
    s = (s or "").strip()
    if not s:
        return []
    out: List[str] = []
    def _add(x: str):
        x = _normalize_lyric_term(x)
        if x and x not in out:
            out.append(x)

    _add(s)

    # () 안/밖 분리
    base = re.sub(r"\([^)]*\)", "", s).strip()
    if base:
        _add(base)
    for inner in re.findall(r"\(([^)]{1,80})\)", s):
        _add(inner)

    # [] 안/밖 분리
    base2 = re.sub(r"\[[^\]]*\]", "", s).strip()
    if base2:
        _add(base2)
    for inner in re.findall(r"\[([^\]]{1,80})\]", s):
        _add(inner)

    return out

def _build_lrclib_candidates(track: "_Track") -> List[Tuple[str, Optional[str]]]:
    """LRCLIB 검색 후보(track_name, artist_name) 목록을 '우선순위' 순으로 만든다."""
    cands: List[Tuple[str, Optional[str]]] = []

    # 1) Spotify 메타가 있으면 최우선
    meta_t = _normalize_lyric_term(getattr(track, "meta_track", "") or "")
    meta_a = _normalize_lyric_term(getattr(track, "meta_artist", "") or "") or _normalize_lyric_term(getattr(track, "artist", "") or "")

    # 2) YouTube/표시 제목에서 추정
    base_t, base_a = _guess_artist_title(track.title or "")
    base_t = _normalize_lyric_term(base_t)
    base_a = _normalize_lyric_term(base_a or "")

    # 후보 문자열 리스트(우선순위 유지)
    track_terms: List[str] = []
    artist_terms: List[str] = []

    def _push_term(lst: List[str], term: str):
        term = _normalize_lyric_term(term)
        if term and term not in lst:
            lst.append(term)

    for t in _split_bracket_variants(meta_t) + _split_bracket_variants(base_t):
        _push_term(track_terms, t)
    for a in _split_bracket_variants(meta_a) + _split_bracket_variants(base_a):
        _push_term(artist_terms, a)

    # (track, artist) 조합 생성 (폭발 방지: 상위 몇 개만)
    track_terms = track_terms[:4]
    artist_terms = artist_terms[:3]

    def _add_pair(t: str, a: Optional[str]):
        t = _normalize_lyric_term(t)
        a2 = _normalize_lyric_term(a or "") if a else ""
        if not t:
            return
        key = (t.lower(), a2.lower() if a2 else "")
        for (et, ea) in cands:
            if (et.lower(), (ea or "").lower()) == key:
                return
        cands.append((t, a2 or None))

    # 우선: 아티스트 포함
    for t in track_terms:
        for a in artist_terms:
            _add_pair(t, a)

    # 다음: track만으로도 시도
    for t in track_terms:
        _add_pair(t, None)

    # 마지막 보험: 원본 제목을 한 번 더(정규화)로
    raw_t = _normalize_lyric_term(track.title or "")
    if raw_t:
        _add_pair(raw_t, None)

    return cands[:10]

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




def _strip_lrc_to_plain(lrc_text: str) -> str:
    """syncedLyrics(LRC)에서 타임코드를 제거해 '순수 가사'만 남긴다."""
    if not lrc_text:
        return ""
    out_lines: List[str] = []
    for raw_line in lrc_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _TAG_LINE_RE.match(line):
            continue
        # 모든 타임스탬프 제거
        line = _TS_RE.sub("", line).strip()
        if line:
            out_lines.append(line)
    return "\n".join(out_lines).strip()


def _parse_plain_lyrics(text: str, *, interval_sec: float = 3.0) -> List[Tuple[float, str]]:
    """plainLyrics(타임코드 없음)를 기존 가사 루프가 쓸 수 있게 '가짜 타임코드'로 변환한다.

    - 표시 목적(타임코드 없는 가사)이라 정확한 싱크는 보장하지 않는다.
    - 1줄당 interval_sec 간격으로 time을 배치한다.
    """
    if not text:
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: List[Tuple[float, str]] = []
    t = 0.0
    for ln in lines:
        out.append((t, ln))
        t += float(interval_sec)
    return out
class MusicState:
    def __init__(self):
        self.queue: asyncio.Queue[_Track] = asyncio.Queue()
        self.now_playing: Optional[_Track] = None
        self.player_task: Optional[asyncio.Task] = None

        self.play_started_at: float = 0.0  # loop.time() 기준
        self.paused_at: Optional[float] = None
        self.paused_total: float = 0.0

        # 점프/구간 재생(Phase3)
        self.play_seek_base: float = 0.0  # 현재 곡의 시작 오프셋(초)
        self.seek_next_sec: Optional[float] = None  # 다음 재생에서 1회 적용
        self.segment_start_sec: Optional[float] = None
        self.segment_end_sec: Optional[float] = None
        self.segment_ab_repeat: bool = False

        # UI 갱신 직렬화/틱 갱신(Phase3)
        self.ui_lock: asyncio.Lock = asyncio.Lock()
        self.panel_tick_task: Optional[asyncio.Task] = None
        self._panel_last_render_key: Optional[str] = None

        self.lock: asyncio.Lock = asyncio.Lock()

        self.auto_leave_task: Optional[asyncio.Task] = None

        self.volume: float = 1.0
        self.loop_all: bool = False


        self.fx_eq_enabled: bool = False
        self.fx_bass_db: float = 0.0
        self.fx_mid_db: float = 0.0
        self.fx_treble_db: float = 0.0
        self.fx_preamp_db: float = 0.0

        self._suppress_requeue_once: bool = False

        self.last_error: Optional[str] = None
        self.last_error_at: float = 0.0

        self.temp_panel_channel_id: Optional[int] = None
        self.temp_panel_message_id: Optional[int] = None

        # panel mode: 'main' | 'queue' | 'sound'
        self.panel_mode: str = 'main'

        self.lyrics_enabled: bool = False
        self.lyrics_task: Optional[asyncio.Task] = None
        self.lyrics_channel_id: Optional[int] = None
        self.lyrics_message_id: Optional[int] = None
        self.lyrics_cache: Dict[str, List[Tuple[float, str]]] = {}
        self.lyrics_miss_until: Dict[str, float] = {}  # key -> epoch seconds (짧은 재시도 방지)
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



class SeekModal(discord.ui.Modal):
    def __init__(self, cog: "MusicCog"):
        super().__init__(title="⏩ 점프")
        self.cog = cog

        self.time = discord.ui.TextInput(
            label="이동할 시간(초 또는 mm:ss)",
            placeholder="예) 45  /  1:23",
            required=True,
            max_length=16,
        )
        self.add_item(self.time)

    async def on_submit(self, interaction: discord.Interaction):
        t = (self.time.value or "").strip()
        await self.cog._seek_from_ui(interaction, t)


class SegmentModal(discord.ui.Modal):
    def __init__(self, cog: "MusicCog"):
        super().__init__(title="🎯 구간 설정")
        self.cog = cog

        self.start = discord.ui.TextInput(
            label="시작 시간(초 또는 mm:ss)",
            placeholder="예) 30  /  0:30",
            required=True,
            max_length=16,
        )
        self.end = discord.ui.TextInput(
            label="끝 시간(초 또는 mm:ss)",
            placeholder="예) 90  /  1:30",
            required=True,
            max_length=16,
        )
        self.ab = discord.ui.TextInput(
            label="AB 반복(선택)",
            placeholder="AB / 반복 / on (비우면 일반 구간)",
            required=False,
            max_length=12,
        )
        self.add_item(self.start)
        self.add_item(self.end)
        self.add_item(self.ab)

    async def on_submit(self, interaction: discord.Interaction):
        s = (self.start.value or "").strip()
        e = (self.end.value or "").strip()
        m = (self.ab.value or "").strip()
        await self.cog._segment_from_ui(interaction, s, e, m)

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
        row=1,
    )
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_pause(interaction)

    @discord.ui.button(
        label="스킵",
        style=discord.ButtonStyle.secondary,
        emoji="⏭",
        custom_id="yume_music_skip",
        row=1,
    )
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._skip(interaction)

    @discord.ui.button(
        label="음량",
        style=discord.ButtonStyle.secondary,
        emoji="🔊",
        custom_id="yume_music_volume",
        row=2,
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
        label="가사",
        style=discord.ButtonStyle.secondary,
        emoji="🎤",
        custom_id="yume_music_lyrics",
        row=1,
    )
    async def lyrics_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_lyrics(interaction)

    @discord.ui.button(
        label="정지",
        style=discord.ButtonStyle.danger,
        emoji="⏹",
        custom_id="yume_music_stop",
        row=2,
    )
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._stop(interaction)

    @discord.ui.button(
        label="대기열 관리",
        style=discord.ButtonStyle.secondary,
        emoji="🧰",
        custom_id="yume_music_queue",
        row=2,
    )
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._open_queue_manage(interaction)

    @discord.ui.button(
        label="점프",
        style=discord.ButtonStyle.secondary,
        emoji="⏩",
        custom_id="yume_music_seek",
        row=2,
    )
    async def seek_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await interaction.response.send_modal(SeekModal(self.cog))

    @discord.ui.button(
        label="구간",
        style=discord.ButtonStyle.secondary,
        emoji="🎯",
        custom_id="yume_music_segment",
        row=2,
    )
    async def segment_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        if interaction.guild is None:
            return
        st = self.cog._state(interaction.guild.id)
        if st.segment_start_sec is not None and st.segment_end_sec is not None:
            await self.cog._clear_segment_from_ui(interaction)
            return
        await interaction.response.send_modal(SegmentModal(self.cog))



    @discord.ui.button(
        label="이퀄라이저 관리",
        style=discord.ButtonStyle.secondary,
        emoji="🎛️",
        custom_id="yume_music_sound",
        row=3,
    )
    async def sound_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._open_sound_manage(interaction)


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
        label="돌아가기",
        style=discord.ButtonStyle.primary,
        emoji="↩️",
        custom_id="yume_music_q_back",
        row=0,
    )
    async def q_back(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._back_to_main_panel(interaction)



class SoundManageView(discord.ui.View):
    """이퀄라이저 관리(토글 메뉴)."""

    def __init__(self, cog: "MusicCog"):
        super().__init__(timeout=None)
        self.cog = cog

    

    @discord.ui.button(
        label="EQ",
        style=discord.ButtonStyle.secondary,
        emoji="🎚️",
        custom_id="yume_music_fx",
        row=0,
    )
    async def eq_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._toggle_eq(interaction)

    @discord.ui.button(
        label="EQ 설정",
        style=discord.ButtonStyle.secondary,
        emoji="⚙️",
        custom_id="yume_music_eq_settings",
        row=0,
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
        label="EQ 초기화",
        style=discord.ButtonStyle.danger,
        emoji="🧼",
        custom_id="yume_music_fx_reset",
        row=1,
    )
    async def eq_reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
        await self.cog._reset_fx(interaction)

    @discord.ui.button(
        label="돌아가기",
        style=discord.ButtonStyle.primary,
        emoji="↩️",
        custom_id="yume_music_sound_back",
        row=1,
    )
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):  # noqa: ARG002
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

        self._panel_cfg: Dict[str, Dict[str, object]] = self._load_panel_config()
        self._panel_cfg_lock = asyncio.Lock()
        self._fx_cfg = self._load_fx_cfg()
        self._fx_cfg_lock = asyncio.Lock()
        self._cache: Dict[str, object] = self._load_music_cache()
        self._cache_lock: asyncio.Lock = asyncio.Lock()
        # Phase4: 영구 캐시
        # - spotify_track_to_youtube: {spotify_track_id: {youtube_id, updated_at, title, artist}}
        # - lyrics_cache: {"track|||artist": {lines:[[sec,text],...], updated_at}}
        sp = self._cache.get("spotify_track_to_youtube")
        if not isinstance(sp, dict):
            sp = {}
            self._cache["spotify_track_to_youtube"] = sp
        self._spotify_track_to_youtube: Dict[str, dict] = sp

        ly = self._cache.get("lyrics_cache")
        if not isinstance(ly, dict):
            ly = {}
            self._cache["lyrics_cache"] = ly
        self._lyrics_cache_persist: Dict[str, dict] = ly

        self._ffmpeg_filters = self._detect_ffmpeg_filters()
        self._restore_task: Optional[asyncio.Task] = None

        self.panel_view = MusicPanelView(self)
        self.queue_view = QueueManageView(self)
        self.sound_view = SoundManageView(self)

        self._spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        self._spotify_client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
        # Spotify 디버그 로그 (서비스 로그가 지저분해지는 걸 막기 위해 기본 OFF)
        # - 1/true/yes/on 중 하나면 활성화
        self._spotify_debug = str(os.getenv("YUME_SPOTIFY_DEBUG", "0")).strip().lower() in {"1", "true", "yes", "y", "on"}
        self._spotify_last_error: str = ""
        self._spotify_token: Optional[str] = None
        self._spotify_token_exp: float = 0.0
        self._spotify_token_lock: asyncio.Lock = asyncio.Lock()

    async def cog_load(self):
        try:
            if self._spotify_enabled():
                logger.info("[Music] Spotify API enabled: SPOTIFY_CLIENT_ID/SECRET loaded.")
            else:
                logger.info("[Music] Spotify API disabled: missing SPOTIFY_CLIENT_ID/SECRET.")
            if getattr(self, "_spotify_debug", False):
                logger.info("[Music] Spotify debug logging is ON (YUME_SPOTIFY_DEBUG=1).")
        except Exception:
            pass
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

            try:
                if st.panel_tick_task and not st.panel_tick_task.done():
                    st.panel_tick_task.cancel()
            except Exception:
                pass
            try:
                if st.lyrics_task and not st.lyrics_task.done():
                    st.lyrics_task.cancel()
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

    def _load_panel_config(self) -> Dict[str, Dict[str, object]]:
        """data/storage/music_panel.json

        Phase3:
        - channel_id/message_id: 고정 플레이어 패널
        - lyrics_enabled/lyrics_channel_id/lyrics_message_id: 고정 가사 패널

        호환성: 과거 파일(채널/메시지만 존재)도 그대로 읽는다.
        """
        try:
            if not os.path.exists(PANEL_CFG_PATH):
                return {}
            with open(PANEL_CFG_PATH, "r", encoding="utf-8") as f:
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

                def _to_int(x) -> int:
                    try:
                        return int(x)
                    except Exception:
                        return 0

                def _to_bool(x) -> bool:
                    if isinstance(x, bool):
                        return x
                    if isinstance(x, (int, float)):
                        return bool(int(x))
                    if isinstance(x, str):
                        return x.strip().lower() in {"1", "true", "yes", "y", "on"}
                    return False

                ch = _to_int(v.get("channel_id", 0))
                mid = _to_int(v.get("message_id", 0))

                lyrics_enabled = _to_bool(v.get("lyrics_enabled", False))
                lch = _to_int(v.get("lyrics_channel_id", 0))
                lmid = _to_int(v.get("lyrics_message_id", 0))

                if ch <= 0:
                    # 고정 패널이 없으면 이 entry는 무시(가사만 켜진 상태는 지원하지 않음)
                    continue

                out[str(gid)] = {
                    "channel_id": ch,
                    "message_id": max(0, mid),
                    "lyrics_enabled": bool(lyrics_enabled),
                    "lyrics_channel_id": max(0, lch),
                    "lyrics_message_id": max(0, lmid),
                }

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
    # Phase4: Persistent cache
    # =========================

    def _load_music_cache(self) -> Dict[str, object]:
        """data/storage/music_cache.json

        저장 항목:
        - spotify_track_to_youtube: {spotify_track_id: {youtube_id, updated_at, title?, artist?}}
        - lyrics_cache: {lyrics_key: {lines: [[sec, line], ...], updated_at}}
        """
        try:
            if not os.path.exists(CACHE_CFG_PATH):
                return {"spotify_track_to_youtube": {}, "lyrics_cache": {}}
            with open(CACHE_CFG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"spotify_track_to_youtube": {}, "lyrics_cache": {}}
            if not isinstance(data.get("spotify_track_to_youtube"), dict):
                data["spotify_track_to_youtube"] = {}
            if not isinstance(data.get("lyrics_cache"), dict):
                data["lyrics_cache"] = {}
            return data
        except Exception:
            return {"spotify_track_to_youtube": {}, "lyrics_cache": {}}

    def _save_music_cache_unlocked(self) -> None:
        try:
            os.makedirs(STORAGE_DIR, exist_ok=True)
            tmp = CACHE_CFG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CACHE_CFG_PATH)
        except Exception as e:
            logger.warning("[Music] failed to save cache: %s", e)

    def _trim_cache_unlocked(self) -> None:
        """캐시 크기 폭발 방지(오래된 것부터 정리)."""
        try:
            max_sp = int(os.getenv("YUME_MUSIC_CACHE_MAX_SPOTIFY", "2000"))
        except Exception:
            max_sp = 2000
        try:
            max_ly = int(os.getenv("YUME_MUSIC_CACHE_MAX_LYRICS", "1500"))
        except Exception:
            max_ly = 1500

        def _trim_map(m: dict, max_n: int):
            if not isinstance(m, dict) or max_n <= 0:
                return
            if len(m) <= max_n:
                return
            items = []
            for k, v in m.items():
                try:
                    ts = float((v or {}).get("updated_at") or 0.0) if isinstance(v, dict) else 0.0
                except Exception:
                    ts = 0.0
                items.append((ts, k))
            items.sort()  # 오래된 것부터
            drop = len(m) - max_n
            for _, k in items[:drop]:
                m.pop(k, None)

        _trim_map(self._spotify_track_to_youtube, max_sp)
        _trim_map(self._lyrics_persist_cache, max_ly)

    def _cache_get_spotify_youtube(self, spotify_id: str) -> Optional[str]:
        if not spotify_id:
            return None
        rec = self._spotify_track_to_youtube.get(str(spotify_id))
        if not isinstance(rec, dict):
            return None
        yid = rec.get("youtube_id")
        if isinstance(yid, str) and yid:
            return yid
        return None

    async def _cache_set_spotify_youtube(self, spotify_id: str, youtube_id: str, *, title: Optional[str] = None, artist: Optional[str] = None):
        if not spotify_id or not youtube_id:
            return
        async with self._cache_lock:
            rec = self._spotify_track_to_youtube.get(str(spotify_id))
            if not isinstance(rec, dict):
                rec = {}
            rec["youtube_id"] = str(youtube_id)
            rec["updated_at"] = time.time()
            if title:
                rec["title"] = str(title)[:120]
            if artist:
                rec["artist"] = str(artist)[:120]
            self._spotify_track_to_youtube[str(spotify_id)] = rec
            self._trim_cache_unlocked()
            self._save_music_cache_unlocked()

    def _cache_get_lyrics(self, key: str) -> Optional[List[Tuple[float, str]]]:
        if not key:
            return None
        rec = self._lyrics_persist_cache.get(str(key))
        if not isinstance(rec, dict):
            return None
        lines = rec.get("lines")
        if not isinstance(lines, list):
            return None
        out: List[Tuple[float, str]] = []
        for it in lines:
            if not isinstance(it, (list, tuple)) or len(it) != 2:
                continue
            try:
                sec = float(it[0])
            except Exception:
                continue
            line = str(it[1] or "").strip()
            if not line:
                continue
            out.append((sec, line))
        return out or None

    async def _cache_set_lyrics(self, key: str, lines: List[Tuple[float, str]]):
        if not key or not lines:
            return
        payload = []
        for sec, line in lines[:5000]:  # 안전장치
            try:
                s = float(sec)
            except Exception:
                continue
            t = str(line or "").strip()
            if not t:
                continue
            payload.append([s, t])
        if not payload:
            return
        async with self._cache_lock:
            self._lyrics_persist_cache[str(key)] = {"lines": payload, "updated_at": time.time()}
            self._trim_cache_unlocked()
            self._save_music_cache_unlocked()


    # =========================
    # FX config (per-guild)
    # =========================


    def _load_fx_cfg(self) -> Dict[str, Dict[str, object]]:
        """data/storage/music_fx.json 에서 길드별 EQ 설정을 읽어온다.

        과거 버전에 리버브/튠 키가 들어있더라도 무시한다(호환성).
        """
        try:
            if not os.path.exists(FX_CFG_PATH):
                return {}
            with open(FX_CFG_PATH, "r", encoding="utf-8") as f:
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

                out[str(gid)] = {
                    "eq_enabled": _bf(v.get("eq_enabled", False), False),
                    "bass_db": _ff(v.get("bass_db", 0.0), 0.0, -24.0, 24.0),
                    "mid_db": _ff(v.get("mid_db", 0.0), 0.0, -24.0, 24.0),
                    "treble_db": _ff(v.get("treble_db", 0.0), 0.0, -24.0, 24.0),
                    "preamp_db": _ff(v.get("preamp_db", 0.0), 0.0, -24.0, 24.0),
                }
            return out
        except Exception:
            return {}

    def _save_fx_cfg_unlocked(self) -> None:
        try:
            os.makedirs(STORAGE_DIR, exist_ok=True)
            tmp = FX_CFG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._fx_cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, FX_CFG_PATH)
        except Exception as e:
            logger.warning("[Music] failed to save fx cfg: %s", e)

    def _apply_fx_cfg_to_state(self, guild_id: int, st: MusicState) -> None:
        cfg = self._fx_cfg.get(str(guild_id))
        if not cfg:
            return
        try:
            st.fx_eq_enabled = bool(cfg.get("eq_enabled", False))
            st.fx_bass_db = float(cfg.get("bass_db", 0.0))
            st.fx_mid_db = float(cfg.get("mid_db", 0.0))
            st.fx_treble_db = float(cfg.get("treble_db", 0.0))
            st.fx_preamp_db = float(cfg.get("preamp_db", 0.0))
        except Exception:
            return

    async def _persist_fx_cfg_from_state(self, guild_id: int, st: MusicState) -> None:
        async with self._fx_cfg_lock:
            self._fx_cfg[str(guild_id)] = {
                "eq_enabled": bool(st.fx_eq_enabled),
                "bass_db": float(_clamp_float(st.fx_bass_db, -24.0, 24.0)),
                "mid_db": float(_clamp_float(st.fx_mid_db, -24.0, 24.0)),
                "treble_db": float(_clamp_float(st.fx_treble_db, -24.0, 24.0)),
                "preamp_db": float(_clamp_float(st.fx_preamp_db, -24.0, 24.0)),
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

    def _fixed_lyrics(self, guild_id: int) -> Tuple[bool, Optional[int], Optional[int]]:
        """(enabled, lyrics_channel_id, lyrics_message_id)"""
        v = self._panel_cfg.get(str(guild_id))
        if not v:
            return (False, None, None)
        try:
            enabled = bool(v.get("lyrics_enabled", False))
            ch = int(v.get("lyrics_channel_id", 0)) or None
            mid = int(v.get("lyrics_message_id", 0)) or None
            return (enabled, ch, mid)
        except Exception:
            return (False, None, None)


    async def _set_fixed_panel(self, guild_id: int, channel_id: int, message_id: int):
        async with self._panel_cfg_lock:
            cur = self._panel_cfg.get(str(guild_id))
            if not isinstance(cur, dict):
                cur = {}
            cur.update({
                "channel_id": int(channel_id),
                "message_id": int(message_id),
            })
            self._panel_cfg[str(guild_id)] = cur
            self._save_panel_config_unlocked()



    async def _set_fixed_lyrics(self, guild_id: int, *, enabled: bool, channel_id: Optional[int], message_id: Optional[int]):
        async with self._panel_cfg_lock:
            cur = self._panel_cfg.get(str(guild_id))
            if not isinstance(cur, dict):
                cur = {}
            cur.update({
                "lyrics_enabled": bool(enabled),
                "lyrics_channel_id": int(channel_id or 0),
                "lyrics_message_id": int(message_id or 0),
            })
            self._panel_cfg[str(guild_id)] = cur
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
            view = self.panel_view

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
                    await msg.edit(embed=embed, view=view)
                else:
                    msg = await ch.send(embed=embed, view=view)
                    await self._set_fixed_panel(gid, channel_id, msg.id)

                # Phase3: 패널 틱 갱신 시작
                self._start_panel_tick(gid)

                # Phase3: 고정 가사 복원(켜져 있으면 동일 채널에서 계속 edit)
                enabled, lch, lmid = self._fixed_lyrics(gid)
                if enabled:
                    st = self._state(gid)
                    st.lyrics_enabled = True
                    st.lyrics_channel_id = lch or channel_id
                    st.lyrics_message_id = lmid or None
                    if st.lyrics_task is None or st.lyrics_task.done():
                        st.lyrics_task = asyncio.create_task(self._lyrics_loop(gid))
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

    def _spotify_dbg(self, fmt: str, *args) -> None:
        """Spotify 관련 디버그 로그.

        서비스 로그는 기본적으로 깔끔해야 하니, 환경변수로 켠 경우에만 출력한다.
        """
        if not getattr(self, "_spotify_debug", False):
            return
        try:
            logger.info("[Spotify] " + fmt, *args)
        except Exception:
            pass

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
                async with session.post(
                    url,
                    data=data,
                    headers={
                        "Authorization": f"Basic {basic}",
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                ) as r:
                    if r.status != 200:
                        body = ""
                        try:
                            body = (await r.text())[:300]
                        except Exception:
                            body = ""
                        self._spotify_last_error = f"token status={r.status}"
                        self._spotify_dbg("token request failed: status=%s body=%s", r.status, body)
                        # 토큰이 꼬인 상태면 강제로 비운다.
                        self._spotify_token = None
                        self._spotify_token_exp = 0.0
                        return None
                    js = await r.json()
            except Exception as e:
                self._spotify_last_error = f"token exception={type(e).__name__}"
                self._spotify_dbg("token exception: %r", e)
                return None

            access = str(js.get("access_token") or "")
            expires_in = int(js.get("expires_in") or 0)
            if not access or expires_in <= 0:
                self._spotify_last_error = "token missing access_token"
                return None

            self._spotify_token = access
            self._spotify_token_exp = time.time() + expires_in
            self._spotify_last_error = ""
            return access

    async def _spotify_api_get(self, session: aiohttp.ClientSession, url: str) -> Optional[dict]:
        tok = await self._spotify_get_token(session)
        if not tok:
            return None

        async def _do_get(bearer: str) -> Tuple[int, Optional[dict], str]:
            try:
                async with session.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {bearer}",
                        "Accept": "application/json",
                    },
                ) as r:
                    if r.status == 200:
                        return (200, await r.json(), "")
                    body = ""
                    try:
                        body = (await r.text())[:300]
                    except Exception:
                        body = ""
                    return (int(r.status), None, body)
            except Exception as e:
                return (0, None, f"exception={type(e).__name__}")

        # 1차 호출
        status, js, body = await _do_get(tok)
        if status == 200 and js is not None:
            self._spotify_last_error = ""
            return js

        # 401/403이면 토큰을 비우고 1회 재시도
        if status in {401, 403}:
            self._spotify_dbg("api status=%s -> clearing token and retrying once (url=%s)", status, url)
            self._spotify_token = None
            self._spotify_token_exp = 0.0
            tok2 = await self._spotify_get_token(session)
            if tok2:
                status2, js2, body2 = await _do_get(tok2)
                if status2 == 200 and js2 is not None:
                    self._spotify_last_error = ""
                    return js2
                status, body = status2, body2

        # 429(rate limit)은 짧게 기다렸다가 1회 재시도
        if status == 429:
            self._spotify_dbg("api rate limited (429). sleeping 1s then retry (url=%s)", url)
            try:
                await asyncio.sleep(1.0)
            except Exception:
                pass
            status2, js2, body2 = await _do_get(tok)
            if status2 == 200 and js2 is not None:
                self._spotify_last_error = ""
                return js2
            status, body = status2, body2

        if status == 0:
            self._spotify_last_error = f"api exception ({body})"
        else:
            self._spotify_last_error = f"api status={status}"
        self._spotify_dbg("api get failed: status=%s body=%s url=%s", status, body, url)
        return None

    
    async def _spotify_track_meta(
        self,
        session: aiohttp.ClientSession,
        track_id: str,
        fallback_url: str,
    ) -> Tuple[str, Optional[str], Optional[str], str]:
        """Spotify 트랙 URL/ID를 (검색쿼리, track_name, artist_name, display_title)로 변환한다.

        Phase1 목표:
        - Spotify URL 자체로 ytsearch 하지 않는다(엉뚱한 결과가 잘 뜸).
        - 가능한 한 '아티스트 + 곡명' 형태의 검색 키워드를 만들어 유튜브에서 더 정확히 찾는다.
        - 가사 검색에도 쓸 수 있게 (track_name, artist_name) 힌트를 함께 반환한다.

        우선순위:
        1) Spotify Web API (CLIENT_ID/SECRET 있을 때)
        2) Spotify oEmbed (키 없이 가능)
        3) Spotify 트랙 페이지 HTML의 og:title 파싱(best-effort)
        """

        def _norm_sep(t: str) -> str:
            return (t or "").replace("—", "-").replace("–", "-").replace("·", "-").strip()

        def _split_title_author(title: str, author: str) -> Tuple[Optional[str], Optional[str]]:
            title = _norm_sep(title)
            author = (author or "").strip()
            if not title:
                return (None, None)

            # 흔한 포맷: "Track - Artist"
            if " - " in title:
                left, right = [x.strip() for x in title.split(" - ", 1)]
                # author가 한쪽에 포함되면 그쪽을 artist로 본다.
                if author:
                    if author.lower() in left.lower() and author.lower() not in right.lower():
                        return (right or None, author)
                    if author.lower() in right.lower() and author.lower() not in left.lower():
                        return (left or None, author)
                # 애매하면 기본을 Track(left) / Artist(right)로 둔다.
                return (left or None, right or None)

            # 구분자가 없으면 title=track, author=artist로 본다.
            return (title or None, author or None)

        # 1) Spotify Web API (있으면 가장 정확)
        if self._spotify_enabled():
            js = await self._spotify_api_get(session, f"https://api.spotify.com/v1/tracks/{track_id}")
            if js:
                name = str(js.get("name") or "").strip()
                artists = js.get("artists") or []
                artist = str(artists[0].get("name") or "").strip() if artists else ""
                track_name = name or None
                artist_name = artist or None
                display = f"{name} - {artist}".strip(" -") if (name or artist) else fallback_url
                # 유튜브 검색은 '아티스트 곡명' 순서가 더 잘 맞는 편
                query = f"{artist} {name}".strip() if (name or artist) else fallback_url
                return (query or fallback_url, track_name, artist_name, display)

        # 2) oEmbed (키 없이 가능)
        # - 간혹 403/429가 날 수 있으니, 디버그 모드에서만 상태를 남기고
        #   실패하면 HTML fallback로 간다.
        oembed = f"https://open.spotify.com/oembed?url={quote(fallback_url, safe='')}"
        try:
            async with session.get(
                oembed,
                headers={
                    "User-Agent": "YumeBot",
                    "Accept": "application/json",
                },
            ) as r:
                if r.status == 200:
                    data = await r.json()
                else:
                    body = ""
                    try:
                        body = (await r.text())[:200]
                    except Exception:
                        body = ""
                    self._spotify_dbg("oembed failed: status=%s body=%s", r.status, body)
                    data = None
        except Exception as e:
            self._spotify_dbg("oembed exception: %r", e)
            data = None

        if isinstance(data, dict):
            title = str(data.get("title") or "").strip()
            author = str(data.get("author_name") or "").strip()
            track_name, artist_name = _split_title_author(title, author)
            if track_name or artist_name:
                display = f"{track_name or title} - {artist_name or author}".strip(" -")
                query = f"{artist_name or author} {track_name or title}".strip()
                return (query or fallback_url, track_name, artist_name, display or fallback_url)

        # 3) HTML og:title 파싱(best-effort)
        try:
            async with session.get(fallback_url, headers={"User-Agent": "YumeBot", "Accept": "text/html"}) as r:
                if r.status == 200:
                    html = await r.text()
                else:
                    html = ""
        except Exception:
            html = ""

        if html:
            # <meta property="og:title" content="Secret Garden - song by OH MY GIRL | Spotify">
            mt = re.search(r'property="og:title"\s+content="([^"]+)"', html)
            if mt:
                og = (mt.group(1) or "").strip()
                og = og.split("|", 1)[0].strip()
                track_name, artist_name = _split_title_author(og, "")
                if track_name or artist_name:
                    display = f"{track_name or og} - {artist_name or ''}".strip(" -")
                    query = f"{artist_name or ''} {track_name or og}".strip()
                    return (query or fallback_url, track_name, artist_name, display or fallback_url)

        # 마지막 fallback: 그래도 URL은 넘긴다(완전 실패)
        return (fallback_url, None, None, fallback_url)

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
        had_error = False

        while url and len(out) < max_n:
            js = await self._spotify_api_get(session, url)
            if not js:
                had_error = True
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

        # 플레이리스트가 비어있어서 out이 []인 것과,
        # API가 실패해서 아무 것도 못 가져온(out==[] && had_error) 경우를 구분한다.
        if had_error and not out:
            return None
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
            src = track.webpage_url

            # Phase4: Spotify 영구 캐시 매핑이 있으면 바로 그 유튜브 영상으로 고정
            try:
                if getattr(track, 'spotify_track_id', None):
                    cached_yid = self._cache_get_spotify_youtube(str(track.spotify_track_id))
                    if cached_yid:
                        src = f"https://www.youtube.com/watch?v={cached_yid}"
            except Exception:
                pass
            entry: dict = {}
            info: Optional[dict] = None

            # Phase2: ytsearch 후보를 채점해서 가장 그럴듯한 영상을 고른다.
            # - Spotify 트랙은 (아티스트 + 곡명) 기반으로 ytsearch1:... 로 큐에 들어오므로,
            #   여기서 ytsearch10으로 확장 후 후보를 스코어링해서 선택한다.
            if isinstance(src, str) and (src.startswith("ytsearch") or not re.match(r"^https?://", src)):
                m = re.match(r"^ytsearch\d*:(.*)$", src)
                qtxt = (m.group(1).strip() if m else src.strip())
                search_q = f"ytsearch10:{qtxt}" if qtxt else src

                info = await _extract_info(search_q)
                if isinstance(info, dict) and isinstance(info.get("entries"), list):
                    entry = _pick_best_ytsearch_entry(info.get("entries") or [], track, qtxt)
                else:
                    entry = _pick_entry(info or {})
            else:
                info = await _extract_info(src)
                entry = _pick_entry(info)

            if not entry:
                return None

            try:
                real_title = entry.get("title")
                try:
                    dur = entry.get('duration')
                    if isinstance(dur, (int, float)) and int(dur) > 0:
                        track.duration_sec = int(dur)
                    track.is_live = bool(entry.get('is_live') or entry.get('live_status') in {'is_live','live'})
                except Exception:
                    pass
                real_page = entry.get("webpage_url") or entry.get("original_url")
                if real_title and isinstance(real_title, str):
                    track.title = real_title
                if real_page and isinstance(real_page, str) and real_page.startswith("http"):
                    track.webpage_url = real_page
            except Exception:
                pass


            # Phase4: resolve 후 Spotify->YouTube 매핑 저장(다음부터는 0초 정답)
            try:
                if getattr(track, 'spotify_track_id', None):
                    yid = None
                    if isinstance(entry.get('id'), str):
                        yid = entry.get('id')
                    if not yid:
                        page = entry.get('webpage_url') or entry.get('original_url') or ''
                        m2 = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{6,})', str(page))
                        if m2:
                            yid = m2.group(1)
                    if yid:
                        await self._cache_set_spotify_youtube(
                            str(track.spotify_track_id),
                            str(yid),
                            title=str(getattr(track, 'meta_track', '') or ''),
                            artist=str(getattr(track, 'meta_artist', '') or ''),
                        )
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
                # Phase3: 점프/구간 재생 적용
                seek = None
                limit = None
                try:
                    # 우선: 구간이 있으면 구간 우선
                    seg_s = st.segment_start_sec
                    seg_e = st.segment_end_sec
                    if seg_s is not None and seg_e is not None and float(seg_e) > float(seg_s):
                        seek = float(seg_s)
                        limit = float(seg_e) - float(seg_s)

                    # 점프(1회) 오버라이드
                    if st.seek_next_sec is not None:
                        j = float(st.seek_next_sec)
                        st.seek_next_sec = None
                        if seek is not None and limit is not None:
                            # 구간 안에서 점프: 시작~끝 범위로 클램프
                            j = max(float(seg_s), min(float(seg_e) - 0.5, j))
                            limit = float(seg_e) - float(j)
                        seek = max(0.0, j)

                    # 라이브는 seek 불가
                    if getattr(track, 'is_live', False) and ((seek or 0.0) > 0.0 or (limit is not None)):
                        seek = None
                        limit = None
                        st.segment_start_sec = None
                        st.segment_end_sec = None
                        st.segment_ab_repeat = False
                        self._set_error(guild_id, "라이브 스트림은 점프/구간이 안 돼.")
                except Exception:
                    seek = None
                    limit = None

                src = _ffmpeg_source(
                    stream_url,
                    volume=st.volume,
                    af_filters=self._build_af_filters(st),
                    seek_sec=seek,
                    limit_sec=limit,
                )

                st.play_started_at = self.bot.loop.time()
                st.play_seek_base = float(seek or 0.0)
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

                # Phase3: AB 반복(구간이 설정되어 있고 AB가 켜져 있으면, 같은 곡을 '맨 앞'에 다시 넣는다)
                ab_requeued = False
                try:
                    if (
                        finished is not None
                        and st.segment_ab_repeat
                        and st.segment_start_sec is not None
                        and st.segment_end_sec is not None
                        and float(st.segment_end_sec) > float(st.segment_start_sec)
                        and (not st._suppress_requeue_once)
                    ):
                        q = getattr(st.queue, '_queue', None)
                        if q is not None and hasattr(q, 'appendleft'):
                            q.appendleft(finished)
                            ab_requeued = True
                except Exception:
                    ab_requeued = False

                # 구간(AB 아님)은 '자연 종료'일 때만 해제한다.
                # (점프/구간 설정을 위해 vc.stop()으로 재시작하는 경우에는 유지해야 함)
                if (not st._suppress_requeue_once) and (not ab_requeued) and (st.segment_start_sec is not None or st.segment_end_sec is not None):
                    st.segment_start_sec = None
                    st.segment_end_sec = None
                    st.segment_ab_repeat = False

                if st.loop_all and finished is not None and (not st._suppress_requeue_once) and (not ab_requeued):
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
        self._start_panel_tick(guild_id)



    def _fx_summary(self, st: MusicState) -> str:
        if st.fx_eq_enabled and (
            abs(st.fx_bass_db) > 0.01
            or abs(st.fx_mid_db) > 0.01
            or abs(st.fx_treble_db) > 0.01
            or abs(st.fx_preamp_db) > 0.01
        ):
            return f"ON (B{st.fx_bass_db:+.0f} M{st.fx_mid_db:+.0f} T{st.fx_treble_db:+.0f} P{st.fx_preamp_db:+.0f})"
        return "OFF"
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

        # Phase3: 진행 상태(틱 갱신으로 주기적으로 업데이트)
        if st.now_playing is not None:
            pos = self._current_pos(st)
            dur = getattr(st.now_playing, 'duration_sec', None)
            seg_s = getattr(st, 'segment_start_sec', None)
            seg_e = getattr(st, 'segment_end_sec', None)

            def _fmt(t: float) -> str:
                t = max(0.0, float(t))
                mm = int(t // 60)
                ss = int(t % 60)
                return f"{mm:02d}:{ss:02d}"

            extra = ""
            if seg_s is not None and seg_e is not None and float(seg_e) > float(seg_s):
                extra = f" | 구간: {_fmt(seg_s)}~{_fmt(seg_e)}" + (" (AB)" if st.segment_ab_repeat else "")

            if isinstance(dur, int) and dur > 0:
                # 20칸 바(스팸 적고 보기 좋게)
                ratio = min(1.0, max(0.0, pos / float(dur)))
                filled = int(ratio * 20)
                bar = "■" * filled + "□" * (20 - filled)
                embed.add_field(name="⏱ 진행", value=f"`{bar}` {_fmt(pos)} / {_fmt(dur)}{extra}", inline=False)
            else:
                embed.add_field(name="⏱ 진행", value=f"{_fmt(pos)}{extra}", inline=False)

        embed.add_field(name="📃 큐", value=f"{st.queue.qsize()}곡", inline=True)
        embed.add_field(name="🔁 반복", value="ON" if st.loop_all else "OFF", inline=True)
        embed.add_field(name="🔊 볼륨", value=f"{int(st.volume * 100)}%", inline=True)
        embed.add_field(name="🎚️ EQ", value=self._fx_summary(st), inline=True)

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
        st = self._state(guild_id)

        mode = getattr(st, 'panel_mode', 'main')
        if mode == 'queue':
            embed = self._build_queue_embed(guild)
            view = self.queue_view
        elif mode == 'sound':
            embed = self._build_sound_embed(guild)
            view = self.sound_view
        else:
            embed = self._build_embed(guild)
            view = self.panel_view

        msg_id: Optional[int] = None
        if fixed:
            _, msg_id = self._fixed_panel(guild_id)
        else:
            msg_id = st.temp_panel_message_id

        msg: Optional[discord.Message] = None
        if msg_id:
            try:
                msg = await ch.fetch_message(int(msg_id))
            except discord.NotFound:
                msg = None
            except Exception:
                msg = None

        try:
            async with st.ui_lock:
                if msg:
                    await msg.edit(embed=embed, view=view)
                    if fixed:
                        self._start_panel_tick(guild_id)
                    return (channel_id, msg.id)

                msg = await ch.send(embed=embed, view=view)
                if fixed:
                    await self._set_fixed_panel(guild_id, channel_id, msg.id)
                    self._start_panel_tick(guild_id)
                else:
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



    # =========================
    # Phase3: 패널 틱(주기) 갱신
    # =========================

    def _start_panel_tick(self, guild_id: int):
        st = self._state(guild_id)
        if st.panel_tick_task and not st.panel_tick_task.done():
            return
        st.panel_tick_task = asyncio.create_task(self._panel_tick_loop(guild_id))

    def _stop_panel_tick(self, guild_id: int):
        st = self._state(guild_id)
        if st.panel_tick_task and not st.panel_tick_task.done():
            try:
                st.panel_tick_task.cancel()
            except Exception:
                pass
        st.panel_tick_task = None
        st._panel_last_render_key = None

    async def _panel_tick_loop(self, guild_id: int):
        """고정/임시 패널 메시지를 2~5초 간격으로 edit한다.

        - panel_mode가 main일 때만 업데이트 (큐/이퀄라이저 화면을 덮어쓰지 않기)
        - 임베드가 동일하면 skip
        """
        await self.bot.wait_until_ready()
        while True:
            st = self._state(guild_id)
            fixed_ch, fixed_mid = self._fixed_panel(guild_id)
            ch_id = fixed_ch or st.temp_panel_channel_id
            mid = fixed_mid or st.temp_panel_message_id

            if not ch_id or not mid:
                return

            # 큐/사운드 패널 열어둔 중엔 자동 갱신 금지
            if st.panel_mode != 'main':
                await asyncio.sleep(2.5)
                continue

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                await asyncio.sleep(2.5)
                continue

            vc = guild.voice_client
            interval = 3.0 if (vc and (vc.is_playing() or vc.is_paused())) else 5.0

            embed = self._build_embed(guild)
            try:
                render_key = json.dumps(embed.to_dict(), ensure_ascii=False, sort_keys=True)
            except Exception:
                render_key = None

            if render_key and render_key == st._panel_last_render_key:
                await asyncio.sleep(interval)
                continue
            st._panel_last_render_key = render_key

            ch = self.bot.get_channel(int(ch_id))
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                await asyncio.sleep(interval)
                continue

            try:
                async with st.ui_lock:
                    pm = ch.get_partial_message(int(mid))
                    await pm.edit(embed=embed, view=self.panel_view)
            except discord.NotFound:
                # 메시지가 사라졌으면 재생성
                try:
                    if fixed_ch:

                        await self._ensure_panel_message(guild_id, int(fixed_ch), fixed=True)

                    else:

                        st.temp_panel_message_id = None
                except Exception:
                    pass
            except Exception:
                pass

            await asyncio.sleep(interval)
    async def _refresh_from_interaction(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)

        if st.panel_mode == 'queue':
            embed = self._build_queue_embed(interaction.guild)
            view = self.queue_view
        elif st.panel_mode == 'sound':
            embed = self._build_sound_embed(interaction.guild)
            view = self.sound_view
        else:
            embed = self._build_embed(interaction.guild)
            view = self.panel_view

        # 가능한 한 interaction.message를 바로 수정해서 '즉시 반영'되게 한다.
        await self._edit_panel_message(gid, embed=embed, view=view, interaction=interaction)
        self._start_panel_tick(gid)

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

            dur = entry.get("duration")
            is_live = bool(entry.get("is_live") or entry.get("live_status") == "is_live")

            track = _Track(title=title, webpage_url=webpage_url, requester_id=interaction.user.id, duration_sec=int(dur) if isinstance(dur, (int, float)) else None, is_live=is_live)

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
                if qs is None:
                    if not self._spotify_enabled():
                        msg = (
                            "플레이리스트를 가져오려면 Spotify API 키가 필요해.\n"
                            "서버 .env에 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET 넣고 재시작해줘."
                        )
                    else:
                        # 키는 있는데 인증/호출이 실패한 케이스
                        reason = (self._spotify_last_error or "알 수 없는 오류").strip()
                        msg = (
                            "Spotify 플레이리스트를 가져오지 못했어.\n"
                            "CLIENT_ID/SECRET 확인해줘.\n"
                            f"(상태: {reason})"
                        )
                    try:
                        await interaction.followup.send(msg, ephemeral=True)
                    except Exception:
                        pass
                    return

                if len(qs) == 0:
                    try:
                        await interaction.followup.send("플레이리스트에 곡이 없네…", ephemeral=True)
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
                q, tn, ar, disp = await self._spotify_track_meta(session, sid, url)

                st = self._state(interaction.guild.id)

                # Phase1: Spotify 트랙은 URL 자체로 ytsearch 하지 않고,
                # (아티스트 + 곡명) 기반으로 ytsearch1:... 를 큐에 넣는다.
                webpage = q
                if q and not re.match(r"^https?://", q):
                    webpage = f"ytsearch1:{q}"

                track = _Track(
                    title=(disp or q or url),
                    webpage_url=str(webpage),
                    requester_id=interaction.user.id,
                    meta_track=tn,
                    meta_artist=ar,
                    spotify_track_id=sid,
                )
                await st.queue.put(track)
                self._start_player_if_needed(interaction.guild.id)

                try:
                    await interaction.followup.send(f"큐에 추가: **{track.title}**", ephemeral=True)
                except Exception:
                    pass
                await self._refresh_from_interaction(interaction)
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
        st.seek_next_sec = None
        st.segment_start_sec = None
        st.segment_end_sec = None
        st.segment_ab_repeat = False
        try:
            vc.stop()
        except Exception:
            pass

        # 혹시라도 player_task가 예외로 종료된 상태면 스킵 후에 다음 곡으로 못 넘어갈 수 있다.
        # 스킵은 "다음 큐로 즉시 진행"을 보장해야 하므로 여기서 한 번 더 킥한다.
        self._start_player_if_needed(interaction.guild.id)

        try:
            await interaction.response.send_message("넘길게. 으헤~", ephemeral=True)
        except Exception:
            pass

        # UI 갱신은 비동기로 돌려서(버튼 콜백이 오래 걸리지 않게)
        # 플레이어 루프가 다음 곡 시작을 늦추는 상황을 최대한 피한다.
        asyncio.create_task(self._refresh_from_interaction(interaction))

    async def _stop(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return

        st = self._state(interaction.guild.id)
        st._suppress_requeue_once = True
        st.seek_next_sec = None
        st.segment_start_sec = None
        st.segment_end_sec = None
        st.segment_ab_repeat = False

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




    async def _send_ephemeral(self, interaction: discord.Interaction, text: str):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            pass

    async def _seek_from_ui(self, interaction: discord.Interaction, t: str):
        if interaction.guild is None:
            return
        st = self._state(interaction.guild.id)
        vc = interaction.guild.voice_client

        if vc is None or not vc.is_connected() or not (vc.is_playing() or vc.is_paused()):
            await self._send_ephemeral(interaction, "지금 재생 중이 아니야.")
            return

        sec = self._parse_time_to_sec(t)
        if sec is None or sec < 0:
            await self._send_ephemeral(interaction, "형식: `초` 또는 `mm:ss` (예: 45 / 1:23)")
            return

        if st.now_playing and getattr(st.now_playing, "is_live", False):
            await self._send_ephemeral(interaction, "라이브 스트림은 점프가 안 돼…")
            return

        cur = st.now_playing
        if cur is None:
            await self._send_ephemeral(interaction, "지금 재생 중인 곡 정보를 못 찾았어…")
            return

        q = getattr(st.queue, "_queue", None)
        if q is not None and hasattr(q, "appendleft"):
            q.appendleft(cur)
        else:
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
            for it in items:
                try:
                    st.queue.put_nowait(it)
                except Exception:
                    pass

        st.seek_next_sec = float(sec)
        st._suppress_requeue_once = True
        try:
            vc.stop()
        except Exception:
            pass

        await self._send_ephemeral(interaction, f"{int(sec)}초로 점프할게.")
        self._start_player_if_needed(interaction.guild.id)
        self._start_panel_tick(interaction.guild.id)
        await self._refresh_from_interaction(interaction)

    async def _segment_from_ui(self, interaction: discord.Interaction, start: str, end: str, mode: str):
        if interaction.guild is None:
            return
        st = self._state(interaction.guild.id)
        vc = interaction.guild.voice_client

        if vc is None or not vc.is_connected() or not (vc.is_playing() or vc.is_paused()):
            await self._send_ephemeral(interaction, "지금 재생 중이 아니야.")
            return

        if st.now_playing and getattr(st.now_playing, "is_live", False):
            await self._send_ephemeral(interaction, "라이브 스트림은 구간 재생이 안 돼…")
            return

        s = self._parse_time_to_sec(start)
        e = self._parse_time_to_sec(end)
        if s is None or e is None:
            await self._send_ephemeral(interaction, "시작/끝 시간을 `30` 또는 `1:30` 형태로 입력해줘.")
            return
        if e <= s:
            await self._send_ephemeral(interaction, "끝 시간이 시작보다 커야 해.")
            return

        st.segment_start_sec = float(s)
        st.segment_end_sec = float(e)
        st.segment_ab_repeat = (mode or "").strip().upper() in {"AB", "A", "R", "REPEAT", "ON", "Y", "YES", "TRUE", "1", "반복"}
        st.seek_next_sec = float(s)

        cur = st.now_playing
        if cur is not None:
            q = getattr(st.queue, "_queue", None)
            if q is not None and hasattr(q, "appendleft"):
                q.appendleft(cur)

        st._suppress_requeue_once = True
        try:
            vc.stop()
        except Exception:
            pass

        def _fmt_time(x: float) -> str:
            mm = int(x // 60)
            ss = int(x % 60)
            return f"{mm:02d}:{ss:02d}"

        await self._send_ephemeral(interaction, f"구간 {_fmt_time(s)}~{_fmt_time(e)}" + (" (AB 반복)" if st.segment_ab_repeat else "") + "으로 재생할게.")
        self._start_player_if_needed(interaction.guild.id)
        self._start_panel_tick(interaction.guild.id)
        await self._refresh_from_interaction(interaction)

    async def _clear_segment_from_ui(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        st = self._state(interaction.guild.id)
        vc = interaction.guild.voice_client

        st.segment_start_sec = None
        st.segment_end_sec = None
        st.segment_ab_repeat = False

        if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
            if st.now_playing and not getattr(st.now_playing, "is_live", False):
                pos = self._current_pos(st)
                cur = st.now_playing
                q = getattr(st.queue, "_queue", None)
                if cur is not None and q is not None and hasattr(q, "appendleft"):
                    q.appendleft(cur)
                st.seek_next_sec = float(pos)
                st._suppress_requeue_once = True
                try:
                    vc.stop()
                except Exception:
                    pass

        await self._send_ephemeral(interaction, "구간 재생을 해제했어.")
        self._start_panel_tick(interaction.guild.id)
        await self._refresh_from_interaction(interaction)


    def _build_af_filters(self, st: MusicState) -> Optional[str]:
        """ffmpeg -af 체인 생성 (EQ 전용)."""
        chain: List[str] = []

        if abs(st.fx_preamp_db) > 0.01:
            chain.append(f"volume={float(st.fx_preamp_db)}dB")

        if st.fx_eq_enabled:
            if abs(st.fx_bass_db) > 0.01:
                chain.append(f"bass=g={float(st.fx_bass_db)}:f=100:w=0.5")
            if abs(st.fx_mid_db) > 0.01 and (self._ffmpeg_filters is not None and "equalizer" in self._ffmpeg_filters):
                chain.append(f"equalizer=f=1000:t=q:w=1:g={float(st.fx_mid_db)}")
            if abs(st.fx_treble_db) > 0.01:
                chain.append(f"treble=g={float(st.fx_treble_db)}:f=3500:w=0.5")

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
        st.panel_mode = 'sound'
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

    async def _set_eq_settings(self, interaction: discord.Interaction, *, guild_id: int, bass: float, mid: float, treble: float, preamp: float):
        st = self._state(guild_id)
        st.panel_mode = 'sound'
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

    async def _reset_fx(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)
        st.panel_mode = "sound"
        async with st.lock:
            st.fx_eq_enabled = False
            st.fx_bass_db = 0.0
            st.fx_mid_db = 0.0
            st.fx_treble_db = 0.0
            st.fx_preamp_db = 0.0

            await self._persist_fx_cfg_from_state(gid, st)
            restarted = await self._replay_current_from_start(gid)

        msg = "EQ 초기화 완료" + (" (현재 곡 재시작)" if restarted else "")
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send(msg, ephemeral=True)
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
            title="유메 - 대기열 관리",
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

        embed.set_footer(text="대기열 관리는 여기서. ↩️ 돌아가기 누르면 메인 패널로 돌아가.")
        return embed

    def _build_sound_embed(self, guild: discord.Guild) -> discord.Embed:
        st = self._state(guild.id)
        vc = guild.voice_client

        embed = discord.Embed(
            title="유메 - 이퀄라이저 관리",
            description="EQ(이퀄라이저)를 조절해.",
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

        eq = self._fx_summary(st)
        embed.add_field(name="🎚️ EQ", value=eq, inline=False)

        if vc and vc.is_connected() and getattr(vc, "channel", None):
            embed.add_field(name="🔊 음성 채널", value=vc.channel.name, inline=False)
        else:
            embed.add_field(name="🔊 음성 채널", value="(연결 안 됨)", inline=False)

        if st.last_error and (time.time() - st.last_error_at) < 300:
            embed.add_field(name="⚠️ 상태", value=st.last_error, inline=False)

        embed.set_footer(text="이퀄라이저 관리는 여기서. ↩️ 돌아가기 누르면 메인 패널로 돌아가.")
        return embed


    async def _edit_panel_message(
        self,
        guild_id: int,
        *,
        embed: discord.Embed,
        view: discord.ui.View,
        interaction: Optional[discord.Interaction] = None,
    ) -> bool:
        st = self._state(guild_id)
        async with st.ui_lock:
            # 버튼 상호작용이면 가능한 한 '해당 메시지'를 즉시 수정한다.
            if interaction is not None and getattr(interaction, "message", None) is not None:
                try:
                    if not interaction.response.is_done():

                        await interaction.response.edit_message(embed=embed, view=view)

                    else:

                        await interaction.message.edit(embed=embed, view=view)  # type: ignore[union-attr]
                    return True
                except Exception:
                    pass

            fixed_ch, fixed_mid = self._fixed_panel(guild_id)
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
        st = self._state(gid)
        st.panel_mode = 'queue'
        embed = self._build_queue_embed(interaction.guild)
        await self._edit_panel_message(gid, embed=embed, view=self.queue_view, interaction=interaction)

    async def _open_sound_manage(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)
        st.panel_mode = 'sound'
        embed = self._build_sound_embed(interaction.guild)
        await self._edit_panel_message(gid, embed=embed, view=self.sound_view, interaction=interaction)

    async def _back_to_main_panel(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        gid = interaction.guild.id
        st = self._state(gid)
        st.panel_mode = 'main'
        embed = self._build_embed(interaction.guild)
        await self._edit_panel_message(gid, embed=embed, view=self.panel_view, interaction=interaction)



    def _lyrics_cache_key(self, track: _Track) -> str:
        # Phase1: Spotify 등에서 얻은 메타가 있으면 그걸 우선 사용(가사 적중률↑)
        tn, ar = _guess_artist_title(track.title)
        tn = (getattr(track, "meta_track", None) or tn).strip()
        ar = (getattr(track, "meta_artist", None) or ar or getattr(track, 'artist', None) or "").strip()
        return f"{tn}|||{ar}".strip()

    def _current_pos(self, st: MusicState) -> float:
        if st.play_started_at <= 0:
            return 0.0
        now = self.bot.loop.time()
        if st.paused_at is not None:
            now = st.paused_at
        pos = now - st.play_started_at - st.paused_total + float(getattr(st, 'play_seek_base', 0.0) or 0.0)
        if pos < 0:
            pos = 0.0
        return float(pos)

    async def _disable_lyrics(self, guild_id: int, *, delete_message: bool):
        st = self._state(guild_id)
        st.lyrics_enabled = False

        # 고정 패널이 있으면 가사 설정도 저장해둔다
        fixed_ch, _ = self._fixed_panel(guild_id)
        if fixed_ch:
            await self._set_fixed_lyrics(guild_id, enabled=False, channel_id=None, message_id=None)

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

        fixed_ch, _mid = self._fixed_panel(gid)
        cid = fixed_ch or interaction.channel_id
        st.lyrics_channel_id = cid

        if fixed_ch:
            await self._set_fixed_lyrics(gid, enabled=True, channel_id=fixed_ch, message_id=None)

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


async def _fetch_lrclib_once(
    self,
    session: aiohttp.ClientSession,
    track_name: str,
    artist_name: Optional[str],
) -> Optional[str]:
    track_name = _normalize_lyric_term(track_name)
    artist_name = _normalize_lyric_term(artist_name or "") or None
    if not track_name:
        return None

    params = {"track_name": track_name}
    if artist_name:
        params["artist_name"] = artist_name

    try:
        async with session.get(LRCLIB_API_BASE, params=params) as resp:
            if resp.status != 200:
                return None
            # 일부 환경에서 content-type이 애매하게 오는 경우가 있어 안전하게 처리
            data = await resp.json(content_type=None)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    plain = data.get("plainLyrics") or data.get("plain_lyrics") or data.get("plainlyrics")
    if isinstance(plain, str) and plain.strip():
        return plain.strip()

    lrc = data.get("syncedLyrics") or data.get("synced_lyrics") or data.get("syncedlyrics")
    if isinstance(lrc, str) and lrc.strip():
        # syncedLyrics만 있을 때는 타임코드를 제거해서 순수 가사로 변환
        return _strip_lrc_to_plain(lrc)

    return None

async def _fetch_lrclib_multi(self, candidates: List[Tuple[str, Optional[str]]]) -> Optional[str]:
    """LRCLIB를 여러 후보로 순차 시도해서 성공 확률을 올린다."""
    # 후보 정리(중복 제거 + 길이 제한)
    uniq: List[Tuple[str, Optional[str]]] = []
    seen: set[tuple[str, str]] = set()
    for tn, ar in (candidates or []):
        tn2 = _normalize_lyric_term(tn)
        ar2 = _normalize_lyric_term(ar or "") if ar else ""
        if not tn2:
            continue
        key = (tn2.lower(), ar2.lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append((tn2, ar2 or None))
    uniq = uniq[:10]
    if not uniq:
        return None

    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for tn, ar in uniq:
                hit = await self._fetch_lrclib_once(session, tn, ar)
                if hit and hit.strip():
                    return hit.strip()
    except Exception:
        return None
    return None

async def _fetch_lrclib(self, track_name: str, artist_name: Optional[str]) -> Optional[str]:
    """(호환용) 단일 후보로 LRCLIB 조회."""
    return await self._fetch_lrclib_multi([(track_name, artist_name)])

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
                    fixed_ch, _ = self._fixed_panel(guild_id)
                    if fixed_ch:
                        await self._set_fixed_lyrics(guild_id, enabled=True, channel_id=st.lyrics_channel_id, message_id=None)


            if msg is None:
                try:
                    m = await ch.send(embed=discord.Embed(title="🎤 유메 - 가사", description="가사를 준비하는 중..."))
                    st.lyrics_message_id = m.id
                    msg = m
                    fixed_ch, _ = self._fixed_panel(guild_id)
                    if fixed_ch:
                        await self._set_fixed_lyrics(guild_id, enabled=True, channel_id=st.lyrics_channel_id, message_id=st.lyrics_message_id)

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
                    # Phase4: persistent lyrics cache(봇 재시작 후에도 재사용)
                    cached_lines = self._cache_get_lyrics(key)
                    if cached_lines:
                        st.lyrics_cache[key] = cached_lines
                        lines_lrc = cached_lines
                    else:
                        now_ts = time.time()
                        miss_until = st.lyrics_miss_until.get(key, 0.0)
                        if now_ts < miss_until:
                            lines_lrc = []
                        else:
                            candidates = _build_lrclib_candidates(track)
                            lrc = await self._fetch_lrclib_multi(candidates)
                            lines_lrc = _parse_plain_lyrics(lrc or "")
                            if lines_lrc:
                                st.lyrics_cache[key] = lines_lrc
                                st.lyrics_miss_until.pop(key, None)
                                await self._cache_set_lyrics(key, lines_lrc)
                            else:
                                # 너무 자주 API를 두드리지 않게 짧은 TTL을 둔다.
                                st.lyrics_miss_until[key] = now_ts + 60.0

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
                async with st.ui_lock:
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
        st.panel_mode = 'queue'

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
        st.panel_mode = 'queue'

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
        st.panel_mode = 'queue'

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
        st.panel_mode = 'queue'

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
        """!음악채널지정 <채널>: 지정한 채널에 음악 패널을 항상 고정한다."""
        if ctx.guild is None:
            await ctx.send("서버 채널에서만 쓸 수 있어.")
            return

        gid = ctx.guild.id

        # 기존 고정 패널/가사 메시지 정리(채널 이동 시)
        old_ch, old_mid = self._fixed_panel(gid)
        old_ly_enabled, old_lch, old_lmid = self._fixed_lyrics(gid)

        if old_ch and old_mid and int(old_ch) != int(channel.id):
            old_channel = self.bot.get_channel(int(old_ch))
            if isinstance(old_channel, (discord.TextChannel, discord.Thread)):
                try:
                    await old_channel.get_partial_message(int(old_mid)).delete()
                except Exception:
                    try:
                        msg = await old_channel.fetch_message(int(old_mid))
                        await msg.delete()
                    except Exception:
                        pass

        if old_ly_enabled and old_lch and old_lmid and int(old_lch) != int(channel.id):
            old_lc = self.bot.get_channel(int(old_lch))
            if isinstance(old_lc, (discord.TextChannel, discord.Thread)):
                try:
                    await old_lc.get_partial_message(int(old_lmid)).delete()
                except Exception:
                    try:
                        m = await old_lc.fetch_message(int(old_lmid))
                        await m.delete()
                    except Exception:
                        pass

        # 새 채널에 패널 생성/갱신
        cid, mid = await self._ensure_panel_message(gid, channel.id, fixed=True)
        if not cid or not mid:
            await ctx.send("그 채널에 패널을 만들 수 없었어(권한을 확인해줘).")
            return

        # 가사 설정 유지: 이전에 켜져있으면 새 채널로 옮겨서 계속 edit
        st = self._state(gid)
        if old_ly_enabled or st.lyrics_enabled:
            st.lyrics_enabled = True
            st.lyrics_channel_id = channel.id
            st.lyrics_message_id = None
            await self._set_fixed_lyrics(gid, enabled=True, channel_id=channel.id, message_id=None)
            if st.lyrics_task is None or st.lyrics_task.done():
                st.lyrics_task = asyncio.create_task(self._lyrics_loop(gid))

        self._start_panel_tick(gid)
        await ctx.send(f"음악 패널 채널을 {channel.mention}로 지정했어. 이제 여기만 갱신할게.")

    @set_music_channel.error
    async def set_music_channel_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("이건 서버 관리 권한(서버 관리)이 필요해.")
            return
        await ctx.send("사용법: `!음악채널지정 <채널>`")

    @commands.command(name="음악채널해제")
    @commands.has_permissions(manage_guild=True)
    async def clear_music_channel(self, ctx: commands.Context):
        """!음악채널해제: 고정 패널 설정을 지운다."""
        if ctx.guild is None:
            await ctx.send("서버 채널에서만 쓸 수 있어.")
            return

        gid = ctx.guild.id
        ch_id, mid = self._fixed_panel(gid)
        ly_enabled, lch, lmid = self._fixed_lyrics(gid)

        # 가사 메시지 삭제(best-effort)
        if ly_enabled and lch and lmid:
            ch = self.bot.get_channel(int(lch))
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    await ch.get_partial_message(int(lmid)).delete()
                except Exception:
                    try:
                        msg = await ch.fetch_message(int(lmid))
                        await msg.delete()
                    except Exception:
                        pass

        # 설정 제거 + 루프 정리
        await self._clear_fixed_panel(gid)
        self._stop_panel_tick(gid)
        await self._disable_lyrics(gid, delete_message=True)

        # 패널 메시지 삭제(best-effort)
        if ch_id and mid:
            ch = self.bot.get_channel(int(ch_id))
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    await ch.get_partial_message(int(mid)).delete()
                except Exception:
                    try:
                        msg = await ch.fetch_message(int(mid))
                        await msg.delete()
                    except Exception:
                        pass

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




    # =========================
    # Phase3: 점프/구간 명령어
    # =========================

    def _parse_time_to_sec(self, s: str) -> Optional[float]:
        s = (s or '').strip()
        if not s:
            return None
        # mm:ss
        if re.match(r"^\d{1,3}:\d{1,2}$", s):
            mm, ss = s.split(':', 1)
            try:
                return float(int(mm) * 60 + int(ss))
            except Exception:
                return None
        try:
            return float(s)
        except Exception:
            return None

    @commands.command(name="점프")
    async def cmd_seek(self, ctx: commands.Context, t: str):
        """!점프 <초|mm:ss>: 현재 곡을 해당 시점으로 이동"""
        if ctx.guild is None:
            return
        st = self._state(ctx.guild.id)
        vc = ctx.guild.voice_client
        if vc is None or not vc.is_connected() or not (vc.is_playing() or vc.is_paused()):
            await ctx.send("지금 재생 중이 아니야.")
            return

        sec = self._parse_time_to_sec(t)
        if sec is None or sec < 0:
            await ctx.send("사용법: `!점프 45` 또는 `!점프 1:23`")
            return

        if st.now_playing and getattr(st.now_playing, 'is_live', False):
            await ctx.send("라이브 스트림은 점프가 안 돼…")
            return

        # 현재 곡을 맨 앞으로 다시 넣고 stop -> 다음 루프에서 seek 적용
        cur = st.now_playing
        if cur is None:
            await ctx.send("지금 재생 중인 곡 정보를 못 찾았어…")
            return

        q = getattr(st.queue, '_queue', None)
        if q is not None and hasattr(q, 'appendleft'):
            q.appendleft(cur)
        else:
            # fallback: 재구성
            items = []
            try:
                while not st.queue.empty():
                    items.append(st.queue.get_nowait())
            except Exception:
                pass
            try:
                st.queue.put_nowait(cur)
            except Exception:
                pass
            for it in items:
                try:
                    st.queue.put_nowait(it)
                except Exception:
                    pass

        st.seek_next_sec = float(sec)
        st._suppress_requeue_once = True
        try:
            vc.stop()
        except Exception:
            pass

        await ctx.send(f"{int(sec)}초로 점프할게.")
        self._start_player_if_needed(ctx.guild.id)
        self._start_panel_tick(ctx.guild.id)

    @commands.command(name="구간")
    async def cmd_segment(self, ctx: commands.Context, start: str, end: str, mode: str = ""):
        """!구간 <시작> <끝> [AB]: 현재 곡을 구간 재생(AB면 반복)"""
        if ctx.guild is None:
            return
        st = self._state(ctx.guild.id)
        vc = ctx.guild.voice_client
        if vc is None or not vc.is_connected() or not (vc.is_playing() or vc.is_paused()):
            await ctx.send("지금 재생 중이 아니야.")
            return

        if st.now_playing and getattr(st.now_playing, 'is_live', False):
            await ctx.send("라이브 스트림은 구간 재생이 안 돼…")
            return

        s = self._parse_time_to_sec(start)
        e = self._parse_time_to_sec(end)
        if s is None or e is None:
            await ctx.send("사용법: `!구간 30 90` 또는 `!구간 0:30 1:30 AB`")
            return

        if e <= s:
            await ctx.send("끝 시간이 시작보다 커야 해.")
            return

        st.segment_start_sec = float(s)
        st.segment_end_sec = float(e)
        st.segment_ab_repeat = (mode or '').strip().upper() in {"AB", "A", "R", "REPEAT"}
        st.seek_next_sec = float(s)

        # 현재 곡을 맨 앞으로 다시 넣고 stop
        cur = st.now_playing
        if cur is not None:
            q = getattr(st.queue, '_queue', None)
            if q is not None and hasattr(q, 'appendleft'):
                q.appendleft(cur)

        st._suppress_requeue_once = True
        try:
            vc.stop()
        except Exception:
            pass

        
        def _fmt_time(x: float) -> str:
            mm = int(x // 60)
            ss = int(x % 60)
            return f"{mm:02d}:{ss:02d}"

        await ctx.send(f"구간 {_fmt_time(s)}~{_fmt_time(e)}" + (" (AB 반복)" if st.segment_ab_repeat else "") + "으로 재생할게.")
        self._start_player_if_needed(ctx.guild.id)
        self._start_panel_tick(ctx.guild.id)

    @commands.command(name="구간해제")
    async def cmd_segment_clear(self, ctx: commands.Context):
        """!구간해제: 구간/AB 반복 해제"""
        if ctx.guild is None:
            return
        st = self._state(ctx.guild.id)
        vc = ctx.guild.voice_client

        st.segment_start_sec = None
        st.segment_end_sec = None
        st.segment_ab_repeat = False

        if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
            # 현재 위치로 이어 재생(가능한 경우)
            if st.now_playing and not getattr(st.now_playing, 'is_live', False):
                pos = self._current_pos(st)
                cur = st.now_playing
                q = getattr(st.queue, '_queue', None)
                if cur is not None and q is not None and hasattr(q, 'appendleft'):
                    q.appendleft(cur)
                st.seek_next_sec = float(pos)
                st._suppress_requeue_once = True
                try:
                    vc.stop()
                except Exception:
                    pass
                await ctx.send("구간 재생을 해제했어.")
            else:
                await ctx.send("구간 재생을 해제했어.")
        else:
            await ctx.send("구간 재생을 해제했어.")

        self._start_panel_tick(ctx.guild.id)

async def setup(bot: commands.Bot):
    cog = MusicCog(bot)
    await bot.add_cog(cog)

    try:
        bot.add_view(cog.panel_view)
        bot.add_view(cog.queue_view)
        bot.add_view(cog.sound_view)
    except Exception:
        pass
