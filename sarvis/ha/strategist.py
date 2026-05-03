"""Strategist 에이전트 (기획서 §4.3) — Stage S3 (L1).

Diagnosis 를 받아 8 카테고리 변경 후보를 생성. **Do Nothing 강제 포함**
(기획서 §4.3.3) — 어떤 사이클에서도 "변경 없음" 옵션이 한 번은 제시돼야
사람이 자율 추천에 휘둘리지 않는다.

L1: 어떤 strategy 도 자동 적용되지 않음. 모두 Improver → Validator →
ha_proposals(pending) 큐에 들어가 사람 승인을 기다림.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .base import HAAgent
from .safety import ensure_running


# 8 카테고리 (기획서 §4.3.2).
CATEGORIES = (
    "prompt_tweak",        # 시스템 프롬프트 일부 수정
    "tool_swap",           # 도구 호출 우선순위/대체
    "heuristic_threshold", # Observer/Diagnostician 임계값 조정
    "knowledge_add",       # knowledge 시드 추가
    "ui_hint",             # UI 안내/배지/토스트
    "model_route",         # 모델 백엔드 라우팅 변경
    "monitoring_only",     # 알람만 추가, 동작 변경 X
    "do_nothing",          # 강제 — 변경하지 않는 선택지
)


# 카테고리 → 진단 카테고리별 strategy 후보.
# 각 strategy 는 summary + rationale + expected_impact + cost_estimate.
_STRATEGY_RULES: Dict[str, List[Dict[str, str]]] = {
    "spike": [
        {"category": "monitoring_only",
         "summary": "에러 급증 경로에 분 단위 알람 추가",
         "rationale": "변경 없이 가시성부터 확보 → 재현 후 진단",
         "expected_impact": "회복 시간 단축 (감지→대응)",
         "cost_estimate": "낮음 (대시보드 위젯 1)"},
        {"category": "tool_swap",
         "summary": "실패 빈도 높은 백엔드를 fallback 우선순위 뒤로 이동",
         "rationale": "단기 회복 + 사용자 영향 최소화",
         "expected_impact": "동일 윈도우 에러율 30~50% 감소(예상)",
         "cost_estimate": "낮음 (라우팅 가중치 변경)"},
        {"category": "heuristic_threshold",
         "summary": "Observer _ERROR_RATE_HIGH 0.10 → 0.07 로 조기 감지",
         "rationale": "조기 신호 확보 후 사람 검토",
         "expected_impact": "다음 사이클 early-warning",
         "cost_estimate": "낮음"},
    ],
    "drift": [
        {"category": "knowledge_add",
         "summary": "최근 부정 피드백 5건을 회귀 시드로 등록",
         "rationale": "동일 패턴 재발 방지 + Validator 향후 재현 가능",
         "expected_impact": "유사 회귀 차단",
         "cost_estimate": "낮음 (수동 라벨 5건)"},
        {"category": "prompt_tweak",
         "summary": "특정 카테고리에서 응답 톤 가이드 추가",
         "rationale": "톤 드리프트가 만족도 하락의 흔한 원인",
         "expected_impact": "7d 만족도 회복",
         "cost_estimate": "중 (프롬프트 변경 + 회귀)"},
        {"category": "monitoring_only",
         "summary": "주간 만족도 추세 일일 집계로 전환",
         "rationale": "변경 전 추세 안정성 확인",
         "expected_impact": "오탐 감소",
         "cost_estimate": "낮음"},
    ],
    "anomaly": [
        {"category": "knowledge_add",
         "summary": "부정 군집 카테고리 시나리오를 Validator 사전에 추가",
         "rationale": "사각 영역의 회귀 커버리지 확보",
         "expected_impact": "동일 카테고리 부정률 감소",
         "cost_estimate": "낮음~중"},
        {"category": "ui_hint",
         "summary": "해당 카테고리 응답 후 '도움됐나요?' 배지 노출",
         "rationale": "초기 부정 감지 + 자가 정정",
         "expected_impact": "피드백 양 증가",
         "cost_estimate": "낮음 (UI 1)"},
    ],
    "cost": [
        {"category": "tool_swap",
         "summary": "비용/지연이 큰 도구 호출에 캐시 또는 사전조건 추가",
         "rationale": "p95 지연 감소가 1순위",
         "expected_impact": "p95 latency 20~40% 감소",
         "cost_estimate": "중"},
        {"category": "model_route",
         "summary": "단순 응답은 경량 백엔드로 라우팅",
         "rationale": "대형 모델 사용 비율 축소",
         "expected_impact": "토큰/지연 동시 감소",
         "cost_estimate": "중 (라우터 룰)"},
        {"category": "monitoring_only",
         "summary": "단계별 시간 분포 일일 보고",
         "rationale": "병목의 변동을 우선 가시화",
         "expected_impact": "원인 식별 가속",
         "cost_estimate": "낮음"},
    ],
    "underutilization": [
        {"category": "ui_hint",
         "summary": "주요 기능 발견성을 높이는 onboarding 카드 추가",
         "rationale": "사용자가 침묵하는 기간의 1/N 은 발견성 저하",
         "expected_impact": "재진입률 증가",
         "cost_estimate": "낮음~중"},
        {"category": "monitoring_only",
         "summary": "침묵 윈도우 추세 알람 (3일 연속 시 운영자 통지)",
         "rationale": "조기 알람으로 적극적 케어",
         "expected_impact": "이탈 방지",
         "cost_estimate": "낮음"},
    ],
}

_FALLBACK_STRATS: List[Dict[str, str]] = [
    {"category": "monitoring_only",
     "summary": "신규 패턴이므로 변경 없이 1주 추세부터 관찰",
     "rationale": "근거 부족 — Observer 룰 보강 후 재평가",
     "expected_impact": "오탐 회피",
     "cost_estimate": "없음"},
]

_DO_NOTHING: Dict[str, str] = {
    "category": "do_nothing",
    "summary": "변경하지 않는다 — 다음 사이클 추세 관찰 후 재검토",
    "rationale": "기획서 §4.3.3 — 자율 추천의 행동 편향을 막는 강제 옵션",
    "expected_impact": "현 상태 유지 (변경 없음)",
    "cost_estimate": "없음",
}


@dataclass
class Strategy:
    strategy_id: str
    diagnosis_id: str
    category: str
    summary: str
    rationale: Optional[str] = None
    expected_impact: Optional[str] = None
    cost_estimate: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "diagnosis_id": self.diagnosis_id,
            "category": self.category,
            "summary": self.summary,
            "rationale": self.rationale,
            "expected_impact": self.expected_impact,
            "cost_estimate": self.cost_estimate,
            "requires_human": True,
        }


class Strategist(HAAgent):
    name = "Strategist"
    read_scope = {"ha_diagnoses", "ha_issues", "ha_messages"}
    write_scope = {"ha_messages", "ha_strategies"}

    def _new_id(self) -> str:
        return f"STRAT-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def _resolve_issue_category(self, diagnosis: Dict[str, Any]) -> str:
        if self.memory is None:
            return ""
        try:
            issues = self.memory.ha_issues_recent(limit=200)
        except Exception:
            return ""
        match = next(
            (i for i in issues if i.get("issue_id") == diagnosis.get("issue_id")),
            None,
        )
        return (match or {}).get("category", "") if match else ""

    def propose(self, diagnosis: Dict[str, Any]) -> List[Strategy]:
        ensure_running()
        cat = self._resolve_issue_category(diagnosis)
        rules = _STRATEGY_RULES.get(cat, list(_FALLBACK_STRATS))
        # Do Nothing 강제 — 마지막에 항상 추가.
        all_rules = list(rules) + [dict(_DO_NOTHING)]
        out: List[Strategy] = []
        for r in all_rules:
            s = Strategy(
                strategy_id=self._new_id(),
                diagnosis_id=diagnosis.get("diagnosis_id", "UNKNOWN"),
                category=r["category"],
                summary=r["summary"],
                rationale=r.get("rationale"),
                expected_impact=r.get("expected_impact"),
                cost_estimate=r.get("cost_estimate"),
            )
            if self.memory is not None:
                try:
                    self.memory.ha_strategy_insert(
                        strategy_id=s.strategy_id,
                        diagnosis_id=s.diagnosis_id,
                        category=s.category, summary=s.summary,
                        rationale=s.rationale,
                        expected_impact=s.expected_impact,
                        cost_estimate=s.cost_estimate,
                    )
                    self.emit("Improver", s.to_payload())
                except Exception as ex:
                    print(f"[Strategist] 영속 실패 {s.strategy_id}: {ex!r}")
            out.append(s)
        return out

    def run_recent(self, limit: int = 5) -> List[Strategy]:
        ensure_running()
        if self.memory is None:
            return []
        diags = self.memory.ha_diagnoses_recent(limit=limit)
        out: List[Strategy] = []
        for d in diags:
            try:
                out.extend(self.propose(d))
            except Exception as ex:
                print(f"[Strategist] propose 실패 {d.get('diagnosis_id')}: {ex!r}")
        return out
