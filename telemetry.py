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
            # 길이만 보존 — str/list/tuple/dict 모두 len() 적용 가능 (none-PII 메타정보).
            try:
                out[f"{k}_len"] = len(v) if hasattr(v, "__len__") else 0
            except TypeError:
                out[f"{k}_len"] = 0
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
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            size_lines = sum(1 for _ in f)
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


# 사이클 #5 T001: 백분위 추적 대상 키 (모두 ms 단위).
LATENCY_KEYS = ("fanout_ms", "llm_ms", "tts_ms", "total_ms")


def _percentile(sorted_vals: List[float], p: float) -> float:
    """순수 파이썬 nearest-rank 백분위 (0<=p<=100). 빈 리스트는 0.0.

    nearest-rank 방식: rank = ceil(p/100 * N), 1-indexed.
    참고: NumPy/Scipy 의 기본(linear interpolation)과 짝수 N 의 p50 등에서
    다소 차이날 수 있다. 운영 관측치는 항상 실 측정값 1개를 가리키므로
    해석 단순함이 장점 (상위 5% 라인은 실제 어떤 턴이었는지 명확).
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    # ceil(p/100 * n) — 정수 산술
    import math
    rank = max(1, min(n, math.ceil((p / 100.0) * n)))
    return float(sorted_vals[rank - 1])


def _latency_stats(rows: List[Dict], key: str) -> Dict[str, float]:
    """단일 지연 키의 avg/p50/p95/p99 (ms). 빈 데이터는 0.0."""
    vals = sorted(r.get(key) for r in rows if isinstance(r.get(key), (int, float)))
    if not vals:
        return {"avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "count": 0}
    avg = sum(vals) / len(vals)
    return {
        "avg": float(avg),
        "p50": _percentile(vals, 50),
        "p95": _percentile(vals, 95),
        "p99": _percentile(vals, 99),
        "count": len(vals),
    }


def _empty_latency_stats() -> Dict[str, float]:
    return {"avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "count": 0}


def summarize(limit: Optional[int] = None) -> Dict:
    """전체 (또는 최근 N개) 턴 집계.

    반환 키:
      total            : int
      backends         : {backend: count}
      input_channels   : {channel: count}
      fallback_rate    : float (0.0~1.0)
      tts_failure_rate : float
      tts_regen_count  : int
      tts_regen_rate   : float
      tts_reasons      : {reason: count}
      intents          : {intent: count}
      avg_fanout_ms    : float  (호환성 유지 — 사이클 #5 이전 클라이언트)
      avg_llm_ms       : float
      avg_tts_ms       : float
      latency          : {key: {avg, p50, p95, p99, count}}  (사이클 #5)
      last_ts          : float | None
    """
    rows = _load_all()
    if limit:
        rows = rows[-limit:]
    total = len(rows)
    if total == 0:
        # 사이클 #4 architect P1: 빈 경로도 비-빈 경로와 동일 키 셋을 반환.
        # 사이클 #5 T001: latency 키 추가 (역시 동일 셋).
        return {
            "total": 0, "backends": {}, "input_channels": {},
            "fallback_rate": 0.0,
            "tts_failure_rate": 0.0,
            "tts_regen_count": 0, "tts_regen_rate": 0.0,
            "tts_reasons": {}, "intents": {},
            "avg_fanout_ms": 0.0, "avg_llm_ms": 0.0, "avg_tts_ms": 0.0,
            "latency": {k: _empty_latency_stats() for k in LATENCY_KEYS},
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

    latency = {k: _latency_stats(rows, k) for k in LATENCY_KEYS}

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
        # 호환성: 기존 avg_* 키 유지 (clients of cycle <=4)
        "avg_fanout_ms": latency["fanout_ms"]["avg"],
        "avg_llm_ms": latency["llm_ms"]["avg"],
        "avg_tts_ms": latency["tts_ms"]["avg"],
        "latency": latency,
        "last_ts": rows[-1].get("ts"),
    }
