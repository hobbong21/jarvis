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


class GeminiBackendTests(unittest.TestCase):
    """Gemini 백엔드 추가가 텔레메트리 집계 + friendly_error 분기에 정상 통합되는지."""

    def test_gemini_counted_in_backends(self):
        with _IsolatedLog():
            for _ in range(3):
                telemetry.log_turn({"backend": "gemini", "llm_ms": 80.0})
            telemetry.log_turn({"backend": "openai", "llm_ms": 120.0})
            s = telemetry.summarize()
        self.assertEqual(s["backends"].get("gemini"), 3)
        self.assertEqual(s["backends"].get("openai"), 1)
        self.assertEqual(s["total"], 4)

    def test_gemini_keyset_matches_other_backends(self):
        with _IsolatedLog():
            empty_keys = set(telemetry.summarize().keys())
        with _IsolatedLog():
            telemetry.log_turn({"backend": "gemini", "llm_ms": 200.0, "input_channel": "voice"})
            gemini_keys = set(telemetry.summarize().keys())
        self.assertEqual(empty_keys, gemini_keys,
                         f"키셋 불일치: 누락={empty_keys - gemini_keys}, 추가={gemini_keys - empty_keys}")

    def test_gemini_in_alt_buttons(self):
        from brain import _ALT_BUTTONS
        # 다른 백엔드의 alt 메시지가 gemini 를 후보로 안내해야 함 (claude/openai/zhipuai)
        self.assertIn("GEMINI", _ALT_BUTTONS["claude"])
        self.assertIn("GEMINI", _ALT_BUTTONS["openai"])
        # gemini 자신의 alt 도 정의되어 있어야 (KeyError 회귀 방지)
        self.assertIn("gemini", _ALT_BUTTONS)

    def test_gemini_friendly_error_korean(self):
        from brain import _friendly_error
        # 인증 실패
        msg = _friendly_error(Exception("401 Unauthorized: API key invalid"), "gemini")
        self.assertIn("Gemini", msg)
        self.assertIn("키", msg)
        self.assertNotIn("401 Unauthorized", msg)  # raw 영문 노출 금지
        # 크레딧/할당량
        msg2 = _friendly_error(Exception("You exceeded your current quota"), "gemini")
        self.assertIn("Gemini", msg2)
        self.assertTrue("크레딧" in msg2 or "할당량" in msg2)

    def test_think_does_not_leak_raw_english_on_failure(self):
        """`server.handle_audio` 가 호출하는 `Brain.think()` 가 백엔드에서 raw 예외를
        던질 때, 반환된 reply 가 한국어 friendly_error 로 감싸져야 한다 (architect
        사이클 #5: 음성 경로 raw 영문 노출 회귀 방지).
        """
        import os
        os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")
        from unittest.mock import patch
        from brain import Brain
        from config import cfg
        from emotion import Emotion

        original_backend = cfg.llm_backend
        try:
            cfg.llm_backend = "gemini"
            b = Brain()
            # GOOGLE_API_KEY 미설정 환경에서도 결정적으로 통과하도록 클라이언트
            # 더미 객체를 강제 주입 (think() 의 조기 None 가드 우회).
            # architect 사이클 #5 권장.
            b.gemini_client = object()
            # _think_gemini_simple 가 raw 영문 예외를 던지도록 패치
            with patch.object(
                b, "_think_gemini_simple",
                side_effect=Exception("401 Unauthorized: invalid api key xyz123"),
            ):
                emotion, reply = b.think("아무말")
            self.assertEqual(emotion, Emotion.CONCERNED)
            # raw 영문 토큰이 사용자 응답에 노출되면 안 됨
            self.assertNotIn("401 Unauthorized", reply)
            self.assertNotIn("xyz123", reply)
            self.assertNotIn("invalid api key", reply)
            # 한국어 friendly_error 의 키 단어 ("키" 또는 "Gemini") 가 들어있어야 함
            self.assertTrue("키" in reply or "Gemini" in reply,
                            f"friendly_error 미적용 의심: {reply!r}")
        finally:
            cfg.llm_backend = original_backend


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


