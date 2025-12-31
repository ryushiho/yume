from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import discord

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "data" / "system" / "status_messages.json"

KST = timezone(timedelta(hours=9))


_DEFAULT_CFG: Dict[str, Any] = {
    "interval_minutes": {"min": 35, "max": 95},
    "items": [
        # 기본/몽롱
        {"type": "playing", "text": "학생회 업무… 하는 척…", "bands": ["morning", "day"]},
        {"type": "playing", "text": "뇌가 로딩 중… 으헤~", "bands": ["night", "evening"]},
        {"type": "watching", "text": "후배들 출석 체크", "bands": ["morning", "day"]},
        {"type": "watching", "text": "후배들 대화", "bands": ["evening", "night"]},
        {"type": "listening", "text": "후배의 한숨", "bands": ["evening", "night"]},
        {"type": "playing", "text": "시간표랑 싸우는 중", "bands": ["morning", "day"]},
        {"type": "playing", "text": "낮잠 계획 세우는 중", "bands": ["evening", "night"]},
        # 후배 보호/케어
        {"type": "watching", "text": "후배들 안부 확인", "bands": ["day", "evening"]},
        {"type": "playing", "text": "담요 챙겨주는 중", "bands": ["night", "evening"]},
        {"type": "listening", "text": "심호흡 소리", "bands": ["night", "evening"]},
        # 학생회장 모드
        {"type": "playing", "text": "보고서 결재 중", "bands": ["morning", "day"]},
        {"type": "playing", "text": "예산표랑 눈싸움", "bands": ["morning", "day"]},
        {"type": "watching", "text": "공지문 검토", "bands": ["day", "evening"]},
        # 호시노 모드(가끔)
        {"type": "watching", "text": "(몰래) 1학년 관찰", "bands": ["day", "evening"]},
        {"type": "playing", "text": "방패 피하는 중…", "bands": ["evening", "night"]},
        {"type": "listening", "text": "\"선배 시끄러워요\"", "bands": ["day", "evening"]},
        # 시스템/세계
        {"type": "playing", "text": "아비도스 날씨 체크", "bands": ["morning", "day", "evening", "night"]},
    ],
}


_cfg_cache: Optional[Dict[str, Any]] = None
_cfg_mtime: Optional[float] = None


def _now_kst() -> datetime:
    return datetime.now(tz=KST)


def _time_band_kst(now: Optional[datetime] = None) -> str:
    """4구간(요청): night/morning/day/evening"""
    dt = now or _now_kst()
    hh = dt.hour
    if 0 <= hh < 7:
        return "night"
    if 7 <= hh < 12:
        return "morning"
    if 12 <= hh < 18:
        return "day"
    return "evening"


def _load_cfg() -> Dict[str, Any]:
    global _cfg_cache, _cfg_mtime  # noqa: PLW0603

    try:
        if CFG_PATH.exists():
            mtime = CFG_PATH.stat().st_mtime
            if _cfg_cache is not None and _cfg_mtime == mtime:
                return _cfg_cache

            with CFG_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                _cfg_cache = data
                _cfg_mtime = mtime
                return data
    except Exception as e:
        logger.warning("[presence] config load failed: %s", e)

    # fallback + ensure file exists (best effort)
    try:
        CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CFG_PATH.write_text(json.dumps(_DEFAULT_CFG, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    _cfg_cache = _DEFAULT_CFG
    _cfg_mtime = None
    return _DEFAULT_CFG


def _pick_interval_seconds(cfg: Dict[str, Any]) -> int:
    iv = cfg.get("interval_minutes") if isinstance(cfg, dict) else None
    if not isinstance(iv, dict):
        return 60 * 60

    mn = int(iv.get("min", 35) or 35)
    mx = int(iv.get("max", 95) or 95)
    if mn < 10:
        mn = 10
    if mx < mn:
        mx = mn
    return int(random.randint(mn, mx) * 60)


def get_next_presence_interval_seconds() -> int:
    cfg = _load_cfg()
    return _pick_interval_seconds(cfg)


def _build_activity(item_type: str, text: str) -> discord.Activity:
    t = (item_type or "playing").strip().lower()
    name = (text or "").strip()[:128]

    if t == "watching":
        return discord.Activity(type=discord.ActivityType.watching, name=name)
    if t == "listening":
        return discord.Activity(type=discord.ActivityType.listening, name=name)
    if t == "competing":
        return discord.Activity(type=discord.ActivityType.competing, name=name)

    # playing(default)
    return discord.Game(name=name)


async def apply_random_presence(bot: discord.Client, *, forced_band: Optional[str] = None) -> Dict[str, Any]:
    """Pick + apply a random presence message.

    Returns dict with: ok/band/type/text
    """
    cfg = _load_cfg()
    band = forced_band or _time_band_kst()

    items = cfg.get("items") if isinstance(cfg, dict) else None
    if not isinstance(items, list) or not items:
        items = _DEFAULT_CFG["items"]

    candidates = []
    for it in items:
        if not isinstance(it, dict):
            continue
        bands = it.get("bands")
        if not bands or (isinstance(bands, list) and band in bands):
            candidates.append(it)

    if not candidates:
        candidates = [it for it in items if isinstance(it, dict)]

    chosen = random.choice(candidates) if candidates else {"type": "playing", "text": "!도움"}

    item_type = str(chosen.get("type") or "playing")
    text = str(chosen.get("text") or "!도움").replace("@", "@\u200b")  # 안전
    activity = _build_activity(item_type, text)

    # discord.py: change_presence requires ready connection
    try:
        if hasattr(bot, "wait_until_ready"):
            await bot.wait_until_ready()  # type: ignore[attr-defined]
    except Exception:
        pass

    await bot.change_presence(activity=activity)

    return {"ok": True, "band": band, "type": item_type, "text": text}
