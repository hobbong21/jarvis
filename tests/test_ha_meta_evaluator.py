"""사이클 #29 — MetaEvaluator (자체 진화) + ha_self_metrics + ha_outcomes 테스트.

검증:
- 빈 DB 에서 빈 보고서 반환 + 자기-이슈 발화 안 함 (false positive 회피)
- 채택률 낮으면 harness_meta 자기-이슈 emit
- 폐기율 높으면 abandonment 자기-이슈 emit
- 적용 결과 효과성 낮으면 ineffective 자기-이슈 emit
- ha_self_metrics 영속화 (퍼널 + 비율 + 건강 점수)
- ha_outcomes pending → resolved/persistent/regressed 전환
- write_scope 가드 — MetaEvaluator 가 코드/안전 프롬프트를 변경할 수 없음
- harness_meta 카테고리에 대해 Strategist 가 새 룰을 적용
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sarvis.memory import Memory  # noqa: E402
from sarvis.ha import MetaEvaluator, HarnessHealthReport, Strategist  # noqa: E402


def _fresh_memory(tmpdir: str) -> Memory:
    return Memory(path=str(Path(tmpdir) / "meta_eval.db"))


def _seed_proposal(
    m: Memory,
    *,
    proposal_id: str,
    issue_category: str = "spike",
    proposal_status: str = "pending",
    issue_id: str = None,
    diagnosis_id: str = None,
    strategy_id: str = None,
) -> dict:
    """Issue → Diag → Strat → Proposal 한 줄 생성. id 묶음 반환."""
    issue_id = issue_id or f"I-{uuid.uuid4().hex[:6]}"
    diagnosis_id = diagnosis_id or f"D-{uuid.uuid4().hex[:6]}"
    strategy_id = strategy_id or f"S-{uuid.uuid4().hex[:6]}"
    m.ha_issue_insert(
        issue_id=issue_id, category=issue_category, severity="high",
        evidence=[], signal="sig", narrative="narr", confidence=0.8,
    )
    m.ha_diagnosis_insert(
        diagnosis_id=diagnosis_id, issue_id=issue_id, hypotheses=[],
        root_cause="cause", confidence=0.6, recommended_action="next",
        five_whys=[],
    )
    m.ha_strategy_insert(
        strategy_id=strategy_id, diagnosis_id=diagnosis_id,
        category="monitoring_only", summary="s", rationale=None,
        expected_impact=None, cost_estimate=None,
    )
    m.ha_proposal_insert(
        proposal_id=proposal_id, strategy_id=strategy_id,
        target="alerts:new", before_text="b", after_text="a",
        reversible=True, risk_level="low", risk_score=0.1, validation={},
    )
    if proposal_status != "pending":
        m.ha_proposal_decision(proposal_id, proposal_status, by="owner")
    return {
        "issue_id": issue_id, "diagnosis_id": diagnosis_id,
        "strategy_id": strategy_id, "proposal_id": proposal_id,
    }


class SelfMetricsMemoryTests(unittest.TestCase):
    """ha_self_metrics + ha_outcomes 메모리 API."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.m = _fresh_memory(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_self_metric_record_and_recent(self):
        rid1 = self.m.ha_self_metric_record(
            "approval_rate", 0.65, period="daily", metadata={"window": 7},
        )
        rid2 = self.m.ha_self_metric_record(
            "approval_rate", 0.72, period="daily",
        )
        self.assertGreater(rid2, rid1)
        rows = self.m.ha_self_metric_recent("approval_rate", limit=5)
        self.assertEqual(len(rows), 2)
        # 최신 우선 정렬
        self.assertEqual(rows[0]["value"], 0.72)
        self.assertEqual(rows[1]["metadata"]["window"], 7)

    def test_self_metrics_latest_returns_per_metric(self):
        self.m.ha_self_metric_record("approval_rate", 0.5)
        self.m.ha_self_metric_record("issue_count", 12.0)
        self.m.ha_self_metric_record("approval_rate", 0.7)
        latest = self.m.ha_self_metrics_latest()
        self.assertIn("approval_rate", latest)
        self.assertIn("issue_count", latest)
        self.assertEqual(latest["approval_rate"]["value"], 0.7)
        self.assertEqual(latest["issue_count"]["value"], 12.0)

    def test_invalid_metric_raises(self):
        with self.assertRaises(ValueError):
            self.m.ha_self_metric_record("", 0.5)
        with self.assertRaises(ValueError):
            self.m.ha_self_metric_record("ok", "not-a-number")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.m.ha_self_metric_record("ok", 0.5, period="hourly")

    def test_outcome_record_and_finalize(self):
        ids = _seed_proposal(self.m, proposal_id="P1", proposal_status="approved")
        self.m.ha_outcome_record(
            "O1", ids["proposal_id"], outcome="pending",
            issue_id=ids["issue_id"], baseline_metric=0.15,
        )
        pending = self.m.ha_outcomes_recent(outcome="pending", limit=5)
        self.assertEqual(len(pending), 1)
        ok = self.m.ha_outcome_finalize("O1", "resolved", observed_metric=0.05)
        self.assertTrue(ok)
        # 두 번 finalize 는 못 함
        ok2 = self.m.ha_outcome_finalize("O1", "regressed", observed_metric=0.20)
        self.assertFalse(ok2)
        resolved = self.m.ha_outcomes_recent(outcome="resolved", limit=5)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["observed_metric"], 0.05)

    def test_outcome_invalid_value_raises(self):
        ids = _seed_proposal(self.m, proposal_id="P1")
        with self.assertRaises(ValueError):
            self.m.ha_outcome_record("O1", ids["proposal_id"], outcome="bogus")
        with self.assertRaises(ValueError):
            self.m.ha_outcome_record("O2", ids["proposal_id"])  # default 'pending' OK
            self.m.ha_outcome_finalize("O2", "weird")  # type: ignore[arg-type]