class Cycle7InsightsTests(unittest.TestCase):
    """사이클 #7 T002: 백엔드 비교 통계 + actionable insights."""

    def test_per_backend_keys_present_when_empty(self):
        with _IsolatedLog():
            s = telemetry.summarize()
        self.assertIn("per_backend", s)
        self.assertIn("insights", s)
        self.assertIsInstance(s["per_backend"], dict)
        self.assertIsInstance(s["insights"], list)

    def test_per_backend_stats_shape(self):
        with _IsolatedLog():
            for i in range(8):
                telemetry.log_turn({
                    "backend": "openai", "intent": "chat",
                    "fanout_ms": 1.0, "llm_ms": 100.0 + i, "tts_ms": 30.0,
                    "total_ms": 200.0, "tts_ok": True, "input_channel": "text",
                })
            s = telemetry.summarize()
        per = s["per_backend"]
        self.assertIn("openai", per)
        row = per["openai"]
        for key in (
            "count", "avg_llm_ms", "p50_llm_ms",
            "tts_failure_rate", "fallback_rate", "tts_regen_rate",
        ):
            self.assertIn(key, row, f"per_backend['openai'] 에 {key} 키 없음")
        self.assertEqual(row["count"], 8)
        self.assertGreater(row["avg_llm_ms"], 0.0)

    def test_insights_warn_on_high_fallback(self):
        """폴백률 10% 초과 시 warn insight 생성."""
        with _IsolatedLog():
            # 20턴 중 4턴 폴백 → 20%
            for i in range(20):
                telemetry.log_turn({
                    "backend": "claude", "intent": "chat",
                    "llm_ms": 100.0, "tts_ms": 50.0, "total_ms": 200.0,
                    "tts_ok": True, "input_channel": "text",
                    "fallback_used": (i < 4),
                })
            s = telemetry.summarize()
        levels = [i.get("level") for i in s["insights"]]
        self.assertTrue(any(l == "warn" for l in levels),
                        f"폴백률 20% 인데 warn insight 없음: {s['insights']}")

    def test_insights_data_insufficient_when_low_volume(self):
        """5턴 미만은 신뢰도/부족 안내 (둘 중 하나)."""
        with _IsolatedLog():
            telemetry.log_turn({
                "backend": "openai", "llm_ms": 50.0, "tts_ms": 30.0,
                "total_ms": 100.0, "tts_ok": True, "input_channel": "text",
            })
            s = telemetry.summarize()
        msgs = " ".join(i.get("message", "") for i in s["insights"])
        self.assertTrue(("부족" in msgs) or ("신뢰도" in msgs),
                        f"데이터 부족/신뢰도 안내 없음: {s['insights']}")

    def test_insights_ignore_ok_sentinel_as_block_reason(self):
        """TTS reason='ok' 는 성공이므로 '차단 사유' 인사이트로 표시되면 안 됨."""
        with _IsolatedLog():
            for _ in range(10):
                telemetry.log_turn({
                    "backend": "openai", "intent": "chat",
                    "llm_ms": 100.0, "tts_ms": 50.0, "total_ms": 200.0,
                    "tts_ok": True, "tts_reason": "ok",
                    "input_channel": "text",
                })
            s = telemetry.summarize()
        for ins in s["insights"]:
            self.assertNotIn("'ok'", ins.get("message", ""),
                             f"성공 sentinel 'ok' 가 차단 사유 인사이트로 노출됨: {ins}")

    def test_summarize_keyset_includes_new_keys_in_both_paths(self):
        """per_backend / insights 키도 빈/비빈 모두에 존재해야 (P1 회귀 방지)."""
        with _IsolatedLog():
            empty = telemetry.summarize()
            telemetry.log_turn({
                "backend": "claude", "llm_ms": 100.0, "tts_ms": 50.0,
                "total_ms": 200.0, "tts_ok": True, "input_channel": "text",
            })
            full = telemetry.summarize()
        self.assertIn("per_backend", empty)
        self.assertIn("insights", empty)
        self.assertEqual(set(empty.keys()), set(full.keys()))


