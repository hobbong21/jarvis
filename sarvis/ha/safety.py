"""HA 안전 가드레일 (기획서 §8 — 본 장은 다른 모든 장보다 우선).

7개 절대 규칙은 base.HAAgent._FORBIDDEN_WRITE 로 1차 차단.
본 모듈은 Kill Switch + 정렬 가드 보조.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


KILL_SWITCH_FILE = Path(
    os.environ.get("SARVIS_HA_KILL_SWITCH_FILE", "data/ha/kill_switch.json")
)
KILL_SWITCH_ENV = "SARVIS_HA_KILL_SWITCH"


class KillSwitchActivated(RuntimeError):
    """Kill Switch 활성 상태에서 HA 작업 시도 시 발생."""


def _truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def is_kill_switch_on() -> bool:
    """환경변수 OR 파일 — 두 경로 모두 확인 (기획서 §2.4 — 관측 회피 금지)."""
    if _truthy(os.environ.get(KILL_SWITCH_ENV)):
        return True
    try:
        if KILL_SWITCH_FILE.is_file():
            data = json.loads(KILL_SWITCH_FILE.read_text(encoding="utf-8"))
            return bool(data.get("active"))
    except (OSError, json.JSONDecodeError):
        # 파일 손상 시 안전 측 = 활성으로 간주 (기획서 §2.3.5 Humble by Default)
        return True
    return False


def activate_kill_switch(by: str = "owner", reason: str = "manual") -> None:
    KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active": True,
        "activated_by": by,
        "activated_at": time.time(),
        "reason": reason,
    }
    KILL_SWITCH_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def deactivate_kill_switch(by: str = "owner") -> None:
    KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active": False,
        "deactivated_by": by,
        "deactivated_at": time.time(),
    }
    KILL_SWITCH_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_running() -> None:
    """HA 동작 시작 전 호출. Kill Switch 활성 시 즉시 예외."""
    if is_kill_switch_on():
        raise KillSwitchActivated(
            "HA Kill Switch 활성. 모든 자율 동작이 정지되었습니다."
        )


# ── PII 마스킹 (기획서 §11.3 — ha_messages 진입 전 강제) ─────────
_PII_PATTERNS = [
    # 이메일
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]"),
    # 전화번호 (한국 010-xxxx-xxxx, 기타 국제식 단순)
    (r"01[016789][- ]?\d{3,4}[- ]?\d{4}", "[PHONE]"),
    # 주민번호 (xxxxxx-xxxxxxx)
    (r"\d{6}[- ]?[1-4]\d{6}", "[KRRRN]"),
    # 신용카드 (4-4-4-4)
    (r"\b(?:\d[ -]*?){13,19}\b", "[CARDLIKE]"),
    # API 키 형태 (sk-..., AIza..., 긴 hex)
    (r"\bsk-[A-Za-z0-9_-]{20,}\b", "[APIKEY]"),
    (r"\bAIza[0-9A-Za-z_-]{20,}\b", "[APIKEY]"),
]


def mask_pii(text: str) -> str:
    """본문 PII 마스킹. LLM 송신/외부 로그 전 필수."""
    if not text or not isinstance(text, str):
        return text or ""
    import re
    out = text
    for pat, repl in _PII_PATTERNS:
        out = re.sub(pat, repl, out)
    return out
