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

    def _render_diagnosis_section(self, issue_id: str) -> str:
        """이슈에 연결된 최신 Diagnosis 가 있으면 마크다운으로 렌더, 없으면 안내."""
        if self.memory is None or not issue_id:
            return ("- (Stage S1 — Diagnostician 미활성)\n"
                    "- Stage S2 에서 5 Whys + 베이지안 가설 랭킹 자동 첨부.")
        try:
            diags = self.memory.ha_diagnoses_for_issue(issue_id, limit=1)
        except Exception:
            diags = []
        if not diags:
            return ("- (대기 중) 다음 진단 사이클에서 Diagnostician 자동 분석 예정.\n"
                    "- 즉시 진단: WS `ha_run_diagnostician`.")
        d = diags[0]
        hyps = d.get("hypotheses") or []
        lines = [f"- **근본원인 (1위 가설)**: {d.get('root_cause') or '(미정)'}",
                 f"- **신뢰도**: {float(d.get('confidence') or 0):.2f}",
                 f"- **방법**: `{d.get('method', 'heuristic')}`"]
        if d.get("recommended_action"):
            lines.append(f"- **권장 다음 액션**: {d['recommended_action']}")
        whys = d.get("five_whys") or []
        if whys:
            lines.append("- **5 Whys 인과 사슬**:")
            for w in whys[:5]:
                lines.append(f"  - {w}")
        if hyps:
            lines.append("- **가설 랭킹**:")
            for i, h in enumerate(hyps[:5], 1):
                post = h.get("posterior", h.get("prior", 0.0))
                tag = " 🤖" if h.get("source") == "llm" else ""
                lines.append(
                    f"  {i}. {h.get('name')} — 사후 {float(post):.2f}{tag}"
                )
                if h.get("check"):
                    lines.append(f"     · 점검: {h['check']}")
        return "\n".join(lines)

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
{self._render_diagnosis_section(issue.get("issue_id", ""))}

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
            return {"issues": [], "messages": [], "stage": "S2",
                    "diagnoses": []}
        issues = self.memory.ha_issues_recent(limit=limit)
        msgs = self.memory.ha_messages_recent(limit=limit)
        try:
            diagnoses = self.memory.ha_diagnoses_recent(limit=limit)
        except Exception:
            diagnoses = []
        try:
            strategies = self.memory.ha_strategies_recent(limit=limit)
        except Exception:
            strategies = []
        try:
            proposals = self.memory.ha_proposals_list(limit=limit)
        except Exception:
            proposals = []
        return {
            "stage": "S3 — Improve Suggest (Observer + Diagnostician + "
                     "Strategist + Improver + Validator + Reporter)",
            "autonomy_level": "L1 (모든 변경 사람 승인, 자동 적용 없음)",
            "issues": issues,
            "messages": msgs,
            "diagnoses": diagnoses,
            "strategies": strategies,
            "proposals": proposals,
            "active_agents": ["Observer", "Diagnostician", "Strategist",
                              "Improver", "Validator", "Reporter"],
            "pending_agents": ["Orchestrator (S4)", "MetaEvaluator (S4)"],
        }
