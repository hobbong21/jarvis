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

# 사이클 #4 T002: 실시간 구독자 (대시보드 WS 등)
# 콜백 시그니처: callback(meta_dict) — 비동기 안전 의무는 콜백 측 책임 (run_coroutine_threadsafe 등).
_subscribers: List = []
_sub_lock = threading.Lock()


def subscribe(callback) -> None:
    """log_turn() 마다 호출될 콜백 등록. 동일 콜백 중복 등록 금지."""
    with _sub_lock:
        if callback not in _subscribers:
            _subscribers.append(callback)


def unsubscribe(callback) -> None:
    with _sub_lock:
        try:
            _subscribers.remove(callback)
        except ValueError:
            pass


def _notify(meta: Dict) -> None:
    """모든 구독자에게 새 turn 메타를 통지. 콜백 예외는 격리."""
    with _sub_lock:
        subs = list(_subscribers)
    for cb in subs:
        try:
            cb(meta)
        except Exception as e:
            print(f"[telemetry] subscriber {cb!r} raised: {e!r}")


def _ensure_dir():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def new_turn_id() -> str:
    return uuid.uuid4().hex[:12]


def log_turn(meta: Dict) -> None:
    """한 턴의 메타데이터 1줄 추가. PII 본문 금지.

    사이클 #4 T002: 디스크 기록 후 _notify(safe) 로 실시간 구독자에게 푸시.
    """
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
    # I/O 성공 여부와 무관하게 구독자 통지 (실시간 우선) — _lock 밖에서 호출.
    _notify(safe)


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


def recent(n: int = 50) -> List[Dict]:
    """최근 N개 턴 메타 반환 (Harness Evolve 입력용)."""
    rows = _load_all()
    return rows[-n:] if n > 0 else rows


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
        # 사이클 #4 architect P1: 빈 경로도 비-빈 경로와 동일 키 셋을 반환해야
        # 클라이언트(대시보드/테스트)가 키 존재를 가정해도 안전하다.
        return {
            "total": 0, "backends": {}, "input_channels": {},
            "fallback_rate": 0.0,
            "tts_failure_rate": 0.0,
            "tts_regen_count": 0, "tts_regen_rate": 0.0,
            "tts_reasons": {}, "intents": {},
            "avg_fanout_ms": 0.0, "avg_llm_ms": 0.0, "avg_tts_ms": 0.0,
            "last_ts": None,
        }

    backends = Counter(r.get("backend") for r in rows if r.get("backend"))
    intents = Counter(r.get("intent") for r in rows if r.get("intent"))
    tts_reasons = Counter(r.get("tts_reason") for r in rows if r.get("tts_reason"))

    fallback_count = sum(1 for r in rows if r.get("fallback_used"))
    tts_fail = sum(1 for r in rows if r.get("tts_ok") is False)
    tts_regen = sum(1 for r in rows if r.get("tts_regenerated"))
    # 입력 채널 (audio vs text) — handle_audio 는 input_channel="audio" 로 기록
    channels = Counter(r.get("input_channel") for r in rows if r.get("input_channel"))

    def _avg(key):
        vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
        return (sum(vals) / len(vals)) if vals else 0.0

    return {
        "total": total,
        "backends": dict(backends),
        "input_channels": dict(channels),
        "fallback_rate": fallback_count / total,
        "tts_failure_rate": tts_fail / total,
        "tts_regen_count": tts_regen,
        "tts_regen_rate": tts_regen / total,
        "tts_reasons": dict(tts_reasons),
        "intents": dict(intents),
        "avg_fanout_ms": _avg("fanout_ms"),
        "avg_llm_ms": _avg("llm_ms"),
        "avg_tts_ms": _avg("tts_ms"),
        "last_ts": rows[-1].get("ts"),
    }
