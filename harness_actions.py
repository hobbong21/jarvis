"""사이클 #9 — SARVIS Harness 자가 개선 액션 시스템.

Telemetry 가 발견한 문제(예: 빈 전사율 높음, 비전 호출 0회, total_ms 느림)를
자동으로 보정 가능한 안전한 설정 변경을 캡슐화한다.

각 액션은:
  - 안전 범위(bounds) 내에서만 적용 가능
  - 1단계 revert 가능 (이전 값을 보관)
  - 적용 시 data/harness_actions.jsonl 에 감사 로그 기록
  - cfg 가 LLM 백엔드/네트워크와 무관한 인-프로세스 dataclass 라
    프로세스 재시작 없이 즉시 효과 (영구 저장은 옵션 — 향후 confirm)

상호작용 흐름:
  1. summarize() → pillars + insights 가 권장값을 산출
  2. recommend_actions(summary) 가 안전 권장을 [{name, suggested, reason}, …] 반환
  3. WS 핸들러가 list_actions() / apply_action(name, value) / revert_action(name) 호출
  4. _audit() 가 감사 로그를 1줄 추가 (timestamp/source/from→to)
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import cfg

AUDIT_PATH = Path(__file__).parent / "data" / "harness_actions.jsonl"
AUDIT_MAX = 2000  # 회전 임계

_audit_lock = threading.Lock()
# 사이클 #9 P1#3 — apply/revert 동시성 보호. _previous(1단계 revert 포인터) 가
# 다중 admin 동시 호출에서 덮어써지지 않도록 모든 mutate 경로를 직렬화.
_state_lock = threading.RLock()


# ============================================================
# Audit log
# ============================================================

def _audit(entry: Dict[str, Any]) -> None:
    """감사 로그 1줄 추가 (실패는 격리; 핵심 흐름 차단 금지)."""
    with _audit_lock:
        try:
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            entry.setdefault("ts", time.time())
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _rotate_audit_if_needed()
        except OSError as e:
            print(f"[harness_actions] audit write failed: {e!r}")


def _rotate_audit_if_needed() -> None:
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8", errors="ignore") as f:
            n = sum(1 for _ in f)
    except OSError:
        return
    if n <= AUDIT_MAX:
        return
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        with open(AUDIT_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines[len(lines) // 2:])
    except OSError as e:
        print(f"[harness_actions] audit rotate failed: {e!r}")


def recent_audit(n: int = 50) -> List[Dict[str, Any]]:
    """최근 감사 로그 N개 (대시보드 표시용)."""
    if not AUDIT_PATH.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out[-n:] if n > 0 else out


# ============================================================
# Action catalog
# ============================================================

@dataclass
class Action:
    """안전 적용 가능한 자가 개선 액션 1개.

    name        : 안정적 식별자 (외부 API key)
    label       : 한국어 표시 이름 (UI)
    category    : 'voice' | 'vision' | 'action' (3-Pillar)
    bounds      : (min, max) 안전 범위. 외부 입력은 항상 클램프.
    parser      : str → typed value (UI 가 텍스트로 보낼 수 있어 지원)
    formatter   : typed value → str (감사 로그/UI 표시용)
    getter      : 현재 값을 반환 (cfg 에서 읽음)
    setter      : 새 값을 적용 (cfg 에 mutate; 다른 부수효과 가능)
    description : 사용자에게 보여줄 짧은 설명
    """
    name: str
    label: str
    category: str
    bounds: Tuple[float, float]
    parser: Callable[[Any], Any]
    formatter: Callable[[Any], str]
    getter: Callable[[], Any]
    setter: Callable[[Any], None]
    description: str = ""
    # 1단계 revert 를 위해 직전 값 보관 (apply 시 갱신).
    _previous: Any = field(default=None, repr=False)
    _has_previous: bool = field(default=False, repr=False)

    def current_value(self) -> Any:
        return self.getter()

    def clamp(self, value: Any) -> Any:
        v = self.parser(value)
        try:
            lo, hi = self.bounds
            if isinstance(v, (int, float)):
                v = max(lo, min(hi, float(v)))
        except Exception:
            pass
        return v

    def apply(self, value: Any, source: str = "manual") -> Dict[str, Any]:
        """값 적용 + 감사 로그 기록. revert 를 위해 직전 값 보관.

        _state_lock 으로 직렬화 — 동시 apply 가 _previous 를 덮어쓰지 않도록
        getter/setter/_previous 갱신을 원자적으로 묶는다.
        """
        with _state_lock:
            prev = self.current_value()
            new_val = self.clamp(value)
            self.setter(new_val)
            self._previous = prev
            self._has_previous = True
            entry = {
                "name": self.name,
                "category": self.category,
                "from": self.formatter(prev),
                "to": self.formatter(new_val),
                "source": source,
                "op": "apply",
            }
        _audit(entry)
        return entry

    def revert(self, source: str = "manual") -> Optional[Dict[str, Any]]:
        """직전 값으로 복원. 적용 이력이 없으면 None 반환.

        _state_lock 으로 _has_previous/_previous 검사·갱신을 원자화 —
        revert 가 두 번 동시에 들어와도 한 번만 성공한다.
        """
        with _state_lock:
            if not self._has_previous:
                return None
            prev = self.current_value()
            self.setter(self._previous)
            entry = {
                "name": self.name,
                "category": self.category,
                "from": self.formatter(prev),
                "to": self.formatter(self._previous),
                "source": source,
                "op": "revert",
            }
            self._previous = None
            self._has_previous = False
        _audit(entry)
        return entry

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "category": self.category,
            "bounds": list(self.bounds),
            "current": self.formatter(self.current_value()),
            "current_value": self.current_value(),
            "can_revert": self._has_previous,
            "description": self.description,
        }


# ============================================================
# tts_rate parser/formatter — Edge-TTS "+5%" / "-10%" 형식
# ============================================================

_TTS_RATE_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*%?\s*$")


def _parse_tts_rate(value: Any) -> float:
    """문자열/숫자 → percent (float). '+5%' → 5.0, '-10' → -10.0."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _TTS_RATE_RE.match(value)
        if m:
            return float(m.group(1))
    raise ValueError(f"tts_rate parse 실패: {value!r}")


