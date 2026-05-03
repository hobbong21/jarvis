"""Improver 에이전트 (기획서 §4.4) — Stage S3 (L1).

Strategy → PatchSpec (텍스트 명세). **실제 파일 수정은 절대 없음** —
모든 결과는 ha_proposals(pending) 큐에 들어가 사람 승인을 기다림.

명세는 (target / before_text / after_text / reversible / rationale) 5개
필드로 표현. Validator 가 이를 받아 위험도 + 회귀 영향을 평가.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .base import HAAgent
from .safety import ensure_running


# 카테고리 → PatchSpec 템플릿 (target + reversible 기본값).
_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "prompt_tweak": {
        "target": "prompt:base.system",
        "reversible": True,
        "before": "(현재 시스템 프롬프트 — 자세한 내용은 `sarvis/brain.py` 참조)",
        "after_template":
            "[+ 추가 가이드] {summary}\n근거: {rationale}\n예상 효과: {impact}",
    },
    "tool_swap": {
        "target": "tool_router:weights",
        "reversible": True,
        "before": "(현재 도구 우선순위 — 동등 가중치 또는 최근 성공률 기반)",
        "after_template":
            "변경: {summary}\n적용 범위: 동일 카테고리 명령\n사유: {rationale}",
    },
    "heuristic_threshold": {
        "target": "observer:thresholds",
        "reversible": True,
        "before": "_ERROR_RATE_HIGH=0.10, _DRIFT_DELTA=0.5, _LATENCY_HIGH_MS=8000",
        "after_template":
            "변경 제안: {summary}\n사유: {rationale}\n영향: {impact}",
    },
    "knowledge_add": {
        "target": "memory.knowledge:append",
        "reversible": True,
        "before": "(현재 knowledge 시드 — `memory.knowledge_*`)",
        "after_template":
            "추가 시드: {summary}\n분류: validator-seed\n사유: {rationale}",
    },
    "ui_hint": {
        "target": "web/index.html:hint",
        "reversible": True,
        "before": "(현재 UI — 해당 위치에 안내 없음)",
        "after_template":
            "추가 카드/배지: {summary}\n트리거: {rationale}\n예상: {impact}",
    },
    "model_route": {
        "target": "brain.router:rules",
        "reversible": True,
        "before": "(현재 라우팅 — 단일 백엔드 또는 fallback 순서)",
        "after_template":
            "라우팅 룰 추가: {summary}\n적용: 단순 분류 응답\n사유: {rationale}",
    },
    "monitoring_only": {
        "target": "alerts:new",
        "reversible": True,
        "before": "(없음)",
        "after_template":
            "알람 신설: {summary}\n주기/임계: 기획서 §6 참고\n사유: {rationale}",
    },
    "do_nothing": {
        "target": "noop",
        "reversible": True,
        "before": "(현 상태)",
        "after_template": "변경 없음 — 다음 사이클 재검토. 사유: {rationale}",
    },
    # 사이클 #29: MetaEvaluator 발 harness_meta 카테고리.
    # 하네스 자체 룰을 손대는 제안이므로 target 을 별도 prefix 로 구분 →
    # Validator 가 위험도를 분리 산정 가능 (현재는 fallback risk 0.5).
    "harness_meta": {
        "target": "harness:self_tuning",
        "reversible": True,
        "before": "(현재 하네스 임계/룰)",
        "after_template":
            "하네스 자기 조정: {summary}\n근거: {rationale}\n예상 효과: {impact}",
    },
}


@dataclass
class PatchSpec:
    proposal_id: str
    strategy_id: str
    target: str
    before_text: str
    after_text: str
    reversible: bool
    rationale: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "strategy_id": self.strategy_id,
            "target": self.target,
            "before_text": self.before_text,
            "after_text": self.after_text,
            "reversible": self.reversible,
            "rationale": self.rationale,
            "status": "pending",
            "requires_human": True,
        }


class Improver(HAAgent):
    name = "Improver"
    read_scope = {"ha_strategies", "ha_messages"}
    # ha_proposals 만 write. 실제 파일/안전 프롬프트/코드 수정 권한 없음.
    write_scope = {"ha_messages", "ha_proposals"}

    def _new_id(self) -> str:
        return f"PROP-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def materialize(self, strategy: Dict[str, Any]) -> PatchSpec:
        ensure_running()
        cat = strategy.get("category", "")
        tmpl = _TEMPLATES.get(cat, _TEMPLATES["monitoring_only"])
        after = tmpl["after_template"].format(
            summary=strategy.get("summary", ""),
            rationale=strategy.get("rationale") or "(없음)",
            impact=strategy.get("expected_impact") or "(미상)",
        )
        spec = PatchSpec(
            proposal_id=self._new_id(),
            strategy_id=strategy.get("strategy_id", "UNKNOWN"),
            target=tmpl["target"],
            before_text=tmpl["before"],
            after_text=after,
            reversible=bool(tmpl["reversible"]),
            rationale=strategy.get("rationale"),
        )
        if self.memory is not None:
            try:
                self.memory.ha_proposal_insert(
                    proposal_id=spec.proposal_id,
                    strategy_id=spec.strategy_id,
                    target=spec.target,
                    before_text=spec.before_text,
                    after_text=spec.after_text,
                    reversible=spec.reversible,
                    risk_level="med", risk_score=0.5,
                    validation={"pending_validator": True},
                )
                self.emit("Validator", spec.to_payload())
            except Exception as ex:
                print(f"[Improver] 영속 실패 {spec.proposal_id}: {ex!r}")
        return spec

    def run_recent(self, limit: int = 50) -> list:
        ensure_running()
        if self.memory is None:
            return []
        # 아직 proposal 이 없는 strategy 만 처리 (중복 방지).
        strats = self.memory.ha_strategies_recent(limit=limit)
        existing = {p["strategy_id"] for p in self.memory.ha_proposals_list(
            limit=500,
        )}
        out = []
        for s in strats:
            if s["strategy_id"] in existing:
                continue
            try:
                out.append(self.materialize(s))
            except Exception as ex:
                print(f"[Improver] materialize 실패 {s.get('strategy_id')}: {ex!r}")
        return out
