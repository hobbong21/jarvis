"""Generate-Verify 게이트 — EdgeTTS 합성 직전 텍스트 안전성/품질 검증.

판단 결과 dict:
  ok       : bool          — 합성 진행 여부
  reason   : str           — 차단/경고 사유 (영문 슬러그)
  sanitized: str           — 정제된 텍스트 (ok=True 일 때 사용)
  warnings : list[str]     — 비치명 경고 (예: 길이 단축)

사유 슬러그(reason):
  ok                — 정상
  empty             — 비어 있음
  too_long          — MAX_LEN 초과 (정제 후에도)
  control_chars     — 인쇄 불가 제어문자
  blocklist:<term>  — 차단어 포함 (시크릿 누설 방지 등)
  low_korean_ratio  — 한국어 비율이 너무 낮음 (TTS 보이스가 한국어이므로)
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Dict, List

# ---- 설정 ----
MAX_LEN = 600                  # 글자 (한국어 기준 ~3분 발화)
MIN_KOREAN_RATIO = 0.10        # 한국어 비율 최소치 (영어/숫자만이어도 통과 가능하지만 너무 낮으면 차단)
MIN_LEN_FOR_KOREAN_CHECK = 12  # 짧은 텍스트("OK", "네")는 한국어 비율 체크 면제

_BLOCKLIST_PATH = Path(__file__).parent / "data" / "tts_blocklist.json"
_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # 탭/줄바꿈은 허용
_MULTI_WS = re.compile(r"[ \t]{2,}")

_blocklist_cache: List[str] | None = None


def _load_blocklist() -> List[str]:
    global _blocklist_cache
    if _blocklist_cache is not None:
        return _blocklist_cache
    try:
        with open(_BLOCKLIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _blocklist_cache = [str(p) for p in data.get("phrases", []) if p]
    except FileNotFoundError:
        _blocklist_cache = []
    except (json.JSONDecodeError, OSError) as e:
        print(f"[tts_verifier] 차단어 로드 실패: {e!r}")
        _blocklist_cache = []
    return _blocklist_cache


def _normalize(text: str) -> str:
    """NFC + 제어문자 제거 + 다중 공백 축약."""
    t = unicodedata.normalize("NFC", text)
    t = _CTRL_RE.sub("", t)
    t = _MULTI_WS.sub(" ", t)
    return t.strip()


def _korean_ratio(text: str) -> float:
    if not text:
        return 0.0
    han = len(_HANGUL_RE.findall(text))
    # 공백/구두점 제외한 의미 있는 문자 수
    meaningful = sum(1 for c in text if not c.isspace())
    if meaningful == 0:
        return 0.0
    return han / meaningful


def _truncate_at_sentence(text: str, limit: int) -> str:
    """문장 끝(., !, ?, …, 다., 요., 죠.) 기준으로 자른다. 못 찾으면 limit 까지 hard-cut."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    # 마지막 문장 종결자 위치
    candidates = [head.rfind(c) for c in (". ", "! ", "? ", "…", "다.", "요.", "죠.", "\n")]
    cut = max(candidates)
    if cut > limit * 0.6:  # 너무 앞에서 잘리면 의미 손실 → 그냥 hard-cut
        end = cut + 1
        return head[:end].rstrip()
    return head.rstrip() + "…"


def verify_tts_candidate(text: str) -> Dict:
    """TTS 합성 직전 안전성/품질 게이트.

    실패해도 sanitized 가 비어있지 않으면 1회 재시도 가능 (예: 너무 길면 잘라서 재시도).
    blocklist 위반은 sanitized 가 비어 있어 재시도 불가."""
    if not text or not text.strip():
        return {"ok": False, "reason": "empty", "sanitized": "", "warnings": []}

    warnings: List[str] = []
    sanitized = _normalize(text)

    # 제어문자: 정제로 제거됐을 것 — 정제 후에도 남았다면 fatal
    if _CTRL_RE.search(sanitized):
        return {"ok": False, "reason": "control_chars", "sanitized": "", "warnings": warnings}

    # 차단어 (시크릿 누설 방지) — sanitize 불가, 즉시 fatal
    lower = sanitized.lower()
    for term in _load_blocklist():
        if term.lower() in lower:
            return {
                "ok": False,
                "reason": f"blocklist:{term[:20]}",
                "sanitized": "",
                "warnings": warnings,
            }

    # 길이: 초과 시 자동 단축 (정제 후 ok)
    if len(sanitized) > MAX_LEN:
        truncated = _truncate_at_sentence(sanitized, MAX_LEN)
        if len(truncated) > MAX_LEN:
            return {
                "ok": False,
                "reason": "too_long",
                "sanitized": truncated[:MAX_LEN],
                "warnings": warnings,
            }
        warnings.append(f"truncated:{len(sanitized)}->{len(truncated)}")
        sanitized = truncated

    # 한국어 비율 (짧은 응답은 면제)
    if len(sanitized) >= MIN_LEN_FOR_KOREAN_CHECK:
        ratio = _korean_ratio(sanitized)
        if ratio < MIN_KOREAN_RATIO:
            warnings.append(f"low_korean_ratio:{ratio:.2f}")
            # 차단하지는 않음 — 한국어 보이스라도 영어 발화는 가능. 경고만.

    return {"ok": True, "reason": "ok", "sanitized": sanitized, "warnings": warnings}


def reload_blocklist() -> int:
    """런타임에 차단어 갱신 (테스트/관리용). 반환: 로드된 항목 수."""
    global _blocklist_cache
    _blocklist_cache = None
    return len(_load_blocklist())
