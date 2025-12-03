from __future__ import annotations

import json
import os
import time
import datetime
from typing import Dict, Any, Optional


# =========================
# 기본 상태 정의
# =========================

DEFAULT_STATE: Dict[str, Any] = {
    "core": {
        # -1.0 ~ +1.0 범위
        "mood": 0.1,        # 기분 (슬픔/짜증 ~ 행복)
        "energy": 0.0,      # 에너지 (지침 ~ 하이텐션)
        "affection": 0.1,   # 전체적인 호감도 베이스
        "irritation": 0.0,  # 짜증 정도
    },
    "guild": {
        # "guild_id": { "bond": 0.0, "trust": 0.0, "last": timestamp }
    },
    "user": {
        # "user_id": { "bond": 0.0, "trust": 0.0, "last": timestamp }
    },
}


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# =========================
# 감정 / 관계 엔진
# =========================

class YumeAI:
    """
    유메 감정 / 관계 상태를 관리하는 코어 클래스.

    - 상태는 JSON 파일에 저장
    - apply_event(...) 로 감정/관계를 조금씩 변화
    - get_core_state() / get_relation_summary() 로 조회
    """

    def __init__(
        self,
        state_path: str = "data/system/yume_personality.json",
        autosave: bool = True,
    ) -> None:
        self.state_path = state_path
        self.autosave = autosave

        self.state: Dict[str, Any] = {}
        self._ensure_dirs()
        self._load_state()

    # -----------------------
    # 파일 IO
    # -----------------------
    def _ensure_dirs(self) -> None:
        base_dir = os.path.dirname(self.state_path)
        if base_dir and not os.path.exists(base_dir):
            os.makedirs(base_dir, exist_ok=True)

    def _load_state(self) -> None:
        if not os.path.exists(self.state_path):
            # 첫 실행: 기본 상태로 초기화
            self.state = json.loads(json.dumps(DEFAULT_STATE))
            self._save_state()
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # 손상된 경우: 기본값으로 리셋
            self.state = json.loads(json.dumps(DEFAULT_STATE))
            self._save_state()
            return

        # 누락 키 보정 (업데이트 시 호환성)
        self.state = json.loads(json.dumps(DEFAULT_STATE))  # deep copy
        self._deep_update(self.state, data)

    def _save_state(self) -> None:
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception:
            # 저장 실패는 조용히 무시 (봇 죽지 않게)
            pass

    @staticmethod
    def _deep_update(base: Dict[str, Any], new: Dict[str, Any]) -> None:
        for k, v in new.items():
            if isinstance(v, dict) and k in base and isinstance(base[k], dict):
                YumeAI._deep_update(base[k], v)
            else:
                base[k] = v

    # -----------------------
    # 내부 유틸
    # -----------------------
    def _get_core(self) -> Dict[str, float]:
        return self.state["core"]

    def _get_rel(
        self,
        bucket: str,  # "guild" or "user"
        key: Optional[str],
        create: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if key is None:
            return None

        bucket_dict = self.state.setdefault(bucket, {})
        rel = bucket_dict.get(key)
        if rel is None and create:
            rel = {"bond": 0.0, "trust": 0.0, "last": None}
            bucket_dict[key] = rel
        return rel

    def _bump_core(self, mood=0.0, energy=0.0, affection=0.0, irritation=0.0) -> None:
        core = self._get_core()
        core["mood"] = _clamp(core["mood"] + mood)
        core["energy"] = _clamp(core["energy"] + energy)
        core["affection"] = _clamp(core["affection"] + affection)
        core["irritation"] = _clamp(core["irritation"] + irritation)

    def _bump_rel(
        self,
        rel: Optional[Dict[str, Any]],
        bond=0.0,
        trust=0.0,
    ) -> None:
        if rel is None:
            return
        rel["bond"] = _clamp(rel.get("bond", 0.0) + bond)
        rel["trust"] = _clamp(rel.get("trust", 0.0) + trust)
        rel["last"] = time.time()

    # -----------------------
    # 외부에서 쓰는 API (이벤트)
    # -----------------------
    def apply_event(
        self,
        event: str,
        *,
        user_id: Optional[str] = None,
        guild_id: Optional[str] = None,
        weight: float = 1.0,
    ) -> None:
        """
        유메에게 일어난 일을 알려주는 함수.

        예시:
        - event="mention"
        - event="friendly_chat"
        - event="insult"
        - event="music_play"
        - event="spammy_ping"
        - event="bot_tired"
        - event="bot_rest"
        """

        weight = max(0.0, weight)

        # 관계 객체
        user_rel = self._get_rel("user", user_id, create=True)
        guild_rel = self._get_rel("guild", guild_id, create=True)

        # 이벤트별 기본 변화량
        if event == "mention":
            self._bump_core(mood=0.05 * weight, affection=0.03 * weight)
            self._bump_rel(user_rel, bond=0.02 * weight, trust=0.01 * weight)
            self._bump_rel(guild_rel, bond=0.01 * weight, trust=0.01 * weight)

        elif event == "friendly_chat":
            self._bump_core(mood=0.07 * weight, affection=0.04 * weight)
            self._bump_rel(user_rel, bond=0.04 * weight, trust=0.02 * weight)

        elif event == "insult":
            self._bump_core(mood=-0.1 * weight, irritation=0.15 * weight)
            self._bump_rel(user_rel, bond=-0.05 * weight, trust=-0.05 * weight)

        elif event == "music_play":
            self._bump_core(mood=0.04 * weight, energy=0.05 * weight)

        elif event == "spammy_ping":
            self._bump_core(mood=-0.06 * weight, irritation=0.12 * weight)
            self._bump_rel(user_rel, bond=-0.02 * weight)

        elif event == "bot_tired":
            self._bump_core(energy=-0.08 * weight)

        elif event == "bot_rest":
            # 밤새 안 불러주면 스스로 에너지 약간 회복하는 느낌
            self._bump_core(energy=0.05 * weight, irritation=-0.05 * weight)

        if self.autosave:
            self._save_state()

    # -----------------------
    # 현재 상태 조회
    # -----------------------
    def get_core_state(self) -> Dict[str, float]:
        """디버깅/로그용: 현재 코어 감정 상태 리턴."""
        return dict(self._get_core())

    def get_relation_summary(
        self,
        *,
        user_id: Optional[str] = None,
        guild_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """특정 유저/길드에 대한 관계값."""
        user_rel = self._get_rel("user", user_id, create=False)
        guild_rel = self._get_rel("guild", guild_id, create=False)
        return {
            "user": dict(user_rel) if user_rel else None,
            "guild": dict(guild_rel) if guild_rel else None,
        }

    # -----------------------
    # 톤(tone) 계산
    # -----------------------
    def compute_tone(
        self,
        *,
        user_id: Optional[str] = None,
        guild_id: Optional[str] = None,
    ) -> str:
        core = self._get_core()
        mood = core["mood"]
        energy = core["energy"]
        irritation = core["irritation"]

        user_rel = self._get_rel("user", user_id, create=False)
        bond = user_rel["bond"] if user_rel else 0.0

        # 아주 대략적인 규칙 기반 톤 분류
        if irritation > 0.6:
            return "annoyed"
        if mood > 0.4 and bond > 0.3:
            return "soft_affectionate"
        if mood > 0.3:
            if energy > 0.2:
                return "cheerful"
            return "calm_happy"
        if energy < -0.3:
            return "tired"
        return "neutral"


# =========================
# 말투 엔진 (컨텍스트 키 → 문장)
# =========================

class YumeSpeaker:
    """
    각 Cog 에서 context_key 로 요청하면
    적당한 문장을 만들어 주는 말투 모듈.

    - LLM 없이, 템플릿 + tone 만 사용
    """

    def __init__(self, core: YumeAI):
        self.core = core

        # 상황별 기본 대사 템플릿들
        self.templates: Dict[str, str] = {
            "music_panel_open": "음악 패널 열어뒀어. 같이 들을까?",
            "music_panel_reuse": "기존 음악 패널을 다시 쓸게.",
            "music_add_search": "✅ **{title}** 추가했어.",
            "music_add_url": "🔗 **{title}** 추가했어.",
            "music_add_spotify": "🎵 Spotify 곡을 찾아서 추가했어: **{title}**",
            "music_loop_changed": "🔁 반복 모드: `{mode}` 로 바꿨어.",
            "voice_left_empty": "아무도 없어서… 나도 나갈게.",
        }

    def say(
        self,
        context_key: str,
        *,
        user_id: Optional[int] = None,
        user_name: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> str:
        extra = extra or {}

        tone = self.core.compute_tone(
            user_id=str(user_id) if user_id is not None else None,
            guild_id=None,
        )

        base = self.templates.get(context_key)
        if not base:
            return ""

        try:
            text = base.format(**extra)
        except Exception:
            text = base

        # 톤에 따라 살짝만 변주
        if tone == "annoyed":
            if "…" not in text:
                text = "… " + text
        elif tone == "soft_affectionate" and user_name:
            text = f"{user_name}, " + text
        elif tone == "tired":
            text = text.replace("!", "…")  # 힘 빠진 느낌

        return text


# =========================
# 간단 로그 / 메모리
# =========================

class YumeMemory:
    """
    mem.log_today("문장") 으로 하루 로그를 파일에 쌓는 간단한 일기장.
    """

    def __init__(self, base_dir: str = "data/system"):
        self.base_dir = base_dir
        self.log_dir = os.path.join(self.base_dir, "logs")
        os.makedirs(self.log_dir, exist_ok=True)

    def log_today(self, text: str) -> None:
        today = datetime.date.today().isoformat()
        path = os.path.join(self.log_dir, f"{today}.log")
        line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {text}\n"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            # 로그 실패해도 봇 죽을 필요는 없다.
            pass


# =========================
# yume.py 에서 호출하는 엔트리
# =========================

def setup_yume_ai(bot) -> None:
    """
    yume.py 의 main() 안에서 한 번만 호출하면 됨.

    - bot.yume_core    : YumeAI (감정/관계 엔진)
    - bot.yume_speaker : YumeSpeaker (말투 엔진)
    - bot.yume_memory  : YumeMemory (일기장/로그)
    """
    core = YumeAI()
    bot.yume_core = core
    bot.yume_speaker = YumeSpeaker(core)
    bot.yume_memory = YumeMemory()