class Cycle7ModelSwitchTests(unittest.TestCase):
    """사이클 #7 T001: brain.switch_model + config.MODEL_CATALOG."""

    def test_catalog_covers_all_real_backends(self):
        import config
        cat = config.MODEL_CATALOG
        for b in ("claude", "openai", "ollama", "gemini"):
            self.assertIn(b, cat, f"MODEL_CATALOG 에 {b} 누락")
            self.assertGreater(len(cat[b]), 0, f"MODEL_CATALOG[{b}] 비어있음")

    def test_switch_model_accepts_known(self):
        import config
        from brain import Brain
        brain = Brain()
        first = config.MODEL_CATALOG["openai"][0]
        # raise 하지 않으면 성공
        brain.switch_model("openai", first)
        self.assertEqual(getattr(config.cfg, "openai_model", None), first)

    def test_switch_model_rejects_unknown(self):
        from brain import Brain
        brain = Brain()
        with self.assertRaises(ValueError):
            brain.switch_model("openai", "gpt-fake-9000")

    def test_switch_model_rejects_unknown_backend(self):
        from brain import Brain
        brain = Brain()
        with self.assertRaises(ValueError):
            brain.switch_model("nonexistent", "anything")

    def test_compare_backend_blocks_model_switch(self):
        """compare 는 모델 변경 불가 (다중 백엔드 동시 호출 의미상)."""
        from brain import Brain
        brain = Brain()
        with self.assertRaises(ValueError):
            brain.switch_model("compare", "anything")

    def test_current_model_falls_back_to_catalog_first_when_env_override(self):
        """architect P1: 환경변수로 카탈로그 외 모델을 지정해도 current_model 은
        UI 가 select 옵션을 잃지 않도록 카탈로그 첫 항목을 반환해야."""
        import config
        old = config.cfg.openai_model
        try:
            config.cfg.openai_model = "totally-custom-experimental-model"
            cur = config.current_model("openai")
            self.assertIn(cur, config.MODEL_CATALOG["openai"],
                          "카탈로그 외 모델일 때 current_model 이 카탈로그 옵션을 반환해야")
        finally:
            config.cfg.openai_model = old

    def test_switch_model_rolls_back_cfg_on_init_failure(self):
        """architect P1: switch_model 의 _init_backend 가 raise 하면 cfg 가 옛 모델로 원복."""
        import config
        from brain import Brain
        brain = Brain()
        old_model = config.cfg.openai_model
        valid_target = config.MODEL_CATALOG["openai"][1]
        # _init_backend 를 강제 실패시켜 롤백 검증.
        old_init = brain._init_backend
        try:
            brain._init_backend = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            old_backend = config.cfg.llm_backend
            config.cfg.llm_backend = "openai"
            try:
                with self.assertRaises(Exception):
                    brain.switch_model("openai", valid_target)
                # cfg 가 옛 모델로 복귀되어야
                self.assertEqual(config.cfg.openai_model, old_model,
                                 "switch_model 실패 후 cfg 가 신규 모델로 남아있음 — 롤백 누락")
            finally:
                config.cfg.llm_backend = old_backend
        finally:
            brain._init_backend = old_init

    def test_server_uses_direct_korean_for_model_validation_error(self):
        """architect P1 회귀 방지: server.py 의 switch_model 핸들러는 ValueError
        를 _friendly_error 로 통과시키면 '통신 오류' 로 오안내된다. 직접
        한국어 메시지를 emit 하는 ValueError 분기가 있어야 한다."""
        from pathlib import Path as _P
        src = _P(__file__).resolve().parent.parent.joinpath("server.py").read_text(encoding="utf-8")
        # switch_model 블록 안에 except ValueError 가 있어야 함.
        idx = src.find('mtype == "switch_model"')
        self.assertGreater(idx, 0, "switch_model 핸들러 누락")
        block = src[idx: idx + 1500]
        self.assertIn("except ValueError", block,
                      "switch_model 의 ValueError 전용 분기가 없음 — _friendly_error 가 "
                      "검증 실패를 '통신 오류' 로 잘못 안내함")