def _format_tts_rate(value: float) -> str:
    """percent → '+5%' 형식. Edge-TTS 가 요구하는 부호 포함."""
    v = float(value)
    sign = "+" if v >= 0 else ""
    return f"{sign}{int(v)}%"


def _set_tts_rate(value: float) -> None:
    cfg.tts_rate = _format_tts_rate(value)


def _get_tts_rate() -> float:
    return _parse_tts_rate(cfg.tts_rate)


# ============================================================
# 카탈로그 빌드 — 안전 화이트리스트
# ============================================================

def _build_catalog() -> Dict[str, Action]:
    """안전하게 인-프로세스 mutate 가능한 cfg 항목만 노출."""
    return {
        "silence_threshold": Action(
            name="silence_threshold",
            label="음성 무음 임계",
            category="voice",
            bounds=(0.005, 0.030),
            parser=float,
            formatter=lambda v: f"{float(v):.4f}",
            getter=lambda: cfg.silence_threshold,
            setter=lambda v: setattr(cfg, "silence_threshold", float(v)),
            description="값이 작을수록 더 작은 소리도 음성으로 인식 — 빈 전사율이 높을 때 낮추세요.",
        ),
        "silence_duration": Action(
            name="silence_duration",
            label="음성 종료 대기",
            category="voice",
            bounds=(0.8, 2.5),
            parser=float,
            formatter=lambda v: f"{float(v):.2f}s",
            getter=lambda: cfg.silence_duration,
            setter=lambda v: setattr(cfg, "silence_duration", float(v)),
            description="발화 끝에서 멈춤이 짧으면 잘림 — 길어지면 응답이 늦어집니다.",
        ),
        "max_recording": Action(
            name="max_recording",
            label="최대 녹음 길이",
            category="voice",
            bounds=(5.0, 30.0),
            parser=float,
            formatter=lambda v: f"{float(v):.0f}s",
            getter=lambda: cfg.max_recording,
            setter=lambda v: setattr(cfg, "max_recording", float(v)),
            description="긴 발화를 자주 하면 늘리고, 응답 지연이 심하면 줄이세요.",
        ),
        "tts_rate": Action(
            name="tts_rate",
            label="TTS 속도(%)",
            category="voice",
            bounds=(-30.0, 30.0),
            parser=_parse_tts_rate,
            formatter=_format_tts_rate,
            getter=_get_tts_rate,
            setter=_set_tts_rate,
            description="TTS 가 차단되거나 너무 길게 들리면 +값으로 빠르게.",
        ),
    }


