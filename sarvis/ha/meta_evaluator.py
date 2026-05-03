"""MetaEvaluator 에이전트 — 자체 진화 (사이클 #29).

기획서 §4.7 / §16 의 Meta-Evaluator. 하네스 자기 자신의 동작을 측정하고,
필요 시 *하네스에 대한* Issue Card 를 발화한다 (Observer 와 같은 패턴).
이 카드는 동일한 Diagnostician → Strategist → Improver → Validator 파이프
라인을 그대로 통과해 사람의 승인 큐에 들어간다 — 자율 적용 절대 없음.

측정하는 것:
  1) 퍼널 — issues / diagnoses / strategies / proposals / approved
  2) 비율 — approval_rate, abandonment_rate, rejection_rate
  3) 신뢰도 보정 — diagnoses 의 평균 confidence
  4) 결과 — outcomes 중 resolved 비율 (closed-loop 효과성)

자체 이슈를 발화하는 조건 (보수적, 폐기 가능):
  - approval_rate < 0.20 + 표본 ≥ 5 → "harness_meta:noisy" (신호가 채택되지 않음)
  - abandonment_rate > 0.50 + 표본 ≥ 5 → "harness_meta:abandoned"
  - 평균 진단 confidence < 0.4 + 표본 ≥ 5 → "harness_meta:low_confidence"
  - 결과 sample 5+ 중 resolved < 0.3 → "harness_meta:ineffective"

L0/L1 안전 — 어떤 자기-제안도 자동 적용되지 않는다. Strategist 가 만드는
PatchSpec 도 전부 ha_proposals(pending) 로만 진입.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .base import HAAgent
from .observer import IssueCard
from .safety import ensure_running


@dataclass
class HarnessHealthReport:
    """MetaEvaluator 의 1회 평가 결과 — UI/대시보드 노출용."""
    period_sec: float
    funnel: Dict[str, int]          # issue_count, diag_count, ...
    rates: Dict[str, float]         # approval_rate, abandonment_rate, ...
    confidence: Dict[str, float]    # avg_diagnosis_conf, ...
    outcomes: Dict[str, int]        # resolved/persistent/regressed/pending
    self_issues: List[str]          # 발화된 issue_id 리스트
    health_score: float             # 종합 0~1
    # 진단/추천 텍스트 (사람이 빠르게 읽도록)
    summary_lines: List[str] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "period_sec": self.period_sec,
            "funnel": dict(self.funnel),
            "rates": dict(self.rates),
            "confidence": dict(self.confidence),
            "outcomes": dict(self.outcomes),
            "self_issues": list(self.self_issues),
            "health_score": self.health_score,
            "summary_lines": list(self.summary_lines),
            "requires_human": True,
        }


class MetaEvaluator(HAAgent):
    """하네스의 자기 평가 에이전트.

    write_scope 는 Observer 와 동일 (`ha_issues` + `ha_messages`) — 자기 이슈를
    파이프라인에 동일하게 흘려보내는 것이 핵심 설계. 추가로 `ha_self_metrics`
    에 퍼널 스냅샷을 적재할 수 있어야 하므로 write_scope 에 포함.
    """

    name = "MetaEvaluator"
    read_scope = {
        "ha_messages", "ha_issues", "ha_diagnoses", "ha_strategies",
        "ha_proposals", "ha_validations", "ha_outcomes", "ha_self_metrics",
    }
    write_scope = {"ha_messages", "ha_issues", "ha_self_metrics"}

    # 자기 이슈 발화 임계 — 표본 부족 시 발화하지 않는다 (false positive 회피).
    _MIN_SAMPLE = 5
    _APPROVAL_LOW = 0.20
    _ABANDON_HIGH = 0.50
    _DIAG_CONF_LOW = 0.40
    _RESOLVED_LOW = 0.30
    # 같은 시그널의 자기-이슈를 24h 내 재발화 금지 (idempotency + feedback loop 차단).
    # MetaEvaluator 가 자기 이슈를 만들어 pending 큐를 늘리고, 그 결과 approval_rate
    # 가 더 낮아져 다음 사이클에 또 자기 이슈를 만드는 양성 피드백 루프 회피.
    _SELF_ISSUE_COOLDOWN_SEC = 24 * 3600.0
    # 시그널 키 — _maybe_emit_self_issues 가 발화를 결정할 때 사용하는 4 종류.
    _SELF_ISSUE_KINDS = (
        "low_approval", "high_abandon", "low_diag_conf", "low_resolved",
    )

    # ── 진입점 ──────────────────────────────────────────────────────
    def evaluate(self, window_sec: float = 7 * 24 * 3600.0) -> HarnessHealthReport:
        """주어진 윈도우의 하네스 동작을 평가하고 자기 이슈를 발화."""
        ensure_running()
        if self.memory is None:
            return self._empty_report(window_sec)

        cutoff = time.time() - max(60.0, float(window_sec))
        funnel = self._compute_funnel(cutoff)
        rates = self._compute_rates(funnel)
        confidence = self._compute_confidence(cutoff)
        outcomes = self._compute_outcomes(cutoff)

        self_issues = self._maybe_emit_self_issues(
            funnel=funnel, rates=rates,
            confidence=confidence, outcomes=outcomes,
        )

        health_score = self._compute_health_score(rates, outcomes, confidence)
        summary_lines = self._render_summary(
            funnel, rates, confidence, outcomes, health_score,
        )

        # 핵심 지표 영속화 — 시계열 비교 가능.
        self._record_metrics(funnel, rates, confidence, outcomes, health_score)

        report = HarnessHealthReport(
            period_sec=window_sec,
            funnel=funnel, rates=rates,
            confidence=confidence, outcomes=outcomes,
            self_issues=self_issues, health_score=health_score,
            summary_lines=summary_lines,
        )
        # 보고는 Reporter 로 전달 — UI 가 동일 채널에서 수신.
        try:
            self.emit("Reporter", report.to_payload())
        except Exception as ex:
            print(f"[MetaEvaluator] Reporter emit 실패: {ex!r}")
        return report

    def _empty_report(self, window_sec: float) -> HarnessHealthReport:
        return HarnessHealthReport(
            period_sec=window_sec,
            funnel={"issue_count": 0, "diag_count": 0, "strategy_count": 0,
                    "proposal_count": 0, "approved_count": 0,
                    "rejected_count": 0, "pending_count": 0},
            rates={"approval_rate": 0.0, "rejection_rate": 0.0,
                   "abandonment_rate": 0.0},
            confidence={"avg_diagnosis_conf": 0.0, "avg_issue_conf": 0.0},
            outcomes={"resolved": 0, "persistent": 0, "regressed": 0, "pending": 0},
            self_issues=[],
            health_score=0.5,
            summary_lines=["메모리 미연결 — 평가 불가."],
        )

    # ── 측정 ────────────────────────────────────────────────────────
    def _compute_funnel(self, cutoff: float) -> Dict[str, int]:
        """카테고리별 카운트가 아닌 윈도우 내 총 카운트를 집계.

        Feedback loop 회피: harness_meta 카테고리 이슈에서 파생된 제안은
        denominator 에 포함하지 않는다. 자기 이슈가 만든 pending 제안이
        approval_rate 분모를 부풀려 또 다른 자기 이슈를 트리거하는 positive
        feedback 을 차단.
        """
        m = self.memory
        issues = [i for i in m.ha_issues_recent(limit=200)
                  if (i.get("created_at") or 0) >= cutoff]
        diagnoses = [d for d in m.ha_diagnoses_recent(limit=200)
                     if (d.get("created_at") or 0) >= cutoff]
        strategies = [s for s in m.ha_strategies_recent(limit=500)
                      if (s.get("created_at") or 0) >= cutoff]
        proposals = [p for p in m.ha_proposals_list(limit=500)
                     if (p.get("created_at") or 0) >= cutoff]

        # harness_meta 자기 이슈에서 파생된 제안 ID 집합 — funnel 에서 제외.
        meta_issue_ids = {
            i["issue_id"] for i in issues if i.get("category") == "harness_meta"
        }
        meta_diag_ids = {
            d["diagnosis_id"] for d in diagnoses
            if d.get("issue_id") in meta_issue_ids
        }
        meta_strat_ids = {
            s["strategy_id"] for s in strategies
            if s.get("diagnosis_id") in meta_diag_ids
        }
        user_proposals = [
            p for p in proposals if p.get("strategy_id") not in meta_strat_ids
        ]
        # User-facing 이슈만 카운트 (harness_meta 제외)
        user_issues = [i for i in issues
                       if i.get("category") != "harness_meta"]

        approved = sum(1 for p in user_proposals if p.get("status") == "approved")
        rejected = sum(1 for p in user_proposals if p.get("status") == "rejected")
        pending = sum(1 for p in user_proposals if p.get("status") == "pending")
        return {
            "issue_count": len(user_issues),
            "diag_count": len(diagnoses) - len(meta_diag_ids),
            "strategy_count": len(strategies) - len(meta_strat_ids),
            "proposal_count": len(user_proposals),
            "approved_count": approved,
            "rejected_count": rejected,
            "pending_count": pending,
            # 자기-이슈 카운트 (참고용, denominator 분리)
            "self_issue_count": len(meta_issue_ids),
        }

    @staticmethod
    def _safe_div(num: float, den: float) -> float:
        return float(num) / float(den) if den else 0.0

    def _compute_rates(self, funnel: Dict[str, int]) -> Dict[str, float]:
        prop = funnel["proposal_count"]
        # abandonment: issue → 어떤 proposal 도 만들지 못함.
        # 근사치: issue_count - 0 vs proposal_count. proposal_count <= issue_count
        # 보장이 안 되므로 max() 로 안전.
        abandoned = max(0, funnel["issue_count"] - prop)
        return {
            "approval_rate": round(
                self._safe_div(funnel["approved_count"], prop), 3,
            ),
            "rejection_rate": round(
                self._safe_div(funnel["rejected_count"], prop), 3,
            ),
            "abandonment_rate": round(
                self._safe_div(abandoned, max(funnel["issue_count"], 1)), 3,
            ),
            "pending_rate": round(
                self._safe_div(funnel["pending_count"], prop), 3,
            ),
        }

    def _compute_confidence(self, cutoff: float) -> Dict[str, float]:
        m = self.memory
        diag_confs = [
            float(d.get("confidence") or 0.0)
            for d in m.ha_diagnoses_recent(limit=200)
            if (d.get("created_at") or 0) >= cutoff
        ]
        issue_confs = [
            float(i.get("confidence") or 0.0)
            for i in m.ha_issues_recent(limit=200)
            if (i.get("created_at") or 0) >= cutoff
        ]
        return {
            "avg_diagnosis_conf": round(
                sum(diag_confs) / len(diag_confs), 3,
            ) if diag_confs else 0.0,
            "avg_issue_conf": round(
                sum(issue_confs) / len(issue_confs), 3,
            ) if issue_confs else 0.0,
            "n_diagnoses": float(len(diag_confs)),
            "n_issues": float(len(issue_confs)),
        }

    def _compute_outcomes(self, cutoff: float) -> Dict[str, int]:
        if not hasattr(self.memory, "ha_outcomes_recent"):
            return {"resolved": 0, "persistent": 0, "regressed": 0, "pending": 0}
        rows = [
            o for o in self.memory.ha_outcomes_recent(limit=500)
            if (o.get("applied_at") or 0) >= cutoff
        ]
        out = {"resolved": 0, "persistent": 0, "regressed": 0, "pending": 0}
        for r in rows:
            k = r.get("outcome", "pending")
            if k in out:
                out[k] += 1
        return out

    def _recent_self_issue_kinds(self) -> set:
        """24h 내 발화된 자기-이슈의 시그널 종류를 메타데이터에서 추출.
        cooldown 비교에 사용 — 반환 set 에 포함된 kind 는 재발화 안 함."""
        if self.memory is None:
            return set()
        now = time.time()
        seen: set = set()
        try:
            for c in self.memory.ha_issues_recent(limit=50):
                if c.get("category") != "harness_meta":
                    continue
                if (now - float(c.get("created_at") or 0)) > self._SELF_ISSUE_COOLDOWN_SEC:
                    continue
                # signal 텍스트로 kind 식별 (간단한 substring 매칭 — 안정적인
                # 비교를 위해 발화 시 signal 첫 단어를 키워드로 통일).
                sig = (c.get("signal") or "")
                if "채택률" in sig:
                    seen.add("low_approval")
                elif "이슈→제안" in sig or "전환 실패율" in sig:
                    seen.add("high_abandon")
                elif "진단 confidence" in sig:
                    seen.add("low_diag_conf")
                elif "resolved 비율" in sig:
                    seen.add("low_resolved")
        except Exception as ex:
            print(f"[MetaEvaluator] cooldown 조회 실패: {ex!r}")
        return seen

    # ── 자기 이슈 발화 ──────────────────────────────────────────────
    def _maybe_emit_self_issues(
        self,
        *,
        funnel: Dict[str, int],
        rates: Dict[str, float],
        confidence: Dict[str, float],
        outcomes: Dict[str, int],
    ) -> List[str]:
        """임계 위반 시 Observer 와 동일 형식의 IssueCard 를 영속 + emit.
        24h cooldown — 같은 종류의 자기 이슈가 활성이면 재발화 안 함."""
        emitted: List[str] = []
        recent = self._recent_self_issue_kinds()

        prop = funnel["proposal_count"]
        if (
            "low_approval" not in recent
            and prop >= self._MIN_SAMPLE
            and rates["approval_rate"] < self._APPROVAL_LOW
        ):
            emitted.append(self._emit_self_issue(
                category="harness_meta",
                severity="medium",
                signal=(
                    f"제안 채택률 {rates['approval_rate']*100:.0f}% "
                    f"({funnel['approved_count']}/{prop}, n>={self._MIN_SAMPLE})"
                ),
                narrative=(
                    "하네스가 만든 제안 다수가 채택되지 못함. Observer 임계 또는 "
                    "Strategist 룰 보정 필요 (노이즈 다발 가능성)."
                ),
                confidence=0.7,
            ))

        issues_n = funnel["issue_count"]
        if (
            "high_abandon" not in recent
            and issues_n >= self._MIN_SAMPLE
            and rates["abandonment_rate"] > self._ABANDON_HIGH
        ):
            emitted.append(self._emit_self_issue(
                category="harness_meta",
                severity="medium",
                signal=(
                    f"이슈→제안 전환 실패율 {rates['abandonment_rate']*100:.0f}% "
                    f"(n_issues={issues_n})"
                ),
                narrative=(
                    "다수의 Issue 가 Diagnostician/Strategist 단계에서 폐기됨. "
                    "휴리스틱 진단 룰 커버리지 또는 카테고리 매핑 점검 필요."
                ),
                confidence=0.65,
            ))

        n_diag = int(confidence.get("n_diagnoses") or 0)
        if (
            "low_diag_conf" not in recent
            and n_diag >= self._MIN_SAMPLE
            and confidence["avg_diagnosis_conf"] < self._DIAG_CONF_LOW
        ):
            emitted.append(self._emit_self_issue(
                category="harness_meta",
                severity="low",
                signal=(
                    f"평균 진단 confidence {confidence['avg_diagnosis_conf']:.2f} "
                    f"(n={n_diag})"
                ),
                narrative=(
                    "진단 신뢰도가 일관되게 낮음 — 5 Whys 룰 또는 가설 prior 갱신 권장. "
                    "장기적으로 Validator 위험 가산 발동 빈도가 늘어남."
                ),
                confidence=0.6,
            ))

        outc_total = sum(outcomes.values())
        outc_observed = outc_total - outcomes.get("pending", 0)
        if outc_observed >= self._MIN_SAMPLE:
            resolved_rate = self._safe_div(outcomes["resolved"], outc_observed)
            if (
                "low_resolved" not in recent
                and resolved_rate < self._RESOLVED_LOW
            ):
                emitted.append(self._emit_self_issue(
                    category="harness_meta",
                    severity="high",
                    signal=(
                        f"적용된 제안의 resolved 비율 {resolved_rate*100:.0f}% "
                        f"({outcomes['resolved']}/{outc_observed})"
                    ),
                    narrative=(
                        "사람이 승인 후 적용한 변경의 효과성이 임계(30%) 미달. "
                        "Strategy 카테고리 가중치 재조정 또는 회귀 시드 보강 필요."
                    ),
                    confidence=0.8,
                ))

        return emitted

    def _emit_self_issue(
        self,
        *,
        category: str,
        severity: str,
        signal: str,
        narrative: str,
        confidence: float,
    ) -> str:
        """Observer 와 동일 인터페이스 — IssueCard 를 영속 + 메시지 emit. issue_id 반환."""
        card = IssueCard(
            issue_id=(
                f"ISS-META-{time.strftime('%Y%m%d-%H%M%S')}-"
                f"{uuid.uuid4().hex[:6]}"
            ),
            category=category,
            severity=severity,
            evidence_traces=[],
            statistical_signal=signal,
            narrative_summary=narrative,
            confidence=confidence,
        )
        try:
            self.memory.ha_issue_insert(
                issue_id=card.issue_id, category=card.category,
                severity=card.severity, evidence=card.evidence_traces,
                signal=card.statistical_signal, narrative=card.narrative_summary,
                confidence=card.confidence,
            )
            self.emit("Reporter", card.to_payload())
        except Exception as ex:
            print(f"[MetaEvaluator] 자기 이슈 영속 실패: {ex!r}")
        return card.issue_id

    # ── 종합 점수 + 요약 ────────────────────────────────────────────
    def _compute_health_score(
        self,
        rates: Dict[str, float],
        outcomes: Dict[str, int],
        confidence: Dict[str, float],
    ) -> float:
        """0~1 단일 지표. UI 게이지에 사용. 동등 가중치 4축.

        - approval_rate (높을수록 좋음)
        - 1 - abandonment_rate (낮을수록 좋음)
        - resolved_rate (높을수록 좋음)
        - avg_diagnosis_conf (높을수록 좋음)

        표본 부족 시 0.5 (중립) 로 각 축이 기여.
        """
        approval = rates.get("approval_rate", 0.0)
        non_aband = 1.0 - rates.get("abandonment_rate", 0.0)
        outc_observed = sum(outcomes.values()) - outcomes.get("pending", 0)
        if outc_observed > 0:
            resolved = self._safe_div(outcomes.get("resolved", 0), outc_observed)
        else:
            resolved = 0.5
        diag_conf = confidence.get("avg_diagnosis_conf", 0.0)
        if confidence.get("n_diagnoses", 0) <= 0:
            diag_conf = 0.5

        score = (approval + non_aband + resolved + diag_conf) / 4.0
        return round(max(0.0, min(1.0, score)), 3)

    def _render_summary(
        self,
        funnel: Dict[str, int],
        rates: Dict[str, float],
        confidence: Dict[str, float],
        outcomes: Dict[str, int],
        health_score: float,
    ) -> List[str]:
        return [
            f"하네스 건강도: {health_score*100:.0f}/100",
            (
                f"퍼널: 이슈 {funnel['issue_count']} → 진단 {funnel['diag_count']} "
                f"→ 전략 {funnel['strategy_count']} → 제안 {funnel['proposal_count']} "
                f"(승인 {funnel['approved_count']} / 거부 {funnel['rejected_count']} "
                f"/ 대기 {funnel['pending_count']})"
            ),
            (
                f"비율: 채택 {rates['approval_rate']*100:.0f}% / "
                f"폐기 {rates['abandonment_rate']*100:.0f}% / "
                f"진단 confidence 평균 {confidence['avg_diagnosis_conf']:.2f}"
            ),
            (
                f"결과: 해결 {outcomes['resolved']} / 지속 {outcomes['persistent']} "
                f"/ 악화 {outcomes['regressed']} / 관찰중 {outcomes['pending']}"
            ),
        ]

    # ── 메트릭 영속 ────────────────────────────────────────────────
    def _record_metrics(
        self,
        funnel: Dict[str, int],
        rates: Dict[str, float],
        confidence: Dict[str, float],
        outcomes: Dict[str, int],
        health_score: float,
    ) -> None:
        m = self.memory
        if not hasattr(m, "ha_self_metric_record"):
            return
        snaps = {
            "issue_count": float(funnel["issue_count"]),
            "diag_count": float(funnel["diag_count"]),
            "proposal_count": float(funnel["proposal_count"]),
            "approval_rate": float(rates["approval_rate"]),
            "abandonment_rate": float(rates["abandonment_rate"]),
            "avg_diagnosis_conf": float(confidence["avg_diagnosis_conf"]),
            "resolved_count": float(outcomes["resolved"]),
            "persistent_count": float(outcomes["persistent"]),
            "regressed_count": float(outcomes["regressed"]),
            "harness_health": float(health_score),
        }
        for name, val in snaps.items():
            try:
                m.ha_self_metric_record(
                    metric_name=name, value=val, period="snapshot",
                    metadata={
                        "funnel": funnel,
                        "rates": rates,
                        "confidence": confidence,
                        "outcomes": outcomes,
                    } if name == "harness_health" else None,
                )
            except Exception as ex:
                print(f"[MetaEvaluator] metric record 실패 ({name}): {ex!r}")
