"""사이클 #23 (HA Stage S1) — Observer/Reporter/안전 가드 테스트.

기획서 보조문서 §4.1, §8, §11 검증.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sarvis.memory import Memory  # noqa: E402
from sarvis.ha import (  # noqa: E402
    Observer, Reporter, HAMessage, HAAgent,
    is_kill_switch_on, activate_kill_switch, deactivate_kill_switch,
    KillSwitchActivated, sign_payload,
)
from sarvis.ha.safety import mask_pii, KILL_SWITCH_FILE  # noqa: E402
from sarvis.ha.base import verify_signature, AGENT_NAMES  # noqa: E402


def _fresh_memory(tmpdir: str) -> Memory:
    return Memory(path=str(Path(tmpdir) / "mem.db"))


class HABaseTests(unittest.TestCase):
    def test_message_signature_roundtrip(self):
        payload = {"a": 1, "b": "hello", "confidence": 0.7}
        sig = sign_payload(payload)
        self.assertTrue(verify_signature(payload, sig))
        self.assertFalse(verify_signature({**payload, "a": 2}, sig))

    def test_message_validates_agents_and_confidence(self):
        with self.assertRaises(ValueError):
            HAMessage(from_agent="Observer", to_agent="Observer", payload={})
        with self.assertRaises(ValueError):
            HAMessage(from_agent="UnknownX", to_agent="Reporter", payload={})
        with self.assertRaises(ValueError):
            HAMessage(from_agent="Observer", to_agent="Reporter",
                      payload={"confidence": 1.5})
        m = HAMessage(from_agent="Observer", to_agent="Reporter",
                      payload={"confidence": 0.5})
        self.assertTrue(m.verify())

    def test_agent_forbidden_write_scope_blocked(self):
        class Bad(HAAgent):
            name = "Observer"
            write_scope = {"sarvis_code"}
        with self.assertRaises(RuntimeError):
            Bad()


class HASafetyTests(unittest.TestCase):
    def setUp(self):
        # 환경/파일 양 경로 모두 reset
        os.environ.pop("SARVIS_HA_KILL_SWITCH", None)
        if KILL_SWITCH_FILE.is_file():
            KILL_SWITCH_FILE.unlink()

    def tearDown(self):
        os.environ.pop("SARVIS_HA_KILL_SWITCH", None)
        if KILL_SWITCH_FILE.is_file():
            KILL_SWITCH_FILE.unlink()

    def test_kill_switch_off_by_default(self):
        self.assertFalse(is_kill_switch_on())

    def test_kill_switch_via_env(self):
        os.environ["SARVIS_HA_KILL_SWITCH"] = "1"
        self.assertTrue(is_kill_switch_on())

    def test_kill_switch_via_file(self):
        activate_kill_switch(by="owner", reason="test")
        self.assertTrue(is_kill_switch_on())
        deactivate_kill_switch(by="owner")
        self.assertFalse(is_kill_switch_on())

    def test_kill_switch_corrupt_file_safe_on(self):
        KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_SWITCH_FILE.write_text("not-json{", encoding="utf-8")
        # 손상 시 안전측 = ON
        self.assertTrue(is_kill_switch_on())

    def test_pii_masking(self):
        text = "연락 me@x.com 010-1234-5678 sk-abcdefghijklmnop1234"
        out = mask_pii(text)
        self.assertNotIn("me@x.com", out)
        self.assertNotIn("010-1234-5678", out)
        self.assertNotIn("sk-abcdefghijklmnop1234", out)
        self.assertIn("[EMAIL]", out)
        self.assertIn("[PHONE]", out)


class ObserverHeuristicsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ha_obs_")
        self.mem = _fresh_memory(self._tmp)
        os.environ.pop("SARVIS_HA_KILL_SWITCH", None)
        if KILL_SWITCH_FILE.is_file():
            KILL_SWITCH_FILE.unlink()

    def _seed_commands(self, n: int, status: str = "done", user_id: str = "owner"):
        ids = []
        for i in range(n):
            cid = self.mem.log_command(
                user_id=user_id, command_text=f"cmd-{i}",
                kind="text", status=status,
            )
            self.mem.update_command(cid, response_text=f"resp-{i}", status=status)
            ids.append(cid)
        return ids

    def test_error_spike_detected(self):
        self._seed_commands(7, status="done")
        self._seed_commands(5, status="error")
        obs = Observer(memory=self.mem)
        cards = obs.scan(window_sec=24 * 3600.0, use_llm=False)
        spike = [c for c in cards if c.category == "spike"]
        self.assertTrue(spike, f"spike 카드가 생성되어야 함: {cards}")

    def test_silence_detected_with_no_traffic(self):
        obs = Observer(memory=self.mem)
        cards = obs.scan(window_sec=24 * 3600.0, use_llm=False)
        sil = [c for c in cards if c.category == "underutilization"]
        self.assertTrue(sil, "트래픽 없으면 silence 카드가 나와야 함")

    def test_negative_cluster_detected(self):
        ids = self._seed_commands(10)
        for cid in ids[:6]:
            self.mem.set_feedback(cid, "owner", -1, "별로")
        for cid in ids[6:]:
            self.mem.set_feedback(cid, "owner", 1, None)
        obs = Observer(memory=self.mem)
        cards = obs.scan(window_sec=24 * 3600.0, use_llm=False)
        neg = [c for c in cards if c.category == "anomaly"]
        self.assertTrue(neg, f"부정 클러스터 카드 필요: {cards}")

    def test_optout_excludes_user_from_observer_input(self):
        self._seed_commands(8, status="error", user_id="owner")
        # 옵트아웃 상태 → Observer 입력에서 제외 → spike 사라짐
        self.mem.ha_optout_set("owner", True)
        obs = Observer(memory=self.mem)
        cards = obs.scan(window_sec=24 * 3600.0, use_llm=False)
        spike = [c for c in cards if c.category == "spike"]
        self.assertFalse(spike, f"옵트아웃 사용자는 분석 제외: {cards}")
        # 해제하면 다시 검출
        self.mem.ha_optout_set("owner", False)
        cards2 = obs.scan(window_sec=24 * 3600.0, use_llm=False)
        spike2 = [c for c in cards2 if c.category == "spike"]
        self.assertTrue(spike2)

    def test_kill_switch_blocks_scan(self):
        self._seed_commands(7, status="error")
        try:
            activate_kill_switch(by="owner", reason="test")
            obs = Observer(memory=self.mem)
            with self.assertRaises(KillSwitchActivated):
                obs.scan(window_sec=24 * 3600.0, use_llm=False)
        finally:
            deactivate_kill_switch(by="owner")

    def test_ha_messages_are_append_only(self):
        # 같은 msg_id 두 번 INSERT 시 ValueError
        self.mem.ha_message_append(
            msg_id="dup-1", from_agent="Observer", to_agent="Reporter",
            payload={"x": 1}, signature="sig",
        )
        with self.assertRaises(ValueError):
            self.mem.ha_message_append(
                msg_id="dup-1", from_agent="Observer", to_agent="Reporter",
                payload={"x": 1}, signature="sig",
            )

    def test_observer_persists_issue_and_emits_message(self):
        self._seed_commands(7, status="error")
        obs = Observer(memory=self.mem)
        cards = obs.scan(window_sec=24 * 3600.0, use_llm=False)
        self.assertTrue(cards)
        issues = self.mem.ha_issues_recent(limit=20)
        self.assertEqual(len(issues), len(cards))
        msgs = self.mem.ha_messages_recent(limit=20)
        self.assertGreaterEqual(len(msgs), len(cards))
        # 모든 메시지 from=Observer, to=Reporter
        for m in msgs[:len(cards)]:
            self.assertEqual(m["from_agent"], "Observer")
            self.assertEqual(m["to_agent"], "Reporter")


class ReporterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ha_rep_")
        self.mem = _fresh_memory(self._tmp)
        os.environ.pop("SARVIS_HA_KILL_SWITCH", None)
        if KILL_SWITCH_FILE.is_file():
            KILL_SWITCH_FILE.unlink()
        # reports dir 격리
        self._reports = Path(self._tmp) / "reports"
        os.environ["SARVIS_HA_REPORTS_DIR"] = str(self._reports)
        # reload module to pick up env
        import importlib, sarvis.ha.reporter as rep_mod
        importlib.reload(rep_mod)
        from sarvis.ha.reporter import Reporter as _R
        self.Reporter = _R

    def tearDown(self):
        os.environ.pop("SARVIS_HA_REPORTS_DIR", None)

    def test_one_pager_written(self):
        rep = self.Reporter(memory=self.mem)
        issue = {
            "issue_id": "ISS-TEST-001",
            "category": "spike", "severity": "high",
            "evidence_traces": [1, 2, 3],
            "statistical_signal": "오류율 30%",
            "narrative_summary": "테스트 카드",
            "confidence": 0.8,
        }
        path = rep.write_one_pager(issue)
        self.assertTrue(path.is_file())
        body = path.read_text(encoding="utf-8")
        self.assertIn("HA Report", body)
        self.assertIn("ISS-TEST-001", body)
        self.assertIn("Stage S1", body)

    def test_growth_diary_returns_stage_info(self):
        rep = self.Reporter(memory=self.mem)
        d = rep.growth_diary(limit=5)
        self.assertIn("S3", d["stage"])
        self.assertIn("L1", d["autonomy_level"])
        self.assertEqual(d["active_agents"],
                         ["Observer", "Diagnostician", "Strategist",
                          "Improver", "Validator", "Reporter"])
        self.assertIsInstance(d["issues"], list)
        self.assertIsInstance(d["messages"], list)


if __name__ == "__main__":
    unittest.main()