_CATALOG: Dict[str, Action] = _build_catalog()


def reset_catalog_for_tests() -> None:
    """테스트 격리: cfg 원복 + 카탈로그 재생성 (revert 이력 삭제)."""
    global _CATALOG
    _CATALOG = _build_catalog()


def list_actions() -> List[Dict[str, Any]]:
    """모든 액션 + 현재 값 + revert 가능 여부."""
    return [a.to_dict() for a in _CATALOG.values()]


def get_action(name: str) -> Optional[Action]:
    return _CATALOG.get(name)


def apply_action(name: str, value: Any, source: str = "manual") -> Dict[str, Any]:
    a = _CATALOG.get(name)
    if a is None:
        raise KeyError(f"unknown action: {name}")
    return a.apply(value, source=source)


def revert_action(name: str, source: str = "manual") -> Optional[Dict[str, Any]]:
    a = _CATALOG.get(name)
    if a is None:
        raise KeyError(f"unknown action: {name}")
    return a.revert(source=source)


# ============================================================
# Recommend — summary → 보수적 권장값
# ============================================================

def recommend_actions(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """telemetry.summarize() 결과로 안전한 권장값을 산출.

    각 권장: {name, suggested, suggested_str, reason, category, current}.
    임계값은 보수적 — 표본 부족(score=None)이면 권장하지 않는다.
    """
    recs: List[Dict[str, Any]] = []
    if not isinstance(summary, dict):
        return recs
    pillars = summary.get("pillars") or {}

    voice = pillars.get("voice") or {}
    v_metrics = voice.get("metrics") or {}
    if voice.get("score") is not None:
        empty_rate = float(v_metrics.get("empty_transcription_rate", 0.0) or 0.0)
        audio_turns = int(v_metrics.get("audio_turns", 0) or 0)
        # 빈 전사율이 20% 초과 + 표본 5+ → silence_threshold 한 단계 낮춤.
        if empty_rate > 0.20 and audio_turns >= 5:
            current = float(cfg.silence_threshold)
            suggested = max(0.005, round(current * 0.75, 4))
            if suggested != current:
                recs.append({
                    "name": "silence_threshold",
                    "category": "voice",
                    "current": f"{current:.4f}",
                    "suggested": suggested,
                    "suggested_str": f"{suggested:.4f}",
                    "reason": (
                        f"빈 전사율 {empty_rate*100:.0f}% — 임계를 25% 낮춰 "
                        f"작은 소리도 캡처."
                    ),
                })
        # TTS 차단율이 10% 초과 → tts_rate 를 +5% 더 빠르게 (응답 길이가 원인일 때 도움).
        tts_fail_rate = float(v_metrics.get("tts_failure_rate", 0.0) or 0.0)
        if tts_fail_rate > 0.10:
            current_rate = _parse_tts_rate(cfg.tts_rate)
            suggested = min(30.0, current_rate + 5.0)
            if suggested != current_rate:
                recs.append({
                    "name": "tts_rate",
                    "category": "voice",
                    "current": _format_tts_rate(current_rate),
                    "suggested": suggested,
                    "suggested_str": _format_tts_rate(suggested),
                    "reason": (
                        f"TTS 차단율 {tts_fail_rate*100:.0f}% — 속도를 5% 올려 "
                        f"전체 발화 시간 단축."
                    ),
                })

    action = pillars.get("action") or {}
    a_metrics = action.get("metrics") or {}
    if action.get("score") is not None:
        p50_total = float(a_metrics.get("p50_total_ms", 0.0) or 0.0)
        # 응답이 일관되게 5s 초과면 max_recording 줄여서 사용자 발화 끊는 시점 단축.
        if p50_total > 5000.0:
            current = float(cfg.max_recording)
            suggested = max(5.0, round(current - 2.0, 1))
            if suggested != current:
                recs.append({
                    "name": "max_recording",
                    "category": "action",
                    "current": f"{current:.0f}s",
                    "suggested": suggested,
                    "suggested_str": f"{suggested:.0f}s",
                    "reason": (
                        f"응답 p50 {p50_total/1000:.1f}s — 긴 녹음 한도를 2초 줄여 "
                        f"즉각성 개선."
                    ),
                })
    return recs
