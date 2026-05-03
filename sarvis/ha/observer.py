"""Observer 에이전트 (기획서 §4.1).

책임:
- Sarvis 트레이스 (commands + command_feedback) 를 일정 주기로 스캔
- 휴리스틱 anomaly 검출 (드리프트/스파이크/침묵/비용)
- (옵션) LLM 패턴 인식 — 무작위 30건 → "이상한 패턴이 있는가" 자유 텍스트 → JSON
- Issue Card 를 ha_issues + ha_messages 로 영속

L0 자율 등급 (Observe-only) — 어떤 변경도 적용하지 않음.
"""
from __future__ import annotations

import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .base import HAAgent
from .safety import ensure_running, mask_pii


@dataclass
class IssueCard:
    """기획서 §4.1.3 — Issue Card."""
    issue_id: str
    category: str       # drift / spike / anomaly / cost / silence / underutilization
    severity: str       # critical / high / medium / low / info
    evidence_traces: List[int]
    statistical_signal: Optional[str]
    narrative_summary: str
    confidence: float

    def to_payload(self) -> Dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "category": self.category,
            "severity": self.severity,
            "evidence_traces": self.evidence_traces,
            "statistical_signal": self.statistical_signal,
            "narrative_summary": self.narrative_summary,
            "confidence": self.confidence,
            "requires_human": True,  # Stage S1 — 항상 사람 검토
        }


