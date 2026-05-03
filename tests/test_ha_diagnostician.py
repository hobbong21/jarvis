"""사이클 #24 (HA Stage S2) — Diagnostician 단위 테스트."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sarvis.memory import Memory  # noqa: E402
from sarvis.ha import (  # noqa: E402
    Observer, Reporter, Diagnostician, KillSwitchActivated,
    activate_kill_switch, deactivate_kill_switch,
)
from sarvis.ha.safety import KILL_SWITCH_FILE  # noqa: E402


def _fresh_memory(tmp: str) -> Memory:
    return Memory(path=str(Path(tmp) / "mem.db"))


class DiagnosticianTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ha_diag_")
        self.mem = _fresh_memory(self._tmp)
        os.environ.pop("SARVIS_HA_KILL_SWITCH", None)
        if KILL_SWITCH_FILE.is_file():
            KILL_SWITCH_FILE.unlink()

    def tearDown(self):
        os.environ.pop("SARVIS_HA_KILL_SWITCH", None)
        if KILL_SWITCH_FILE.is_file():
            KILL_SWITCH_FILE.unlink()

    def _seed_issue(self, category: str = "spike", severity: str = "high",
                    issue_id: str = "ISS-T-001", confidence: float = 0.8):
        self.mem.ha_issue_insert(
            issue_id=issue_id, category=category, severity=severity,
            evidence=[1, 2], signal="테스트 신호",
            narrative="테스트 카드", confidence=confidence,
        )

    def test_diagnose_single_issue(self):
        self._seed_issue("spike")
        diag = Diagnostician(memory=self.mem)
        issue = self.mem.ha_issues_open(limit=5)[0]
        result = diag.diagnose(issue)
        self.assertIsNotNone(result.diagnosis_id)
        self.assertEqual(result.issue_id, "ISS-T-001")
        self.assertGreater(len(result.hypotheses), 0)
        self.assertIsNotNone(result.root_cause)
        # 가설은 사후 확률 내림차순
        posts = [h["posterior"] for h in result.hypotheses]
        self.assertEqual(posts, sorted(posts, reverse=True))

    def test_diagnose_persists_and_emits(self):
        self._seed_issue("anomaly")
        diag = Diagnostician(memory=self.mem)
        issue = self.mem.ha_issues_open(limit=5)[0]
        result = diag.diagnose(issue)
        # ha_diagnoses 영속
        diags = self.mem.ha_diagnoses_for_issue("ISS-T-001")
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0]["diagnosis_id"], result.diagnosis_id)
        # 메시지 emit
        msgs = self.mem.ha_messages_recent(limit=10)
        diag_msg = [m for m in msgs if m["from_agent"] == "Diagnostician"]
        self.assertTrue(diag_msg)
        self.assertEqual(diag_msg[0]["to_agent"], "Reporter")

    def test_diagnose_updates_status(self):
        self._seed_issue("drift")
        diag = Diagnostician(memory=self.mem)
        issue = self.mem.ha_issues_open(limit=5)[0]
        diag.diagnose(issue)
        # status='diagnosed' 로 갱신 → ha_issues_open 에서 제외
        self.assertEqual(self.mem.ha_issues_open(limit=5), [])

    def test_run_pending_diagnoses_all_open(self):
        self._seed_issue("spike", issue_id="ISS-A")
        self._seed_issue("drift", issue_id="ISS-B")
        self._seed_issue("cost", issue_id="ISS-C")
        diag = Diagnostician(memory=self.mem)
        results = diag.run_pending(limit=10)
        self.assertEqual(len(results), 3)
        self.assertEqual(self.mem.ha_issues_open(limit=10), [])

    def test_unknown_category_uses_fallback(self):
        self._seed_issue("__nope__")
        diag = Diagnostician(memory=self.mem)
        issue = self.mem.ha_issues_open(limit=5)[0]
        result = diag.diagnose(issue)
        self.assertGreater(len(result.hypotheses), 0)
        self.assertIn("미정의", result.hypotheses[0]["name"])

    def test_kill_switch_blocks_diagnose(self):
        self._seed_issue("spike")
        try:
            activate_kill_switch(by="owner", reason="test")
            diag = Diagnostician(memory=self.mem)
            with self.assertRaises(KillSwitchActivated):
                diag.run_pending(limit=5)
        finally:
            deactivate_kill_switch(by="owner")

    def test_diagnostician_forbidden_writes_blocked(self):
        # base.HAAgent._FORBIDDEN_WRITE 검증 — sarvis_code 등 우회 불가.
        diag = Diagnostician(memory=self.mem)
        for forbidden in ("sarvis_code", "sarvis_safety_prompt",
                          "ha_kill_switch", "user_data_export"):
            self.assertFalse(diag.can_write(forbidden))

    def test_observer_to_diagnostician_chain(self):
        # Observer 가 만든 카드를 Diagnostician 이 받아 진단까지 자동 연결.
        for i in range(8):
            cid = self.mem.log_command(
                user_id="owner", command_text=f"x{i}", kind="text",
                status="error",
            )
            self.mem.update_command(cid, status="error")
        obs = Observer(memory=self.mem)
        cards = obs.scan(window_sec=24 * 3600.0, use_llm=False)
        self.assertTrue(cards)
        diag = Diagnostician(memory=self.mem)
        results = diag.run_pending(limit=10)
        self.assertEqual(len(results), len(cards))
        # 각 진단은 자신의 issue_id 와 매칭
        ids_in = {c.issue_id for c in cards}
        ids_out = {r.issue_id for r in results}
        self.assertEqual(ids_in, ids_out)

    def test_diagnosis_id_unique_constraint(self):
        self._seed_issue("spike")
        self.mem.ha_diagnosis_insert(
            diagnosis_id="DUP-1", issue_id="ISS-T-001",
            hypotheses=[], root_cause="x", confidence=0.5,
            recommended_action=None,
        )
        with self.assertRaises(ValueError):
            self.mem.ha_diagnosis_insert(
                diagnosis_id="DUP-1", issue_id="ISS-T-001",
                hypotheses=[], root_cause="y", confidence=0.5,
                recommended_action=None,
            )

    def test_invalid_status_rejected(self):
        self._seed_issue("spike")
        with self.assertRaises(ValueError):
            self.mem.ha_issue_set_status("ISS-T-001", "garbage")

    def test_diagnosis_includes_five_whys(self):
        self._seed_issue("drift")
        diag = Diagnostician(memory=self.mem)
        issue = self.mem.ha_issues_open(limit=5)[0]
        result = diag.diagnose(issue)
        self.assertEqual(len(result.five_whys), 5)
        for w in result.five_whys:
            self.assertTrue(w.startswith("Why "))
        # 영속된 결과에서도 동일하게 노출
        d = self.mem.ha_diagnoses_for_issue("ISS-T-001")[0]
        self.assertEqual(len(d["five_whys"]), 5)
        # payload 에도 포함
        self.assertIn("five_whys", result.to_payload())

    def test_unknown_category_uses_fallback_five_whys(self):
        self._seed_issue("__nope__")
        diag = Diagnostician(memory=self.mem)
        result = diag.diagnose(self.mem.ha_issues_open(limit=5)[0])
        self.assertEqual(len(result.five_whys), 5)
        self.assertIn("정의되지 않았다", result.five_whys[0])

    def test_ha_messages_append_only_db_trigger(self):
        # DB 레벨 트리거: UPDATE/DELETE 모두 거부.
        import sqlite3
        from sarvis.memory import _conn_ctx
        self.mem.ha_message_append(
            msg_id="M-1", from_agent="Observer", to_agent="Reporter",
            payload={"x": 1}, signature="dummy-sig",
        )
        with _conn_ctx(self.mem.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE ha_messages SET to_agent='X' WHERE msg_id='M-1'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM ha_messages WHERE msg_id='M-1'")

    def test_ha_diagnoses_append_only_db_trigger(self):
        import sqlite3
        from sarvis.memory import _conn_ctx
        self._seed_issue("spike")
        Diagnostician(memory=self.mem).run_pending(limit=5)
        with _conn_ctx(self.mem.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE ha_diagnoses SET root_cause='x'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM ha_diagnoses")

    def test_reporter_includes_diagnosis_section(self):
        # Reporter One-Pager 가 diagnosis 첨부 시 가설 랭킹을 포함해야 함.
        self._seed_issue("spike")
        diag = Diagnostician(memory=self.mem)
        diag.run_pending(limit=5)
        # reports 디렉토리 격리
        rep_dir = Path(self._tmp) / "reports"
        os.environ["SARVIS_HA_REPORTS_DIR"] = str(rep_dir)
        import importlib, sarvis.ha.reporter as rep_mod
        importlib.reload(rep_mod)
        rep = rep_mod.Reporter(memory=self.mem)
        issue = self.mem.ha_issues_recent(limit=5)[0]
        path = rep.write_one_pager(issue)
        body = path.read_text(encoding="utf-8")
        self.assertIn("근본원인", body)
        self.assertIn("가설 랭킹", body)
        os.environ.pop("SARVIS_HA_REPORTS_DIR", None)


if __name__ == "__main__":
    unittest.main()
