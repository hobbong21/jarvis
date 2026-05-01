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


class ZhipuAIBackendTests(unittest.TestCase):
    """사이클 #6: 신규 zhipuai 백엔드가 텔레메트리 집계에 정상 포함되는지."""

    def test_zhipuai_counted_in_backends(self):
        """`backend`='zhipuai' 가 backends Counter 에 누락 없이 집계되어야 한다."""
        with _IsolatedLog():
            for _ in range(4):
                telemetry.log_turn({"backend": "zhipuai", "llm_ms": 100.0})
            telemetry.log_turn({"backend": "claude", "llm_ms": 50.0})
            s = telemetry.summarize()
        self.assertEqual(s["backends"].get("zhipuai"), 4)
        self.assertEqual(s["backends"].get("claude"), 1)
        self.assertEqual(s["total"], 5)

    def test_zhipuai_keyset_matches_other_backends(self):
        """zhipuai 만 있는 경로의 summarize 키셋이 빈 경로와 동등해야 한다 (사이클 #4 P1 패턴 유지)."""
        with _IsolatedLog():
            empty_keys = set(telemetry.summarize().keys())
        with _IsolatedLog():
            telemetry.log_turn({"backend": "zhipuai", "llm_ms": 200.0, "input_channel": "text"})
            zhipuai_keys = set(telemetry.summarize().keys())
        self.assertEqual(empty_keys, zhipuai_keys,
                         f"키셋 불일치: 누락={empty_keys - zhipuai_keys}, 추가={zhipuai_keys - empty_keys}")


class FriendlyErrorTests(unittest.TestCase):
    """사이클 #6 핫픽스: server.py 가 raw 영문 예외(`Internal Server Error`,
    `Connection error` 등)를 사용자에게 그대로 노출하던 회귀를 막기 위한
    `brain._friendly_error` 분기 검증.
    """

    def test_credit_message_in_korean(self):
        from brain import _friendly_error
        msg = _friendly_error(Exception("Your credit balance is too low"), "claude")
        self.assertIn("크레딧", msg)
        self.assertIn("Claude", msg.title()) if False else self.assertTrue(
            "CLAUDE" in msg or "Claude" in msg
        )

    def test_zhipuai_auth_message(self):
        from brain import _friendly_error
        msg = _friendly_error(Exception("身份验证失败 1000"), "zhipuai")
        self.assertIn("ZhipuAI", msg)
        self.assertIn("키", msg)

    def test_network_message_generic(self):
        from brain import _friendly_error
        msg = _friendly_error(Exception("Connection timed out"), "openai")
        self.assertIn("연결", msg)
        self.assertNotIn("Connection timed out", msg)  # raw 영문 노출 금지

    def test_unknown_falls_back_to_generic_korean(self):
        from brain import _friendly_error
        msg = _friendly_error(Exception("Some weird internal server error xyz"), "claude")
        # raw 영문이 그대로 노출되면 안 됨
        self.assertNotIn("xyz", msg)
        self.assertIn("⚠", msg)


class BrainCfgRegressionTests(unittest.TestCase):
    """사이클 #6 핫픽스: handle_audio() 의 turn_meta 초기화에서
    `session.brain.cfg` 접근 시 AttributeError 가 dict literal 평가 중
    raise 되어 try 진입 전에 핸들러가 죽고 WS 가 끊기던 회귀.
    Brain 인스턴스에는 .cfg 속성이 존재하지 않아야 — 그리고 server.py 는
    모듈 레벨 cfg 를 직접 사용해야 한다.
    """

    def test_brain_instance_has_no_cfg_attr(self):
        # Brain 을 실제 초기화하지 않고 클래스 차원에서 검증
        # (init 은 외부 키에 의존하므로 import 만)
        from brain import Brain
        # 클래스 정의 또는 __init__ 의 self 할당에 cfg 가 없어야
        # (이 회귀 패턴이 다시 들어오면 이 테스트가 실패하도록 명시)
        import inspect
        src = inspect.getsource(Brain.__init__)
        self.assertNotIn("self.cfg", src,
                         "Brain.__init__ 에 self.cfg 가 도입되면 server.py 의 "
                         "session.brain.cfg 접근이 다시 가능해져 회귀 위험이 있습니다.")

    def test_server_handle_audio_uses_module_cfg(self):
        # server.py 가 session.brain.cfg 가 아니라 cfg.llm_backend 를 쓰는지
        from pathlib import Path as _P
        src = _P(__file__).resolve().parent.parent.joinpath("server.py").read_text(encoding="utf-8")
        self.assertNotIn("session.brain.cfg", src,
                         "session.brain.cfg 접근은 AttributeError 를 일으킵니다 "
                         "(Brain 에는 cfg 속성이 없음). cfg.llm_backend 를 사용하세요.")

    def test_server_never_emits_raw_str_exception(self):
        """사이클 #6 핫픽스 회귀 방지: server.py 가 사용자에게 보내는 error
        emit 에서 raw `str(e)` 를 그대로 노출하면 안 된다 (반드시 _friendly_error
        를 거쳐야 함). 정확한 한국어 친절 메시지가 보장되도록 호출 지점 자체를
        AST 로 검사한다.
        """
        import ast
        from pathlib import Path as _P
        src_path = _P(__file__).resolve().parent.parent.joinpath("server.py")
        tree = ast.parse(src_path.read_text(encoding="utf-8"))

        offending = []
        for node in ast.walk(tree):
            # await emit(type="error", message=...) 형태 호출만 검사
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_emit = (isinstance(func, ast.Name) and func.id == "emit")
            if not is_emit:
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            t = kw.get("type")
            if not (isinstance(t, ast.Constant) and t.value == "error"):
                continue
            msg = kw.get("message")
            # message=str(e) 또는 f"오류: {e}" 등 raw 노출 패턴 차단
            if isinstance(msg, ast.Call) and isinstance(msg.func, ast.Name) and msg.func.id == "str":
                offending.append(ast.dump(node))
            if isinstance(msg, ast.JoinedStr):
                # f-string 안에 FormattedValue 가 있고 _friendly_error 가 아니면 의심
                has_format = any(isinstance(v, ast.FormattedValue) for v in msg.values)
                if has_format:
                    offending.append(ast.dump(node))

        self.assertEqual(offending, [],
                         "server.py 가 사용자 에러 토스트로 raw 예외/포맷팅 문자열을 "
                         "직접 노출하고 있습니다. _friendly_error(e, backend) 를 사용하세요:\n"
                         + "\n".join(offending))


if __name__ == "__main__":
    unittest.main(verbosity=2)