class Cycle7SemanticIndexTests(unittest.TestCase):
    """사이클 #7 T003: SemanticIndex 옵셔널 폴백."""

    def test_disabled_by_default(self):
        import os, importlib
        old = os.environ.pop("SARVIS_SEMANTIC", None)
        try:
            import memory
            importlib.reload(memory)
            idx = memory.SemanticIndex()
            self.assertFalse(idx.available, "기본 환경에서 의미 검색이 활성화됨")
            self.assertEqual(idx.search("u", "안녕", k=5), [])
            self.assertFalse(idx.index_message(1, "u", "테스트"))
        finally:
            if old is not None:
                os.environ["SARVIS_SEMANTIC"] = old

    def test_search_messages_falls_back_to_like_when_semantic_unavailable(self):
        """SemanticIndex 비활성 + LIKE 폴백 정상 동작."""
        import tempfile, os
        from memory import Memory
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            mem = Memory(path=tmp.name)
            cid = mem.start_conversation("u1")
            mem.add_message(cid, "user", "오늘 날씨가 정말 좋네요")
            mem.add_message(cid, "user", "저녁 메뉴 추천해줘")
            results = mem.search_messages("u1", "날씨", limit=5)
            self.assertEqual(len(results), 1)
            self.assertIn("날씨", results[0]["content"])
        finally:
            os.unlink(tmp.name)

    def test_custom_path_isolates_from_global_singleton(self):
        """architect P0: 사용자 지정 path Memory 는 운영 chromadb 와 격리되어야."""
        import tempfile, os
        from memory import Memory, _NullSemanticIndex
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            mem = Memory(path=tmp.name)
            # 운영 SemanticIndex 가 활성이든 아니든, 테스트 인스턴스는 NullIndex 여야.
            self.assertIsInstance(mem._semantic, _NullSemanticIndex,
                                  "사용자 지정 path 인데 전역 SemanticIndex 가 주입됨 — "
                                  "운영 chromadb 오염 위험")
            self.assertFalse(mem._semantic.available)
        finally:
            os.unlink(tmp.name)

    def test_add_message_safe_when_semantic_disabled(self):
        """의미 검색 비활성 상태에서도 add_message 가 정상 반환."""
        import tempfile, os
        from memory import Memory
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            mem = Memory(path=tmp.name)
            cid = mem.start_conversation("u1")
            mid = mem.add_message(cid, "user", "테스트")
            self.assertIsInstance(mid, int)
            self.assertGreater(mid, 0)
        finally:
            os.unlink(tmp.name)


