"""SARVIS Harness Agent (HA) — Stage S1 Read-Only.

보조 기획서 `Sarvis_Harness_Agent_기획서_1777804423173.docx` 기반.
6대 에이전트(Observer/Diagnostician/Strategist/Improver/Validator/Reporter)
+ Orchestrator + Meta-Evaluator 자율 진화 시스템 — 사이클 #23 은 Observer +
Reporter(미니) 만 활성. 자율 등급 L0 (Observe-only).
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

__all__ = [
    "HAMessage", "HAAgent", "sign_payload", "AGENT_NAMES",
    "is_kill_switch_on", "activate_kill_switch", "deactivate_kill_switch",
    "KillSwitchActivated", "Observer", "Reporter",
    "Diagnostician", "DiagnosisResult",
    "Strategist", "Strategy", "STRATEGY_CATEGORIES",
    "Improver", "PatchSpec",
    "Validator", "ValidationReport",
]
