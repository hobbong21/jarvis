"""사이클 #5 T002: 텔레메트리 회귀 테스트.

architect 사이클 #4 권장사항:
  - summarize() 빈/비빈 키셋 동등성 (P1 재발 방지)
  - log_turn → _notify 호출 보장
  - 콜백 예외 격리 (한 구독자 실패가 다른 구독자에 영향 없어야)

추가 보호:
  - PII 차단 키 (text/prompt/user_text/reply/body/history) 가 길이로만 보존
  - 백분위 정확성 (알려진 샘플)
  - latency 키셋 일관성

실행: python -m unittest tests.test_telemetry -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import telemetry  # noqa: E402


class _IsolatedLog:
    """LOG_PATH 를 임시 파일로 우회 + _subscribers 초기화하는 컨텍스트."""

    def __enter__(self):
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        self._tmp.close()
        self._orig_path = telemetry.LOG_PATH
        telemetry.LOG_PATH = Path(self._tmp.name)
        self._orig_subs = list(telemetry._subscribers)
        telemetry._subscribers.clear()
        return self

    def __exit__(self, *exc):
        telemetry.LOG_PATH = self._orig_path
        telemetry._subscribers.clear()
        telemetry._subscribers.extend(self._orig_subs)
        try:
            Path(self._tmp.name).unlink()
        except OSError:
            pass


class SummarizeKeySetTests(unittest.TestCase):
    """architect 사이클 #4 P1: 빈/비빈 경로 키셋이 동일해야 한다."""

    def test_empty_and_nonempty_keysets_identical(self):
        with _IsolatedLog():
            empty = telemetry.summarize()
            telemetry.log_turn({
                "backend": "openai", "intent": "chat",
                "fanout_ms": 10.0, "llm_ms": 100.0, "tts_ms": 50.0,
                "total_ms": 200.0, "tts_ok": True, "input_channel": "text",
            })
            non_empty = telemetry.summarize()

        self.assertEqual(set(empty.keys()), set(non_empty.keys()),
                         "summarize() 빈/비빈 경로 키셋 불일치 — P1 재발")

    def test_latency_keyset_consistency(self):
        """latency 하위 키도 빈/비빈에서 동일해야."""
        with _IsolatedLog():
            empty_lat = telemetry.summarize()["latency"]
            telemetry.log_turn({"backend": "openai", "llm_ms": 50.0})
            non_empty_lat = telemetry.summarize()["latency"]

        self.assertEqual(set(empty_lat.keys()), set(non_empty_lat.keys()))
        for key in empty_lat:
            self.assertEqual(set(empty_lat[key].keys()),
                             set(non_empty_lat[key].keys()),
                             f"latency.{key} 하위 키셋 불일치")

    def test_required_keys_present(self):
        """문서화된 키가 누락되지 않아야 (회귀 방지)."""
        required = {
            "total", "backends", "input_channels", "fallback_rate",
            "tts_failure_rate", "tts_regen_count", "tts_regen_rate",
            "tts_reasons", "intents",
            "avg_fanout_ms", "avg_llm_ms", "avg_tts_ms",
            "latency", "last_ts",
        }
        with _IsolatedLog():
            for s in (telemetry.summarize(),):
                missing = required - set(s.keys())
                self.assertFalse(missing, f"필수 키 누락: {missing}")