class Cycle9PillarTests(unittest.TestCase):
    """사이클 #9 — 3-Pillar (voice/vision/action) 메트릭 회귀."""

    def test_pillars_in_summarize_keyset_empty_and_nonempty(self):
        """빈/비빈 둘 다 'pillars' 키 + 동일 하위 키셋."""
        with _IsolatedLog():
            empty = telemetry.summarize()
            self.assertIn("pillars", empty)
            for p in ("voice", "vision", "action"):
                self.assertIn(p, empty["pillars"])
                self.assertEqual(
                    set(empty["pillars"][p].keys()),
                    {"score", "samples", "metrics", "notes"},
                )
            telemetry.log_turn({
                "backend": "openai", "input_channel": "audio",
                "fanout_ms": 100.0, "llm_ms": 200.0, "tts_ms": 300.0,
                "total_ms": 600.0, "tts_ok": True, "vision_used": True,
                "tool_count": 1, "tool_ms": 250.0,
            })
            non_empty = telemetry.summarize()
            self.assertIn("pillars", non_empty)
            for p in ("voice", "vision", "action"):
                self.assertEqual(
                    set(non_empty["pillars"][p].keys()),
                    set(empty["pillars"][p].keys()),
                    f"pillars.{p} 빈/비빈 키셋 불일치",
                )

    def test_voice_pillar_audio_ratio_drives_score(self):
        """audio 비율이 100% 이고 빈전사·TTS차단 0% 면 voice score = 100."""
        with _IsolatedLog():
            for _ in range(5):
                telemetry.log_turn({
                    "backend": "openai", "input_channel": "audio",
                    "tts_ok": True, "fanout_ms": 100.0,
                    "llm_ms": 100.0, "total_ms": 300.0,
                })
            voice = telemetry.summarize()["pillars"]["voice"]
            self.assertEqual(voice["samples"], 5)
            self.assertAlmostEqual(voice["score"], 100.0, places=1)
            self.assertEqual(voice["metrics"]["audio_ratio"], 1.0)

    def test_voice_pillar_text_only_low_score_with_note(self):
        """텍스트 입력만 5턴이면 audio_ratio=0 → 음성 점수 매우 낮음 + 권장 note."""
        with _IsolatedLog():
            for _ in range(5):
                telemetry.log_turn({
                    "backend": "openai", "input_channel": "text",
                    "tts_ok": True, "total_ms": 300.0,
                })
            voice = telemetry.summarize()["pillars"]["voice"]
            self.assertLess(voice["score"], 50.0)
            joined = " ".join(voice["notes"])
            self.assertIn("음성 입력 비율", joined)

    def test_vision_pillar_zero_use_low_score(self):
        """vision_used=False 만 5턴 → vision_use_ratio=0, score 낮음, 권장 note."""
        with _IsolatedLog():
            for _ in range(5):
                telemetry.log_turn({
                    "backend": "openai", "input_channel": "text",
                    "tts_ok": True, "total_ms": 300.0, "vision_used": False,
                })
            vis = telemetry.summarize()["pillars"]["vision"]
            self.assertEqual(vis["metrics"]["vision_use_ratio"], 0.0)
            self.assertLess(vis["score"], 50.0)
            joined = " ".join(vis["notes"])
            self.assertTrue("비전 호출" in joined or "카메라" in joined)

    def test_action_pillar_fast_total_ms_high_score(self):
        """total_ms 가 일관되게 < 2000ms 이고 에러 0 → action score ≥ 90."""
        with _IsolatedLog():
            for _ in range(5):
                telemetry.log_turn({
                    "backend": "openai", "input_channel": "text",
                    "tts_ok": True, "total_ms": 800.0, "tool_count": 1,
                    "tool_ms": 300.0,
                })
            act = telemetry.summarize()["pillars"]["action"]
            self.assertGreaterEqual(act["score"], 90.0)
            self.assertEqual(act["metrics"]["tool_use_ratio"], 1.0)

    def test_action_pillar_slow_total_ms_low_score(self):
        """total_ms 가 일관되게 > 8000ms → action speed_score=0, 종합 score 낮음."""
        with _IsolatedLog():
            for _ in range(5):
                telemetry.log_turn({
                    "backend": "openai", "input_channel": "text",
                    "tts_ok": True, "total_ms": 9500.0,
                })
            act = telemetry.summarize()["pillars"]["action"]
            self.assertLess(act["score"], 30.0)
            joined = " ".join(act["notes"])
            self.assertIn("즉각성", joined)

    def test_pillar_score_none_when_under_3_samples(self):
        """3턴 미만이면 score=None (UI 가 '측정중' 표시)."""
        with _IsolatedLog():
            telemetry.log_turn({
                "backend": "openai", "input_channel": "audio",
                "tts_ok": True, "total_ms": 500.0, "vision_used": True,
            })
            p = telemetry.summarize()["pillars"]
            for name in ("voice", "vision", "action"):
                self.assertIsNone(p[name]["score"], f"{name} score should be None")
                self.assertIn("측정 표본 부족", " ".join(p[name]["notes"]))

    def test_pillar_notes_propagate_to_insights(self):
        """pillar notes 가 summarize.insights 에 [pillar] 접두로 승격되어야."""
        with _IsolatedLog():
            for _ in range(10):
                telemetry.log_turn({
                    "backend": "openai", "input_channel": "text",
                    "tts_ok": True, "total_ms": 9500.0,
                })
            s = telemetry.summarize()
            tags = [m["message"] for m in s["insights"] if m["message"].startswith("[")]
            self.assertTrue(any(t.startswith("[action]") for t in tags),
                            f"action pillar 권장이 insights 로 승격 안 됨: {tags}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