class Observer(HAAgent):
    name = "Observer"
    read_scope = {"commands", "command_feedback", "metrics"}
    # Stage S1: Issue Card + 메시지 기록만. 안전 프롬프트/코드 수정 불가.
    write_scope = {"ha_issues", "ha_messages"}

    # 휴리스틱 임계 — 보조 기획서 §6.2 + §16
    _DRIFT_DELTA = 0.5      # 만족도 7d - 28d 차이 (rating 단위, -1..+1)
    _ERROR_RATE_HIGH = 0.10 # 에러율 10% 초과
    _LATENCY_HIGH_MS = 8000 # 평균 지연 8초 초과
    _MIN_SAMPLES = 5        # 분석 최소 트레이스 수

    def __init__(self, memory=None, brain=None) -> None:
        super().__init__(memory=memory)
        # brain 은 LLM 호출용 (선택). 미지정 시 휴리스틱만으로 동작.
        self.brain = brain

    # ── 메인 진입점 ────────────────────────────────────────────────
    def scan(
        self, window_sec: float = 24 * 3600.0, use_llm: bool = False,
    ) -> List[IssueCard]:
        """주기 스캔. Kill Switch 활성 시 즉시 예외."""
        ensure_running()
        if self.memory is None:
            return []
        data = self.memory.ha_observer_input(
            window_sec=window_sec, exclude_optout=True,
        )
        traces: List[Dict[str, Any]] = data.get("traces", [])
        baseline: Dict[str, Any] = data.get("baseline", {})

        cards: List[IssueCard] = []
        cards.extend(self._heuristic_drift(baseline))
        cards.extend(self._heuristic_error_spike(traces))
        cards.extend(self._heuristic_latency(traces))
        cards.extend(self._heuristic_silence(traces, window_sec))
        cards.extend(self._heuristic_negative_cluster(traces))

        if use_llm and self.brain is not None and traces:
            try:
                llm_card = self._llm_pattern_recognition(traces)
                if llm_card is not None:
                    cards.append(llm_card)
            except Exception as ex:
                print(f"[Observer] LLM 패턴 인식 실패 (휴리스틱만 사용): {ex!r}")

        # 영속 + 메시지 emit
        for card in cards:
            try:
                self.memory.ha_issue_insert(
                    issue_id=card.issue_id,
                    category=card.category,
                    severity=card.severity,
                    evidence=card.evidence_traces,
                    signal=card.statistical_signal,
                    narrative=card.narrative_summary,
                    confidence=card.confidence,
                )
                self.emit("Reporter", card.to_payload())
            except Exception as ex:
                print(f"[Observer] 카드 영속 실패 {card.issue_id}: {ex!r}")
        return cards

    # ── 휴리스틱 ──────────────────────────────────────────────────
    def _new_id(self, tag: str) -> str:
        return f"ISS-{time.strftime('%Y%m%d-%H%M%S')}-{tag}-{uuid.uuid4().hex[:6]}"

    def _heuristic_drift(self, baseline: Dict[str, Any]) -> List[IssueCard]:
        s7 = baseline.get("sat_7d")
        s28 = baseline.get("sat_28d")
        n7 = baseline.get("n_7d", 0)
        n28 = baseline.get("n_28d", 0)
        if s7 is None or s28 is None or n7 < self._MIN_SAMPLES \
                or n28 < self._MIN_SAMPLES:
            return []
        delta = float(s7) - float(s28)
        if delta <= -self._DRIFT_DELTA:
            return [IssueCard(
                issue_id=self._new_id("DRIFT"),
                category="drift",
                severity="high" if delta <= -0.8 else "medium",
                evidence_traces=[],
                statistical_signal=(
                    f"만족도 7일 평균 {s7:.2f} vs 28일 평균 {s28:.2f} "
                    f"(Δ={delta:+.2f})"
                ),
                narrative_summary=(
                    "최근 7일 사용자 만족도가 28일 베이스라인 대비 유의미하게 하락. "
                    "Diagnostician 진단 권장."
                ),
                confidence=min(0.9, 0.5 + abs(delta) / 2),
            )]
        return []

    def _heuristic_error_spike(self, traces: List[Dict[str, Any]]) -> List[IssueCard]:
        if len(traces) < self._MIN_SAMPLES:
            return []
        errs = [t for t in traces if t.get("status") == "error"]
        rate = len(errs) / len(traces)
        if rate >= self._ERROR_RATE_HIGH:
            evid = [int(t["id"]) for t in errs[:20]]
            return [IssueCard(
                issue_id=self._new_id("ERRSPIKE"),
                category="spike",
                severity="critical" if rate >= 0.25 else "high",
                evidence_traces=evid,
                statistical_signal=(
                    f"오류율 {rate*100:.1f}% ({len(errs)}/{len(traces)})"
                ),
                narrative_summary=(
                    f"윈도우 내 명령의 {rate*100:.1f}% 가 오류 상태. "
                    "공통 패턴 진단 필요."
                ),
                confidence=0.85,
            )]
        return []

    def _heuristic_latency(self, traces: List[Dict[str, Any]]) -> List[IssueCard]:
        lats: List[float] = []
        for t in traces:
            ca = t.get("created_at"); co = t.get("completed_at")
            if ca and co:
                lats.append((float(co) - float(ca)) * 1000.0)
        if len(lats) < self._MIN_SAMPLES:
            return []
        avg = sum(lats) / len(lats)
        if avg >= self._LATENCY_HIGH_MS:
            return [IssueCard(
                issue_id=self._new_id("LATENCY"),
                category="cost",
                severity="medium",
                evidence_traces=[],
                statistical_signal=f"평균 응답 시간 {avg:.0f}ms (n={len(lats)})",
                narrative_summary=(
                    "평균 응답 지연이 임계(8초)를 초과. 모델 라우팅·도구 호출 비용 검토 권장."
                ),
                confidence=0.7,
            )]
        return []

    def _heuristic_silence(
        self, traces: List[Dict[str, Any]], window_sec: float,
    ) -> List[IssueCard]:
        # 기획서 §5.5 — 채택률 저조 신호.
        # window 가 충분히 길고(≥ 24h), 트레이스가 5건 미만이면 침묵 카드.
        if window_sec < 24 * 3600.0:
            return []
        if len(traces) >= self._MIN_SAMPLES:
            return []
        return [IssueCard(
            issue_id=self._new_id("SILENCE"),
            category="underutilization",
            severity="info",
            evidence_traces=[],
            statistical_signal=f"window {window_sec/3600:.0f}h 동안 명령 {len(traces)}건",
            narrative_summary=(
                "최근 사용량이 매우 적음. 기능 발견성/사용자 컨텍스트 점검 권장."
            ),
            confidence=0.6,
        )]

    def _heuristic_negative_cluster(
        self, traces: List[Dict[str, Any]],
    ) -> List[IssueCard]:
        neg = [t for t in traces if (t.get("rating") or 0) < 0]
        rated = [t for t in traces if t.get("rating") is not None]
        if len(rated) < self._MIN_SAMPLES:
            return []
        rate = len(neg) / len(rated)
        if rate >= 0.4:
            evid = [int(t["id"]) for t in neg[:20]]
            return [IssueCard(
                issue_id=self._new_id("NEGCLUSTER"),
                category="anomaly",
                severity="high" if rate >= 0.6 else "medium",
                evidence_traces=evid,
                statistical_signal=(
                    f"부정 피드백 비율 {rate*100:.0f}% ({len(neg)}/{len(rated)})"
                ),
                narrative_summary=(
                    "👎 비율이 임계를 초과. 응답 톤·정확도 진단 필요."
                ),
                confidence=0.8,
            )]
        return []

    # ── (옵션) LLM 패턴 인식 ───────────────────────────────────────
    def _llm_pattern_recognition(
        self, traces: List[Dict[str, Any]],
    ) -> Optional[IssueCard]:
        """기획서 §4.1.4 — 무작위 샘플을 LLM 이 자유 텍스트로 분석.

        Stage S1 에서는 호출 결과를 강제로 휴리스틱 형식 IssueCard 로 매핑.
        실패해도 휴리스틱은 그대로 발화하므로 안전.
        """
        if self.brain is None or not traces:
            return None
        sample = random.sample(traces, min(30, len(traces)))
        # PII 마스킹 후 LLM 송신
        lines = []
        for t in sample:
            txt = mask_pii(str(t.get("command_text") or ""))[:200]
            resp = mask_pii(str(t.get("response_text") or ""))[:200]
            lines.append(f"- [{t.get('kind')}|{t.get('status')}] {txt} → {resp}")
        prompt = (
            "다음은 SARVIS AI 비서의 최근 명령 트레이스 샘플이다 (PII 마스킹됨). "
            "이상한 패턴(품질 저하/일관성 결여/안전 위험/스타일 일탈)이 있는가? "
            "있다면 한 줄 요약 + severity(low/medium/high) + confidence(0~1) 만 "
            "JSON 으로: {\"summary\": str, \"severity\": str, \"confidence\": float}. "
            "없다면 {\"summary\": null}.\n\n트레이스:\n" + "\n".join(lines)
        )
        try:
            text = self.brain.think_once_text(prompt)  # 동기 단발 호출
        except Exception as ex:
            print(f"[Observer.LLM] think_once_text 실패: {ex!r}")
            return None
        import json as _json
        try:
            # 응답에서 첫 JSON 객체 추출
            start = text.find("{"); end = text.rfind("}")
            if start < 0 or end <= start:
                return None
            obj = _json.loads(text[start:end+1])
            summary = obj.get("summary")
            if not summary:
                return None
            sev = obj.get("severity", "low")
            if sev not in ("low", "medium", "high", "critical"):
                sev = "low"
            conf = float(obj.get("confidence", 0.5) or 0.5)
            conf = max(0.0, min(1.0, conf))
            return IssueCard(
                issue_id=self._new_id("LLM"),
                category="anomaly",
                severity=sev,
                evidence_traces=[int(t["id"]) for t in sample[:10]],
                statistical_signal=f"LLM 패턴 인식 (n={len(sample)})",
                narrative_summary=str(summary)[:500],
                confidence=conf,
            )
        except (ValueError, _json.JSONDecodeError) as ex:
            print(f"[Observer.LLM] JSON 파싱 실패: {ex!r}")
            return None