class PIISanitizationTests(unittest.TestCase):
    """log_turn 의 본문 차단 키 검증."""

    def test_blocked_keys_become_length_only(self):
        with _IsolatedLog():
            telemetry.log_turn({
                "backend": "openai",
                "text": "비밀입니다",  # 5 chars
                "prompt": "사용자 발화",  # 6 chars
                "user_text": "X" * 100,
                "reply": "응답",  # 2
                "body": "본문",  # 2
                "history": ["a", "b", "c"],  # list len 3
            })
            rows = telemetry.recent(1)

        self.assertEqual(len(rows), 1)
        r = rows[0]
        for k in ("text", "prompt", "user_text", "reply", "body", "history"):
            self.assertNotIn(k, r, f"PII 키 '{k}' 가 저장됨 — 차단 회귀")
        self.assertEqual(r.get("text_len"), 5)
        self.assertEqual(r.get("prompt_len"), 6)
        self.assertEqual(r.get("user_text_len"), 100)
        self.assertEqual(r.get("reply_len"), 2)
        self.assertEqual(r.get("body_len"), 2)
        # list 는 len() 적용됨
        self.assertEqual(r.get("history_len"), 3)


class PercentileTests(unittest.TestCase):
    """nearest-rank 백분위 정확성."""

    def test_known_distribution(self):
        # 1..10 → p50=ceil(0.5*10)=5번째=5, p95=ceil(0.95*10)=10번째=10
        vals = [float(i) for i in range(1, 11)]
        self.assertEqual(telemetry._percentile(vals, 50), 5.0)
        self.assertEqual(telemetry._percentile(vals, 95), 10.0)
        self.assertEqual(telemetry._percentile(vals, 99), 10.0)

    def test_empty_and_single(self):
        self.assertEqual(telemetry._percentile([], 95), 0.0)
        self.assertEqual(telemetry._percentile([42.0], 99), 42.0)

    def test_summarize_latency_uses_percentile(self):
        with _IsolatedLog():
            for ms in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
                telemetry.log_turn({"backend": "openai", "llm_ms": ms})
            s = telemetry.summarize()

        lat = s["latency"]["llm_ms"]
        self.assertEqual(lat["count"], 10)
        self.assertAlmostEqual(lat["avg"], 55.0)
        self.assertEqual(lat["p50"], 50.0)
        self.assertEqual(lat["p95"], 100.0)


class SubscriberTests(unittest.TestCase):
    """pub-sub 모델 동작 + 격리 검증."""

    def test_notify_called_after_log_turn(self):
        with _IsolatedLog():
            received = []
            telemetry.subscribe(received.append)
            telemetry.log_turn({"backend": "openai", "llm_ms": 5.0})
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].get("backend"), "openai")

    def test_subscriber_exception_isolated(self):
        """한 구독자 예외가 다른 구독자에 영향 없어야."""
        with _IsolatedLog():
            calls = []

            def bad(meta):
                raise RuntimeError("boom")

            def good(meta):
                calls.append(meta.get("backend"))

            telemetry.subscribe(bad)
            telemetry.subscribe(good)
            # 예외가 새어나오면 안 됨
            telemetry.log_turn({"backend": "claude"})
            self.assertEqual(calls, ["claude"])

    def test_unsubscribe_stops_notifications(self):
        with _IsolatedLog():
            received = []
            cb = received.append
            telemetry.subscribe(cb)
            telemetry.log_turn({"backend": "a"})
            telemetry.unsubscribe(cb)
            telemetry.log_turn({"backend": "b"})
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].get("backend"), "a")

    def test_subscribe_idempotent(self):
        """동일 콜백 중복 등록 시 한 번만 호출돼야."""
        with _IsolatedLog():
            received = []
            cb = received.append
            telemetry.subscribe(cb)
            telemetry.subscribe(cb)
            telemetry.log_turn({"backend": "x"})
            self.assertEqual(len(received), 1)


class InputChannelTests(unittest.TestCase):
    """사이클 #4: input_channels 분포 검증."""

    def test_audio_and_text_counted_separately(self):
        with _IsolatedLog():
            for _ in range(3):
                telemetry.log_turn({"input_channel": "text", "backend": "o"})
            for _ in range(2):
                telemetry.log_turn({"input_channel": "audio", "backend": "o"})
            s = telemetry.summarize()
        self.assertEqual(s["input_channels"], {"text": 3, "audio": 2})


if __name__ == "__main__":
    unittest.main(verbosity=2)
