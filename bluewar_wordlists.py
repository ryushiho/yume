"""bluewar_wordlists.py

Phase 5: 블루전 단어 DB를 웹(yume-admin)에서 가져오고, 로컬 캐시로 저장한다.

- 읽기 API (웹):
  - GET {BASE}/api/bluewar/wordlists/meta
  - GET {BASE}/api/bluewar/wordlists/suggestion.txt
  - GET {BASE}/api/bluewar/wordlists/blue_archive_words.txt

  ※ 운영 웹에서 원본 TXT를 토큰으로 보호하는 경우
    env YUME_WORDLIST_TOKEN 을 봇과 웹 둘 다 동일하게 설정해야 한다.

- BASE URL 우선순위:
  - YUME_WORDLIST_BASE_URL
  - YUME_ADMIN_URL (전적 전송에 쓰는 동일 base)

봇은 네트워크가 실패해도 게임이 멈추면 안 되므로,
"캐시 -> 로컬 파일" 순으로 폴백한다.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

from config import (
    BLUEWAR_WORDLIST_CACHE_DIR,
    BLUEWAR_CACHED_WORDS_FILE,
    BLUEWAR_CACHED_SUGGESTION_FILE,
    BLUEWAR_WORDLIST_META_FILE,
)

logger = logging.getLogger(__name__)


ALLOWED_LISTS = {
    "suggestion": "suggestion.txt",
    "blue_archive_words": "blue_archive_words.txt",
}


def get_wordlist_base_url() -> str:
    base = (os.getenv("YUME_WORDLIST_BASE_URL") or os.getenv("YUME_ADMIN_URL") or "").strip()
    return base.rstrip("/")


def get_wordlist_token() -> str:
    """원본 TXT 다운로드 보호 토큰.

    웹(yume-web)에서 /api/bluewar/wordlists/*.txt 를 토큰으로 보호할 때 사용.
    - env: YUME_WORDLIST_TOKEN
    """
    return (os.getenv("YUME_WORDLIST_TOKEN") or "").strip()


def _http_get(url: str, *, timeout: float = 5.0) -> bytes:
    token = get_wordlist_token()
    headers = {
        "User-Agent": "yumebot-bluewar-wordlists/1.0",
        "Accept": "application/json, text/plain, */*",
    }
    if token:
        headers["X-Yume-Wordlist-Token"] = token
    req = urllib.request.Request(
        url=url,
        headers=headers,
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read() or b""


def _atomic_write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def load_local_meta() -> Dict[str, Dict[str, Optional[str]]]:
    try:
        if os.path.exists(BLUEWAR_WORDLIST_META_FILE):
            with open(BLUEWAR_WORDLIST_META_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data  # type: ignore[return-value]
    except Exception:
        return {}
    return {}


def save_local_meta(meta: Dict[str, Dict[str, Optional[str]]]) -> None:
    try:
        _atomic_write_bytes(BLUEWAR_WORDLIST_META_FILE, json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
    except Exception as e:
        logger.warning("[Wordlists] meta 저장 실패: %s", e)


def fetch_remote_meta(base_url: str, *, timeout: float = 5.0) -> Dict[str, Dict[str, Optional[str]]]:
    url = f"{base_url}/api/bluewar/wordlists/meta"
    raw = _http_get(url, timeout=timeout)
    try:
        meta = json.loads(raw.decode("utf-8"))
    except Exception:
        meta = None
    if not isinstance(meta, dict):
        raise RuntimeError("meta response is not a dict")
    return meta  # type: ignore[return-value]


def download_list_txt(base_url: str, list_name: str, *, timeout: float = 8.0) -> bytes:
    if list_name not in ALLOWED_LISTS:
        raise ValueError("unknown list_name")
    url = f"{base_url}/api/bluewar/wordlists/{list_name}.txt"
    return _http_get(url, timeout=timeout)


@dataclass
class SyncResult:
    synced: bool
    changed: bool
    changed_lists: List[str]
    reason: str
    remote_meta: Dict[str, Dict[str, Optional[str]]]


def sync_wordlists(*, force: bool = False, timeout_meta: float = 5.0, timeout_txt: float = 10.0) -> SyncResult:
    """웹에서 단어 리스트를 내려받아 캐시로 동기화한다.

    - force=True면 meta 비교 없이 무조건 다운로드 시도
    - 실패해도 예외를 던지지 않고 reason에 담아 돌려준다.
    """
    base_url = get_wordlist_base_url()
    if not base_url:
        return SyncResult(synced=False, changed=False, changed_lists=[], reason="no base_url", remote_meta={})

    os.makedirs(BLUEWAR_WORDLIST_CACHE_DIR, exist_ok=True)

    local_meta = load_local_meta()

    try:
        remote_meta = fetch_remote_meta(base_url, timeout=timeout_meta)
    except Exception as e:
        return SyncResult(synced=False, changed=False, changed_lists=[], reason=f"meta fetch failed: {e}", remote_meta={})

    changed_lists: List[str] = []

    for list_name in ALLOWED_LISTS.keys():
        rm = remote_meta.get(list_name) if isinstance(remote_meta, dict) else None
        if not isinstance(rm, dict):
            continue

        remote_sha = (rm.get("sha256") or "").strip() if rm else ""
        local_sha = ""
        try:
            lm = local_meta.get(list_name) if isinstance(local_meta, dict) else None
            if isinstance(lm, dict):
                local_sha = (lm.get("sha256") or "").strip()
        except Exception:
            local_sha = ""

        target_path = BLUEWAR_CACHED_SUGGESTION_FILE if list_name == "suggestion" else BLUEWAR_CACHED_WORDS_FILE
        need = force or (not os.path.exists(target_path)) or (remote_sha and remote_sha != local_sha)

        if not need:
            continue

        try:
            data = download_list_txt(base_url, list_name, timeout=timeout_txt)
            if data is None or len(data) == 0:
                raise RuntimeError("empty txt")
            _atomic_write_bytes(target_path, data)
            changed_lists.append(list_name)
        except Exception as e:
            # 특정 리스트 다운로드 실패는 전체 실패로 보지 않고, 다른 리스트는 계속 시도.
            logger.warning("[Wordlists] %s 다운로드 실패: %s", list_name, e)

    if changed_lists:
        # meta 저장
        try:
            # 우리가 관심있는 필드만 저장(혹시 서버 형식이 바뀌어도 안전)
            saved: Dict[str, Dict[str, Optional[str]]] = {}
            for k in ALLOWED_LISTS.keys():
                v = remote_meta.get(k)
                if isinstance(v, dict):
                    saved[k] = {
                        "sha256": v.get("sha256"),
                        "updated_at": v.get("updated_at"),
                        "count": v.get("count"),
                        "filename": v.get("filename"),
                    }
            save_local_meta(saved)
        except Exception:
            pass

    return SyncResult(
        synced=True,
        changed=bool(changed_lists),
        changed_lists=changed_lists,
        reason="ok" if changed_lists else "up-to-date",
        remote_meta=remote_meta,
    )


def get_cached_paths() -> Tuple[str, str]:
    """(words_path, suggestion_path)"""
    return BLUEWAR_CACHED_WORDS_FILE, BLUEWAR_CACHED_SUGGESTION_FILE
