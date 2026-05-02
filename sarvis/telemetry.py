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

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "harness_telemetry.jsonl"
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


# 사이클 #7 — 백엔드별 비교 통계.
def _per_backend_stats(rows: List[Dict]) -> Dict[str, Dict]:
    """백엔드별 count / avg/p50 LLM ms / TTS 실패율 / 폴백률 / 재생성률.

    같은 키셋(빈 데이터 포함)을 보장 — 대시보드가 안전 렌더하도록.
    """
    by: Dict[str, List[Dict]] = {}
    for r in rows:
        b = r.get("backend")
        if not b:
            continue
        by.setdefault(b, []).append(r)
    out: Dict[str, Dict] = {}
    for b, lst in by.items():
        n = len(lst)
        llm_vals = sorted(r.get("llm_ms") for r in lst if isinstance(r.get("llm_ms"), (int, float)))
        avg_llm = (sum(llm_vals) / len(llm_vals)) if llm_vals else 0.0
        p50_llm = _percentile(llm_vals, 50)
        tts_fail = sum(1 for r in lst if r.get("tts_ok") is False)
        tts_regen = sum(1 for r in lst if r.get("tts_regenerated"))
        fb = sum(1 for r in lst if r.get("fallback_used"))
        out[b] = {
            "count": n,
            "avg_llm_ms": float(avg_llm),
            "p50_llm_ms": float(p50_llm),
            "tts_failure_rate": (tts_fail / n) if n else 0.0,
            "tts_regen_rate": (tts_regen / n) if n else 0.0,
            "fallback_rate": (fb / n) if n else 0.0,
        }
    return out


# ============================================================
# 사이클 #9 — 3-Pillar (Voice / Vision / Action) 메트릭 + 점수
# ============================================================
#
# Pillar 의미:
#   - Voice  : 음성이 기본 인터페이스. STT/TTS 품질, audio 비율, 빈 전사율.
#   - Vision : 카메라를 통한 공통 이미지 분석. _t_see/_t_observe 호출 빈도.
#   - Action : 사람이 시키는 일의 즉각 실행. tool 호출 latency·성공률, total_ms.
#
# 점수(0~100)는 결정적 — LLM 호출 없이 즉시 계산. 임계값 기반 가중 평균.
# 빈 데이터(데이터 부족)는 score=None 으로 반환해 클라이언트가 "측정중" UI 가능.
PILLAR_KEYS = ("voice", "vision", "action")


def _empty_pillar() -> Dict:
    """빈/비빈 키셋 동등성 — 클라이언트가 안전 렌더 가능."""
    return {
        "score": None,
        "samples": 0,
        "metrics": {},
        "notes": [],
    }


