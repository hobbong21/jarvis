"""Reporter 에이전트 (기획서 §4.6) — Stage S1 미니 버전.

책임 (Stage S1):
- Observer 가 emit 한 Issue Card 를 One-Pager 마크다운으로 정리
- `data/ha/reports/<issue_id>.md` 로 영속
- 사용자 '성장 일기' 카드용 JSON 요약 제공 (기획서 §12.3)

Stage S2+ 에서 Diagnostician/Validator 결과까지 통합.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import HAAgent
from .safety import ensure_running


REPORTS_DIR = Path(
    os.environ.get("SARVIS_HA_REPORTS_DIR", "data/ha/reports")
)


def _severity_emoji(sev: str) -> str:
    return {
        "critical": "🚨", "high": "⚠️", "medium": "🔶",
        "low": "🔷", "info": "ℹ️",
    }.get(sev, "•")


class Reporter(HAAgent):
    name = "Reporter"
    read_scope = {"ha_issues", "ha_messages"}
    # Stage S1: 보고서 파일 + ha_messages append 만. 코드/안전 프롬프트 수정 불가.
    write_scope = {"ha_messages", "reports_files"}

    def write_one_pager(self, issue: Dict[str, Any]) -> Path:
        """Issue Card → One-Pager 마크다운 파일 (기획서 §4.6.2)."""
        ensure_running()
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        iid = issue.get("issue_id", "UNKNOWN")
        path = REPORTS_DIR / f"{iid}.md"
        sev = issue.get("severity", "info")
        body = f"""# {_severity_emoji(sev)} HA Report — {iid}

> Stage S1 (Read-Only) — Observer 자동 생성. 변경 적용 없음.
> 생성: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(issue.get('created_at') or time.time()))}

## TL;DR
{issue.get('narrative', issue.get('narrative_summary', '(요약 없음)'))}

## Observation (관찰)
- **분류**: {issue.get('category', '?')}
- **심각도**: {sev}
- **신호**: {issue.get('signal') or issue.get('statistical_signal') or '(없음)'}
- **신뢰도**: {issue.get('confidence', 0.0):.2f}
- **증거 트레이스**: {len(issue.get('evidence', issue.get('evidence_traces', []) or []))} 건

## Diagnosis (진단)
- Stage S1 에서는 진단을 수행하지 않음 (Observer 만 활성).
- Stage S2 에서 Diagnostician 이 5 Whys + Bayesian 분석 예정.

## Proposal (제안)
- (해당 없음 — Read-Only 단계)

## Validation (검증)
- (해당 없음 — Read-Only 단계)

## Risk (위험)
- 본 보고서 자체는 변경을 일으키지 않음. 운영자 주의 환기 목적.

## Decision (결정 요청)
- [ ] 승인 (다음 진단 사이클로 전달)
- [ ] 반려 (false positive 표시)
- [ ] 유보 (추가 관찰)
"""
        path.write_text(body, encoding="utf-8")
        # 보고서 생성 사실을 메시지로 기록 (감사 추적)
        try:
            self.emit("Operator", {
                "report_path": str(path),
                "issue_id": iid,
                "severity": sev,
                "confidence": float(issue.get("confidence", 0.0)),
            })
        except Exception as ex:
            print(f"[Reporter] emit 실패 {iid}: {ex!r}")
        return path

    def growth_diary(
        self, limit: int = 10,
    ) -> Dict[str, Any]:
        """사용자 '성장 일기' (기획서 §12.3 + §HAR-05).

        현재 단계는 변경 이력이 없으므로 최근 issue + 메시지 추세만 노출.
        """
        ensure_running()
        if self.memory is None:
            return {"issues": [], "messages": [], "stage": "S1"}
        issues = self.memory.ha_issues_recent(limit=limit)
        msgs = self.memory.ha_messages_recent(limit=limit)
        return {
            "stage": "S1 — Read-Only (Observer + Reporter)",
            "autonomy_level": "L0 (Observe-only)",
            "issues": issues,
            "messages": msgs,
            "active_agents": ["Observer", "Reporter"],
            "pending_agents": [
                "Diagnostician (S2)", "Strategist (S3)",
                "Improver (S3)", "Validator (S3)", "MetaEvaluator (S4)",
            ],
        }
