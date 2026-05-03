"""사이클 #25 (HA Stage S3) — Strategist + Improver + Validator 단위 테스트."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sarvis.memory import Memory, _conn_ctx  # noqa: E402
from sarvis.ha import (  # noqa: E402
    Diagnostician, Strategist, Improver, Validator,
    KillSwitchActivated, activate_kill_switch, deactivate_kill_switch,
    STRATEGY_CATEGORIES,
)
from sarvis.ha.safety import KILL_SWITCH_FILE  # noqa: E402


def _seed_diag(mem: Memory, category: str = "spike",
               issue_id: str = "ISS-S3-001") -> str:
    mem.ha_issue_insert(
        issue_id=issue_id, category=category, severity="high",
        evidence=[1], signal="sig", narrative="n", confidence=0.7,
    )
    diag = Diagnostician(memory=mem)
    issue = next(i for i in mem.ha_issues_open(limit=10)
                 if i["issue_id"] == issue_id)
    return diag.diagnose(issue).diagnosis_id


class StrategistTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ha_s3_")
        self.mem = Memory(path=str(Path(self._tmp) / "mem.db"))
        os.environ.pop("SARVIS_HA_KILL_SWITCH", None)
        if KILL_SWITCH_FILE.is_file():
            KILL_SWITCH_FILE.unlink()

    def tearDown(self):
        os.environ.pop("SARVIS_HA_KILL_SWITCH", None)
        if KILL_SWITCH_FILE.is_file():
            KILL_SWITCH_FILE.unlink()

    def test_propose_includes_do_nothing_strategy(self):
        diag_id = _seed_diag(self.mem, "spike")
        diag = self.mem.ha_diagnoses_for_issue("ISS-S3-001")[0]
        strats = Strategist(memory=self.mem).propose(diag)
        cats = [s.category for s in strats]
        self.assertIn("do_nothing", cats)
        self.assertEqual(cats[-1], "do_nothing")

    def test_propose_persists_and_emits(self):
        diag_id = _seed_diag(self.mem, "drift")
        diag = self.mem.ha_diagnoses_for_issue("ISS-S3-001")[0]
        Strategist(memory=self.mem).propose(diag)
        rows = self.mem.ha_strategies_for_diagnosis(diag_id)
        self.assertGreaterEqual(len(rows), 2)
        msgs = [m for m in self.mem.ha_messages_recent(limit=50)
                if m["from_agent"] == "Strategist"]
        self.assertTrue(msgs)
        self.assertEqual(msgs[0]["to_agent"], "Improver")

    def test_unknown_category_uses_fallback(self):
        diag_id = _seed_diag(self.mem, "__nope__")
        diag = self.mem.ha_diagnoses_for_issue("ISS-S3-001")[0]
        strats = Strategist(memory=self.mem).propose(diag)
        # fallback 1 + do_nothing = 2
        self.assertEqual(len(strats), 2)
        self.assertEqual(strats[-1].category, "do_nothing")

    def test_run_recent_processes_diagnoses(self):
        _seed_diag(self.mem, "spike", "ISS-A")
        _seed_diag(self.mem, "drift", "ISS-B")
        out = Strategist(memory=self.mem).run_recent(limit=10)
        self.assertGreater(len(out), 4)

    def test_strategy_categories_subset(self):
        diag_id = _seed_diag(self.mem, "spike")
        diag = self.mem.ha_diagnoses_for_issue("ISS-S3-001")[0]
        for s in Strategist(memory=self.mem).propose(diag):
            self.assertIn(s.category, STRATEGY_CATEGORIES)

    def test_strategist_forbidden_writes_blocked(self):
        s = Strategist(memory=self.mem)
        for f in ("sarvis_code", "sarvis_safety_prompt", "ha_kill_switch"):
            self.assertFalse(s.can_write(f))


class ImproverTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ha_s3i_")
        self.mem = Memory(path=str(Path(self._tmp) / "mem.db"))

    def test_materialize_creates_pending_proposal(self):
        diag_id = _seed_diag(self.mem, "spike")
        diag = self.mem.ha_diagnoses_for_issue("ISS-S3-001")[0]
        strats = Strategist(memory=self.mem).propose(diag)
        spec = Improver(memory=self.mem).materialize(
            {"strategy_id": strats[0].strategy_id,
             "category": strats[0].category,
             "summary": strats[0].summary,
             "rationale": strats[0].rationale,
             "expected_impact": strats[0].expected_impact}
        )
        self.assertTrue(spec.proposal_id.startswith("PROP-"))
        rows = self.mem.ha_proposals_list(status="pending")
        self.assertTrue(any(r["proposal_id"] == spec.proposal_id for r in rows))

    def test_materialize_emits_to_validator(self):
        diag_id = _seed_diag(self.mem, "drift")
        diag = self.mem.ha_diagnoses_for_issue("ISS-S3-001")[0]
        strats = Strategist(memory=self.mem).propose(diag)
        Improver(memory=self.mem).materialize(
            {"strategy_id": strats[0].strategy_id,
             "category": strats[0].category, "summary": strats[0].summary}
        )
        msgs = [m for m in self.mem.ha_messages_recent(limit=50)
                if m["from_agent"] == "Improver"]
        self.assertTrue(msgs)
        self.assertEqual(msgs[0]["to_agent"], "Validator")

    def test_run_recent_skips_existing_proposals(self):
        _seed_diag(self.mem, "spike")
        diag = self.mem.ha_diagnoses_for_issue("ISS-S3-001")[0]
        Strategist(memory=self.mem).propose(diag)
        imp = Improver(memory=self.mem)
        first = imp.run_recent(limit=50)
        second = imp.run_recent(limit=50)
        self.assertGreater(len(first), 0)
        self.assertEqual(len(second), 0)  # 중복 방지

    def test_improver_forbidden_writes_blocked(self):
        i = Improver(memory=self.mem)
        for f in ("sarvis_code", "sarvis_safety_prompt", "user_data_export"):
            self.assertFalse(i.can_write(f))

    def test_proposal_decision_pending_to_approved(self):
        diag_id = _seed_diag(self.mem, "spike")
        diag = self.mem.ha_diagnoses_for_issue("ISS-S3-001")[0]
        strats = Strategist(memory=self.mem).propose(diag)
        spec = Improver(memory=self.mem).materialize({
            "strategy_id": strats[0].strategy_id,
            "category": strats[0].category, "summary": strats[0].summary,
        })
        ok = self.mem.ha_proposal_decision(
            spec.proposal_id, "approved", by="owner",
        )
        self.assertTrue(ok)
        # 재승인은 idempotent — pending 이 아니므로 False
        again = self.mem.ha_proposal_decision(
            spec.proposal_id, "approved", by="owner",
        )
        self.assertFalse(again)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ha_s3v_")
        self.mem = Memory(path=str(Path(self._tmp) / "mem.db"))

    def _seed_proposal(self, target: str = "alerts:new",
                       reversible: bool = True) -> str:
        diag_id = _seed_diag(self.mem, "spike")
        diag = self.mem.ha_diagnoses_for_issue("ISS-S3-001")[0]
        strats = Strategist(memory=self.mem).propose(diag)
        spec = Improver(memory=self.mem).materialize({
            "strategy_id": strats[0].strategy_id,
            "category": strats[0].category, "summary": strats[0].summary,
        })
        return spec.proposal_id

    def test_low_risk_for_alerts_only(self):
        pid = self._seed_proposal()
        prop = next(p for p in self.mem.ha_proposals_list()
                    if p["proposal_id"] == pid)
        # alerts:new 가 아닌 monitoring_only(=alerts:new) 인지 확인
        rep = Validator(memory=self.mem).evaluate(prop)
        self.assertIn(rep.risk_level, ("low", "med"))
        self.assertTrue(rep.auto_approval_blocked)
        self.assertGreater(len(rep.checks), 1)

    def test_high_risk_for_irreversible_prompt_change(self):
        prop = {"proposal_id": "PROP-X", "target": "prompt:base.system",
                "reversible": False, "validation": {}}
        rep = Validator(memory=self.mem).evaluate(prop)
        self.assertGreaterEqual(rep.risk_score, 0.67)
        self.assertEqual(rep.risk_level, "high")

    def test_run_pending_skips_already_validated(self):
        pid = self._seed_proposal()
        v = Validator(memory=self.mem)
        first = v.run_pending(limit=50)
        second = v.run_pending(limit=50)
        self.assertGreater(len(first), 0)
        self.assertEqual(len(second), 0)

    def test_auto_approval_always_blocked_in_l1(self):
        pid = self._seed_proposal()
        prop = next(p for p in self.mem.ha_proposals_list()
                    if p["proposal_id"] == pid)
        rep = Validator(memory=self.mem).evaluate(prop)
        self.assertTrue(rep.auto_approval_blocked)
        # DB 영속도 동일
        vs = self.mem.ha_validations_for_proposal(pid)
        self.assertTrue(vs[0]["auto_approval_blocked"])


class S3MemoryGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ha_s3m_")
        self.mem = Memory(path=str(Path(self._tmp) / "mem.db"))

    def test_strategies_append_only_db_trigger(self):
        diag_id = _seed_diag(self.mem, "spike")
        diag = self.mem.ha_diagnoses_for_issue("ISS-S3-001")[0]
        Strategist(memory=self.mem).propose(diag)
        with _conn_ctx(self.mem.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE ha_strategies SET summary='x'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM ha_strategies")

    def test_validations_append_only_db_trigger(self):
        diag_id = _seed_diag(self.mem, "spike")
        diag = self.mem.ha_diagnoses_for_issue("ISS-S3-001")[0]
        strats = Strategist(memory=self.mem).propose(diag)
        Improver(memory=self.mem).materialize({
            "strategy_id": strats[0].strategy_id,
            "category": strats[0].category, "summary": strats[0].summary,
        })
        Validator(memory=self.mem).run_pending(limit=10)
        with _conn_ctx(self.mem.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE ha_validations SET risk_level='high'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM ha_validations")

    def test_proposal_invalid_decision_rejected(self):
        with self.assertRaises(ValueError):
            self.mem.ha_proposal_decision("X", "garbage")

    def test_proposals_list_invalid_status(self):
        with self.assertRaises(ValueError):
            self.mem.ha_proposals_list(status="garbage")

    def test_kill_switch_blocks_strategist(self):
        diag_id = _seed_diag(self.mem, "spike")
        try:
            activate_kill_switch(by="owner", reason="t")
            with self.assertRaises(KillSwitchActivated):
                Strategist(memory=self.mem).run_recent(limit=5)
        finally:
            deactivate_kill_switch(by="owner")


if __name__ == "__main__":
    unittest.main()
