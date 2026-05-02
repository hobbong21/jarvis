"""사이클 #9 — harness_actions 회귀 테스트.

다음을 검증한다:
  * 카탈로그 기본값/메타 무결성
  * bounds 클램프 (out-of-range 값은 자동 corrals)
  * apply → revert 1단계 왕복 + 두 번째 revert 는 None
  * tts_rate 파서/포매터 형식 ('+5%' / '-10%')
  * 감사 로그 (apply/revert 모두 기록)
  * recommend_actions: 음성 빈전사율 / TTS 차단율 / 응답 latency 임계 트리거
  * 표본 부족(score=None)에서는 권장 안 함
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sarvis import harness_actions as ha
from sarvis.config import cfg


class _IsolatedAudit:
    """AUDIT_PATH 를 임시 파일로 우회 + 카탈로그/cfg 원복."""

    def __init__(self):
        self._orig_path = ha.AUDIT_PATH
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        self._tmp.close()
        # cfg 의 변경 가능 항목 원복용 스냅샷
        self._snap = {
            "silence_threshold": cfg.silence_threshold,
            "silence_duration": cfg.silence_duration,
            "max_recording": cfg.max_recording,
            "tts_rate": cfg.tts_rate,
        }

    def __enter__(self):
        ha.AUDIT_PATH = Path(self._tmp.name)
        ha.reset_catalog_for_tests()
        return self

    def __exit__(self, *exc):
        ha.AUDIT_PATH = self._orig_path
        for k, v in self._snap.items():
            setattr(cfg, k, v)
        ha.reset_catalog_for_tests()
        try:
            Path(self._tmp.name).unlink()
        except OSError:
            pass


class CatalogTests(unittest.TestCase):

    def test_catalog_has_required_actions(self):
        with _IsolatedAudit():
            names = {a["name"] for a in ha.list_actions()}
            self.assertEqual(
                names,
                {"silence_threshold", "silence_duration", "max_recording", "tts_rate"},
            )

    def test_each_action_has_meta(self):
        with _IsolatedAudit():
            for a in ha.list_actions():
                for k in ("name", "label", "category", "bounds", "current",
                          "current_value", "can_revert", "description"):
                    self.assertIn(k, a, f"missing {k} in {a}")
                self.assertEqual(len(a["bounds"]), 2)
                self.assertFalse(a["can_revert"])  # 적용 전이므로

    def test_unknown_action_raises(self):
        with _IsolatedAudit():
            with self.assertRaises(KeyError):
                ha.apply_action("does_not_exist", 1.0)


class BoundsClampTests(unittest.TestCase):

    def test_silence_threshold_upper_clamp(self):
        with _IsolatedAudit():
            ha.apply_action("silence_threshold", 999.0)
            self.assertAlmostEqual(cfg.silence_threshold, 0.030, places=5)

    def test_silence_threshold_lower_clamp(self):
        with _IsolatedAudit():
            ha.apply_action("silence_threshold", -1.0)
            self.assertAlmostEqual(cfg.silence_threshold, 0.005, places=5)

    def test_max_recording_clamps(self):
        with _IsolatedAudit():
            ha.apply_action("max_recording", 1.0)
            self.assertEqual(cfg.max_recording, 5.0)
            ha.apply_action("max_recording", 999.0)
            self.assertEqual(cfg.max_recording, 30.0)


class ApplyRevertTests(unittest.TestCase):

    def test_apply_then_revert_round_trip(self):
        with _IsolatedAudit():
            orig = cfg.silence_duration
            ha.apply_action("silence_duration", 2.2)
            self.assertEqual(cfg.silence_duration, 2.2)
            entry = ha.revert_action("silence_duration")
            self.assertIsNotNone(entry)
            self.assertEqual(entry["op"], "revert")
            self.assertEqual(cfg.silence_duration, orig)

    def test_double_revert_returns_none(self):
        with _IsolatedAudit():
            ha.apply_action("silence_duration", 2.2)
            ha.revert_action("silence_duration")
            self.assertIsNone(ha.revert_action("silence_duration"))

    def test_can_revert_flag_toggles(self):
        with _IsolatedAudit():
            ha.apply_action("silence_duration", 1.9)
            d = next(a for a in ha.list_actions() if a["name"] == "silence_duration")
            self.assertTrue(d["can_revert"])
            ha.revert_action("silence_duration")
            d = next(a for a in ha.list_actions() if a["name"] == "silence_duration")
            self.assertFalse(d["can_revert"])


class TtsRateFormatTests(unittest.TestCase):

    def test_parse_signed_percent(self):
        self.assertEqual(ha._parse_tts_rate("+5%"), 5.0)
        self.assertEqual(ha._parse_tts_rate("-10%"), -10.0)
        self.assertEqual(ha._parse_tts_rate("0"), 0.0)

    def test_format_keeps_sign(self):
        self.assertEqual(ha._format_tts_rate(5.0), "+5%")
        self.assertEqual(ha._format_tts_rate(-10.0), "-10%")
        self.assertEqual(ha._format_tts_rate(0.0), "+0%")

    def test_apply_writes_edge_tts_format_to_cfg(self):
        with _IsolatedAudit():
            ha.apply_action("tts_rate", 12.0)
            self.assertEqual(cfg.tts_rate, "+12%")
            ha.apply_action("tts_rate", "-7%")
            self.assertEqual(cfg.tts_rate, "-7%")


class AuditLogTests(unittest.TestCase):

    def test_apply_writes_audit_line(self):
        with _IsolatedAudit() as ctx:
            ha.apply_action("silence_duration", 1.7, source="dashboard")
            lines = Path(ctx._tmp.name).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["name"], "silence_duration")
            self.assertEqual(entry["source"], "dashboard")
            self.assertEqual(entry["op"], "apply")

    def test_revert_writes_audit_line(self):
        with _IsolatedAudit() as ctx:
            ha.apply_action("silence_duration", 1.7)
            ha.revert_action("silence_duration")
            lines = Path(ctx._tmp.name).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[1])["op"], "revert")

    def test_recent_audit_returns_last_n(self):
        with _IsolatedAudit():
            for v in (1.0, 1.2, 1.4, 1.6, 1.8):
                ha.apply_action("silence_duration", v)
            recent = ha.recent_audit(3)
            self.assertEqual(len(recent), 3)


class RecommendTests(unittest.TestCase):

    def _summary(self, voice=None, action=None):
        s = {
            "pillars": {
                "voice": voice or {"score": None, "metrics": {}, "notes": []},
                "vision": {"score": None, "metrics": {}, "notes": []},
                "action": action or {"score": None, "metrics": {}, "notes": []},
            }
        }
        return s

    def test_no_recs_when_score_none(self):
        recs = ha.recommend_actions(self._summary())
        self.assertEqual(recs, [])

    def test_high_empty_rate_recommends_lower_threshold(self):
        with _IsolatedAudit():
            cfg.silence_threshold = 0.020
            ha.reset_catalog_for_tests()
            s = self._summary(voice={
                "score": 50.0,
                "metrics": {"empty_transcription_rate": 0.30, "audio_turns": 6,
                            "tts_failure_rate": 0.0},
                "notes": [],
            })
            recs = ha.recommend_actions(s)
            names = {r["name"] for r in recs}
            self.assertIn("silence_threshold", names)
            r = next(x for x in recs if x["name"] == "silence_threshold")
            self.assertLess(r["suggested"], 0.020)
            self.assertGreaterEqual(r["suggested"], 0.005)

    def test_high_tts_fail_recommends_faster_rate(self):
        with _IsolatedAudit():
            cfg.tts_rate = "+0%"
            ha.reset_catalog_for_tests()
            s = self._summary(voice={
                "score": 60.0,
                "metrics": {"empty_transcription_rate": 0.0, "audio_turns": 6,
                            "tts_failure_rate": 0.20},
                "notes": [],
            })
            recs = ha.recommend_actions(s)
            r = next((x for x in recs if x["name"] == "tts_rate"), None)
            self.assertIsNotNone(r)
            self.assertEqual(r["suggested"], 5.0)
            self.assertEqual(r["suggested_str"], "+5%")

    def test_slow_total_ms_recommends_shorter_max_recording(self):
        with _IsolatedAudit():
            cfg.max_recording = 15.0
            ha.reset_catalog_for_tests()
            s = self._summary(action={
                "score": 30.0,
                "metrics": {"p50_total_ms": 6500.0},
                "notes": [],
            })
            recs = ha.recommend_actions(s)
            r = next((x for x in recs if x["name"] == "max_recording"), None)
            self.assertIsNotNone(r)
            self.assertEqual(r["suggested"], 13.0)


class ConcurrencyTests(unittest.TestCase):
    """사이클 #9 P1#3 — apply/revert 동시성: _previous 가 깨지지 않아야 한다."""

    def test_concurrent_apply_keeps_invariants(self):
        import threading
        with _IsolatedAudit():
            errors: list = []

            def worker(v: float):
                try:
                    ha.apply_action("silence_duration", v, source="t")
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(1.0 + i * 0.1,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            a = ha.get_action("silence_duration")
            self.assertTrue(a._has_previous)
            # 현재값과 _previous 가 모두 bounds 안에 있고 서로 다를 수도/같을 수도 있음.
            lo, hi = a.bounds
            self.assertTrue(lo <= a.current_value() <= hi)
            self.assertTrue(lo <= a._previous <= hi)

    def test_concurrent_revert_only_one_succeeds(self):
        import threading
        with _IsolatedAudit():
            ha.apply_action("silence_duration", 2.0, source="t")
            results: list = []

            def worker():
                results.append(ha.revert_action("silence_duration", source="t"))

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            successes = [r for r in results if r is not None]
            self.assertEqual(len(successes), 1, f"정확히 한 개만 성공해야 함: {results}")
            self.assertFalse(ha.get_action("silence_duration")._has_previous)


class HarnessActionsRouteAstTests(unittest.TestCase):
    """T003 회귀 — server.py 가 4개 액션 REST 핸들러를 보유하는지 AST 검증."""

    def test_server_has_action_routes(self):
        import ast
        src = Path(__file__).resolve().parent.parent / "sarvis" / "server.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        routes: set = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for dec in node.decorator_list:
                # 형식: @app.get("/path") / @app.post("/path")
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    method = dec.func.attr
                    if method in ("get", "post") and dec.args:
                        a0 = dec.args[0]
                        if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                            routes.add(f"{method.upper()} {a0.value}")
        self.assertIn("GET /api/harness/actions", routes)
        self.assertIn("POST /api/harness/actions/apply", routes)
        self.assertIn("POST /api/harness/actions/revert", routes)
        self.assertIn("GET /api/harness/actions/audit", routes)

    def test_vision_tool_whitelist_includes_identify_person(self):
        """P1#1 회귀 — _on_tool_event 의 vision tool 집합이 identify_person 포함."""
        src = (Path(__file__).resolve().parent.parent / "sarvis" / "server.py").read_text(encoding="utf-8")
        # 단순 구문 스캔: 한 줄에 세 도구가 모두 있으면 OK.
        line_ok = any(
            ("see" in ln and "observe_action" in ln and "identify_person" in ln
             and "_turn_vision_used" in ln_next)
            for ln, ln_next in zip(src.splitlines(), src.splitlines()[1:])
        )
        self.assertTrue(
            line_ok,
            "server.py 의 vision tool whitelist 에 identify_person 이 포함돼야 합니다.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
