"""HA 에이전트 베이스 클래스 + 메시지 스키마.

기획서 §3.4 (통신 규약) + §11.2 (메시지 스키마) 구현.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


AGENT_NAMES: Set[str] = {
    "Observer", "Diagnostician", "Strategist", "Improver",
    "Validator", "Reporter", "Orchestrator", "MetaEvaluator", "Operator",
}


def _signing_key() -> bytes:
    """메시지 서명 키. SARVIS_HA_SIGNING_KEY 환경변수 우선, 없으면 path 기반 고정값.

    단일 주인 시스템이므로 외부 노출이 없을 때는 고정값으로 충분 (위변조 감지용).
    """
    k = os.environ.get("SARVIS_HA_SIGNING_KEY")
    if k:
        return k.encode("utf-8")
    return b"sarvis-ha-default-do-not-share-in-prod"


def sign_payload(payload: Dict[str, Any]) -> str:
    """JSON payload 의 HMAC-SHA256 서명. sort_keys 로 결정성 보장."""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hmac.new(_signing_key(), body, hashlib.sha256).hexdigest()


def verify_signature(payload: Dict[str, Any], signature: str) -> bool:
    expected = sign_payload(payload)
    return hmac.compare_digest(expected, signature)


@dataclass
class HAMessage:
    """에이전트 간 구조화 메시지 (기획서 §11.2).

    필수 필드 schema 검증은 __post_init__ 에서 수행.
    """
    from_agent: str
    to_agent: str
    payload: Dict[str, Any]
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: str = "1.0"
    created_at: float = field(default_factory=time.time)
    signature: Optional[str] = None

    def __post_init__(self) -> None:
        if self.from_agent not in AGENT_NAMES:
            raise ValueError(f"unknown from_agent: {self.from_agent}")
        if self.to_agent not in AGENT_NAMES:
            raise ValueError(f"unknown to_agent: {self.to_agent}")
        if self.from_agent == self.to_agent:
            raise ValueError("from_agent must differ from to_agent")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be dict")
        # confidence 가 있다면 0~1 검증 (기획서 §3.4)
        c = self.payload.get("confidence")
        if c is not None:
            try:
                cf = float(c)
            except (TypeError, ValueError):
                raise ValueError("confidence must be number")
            if not (0.0 <= cf <= 1.0):
                raise ValueError(f"confidence out of range: {cf}")
        if self.signature is None:
            self.signature = sign_payload(self.payload)

    def verify(self) -> bool:
        return self.signature is not None and verify_signature(
            self.payload, self.signature
        )


class HAAgent:
    """모든 HA 에이전트의 베이스. read/write scope 명시.

    기획서 §8.3 권한 분리: 어떤 에이전트도 단독으로 변경을 적용하지 못한다.
    Stage S1 에서는 Observer/Reporter 둘만 활성, 둘 다 write_scope 가
    'ha_messages' + 'ha_issues' (Reporter 는 추가로 'reports/') 로 한정.
    """

    name: str = "AbstractAgent"
    read_scope: Set[str] = set()   # 읽기 가능한 자원 키
    write_scope: Set[str] = set()  # 쓰기 가능한 자원 키 (코드 수정/안전 프롬프트 절대 금지)

    # 절대 차단 자원 — 모든 에이전트에서 공통 거부 (기획서 §8.2)
    _FORBIDDEN_WRITE: Set[str] = {
        "sarvis_code", "sarvis_safety_prompt", "ha_audit_log",
        "ha_kill_switch", "meta_evaluator_io", "user_data_export",
        "payment", "permanent_delete",
    }

    def __init__(self, memory=None) -> None:
        self.memory = memory  # sarvis.memory.Memory 인스턴스
        if self.name not in AGENT_NAMES:
            raise ValueError(f"unregistered agent name: {self.name}")
        forbidden = self._FORBIDDEN_WRITE & self.write_scope
        if forbidden:
            raise RuntimeError(
                f"{self.name}: 금지된 write scope 보유: {forbidden}"
            )

    def can_read(self, resource: str) -> bool:
        return resource in self.read_scope

    def can_write(self, resource: str) -> bool:
        if resource in self._FORBIDDEN_WRITE:
            return False
        return resource in self.write_scope

    def emit(self, to_agent: str, payload: Dict[str, Any]) -> HAMessage:
        """메시지 생성 + ha_messages 에 append-only 기록."""
        if not self.can_write("ha_messages"):
            raise PermissionError(f"{self.name}: ha_messages write 권한 없음")
        msg = HAMessage(
            from_agent=self.name, to_agent=to_agent, payload=payload,
        )
        if self.memory is not None:
            self.memory.ha_message_append(
                msg_id=msg.msg_id,
                from_agent=msg.from_agent,
                to_agent=msg.to_agent,
                payload=msg.payload,
                signature=msg.signature or "",
                schema_version=msg.schema_version,
            )
        return msg
