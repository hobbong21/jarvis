"""Harness Evolution 텔레메트리 — 라우팅/지연/품질 메타데이터 수집.

PII 미수집 원칙: 사용자 발화 본문은 저장하지 않는다 (길이만).
저장: data/harness_telemetry.jsonl (line-delimited JSON).
조회: summarize() — 집계 통계 dict 반환.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

LOG_PATH = Path(__file__).parent / "data" / "harness_telemetry.jsonl"
MAX_LINES = 5000  # 자동 회전 임계 (초과 시 절반만 유지)

_lock = threading.Lock()


def _ensure_dir():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def new_turn_id() -> str:
    return uuid.uuid4().hex[:12]


def log_turn(meta: Dict) -> None:
    """한 턴의 메타데이터 1줄 추가. PII 본문 금지."""
    safe = _sanitize(meta)
    safe.setdefault("ts", time.time())
    line = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        try:
            _ensure_dir()
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _rotate_if_needed()
        except OSError as e:
            print(f"[telemetry] write failed: {e!r}")


def _sanitize(meta: Dict) -> Dict:
    """저장 금지 키 제거 (사용자 발화/응답 본문)."""
    blocked = {"text", "prompt", "user_text", "reply", "body", "history"}
    out = {}
    for k, v in meta.items():
        if k in blocked:
            # 길이만 보존
            try:
                out[f"{k}_len"] = len(v) if isinstance(v, str) else 0
            except TypeError:
                pass
            continue
        # 컬렉션은 평탄화
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = list(v)[:20]
        elif isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in v.items() if isinstance(vv, (str, int, float, bool))}
        else:
            out[k] = str(v)[:200]
    return out


def _rotate_if_needed():
    """라인이 너무 많으면 후반부 절반만 유지 (단순 회전)."""
    try:
        size_lines = sum(1 for _ in open(LOG_PATH, "r", encoding="utf-8", errors="ignore"))
    except OSError:
        return
    if size_lines <= MAX_LINES:
        return
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        keep = lines[len(lines) // 2:]
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(keep)
    except OSError as e:
        print(f"[telemetry] rotate failed: {e!r}")


def _load_all() -> List[Dict]:
    if not LOG_PATH.exists():
        return []
    out = []
    with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return out


def summarize(limit: Optional[int] = None) -> Dict:
    """전체 (또는 최근 N개) 턴 집계.

    반환 키:
      total            : int
      backends         : {backend: count}
      fallback_rate    : float (0.0~1.0)
      tts_failure_rate : float
      tts_reasons      : {reason: count}
      intents          : {intent: count}
      avg_fanout_ms    : float
      avg_llm_ms       : float
      avg_tts_ms       : float
      last_ts          : float | None
    """
    rows = _load_all()
    if limit:
        rows = rows[-limit:]
    total = len(rows)
    if total == 0:
        return {
            "total": 0, "backends": {}, "fallback_rate": 0.0,
            "tts_failure_rate": 0.0, "tts_reasons": {}, "intents": {},
            "avg_fanout_ms": 0.0, "avg_llm_ms": 0.0, "avg_tts_ms": 0.0,
            "last_ts": None,
        }

    backends = Counter(r.get("backend") for r in rows if r.get("backend"))
    intents = Counter(r.get("intent") for r in rows if r.get("intent"))
    tts_reasons = Counter(r.get("tts_reason") for r in rows if r.get("tts_reason"))

    fallback_count = sum(1 for r in rows if r.get("fallback_used"))
    tts_fail = sum(1 for r in rows if r.get("tts_ok") is False)

    def _avg(key):
        vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
        return (sum(vals) / len(vals)) if vals else 0.0

    return {
        "total": total,
        "backends": dict(backends),
        "fallback_rate": fallback_count / total,
        "tts_failure_rate": tts_fail / total,
        "tts_reasons": dict(tts_reasons),
        "intents": dict(intents),
        "avg_fanout_ms": _avg("fanout_ms"),
        "avg_llm_ms": _avg("llm_ms"),
        "avg_tts_ms": _avg("tts_ms"),
        "last_ts": rows[-1].get("ts"),
    }
