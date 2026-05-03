"""Validator 에이전트 (기획서 §4.5) — Stage S3 (L1).

PatchSpec(=ha_proposals) 를 받아 회귀 위험·가역성·안전 영향을 점수화.
**자동 승인 절대 없음** — auto_approval_blocked=True 고정 (L1 규칙).

위험 점수 = (target 위험도 0.0~0.5) + (reversible False 시 +0.3) +
           (high severity 진단 +0.2) + (코드 수정류 카테고리 +0.2).
risk_level: <0.34 low / <0.67 med / 그 외 high.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .base import HAAgent
from .safety import ensure_running


# target prefix → 기본 위험도. 코드/프롬프트는 높음, 알람/모니터링은 낮음.
_TARGET_RISK: Dict[str, float] = {
    "prompt:": 0.45,
    "tool_router:": 0.35,
    "observer:": 0.30,
    "memory.knowledge:": 0.25,
    "web/": 0.20,
    "brain.router:": 0.40,
    "alerts:": 0.10,
    "noop": 0.05,
    # 사이클 #29: 하네스 자기 조정 (MetaEvaluator → Strategist harness_meta).
    # Observer 임계 등 reversible 한 룰 변경이므로 observer: 와 동등한 위험.
    "harness:": 0.30,
}


def _base_target_risk(target: str) -> float:
    for prefix, risk in _TARGET_RISK.items():
        if target.startswith(prefix) or target == prefix:
            return risk
    return 0.5  # 알 수 없는 대상 — 보수적


@dataclass
class ValidationReport:
    validation_id: str
    proposal_id: str
    risk_score: float
    risk_level: str   # low/med/high
    checks: List[Dict[str, Any]] = field(default_factory=list)
    auto_approval_blocked: bool = True

    def to_payload(self) -> Dict[str, Any]:
        return {
            "validation_id": self.validation_id,
            "proposal_id": self.proposal_id,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "checks": self.checks,
            "auto_approval_blocked": self.auto_approval_blocked,
            "requires_human": True,
        }


class Validator(HAAgent):
    name = "Validator"
    read_scope = {"ha_proposals", "ha_strategies", "ha_diagnoses",
                  "ha_messages"}
    # 검증 보고서만 write. 실제 적용/승인 권한 없음.
    write_scope = {"ha_messages", "ha_validations"}

    def _new_id(self) -> str:
        return f"VAL-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _level(score: float) -> str:
        if score < 0.34:
            return "low"
        if score < 0.67:
            return "med"
        return "high"

    def evaluate(self, proposal: Dict[str, Any]) -> ValidationReport:
        ensure_running()
        target = str(proposal.get("target", ""))
        reversible = bool(proposal.get("reversible", True))
        checks: List[Dict[str, Any]] = []

        base = _base_target_risk(target)
        checks.append({
            "name": "target_base_risk",
            "value": base,
            "detail": f"target={target}",
        })

        score = base
        if not reversible:
            score += 0.3
            checks.append({
                "name": "reversibility_penalty", "value": 0.3,
                "detail": "비가역 변경 — 되돌리기 어려움",
            })
        else:
            checks.append({
                "name": "reversibility_ok", "value": 0.0,
                "detail": "가역적 (rollback 가능)",
            })

        if target.startswith(("prompt:", "brain.router:", "tool_router:")):
            score += 0.2
            checks.append({
                "name": "code_path_risk", "value": 0.2,
                "detail": "코드/프롬프트 영역 변경 — 회귀 위험",
            })

        # diagnostician 신뢰도가 낮으면 위험도 +.
        diag_conf = float(proposal.get("validation", {})
                          .get("diagnosis_confidence", 0.5) or 0.5)
        if diag_conf < 0.4:
            score += 0.15
            checks.append({
                "name": "low_diagnosis_confidence", "value": 0.15,
                "detail": f"diag_conf={diag_conf:.2f} — 근거 약함",
            })

        score = round(min(1.0, max(0.0, score)), 3)
        level = self._level(score)
        checks.append({
            "name": "auto_approval_blocked", "value": True,
            "detail": "L1 규칙 — 자동 승인 절대 금지",
        })

        report = ValidationReport(
            validation_id=self._new_id(),
            proposal_id=proposal.get("proposal_id", "UNKNOWN"),
            risk_score=score, risk_level=level, checks=checks,
            auto_approval_blocked=True,
        )
        if self.memory is not None:
            try:
                self.memory.ha_validation_insert(
                    validation_id=report.validation_id,
                    proposal_id=report.proposal_id,
                    risk_score=report.risk_score,
                    risk_level=report.risk_level,
                    checks=report.checks,
                    auto_approval_blocked=True,
                )
                self.emit("Reporter", report.to_payload())
            except Exception as ex:
                print(f"[Validator] 영속 실패 {report.validation_id}: {ex!r}")
        return report

    def run_pending(self, limit: int = 50) -> List[ValidationReport]:
        ensure_running()
        if self.memory is None:
            return []
        proposals = self.memory.ha_proposals_list(status="pending", limit=limit)
        out: List[ValidationReport] = []
        for p in proposals:
            # 이미 validation 있는 proposal 은 건너뜀.
            existing = self.memory.ha_validations_for_proposal(
                p["proposal_id"], limit=1,
            )
            if existing:
                continue
            try:
                out.append(self.evaluate(p))
            except Exception as ex:
                print(f"[Validator] evaluate 실패 {p.get('proposal_id')}: {ex!r}")
        return out