def _voice_pillar(rows: List[Dict]) -> Dict:
    """음성 기둥. 점수 = (audio 비율 60 + (1-empty율) * 25 + (1-tts차단율) * 15)
    samples = audio turn 수.
    """
    if not rows:
        return _empty_pillar()
    audio_rows = [r for r in rows if r.get("input_channel") == "audio"]
    n_audio = len(audio_rows)
    n_total = len(rows)
    audio_ratio = n_audio / n_total if n_total else 0.0
    # 빈 전사율 (audio 만 대상). empty_transcription=True 이면 STT 가 음성 캡처 실패.
    n_empty = sum(1 for r in audio_rows if r.get("empty_transcription"))
    empty_rate = (n_empty / n_audio) if n_audio else 0.0
    # TTS 차단율 (전체 turn). tts_ok 가 False 인 비율.
    tts_eval = [r for r in rows if r.get("tts_ok") is not None]
    tts_fail = sum(1 for r in tts_eval if r.get("tts_ok") is False)
    tts_fail_rate = (tts_fail / len(tts_eval)) if tts_eval else 0.0
    # STT latency (audio 의 fanout_ms 가 stt 포함). p50 만 표면화.
    fanout_vals = sorted(
        r.get("fanout_ms") for r in audio_rows
        if isinstance(r.get("fanout_ms"), (int, float))
    )
    p50_fanout = _percentile(fanout_vals, 50) if fanout_vals else 0.0

    # 데이터 신뢰도가 낮으면 score=None.
    if n_total < 3:
        return {
            "score": None,
            "samples": n_audio,
            "metrics": {
                "audio_ratio": audio_ratio,
                "empty_transcription_rate": empty_rate,
                "tts_failure_rate": tts_fail_rate,
                "p50_fanout_ms": p50_fanout,
                "audio_turns": n_audio,
                "total_turns": n_total,
            },
            "notes": ["측정 표본 부족 (3턴 미만)"],
        }

    score = (
        audio_ratio * 60.0
        + (1.0 - empty_rate) * 25.0
        + (1.0 - tts_fail_rate) * 15.0
    )
    notes: List[str] = []
    if audio_ratio < 0.3:
        notes.append("음성 입력 비율이 낮음 — 사용자가 텍스트로 입력 중")
    if empty_rate > 0.20 and n_audio >= 5:
        notes.append("빈 전사율 높음 — 마이크/STT 임계 점검 필요")
    if tts_fail_rate > 0.10:
        notes.append("TTS 차단율 높음 — 응답 길이/포맷 검토")
    return {
        "score": round(score, 1),
        "samples": n_audio,
        "metrics": {
            "audio_ratio": round(audio_ratio, 3),
            "empty_transcription_rate": round(empty_rate, 3),
            "tts_failure_rate": round(tts_fail_rate, 3),
            "p50_fanout_ms": round(p50_fanout, 1),
            "audio_turns": n_audio,
            "total_turns": n_total,
        },
        "notes": notes,
    }


def _vision_pillar(rows: List[Dict]) -> Dict:
    """비전 기둥. 점수 = (vision_use_ratio * 70) + 30 - latency_penalty.
    - vision_use_ratio: 카메라 기반 도구(_t_see / observe_action / identify_person) 가
      호출된 turn 의 비율.
    - latency_penalty: vision turn 의 tool_ms p50 이 4000ms 초과 시 100ms 당 1점,
      최대 30점 차감 (사용은 했지만 너무 느린 케이스).
    samples = vision_used=True 인 turn 수. 빈 데이터(턴 < 3)는 score=None.
    """
    if not rows:
        return _empty_pillar()
    n_total = len(rows)
    vision_rows = [r for r in rows if r.get("vision_used")]
    n_vision = len(vision_rows)
    vision_ratio = n_vision / n_total if n_total else 0.0
    # vision tool 의 평균 latency (tool_ms 합산, vision_used turn 만).
    tool_ms_vals = sorted(
        r.get("tool_ms") for r in vision_rows
        if isinstance(r.get("tool_ms"), (int, float)) and r.get("tool_ms") > 0
    )
    p50_tool = _percentile(tool_ms_vals, 50) if tool_ms_vals else 0.0

    if n_total < 3:
        return {
            "score": None,
            "samples": n_vision,
            "metrics": {
                "vision_use_ratio": vision_ratio,
                "p50_vision_tool_ms": p50_tool,
                "vision_turns": n_vision,
                "total_turns": n_total,
            },
            "notes": ["측정 표본 부족 (3턴 미만)"],
        }

    # 점수: 사용 비율이 핵심. 사용 안 하면 0 에 가까움. 사용해도 latency 가
    # 너무 길면 -페널티. 4초 이상 = 절반 차감.
    latency_penalty = 0.0
    if p50_tool > 4000.0:
        latency_penalty = min(30.0, (p50_tool - 4000.0) / 100.0)  # 4s 초과 1ms당 0.01점
    score = vision_ratio * 70.0 + 30.0 - latency_penalty
    score = max(0.0, min(100.0, score))
    notes: List[str] = []
    if vision_ratio == 0.0 and n_total >= 5:
        notes.append("비전 호출 0회 — 카메라가 꺼져 있거나 _t_see 미사용")
    elif vision_ratio < 0.10 and n_total >= 10:
        notes.append("비전 호출 비율이 낮음 — 화면 분석 활용도 점검")
    if p50_tool > 4000.0:
        notes.append(f"비전 도구 p50 {p50_tool:.0f}ms — 4s 초과")
    return {
        "score": round(score, 1),
        "samples": n_vision,
        "metrics": {
            "vision_use_ratio": round(vision_ratio, 3),
            "p50_vision_tool_ms": round(p50_tool, 1),
            "vision_turns": n_vision,
            "total_turns": n_total,
        },
        "notes": notes,
    }


