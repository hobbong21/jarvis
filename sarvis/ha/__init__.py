"""SARVIS Harness Agent (HA).

보조 기획서 `Sarvis_Harness_Agent_기획서_1777804423173.docx` 기반.
7대 에이전트(Observer / Diagnostician / Strategist / Improver / Validator /
Reporter / **MetaEvaluator**) + 향후 Orchestrator. 자율 등급 L1 — 모든 변경
제안은 사람 승인 큐(ha_proposals.pending)에 누적되며, 자체 진화 사이클
(MetaEvaluator → 자기-이슈 → 동일 파이프라인)을 통해 하네스 자기 자신도
같은 안전 게이트로 개선된다 (자율 적용 절대 없음).
"""
from .base import HAMessage, HAAgent, sign_payload, AGENT_NAMES
from .safety import (
    is_kill_switch_on,
    activate_kill_switch,
    deactivate_kill_switch,
    KillSwitchActivated,
)
from .observer import Observer
from .reporter import Reporter
from .diagnostician import Diagnostician, DiagnosisResult
from .strategist import Strategist, Strategy, CATEGORIES as STRATEGY_CATEGORIES
from .improver import Improver, PatchSpec
from .validator import Validator, ValidationReport
from .meta_evaluator import MetaEvaluator, HarnessHealthReport

__all__ = [
    "HAMessage", "HAAgent", "sign_payload", "AGENT_NAMES",
    "is_kill_switch_on", "activate_kill_switch", "deactivate_kill_switch",
    "KillSwitchActivated", "Observer", "Reporter",
    "Diagnostician", "DiagnosisResult",
    "Strategist", "Strategy", "STRATEGY_CATEGORIES",
    "Improver", "PatchSpec",
    "Validator", "ValidationReport",
    "MetaEvaluator", "HarnessHealthReport",
]