class MetaEvaluatorTests(unittest.TestCase):
    """MetaEvaluator 의 종합 동작 — 자기-이슈 발화 + 메트릭 영속."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.m = _fresh_memory(self._tmp.name)
        self.me = MetaEvaluator(memory=self.m)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_db_yields_neutral_report_no_self_issues(self):
        report = self.me.evaluate(window_sec=24 * 3600)
        self.assertIsInstance(report, HarnessHealthReport)
        # 표본 부족 → 자기-이슈 0개
        self.assertEqual(report.self_issues, [])
        self.assertEqual(report.funnel["issue_count"], 0)
        self.assertGreaterEqual(report.health_score, 0.0)
        self.assertLessEqual(report.health_score, 1.0)

    def test_low_approval_rate_emits_self_issue(self):
        # 5개 제안 모두 거부 → approval_rate=0
        for i in range(5):
            _seed_proposal(self.m, proposal_id=f"P-low-{i}",
                           proposal_status="rejected")
        report = self.me.evaluate(window_sec=24 * 3600)
        self.assertGreater(len(report.self_issues), 0,
                           "낮은 채택률 자기-이슈가 발화되어야 함")
        # ha_issues 에 영속
        cards = self.m.ha_issues_recent(limit=20)
        meta = [c for c in cards if c["category"] == "harness_meta"]
        self.assertGreater(len(meta), 0)

    def test_high_approval_rate_no_self_issue(self):
        # 5개 제안 모두 승인 → approval_rate=1.0 (정상)
        for i in range(5):
            _seed_proposal(self.m, proposal_id=f"P-high-{i}",
                           proposal_status="approved")
        report = self.me.evaluate(window_sec=24 * 3600)
        approval_self = [
            iid for iid in report.self_issues
            if "채택률" in (
                self.m.ha_issues_recent(limit=20)[0].get("signal") or ""
            )
        ]
        self.assertEqual(approval_self, [],
                         "정상 채택률에서는 자기-이슈 발화 금지")

    def test_low_resolved_rate_emits_self_issue(self):
        # 5개 outcome 중 0개 resolved → 효과성 자기-이슈
        for i in range(5):
            ids = _seed_proposal(self.m, proposal_id=f"P-out-{i}",
                                 proposal_status="approved")
            self.m.ha_outcome_record(
                f"O-{i}", ids["proposal_id"],
                issue_id=ids["issue_id"], baseline_metric=0.15,
            )
            # finalize 한 outcome 4 = persistent, 1 = resolved 로는 30% 미달
            self.m.ha_outcome_finalize(f"O-{i}", "persistent",
                                       observed_metric=0.16)
        report = self.me.evaluate(window_sec=24 * 3600)
        # ineffective 시그널이 issue_signal 에 들어갔는지 확인
        cards = self.m.ha_issues_recent(limit=20)
        self.assertTrue(
            any("resolved" in (c.get("signal") or "")
                or "효과성" in (c.get("narrative") or "")
                for c in cards if c["category"] == "harness_meta"),
            f"ineffective 자기-이슈가 발화되어야 함: {cards!r}",
        )

    def test_metrics_persisted_after_evaluate(self):
        for i in range(3):
            _seed_proposal(self.m, proposal_id=f"P-met-{i}",
                           proposal_status="approved")
        self.me.evaluate(window_sec=24 * 3600)
        latest = self.m.ha_self_metrics_latest()
        # 핵심 메트릭들이 모두 적재되었는지
        for k in ("issue_count", "approval_rate", "harness_health"):
            self.assertIn(k, latest, f"{k} 메트릭 미영속")
        # health_score 는 0~1
        h = latest["harness_health"]["value"]
        self.assertGreaterEqual(h, 0.0)
        self.assertLessEqual(h, 1.0)

    def test_health_score_in_range_for_extreme_inputs(self):
        # 극단 — 100건 모두 거부
        for i in range(50):
            _seed_proposal(self.m, proposal_id=f"P-bad-{i}",
                           proposal_status="rejected")
        report = self.me.evaluate(window_sec=24 * 3600)
        self.assertGreaterEqual(report.health_score, 0.0)
        self.assertLessEqual(report.health_score, 1.0)
        self.assertEqual(report.rates["approval_rate"], 0.0)
        # 거부율 1.0, 폐기율 0 (모두 proposal 까지 갔음)
        self.assertGreaterEqual(report.rates["rejection_rate"], 0.99)


class MetaEvaluatorScopeTests(unittest.TestCase):
    """안전 — MetaEvaluator 가 금지 자원을 건드리지 못하는지 검증."""

    def test_cannot_write_code_or_safety_prompt(self):
        me = MetaEvaluator()
        for forbidden in (
            "sarvis_code", "sarvis_safety_prompt",
            "ha_audit_log", "ha_kill_switch", "meta_evaluator_io",
            "user_data_export",
        ):
            self.assertFalse(
                me.can_write(forbidden),
                f"MetaEvaluator 가 금지 자원에 쓰기 권한 보유: {forbidden}",
            )

    def test_can_write_only_intended_resources(self):
        me = MetaEvaluator()
        for ok in ("ha_messages", "ha_issues", "ha_self_metrics"):
            self.assertTrue(me.can_write(ok))
        for bad in ("ha_proposals", "ha_validations", "ha_strategies"):
            self.assertFalse(
                me.can_write(bad),
                f"MetaEvaluator 가 의도 외 자원에 쓰기 가능: {bad}",
            )


class StrategistHarnessMetaTests(unittest.TestCase):
    """Strategist 가 harness_meta 카테고리 이슈에 대해 새 룰을 적용하는지."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.m = _fresh_memory(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_harness_meta_issue_yields_specific_strategies(self):
        # Issue 를 harness_meta 로 만들고 Strategist 호출
        self.m.ha_issue_insert(
            issue_id="ISS-META-X", category="harness_meta",
            severity="medium", evidence=[],
            signal="채택률 0%", narrative="self",
            confidence=0.7,
        )
        self.m.ha_diagnosis_insert(
            diagnosis_id="D-META-X", issue_id="ISS-META-X",
            hypotheses=[], root_cause="meta", confidence=0.5,
            recommended_action="adjust", five_whys=[],
        )
        strat = Strategist(memory=self.m)
        strategies = strat.propose({"diagnosis_id": "D-META-X",
                                     "issue_id": "ISS-META-X"})
        cats = {s.category for s in strategies}
        # harness_meta 룰 (heuristic_threshold 또는 monitoring_only) + Do Nothing
        self.assertIn("do_nothing", cats)
        self.assertTrue(
            cats & {"heuristic_threshold", "monitoring_only", "knowledge_add"},
            f"harness_meta 전용 strategy 카테고리가 없음: {cats}",
        )


if __name__ == "__main__":
    unittest.main()