def _action_pillar(rows: List[Dict]) -> Dict:
    """액션 기둥. 점수 = total_ms 빠를수록 + tool 호출 성공률 + error 0율.
    중점: total_ms (즉각성) p50 < 2000ms = 만점, > 8000ms = 0점, 선형 보간.
    samples = tool_count > 0 인 turn 수.
    """
    if not rows:
        return _empty_pillar()
    n_total = len(rows)
    total_ms_vals = sorted(
        r.get("total_ms") for r in rows
        if isinstance(r.get("total_ms"), (int, float)) and r.get("total_ms") > 0
    )
    p50_total = _percentile(total_ms_vals, 50) if total_ms_vals else 0.0
    p95_total = _percentile(total_ms_vals, 95) if total_ms_vals else 0.0
    n_with_tools = sum(1 for r in rows if (r.get("tool_count") or 0) > 0)
    n_errors = sum(1 for r in rows if r.get("error"))
    err_rate = (n_errors / n_total) if n_total else 0.0
    tool_use_ratio = (n_with_tools / n_total) if n_total else 0.0

    if n_total < 3:
        return {
            "score": None,
            "samples": n_with_tools,
            "metrics": {
                "p50_total_ms": p50_total,
                "p95_total_ms": p95_total,
                "tool_use_ratio": tool_use_ratio,
                "error_rate": err_rate,
                "total_turns": n_total,
            },
            "notes": ["측정 표본 부족 (3턴 미만)"],
        }

    # 즉각성 점수: 2000ms = 100, 8000ms = 0, 선형.
    if p50_total <= 2000.0:
        speed_score = 100.0
    elif p50_total >= 8000.0:
        speed_score = 0.0
    else:
        speed_score = 100.0 * (1.0 - (p50_total - 2000.0) / 6000.0)
    # 가중 평균: 즉각성 70 + 무에러율 20 + 도구 활용도 10.
    score = speed_score * 0.7 + (1.0 - err_rate) * 100.0 * 0.2 + tool_use_ratio * 100.0 * 0.1
    score = max(0.0, min(100.0, score))
    notes: List[str] = []
    if p50_total > 5000.0:
        notes.append(f"전체 응답 p50 {p50_total/1000:.1f}s — 즉각성 저하")
    if err_rate > 0.05:
        notes.append(f"에러율 {err_rate*100:.1f}% — 백엔드/도구 안정성 점검")
    if tool_use_ratio == 0.0 and n_total >= 10:
        notes.append("도구 호출 0회 — 액션이 단순 응답에 그침")
    return {
        "score": round(score, 1),
        "samples": n_with_tools,
        "metrics": {
            "p50_total_ms": round(p50_total, 1),
            "p95_total_ms": round(p95_total, 1),
            "tool_use_ratio": round(tool_use_ratio, 3),
            "error_rate": round(err_rate, 3),
            "total_turns": n_total,
        },
        "notes": notes,
    }


def _pillar_metrics(rows: List[Dict]) -> Dict[str, Dict]:
    """3-Pillar (voice/vision/action) 통합 결정적 메트릭.

    빈/비빈 키셋 동등 — 항상 PILLAR_KEYS 모두 포함, 각각 _empty_pillar() 동형.
    """
    return {
        "voice": _voice_pillar(rows),
        "vision": _vision_pillar(rows),
        "action": _action_pillar(rows),
    }


def _build_insights(rows: List[Dict], by_backend: Dict[str, Dict],
                    tts_reasons: Dict[str, int], total: int) -> List[Dict]:
    """SARVIS 자기 개선에 actionable 한 자동 인사이트 (사이클 #7).

    각 인사이트: {level: 'info'|'warn'|'err', message: str}.
    빈 데이터는 빈 리스트.
    """
    insights: List[Dict] = []
    if total == 0:
        return insights

    # (1) 가장 빠른 / 가장 느린 백엔드 (p50 LLM ms 기준, count >= 3 만 비교).
    eligible = {b: s for b, s in by_backend.items() if s["count"] >= 3 and s["p50_llm_ms"] > 0}
    if len(eligible) >= 2:
        fastest = min(eligible.items(), key=lambda kv: kv[1]["p50_llm_ms"])
        slowest = max(eligible.items(), key=lambda kv: kv[1]["p50_llm_ms"])
        if fastest[0] != slowest[0]:
            insights.append({
                "level": "info",
                "message": (
                    f"가장 빠른 백엔드는 {fastest[0]} (p50 {fastest[1]['p50_llm_ms']:.0f}ms), "
                    f"가장 느린 백엔드는 {slowest[0]} (p50 {slowest[1]['p50_llm_ms']:.0f}ms)."
                ),
            })

    # (2) 폴백률 10% 초과 백엔드 경고.
    for b, s in by_backend.items():
        if s["count"] >= 5 and s["fallback_rate"] > 0.10:
            insights.append({
                "level": "warn",
                "message": (
                    f"{b} 백엔드의 폴백률이 {s['fallback_rate']*100:.0f}% — "
                    f"{s['count']}회 중 {int(s['fallback_rate']*s['count'])}건 다른 백엔드로 넘어감. "
                    f"키/모델 점검 권장."
                ),
            })

    # (3) TTS 실패율 5% 초과 백엔드 경고.
    for b, s in by_backend.items():
        if s["count"] >= 5 and s["tts_failure_rate"] > 0.05:
            insights.append({
                "level": "err" if s["tts_failure_rate"] > 0.20 else "warn",
                "message": (
                    f"{b} 백엔드의 TTS 차단률이 {s['tts_failure_rate']*100:.0f}% — "
                    f"응답 길이/포맷 위반 가능. system_prompt 강화 또는 verifier 임계 검토."
                ),
            })

    # (4) TTS 차단 사유 Top 1.
    # 'ok' / 'success' 등 성공 sentinel 은 제외 — 차단 사유가 아님.
    SUCCESS_SENTINELS = {"ok", "success", "passed", "none", ""}
    blocked_reasons = {
        k: v for k, v in tts_reasons.items()
        if (k or "").strip().lower() not in SUCCESS_SENTINELS
    }
    if blocked_reasons:
        top_reason, top_n = max(blocked_reasons.items(), key=lambda kv: kv[1])
        ratio = top_n / total
        if ratio >= 0.02:
            insights.append({
                "level": "warn" if ratio >= 0.05 else "info",
                "message": (
                    f"TTS 차단 사유 1위: '{top_reason}' ({top_n}건, 전체의 {ratio*100:.1f}%)."
                ),
            })

    # (5) 데이터가 충분치 않으면 안내.
    if total < 20:
        insights.append({
            "level": "info",
            "message": f"누적 턴 {total}회 — 통계 신뢰도가 낮음. 20회 이상 권장.",
        })
    return insights


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
            # 사이클 #7 — 빈/비빈 키셋 동등성 유지.
            "per_backend": {},
            "insights": [],
            # 사이클 #9 — 3-Pillar 점수 (음성/비전/액션). 빈 데이터 기본 모양.
            "pillars": {k: _empty_pillar() for k in PILLAR_KEYS},
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
    per_backend = _per_backend_stats(rows)
    pillars = _pillar_metrics(rows)  # 사이클 #9
    insights = _build_insights(rows, per_backend, dict(tts_reasons), total)
    # 사이클 #9 — 각 pillar 의 notes 를 insights 로 승격 (warn 레벨 통일).
    for pname in PILLAR_KEYS:
        for note in pillars[pname].get("notes", []) or []:
            insights.append({
                "level": "warn",
                "message": f"[{pname}] {note}",
            })

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
        # 사이클 #7 — SARVIS 자기 개선용 백엔드 비교 + actionable 인사이트.
        "per_backend": per_backend,
        "insights": insights,
        # 사이클 #9 — 3-Pillar 점수.
        "pillars": pillars,
        "last_ts": rows[-1].get("ts"),
    }
