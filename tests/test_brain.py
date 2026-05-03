"""brain.py 단위 테스트 — _friendly_error 분기, switch_backend/model, tool-use 루프.

architect 사이클 #7 follow-up:
  - _friendly_error 의 분기 (크레딧/인증/모델/rate/네트워크/일반) 확장 검증
  - _model_switch_friendly 래퍼
  - switch_backend / switch_model 의 카탈로그 검증·롤백
  - _think_with_tools 의 tool 호출 루프 (일반→tool→일반 종료)와 max_iters 안전장치
  - available_backends / _fallback_chain 우선순위
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")

from sarvis.emotion import Emotion  # noqa: E402


# brain 모듈은 import 만 했을 때 사이드이펙트가 적음 (Brain 인스턴스화는 무거움)
from sarvis import brain as brain_mod  # noqa: E402
from sarvis.brain import (  # noqa: E402
    Brain,
    _friendly_error,
    _model_switch_friendly,
)


class FriendlyErrorBranchTests(unittest.TestCase):
    """_friendly_error 의 모든 주요 분기를 한 번씩 친다."""

    def test_credit_for_each_backend(self):
        for backend in ("claude", "openai", "zhipuai", "gemini"):
            msg = _friendly_error(Exception("Your credit balance is too low"), backend)
            self.assertIn("크레딧", msg)
        # 미지정 백엔드도 친절 메시지
        msg = _friendly_error(Exception("billing"), "ollama")
        self.assertIn("크레딧", msg)

    def test_quota_phrase_routed_to_credit(self):
        msg = _friendly_error(Exception("You exceeded your current quota"), "openai")
        self.assertIn("크레딧", msg)

    def test_auth_failure_for_zhipuai_and_gemini(self):
        msg = _friendly_error(Exception("身份验证 1000"), "zhipuai")
        self.assertIn("ZhipuAI", msg)
        self.assertIn("키", msg)

        msg = _friendly_error(Exception("401 Unauthorized"), "gemini")
        self.assertIn("Gemini", msg)
        self.assertIn("키", msg)

    def test_auth_failure_generic(self):
        msg = _friendly_error(Exception("invalid_api_key"), "openai")
        self.assertIn("OPENAI", msg)
        self.assertIn("키", msg)

    def test_permission_denied_branch(self):
        msg = _friendly_error(Exception("403 Forbidden / model_not_found"), "claude")
        self.assertIn("권한", msg)

    def test_rate_limit_branch(self):
        msg = _friendly_error(Exception("429 rate limit exceeded"), "openai")
        self.assertIn("한도", msg)

    def test_ollama_connection_message_specific(self):
        msg = _friendly_error(Exception("Connection refused"), "ollama")
        self.assertIn("Ollama", msg)

    def test_generic_network_message(self):
        msg = _friendly_error(Exception("network unreachable"), "claude")
        self.assertIn("연결", msg)

    def test_unknown_returns_generic_korean(self):
        msg = _friendly_error(Exception("totally unrelated xyz"), "claude")
        self.assertNotIn("xyz", msg)
        self.assertIn("⚠", msg)

    def test_model_switch_friendly_wraps_message(self):
        out = _model_switch_friendly(ValueError("백엔드 X 의 카탈로그에 없는 모델: Y"))
        self.assertIn("⚠", out)
        self.assertIn("모델 변경 실패", out)
        self.assertIn("X", out)


class _BrainNoInitMixin:
    """Brain 을 _init_backend 없이 인스턴스화 (외부 키 의존 회피)."""

    def make_brain(self, tool_executor=None):
        with patch.object(Brain, "_init_backend", lambda self: None):
            b = Brain(tool_executor=tool_executor)
        return b


class SwitchBackendTests(_BrainNoInitMixin, unittest.TestCase):
    def test_unknown_backend_raises(self):
        b = self.make_brain()
        with self.assertRaises(ValueError):
            b.switch_backend("nope")

    def test_known_backend_updates_cfg(self):
        b = self.make_brain()
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            with patch.object(Brain, "_init_backend") as init:
                b.switch_backend("claude")
                self.assertEqual(cfg.llm_backend, "claude")
                init.assert_called_once()
        finally:
            cfg.llm_backend = original


class SwitchModelTests(_BrainNoInitMixin, unittest.TestCase):
    def test_compare_mode_unsupported(self):
        b = self.make_brain()
        with self.assertRaises(ValueError) as ctx:
            b.switch_model("compare", "claude-sonnet-4-6")
        self.assertIn("compare", str(ctx.exception))

    def test_unknown_backend_raises(self):
        b = self.make_brain()
        with self.assertRaises(ValueError):
            b.switch_model("nope", "model-x")

    def test_unknown_model_raises(self):
        b = self.make_brain()
        with self.assertRaises(ValueError) as ctx:
            b.switch_model("claude", "model-not-in-catalog")
        self.assertIn("카탈로그", str(ctx.exception))

    def test_valid_model_sets_cfg(self):
        b = self.make_brain()
        from sarvis.config import cfg, MODEL_CATALOG
        original = cfg.claude_model
        try:
            target = MODEL_CATALOG["claude"][0]
            # 현재 활성 백엔드가 아닌 상태로 변경 — _init_backend 호출 없음
            cfg.llm_backend = "openai"
            b.switch_model("claude", target)
            self.assertEqual(cfg.claude_model, target)
        finally:
            cfg.claude_model = original

    def test_init_failure_rolls_back_cfg(self):
        b = self.make_brain()
        from sarvis.config import cfg, MODEL_CATALOG
        original = cfg.claude_model
        try:
            target = MODEL_CATALOG["claude"][1]
            cfg.llm_backend = "claude"
            # _init_backend 가 raise → 원복 시도(또 raise) → 결국 raise
            with patch.object(Brain, "_init_backend", side_effect=RuntimeError("init fail")):
                with self.assertRaises(RuntimeError):
                    b.switch_model("claude", target)
            # 롤백되어 원래 모델로 복귀
            self.assertEqual(cfg.claude_model, original)
        finally:
            cfg.claude_model = original
            cfg.llm_backend = "claude"


class ToolUseLoopTests(_BrainNoInitMixin, unittest.TestCase):
    def _make_response(self, *, stop_reason: str, blocks):
        """anthropic 응답 객체 mock."""
        resp = MagicMock()
        resp.stop_reason = stop_reason
        resp.content = blocks
        return resp

    def _text_block(self, text):
        b = SimpleNamespace(type="text", text=text)
        return b

    def _tool_use_block(self, name, inputs, block_id="t_1"):
        return SimpleNamespace(
            type="tool_use", id=block_id, name=name, input=inputs
        )

    def test_no_tool_returns_text_directly(self):
        tools = MagicMock()
        tools.definitions.return_value = []
        b = self.make_brain(tool_executor=tools)
        b.client = MagicMock()
        # 한 번에 종료
        b.client.messages.create.return_value = self._make_response(
            stop_reason="end_turn",
            blocks=[self._text_block("[emotion:happy] 안녕하세요")],
        )
        b.history = [{"role": "user", "content": "안녕"}]
        emotion, body = b._think_with_tools()
        self.assertEqual(emotion, Emotion.HAPPY)
        self.assertIn("안녕하세요", body)

    def test_tool_then_final(self):
        tools = MagicMock()
        tools.definitions.return_value = []
        tools.execute.return_value = "지금은 12시"
        b = self.make_brain(tool_executor=tools)
        b.client = MagicMock()

        first = self._make_response(
            stop_reason="tool_use",
            blocks=[self._tool_use_block("get_time", {}, block_id="abc")],
        )
        second = self._make_response(
            stop_reason="end_turn",
            blocks=[self._text_block("지금은 정오입니다.")],
        )
        b.client.messages.create.side_effect = [first, second]
        b.history = [{"role": "user", "content": "시간?"}]
        emotion, body = b._think_with_tools()
        # 도구 결과가 history 에 들어갔어야
        self.assertTrue(any(
            isinstance(h["content"], list) and any(
                isinstance(item, dict) and item.get("type") == "tool_result"
                for item in h["content"]
            )
            for h in b.history
        ))
        tools.execute.assert_called_once_with("get_time", {})
        self.assertIn("정오", body)

    def test_max_iters_safety(self):
        tools = MagicMock()
        tools.definitions.return_value = []
        tools.execute.return_value = "..."
        b = self.make_brain(tool_executor=tools)
        b.client = MagicMock()
        # 항상 tool_use 만 반환 → 8회 후 안전 종료
        b.client.messages.create.return_value = self._make_response(
            stop_reason="tool_use",
            blocks=[self._tool_use_block("get_time", {}, block_id="t")],
        )
        b.history = [{"role": "user", "content": "x"}]
        emotion, body = b._think_with_tools()
        self.assertEqual(emotion, Emotion.CONCERNED)
        self.assertIn("도구 호출", body)
        # 정확히 max_iters(=8) 회 호출되어야
        self.assertEqual(b.client.messages.create.call_count, 8)

    def test_intent_only_response_triggers_auto_nudge(self):
        """'검색해볼게요'만 답하고 도구 미사용 → brain 이 자동 재촉해 실제 결과까지 한 턴에 도출."""
        tools = MagicMock()
        tools.definitions.return_value = []
        tools.execute.return_value = "검색 결과: 오늘 코스피 +1.2%"
        b = self.make_brain(tool_executor=tools)
        b.client = MagicMock()

        # 1) 첫 응답 — 도구 안 부르고 안내문만
        intent_only = self._make_response(
            stop_reason="end_turn",
            blocks=[self._text_block("[emotion:thinking] 검색해볼게요.")],
        )
        # 2) 재촉 후 — 이번엔 도구 호출
        tool_call = self._make_response(
            stop_reason="tool_use",
            blocks=[self._tool_use_block("web_search", {"query": "오늘 증시"}, block_id="t1")],
        )
        # 3) 도구 결과 받고 최종 답변
        final = self._make_response(
            stop_reason="end_turn",
            blocks=[self._text_block("[emotion:speaking] 오늘 코스피는 1.2% 상승했어요.")],
        )
        b.client.messages.create.side_effect = [intent_only, tool_call, final]
        b.history = [{"role": "user", "content": "오늘 증시 어때?"}]

        emotion, body = b._think_with_tools()

        # 자동 재촉으로 한 사이클 안에서 실제 답변까지 도출
        self.assertEqual(emotion, Emotion.SPEAKING)
        self.assertIn("코스피", body)
        # API 가 3번 호출됐어야 (안내문 → 재촉 → 최종)
        self.assertEqual(b.client.messages.create.call_count, 3)
        # 도구도 실제로 호출됐어야
        tools.execute.assert_called_once_with("web_search", {"query": "오늘 증시"})

    def test_nudge_cleans_intermediate_history_on_success(self):
        """자동 재촉 후 history 에는 의도-only 응답과 합성 user 메시지가 남으면 안 됨.
        다음 turn 의 LLM 컨텍스트가 깨끗해지도록 splice 가 수행되는지 검증."""
        tools = MagicMock()
        tools.definitions.return_value = []
        tools.execute.return_value = "검색 결과: ..."
        b = self.make_brain(tool_executor=tools)
        b.client = MagicMock()

        intent_only = self._make_response(
            stop_reason="end_turn",
            blocks=[self._text_block("[emotion:thinking] 검색해볼게요.")],
        )
        tool_call = self._make_response(
            stop_reason="tool_use",
            blocks=[self._tool_use_block("web_search", {"query": "x"}, block_id="t1")],
        )
        final = self._make_response(
            stop_reason="end_turn",
            blocks=[self._text_block("[emotion:speaking] 결과 알려드려요.")],
        )
        b.client.messages.create.side_effect = [intent_only, tool_call, final]
        b.history = [{"role": "user", "content": "검색해줘"}]
        b._think_with_tools()

        # history 가 다음 turn 에 LLM 에게 노출되어도 자연스러운 흐름이어야 함.
        # 임시 메시지(의도-only assistant + synthetic user) 가 모두 제거되었는지 확인.
        text_payloads = []
        for h in b.history:
            c = h.get("content")
            if isinstance(c, str):
                text_payloads.append(c)
            elif isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        text_payloads.append(blk.get("text", ""))

        joined = " ".join(text_payloads)
        self.assertNotIn("검색해볼게요", joined,
                         f"의도-only 응답이 history 에 남음. payloads={text_payloads}")
        self.assertNotIn("방금 말한 작업을 지금", joined,
                         f"합성 user nudge 가 history 에 남음. payloads={text_payloads}")
        # 정상 응답은 보존되어야 함
        self.assertIn("결과 알려드려요", joined)

    def test_intent_announce_only_nudges_once(self):
        """안내문만 두 번 연속해도 무한 재촉하지 않음 — 1회만 적용 후 그 응답을 그대로 반환."""
        tools = MagicMock()
        tools.definitions.return_value = []
        b = self.make_brain(tool_executor=tools)
        b.client = MagicMock()

        # 두 번 모두 안내문만 (도구 미사용)
        announce = self._make_response(
            stop_reason="end_turn",
            blocks=[self._text_block("[emotion:neutral] 잠시만 기다려주세요.")],
        )
        b.client.messages.create.return_value = announce
        b.history = [{"role": "user", "content": "오늘 뉴스"}]

        emotion, body = b._think_with_tools()

        # 정확히 2번만 호출 (재촉 1회 후 종료, 무한루프 X)
        self.assertEqual(b.client.messages.create.call_count, 2)
        # 마지막 응답을 그대로 반환
        self.assertIn("기다", body)


class IntentAnnounceDetectorTests(unittest.TestCase):
    """_is_intent_only_announce — '검색해볼게요' 류 감지기."""

    def test_detects_korean_search_intent(self):
        from sarvis.brain import _is_intent_only_announce
        self.assertTrue(_is_intent_only_announce("[emotion:thinking] 검색해볼게요"))
        self.assertTrue(_is_intent_only_announce("찾아볼게요."))
        self.assertTrue(_is_intent_only_announce("알아볼게."))
        self.assertTrue(_is_intent_only_announce("확인해볼게요"))
        self.assertTrue(_is_intent_only_announce("잠시만 기다려주세요"))

    def test_detects_english_intent(self):
        from sarvis.brain import _is_intent_only_announce
        self.assertTrue(_is_intent_only_announce("Let me search for that"))
        self.assertTrue(_is_intent_only_announce("I'll check that for you"))

    def test_long_response_with_actual_answer_not_flagged(self):
        from sarvis.brain import _is_intent_only_announce
        # 100자 넘는 응답이면 이미 답변이 있다고 간주
        long_text = (
            "검색해보니 오늘 코스피는 2,750으로 마감했고 "
            "삼성전자는 1.5% 상승, SK하이닉스는 0.8% 상승했어요. "
            "전체적으로 반도체 섹터가 강세를 보였습니다."
        )
        self.assertFalse(_is_intent_only_announce(long_text))

    def test_normal_answer_not_flagged(self):
        from sarvis.brain import _is_intent_only_announce
        self.assertFalse(_is_intent_only_announce("[emotion:happy] 안녕하세요!"))
        self.assertFalse(_is_intent_only_announce("오늘 날씨는 맑아요."))

    def test_empty_text_not_flagged(self):
        from sarvis.brain import _is_intent_only_announce
        self.assertFalse(_is_intent_only_announce(""))
        self.assertFalse(_is_intent_only_announce(None))


class ThinkErrorWrappingTests(_BrainNoInitMixin, unittest.TestCase):
    """think() 가 raw 영문 예외를 _friendly_error 로 감싸는지."""

    def test_raw_exception_translated_to_korean(self):
        b = self.make_brain()
        b.client = MagicMock()
        # 어떤 백엔드로 분기되든 결국 RuntimeError 가 외부로 나가지 않아야
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "claude"
            with patch.object(Brain, "_think_claude_simple",
                              side_effect=RuntimeError("Some weird error")):
                emotion, body = b.think("hi")
            self.assertEqual(emotion, Emotion.CONCERNED)
            self.assertIn("⚠", body)
            self.assertNotIn("Some weird error", body)
        finally:
            cfg.llm_backend = original

    def test_no_client_returns_friendly(self):
        b = self.make_brain()
        b.client = None
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "openai"
            emotion, body = b.think("hi")
            self.assertEqual(emotion, Emotion.CONCERNED)
            self.assertIn("OPENAI", body)
            self.assertIn("API 키", body)
        finally:
            cfg.llm_backend = original


class AvailableBackendsTests(_BrainNoInitMixin, unittest.TestCase):
    def test_lists_only_clients_with_keys(self):
        b = self.make_brain()
        b.anthropic_client = object()
        b.openai_client = None
        b.zhipuai_client = object()
        b.gemini_client = None
        b.client = None
        # ollama 헬스체크는 False 로
        with patch.object(brain_mod, "_ollama_healthcheck", return_value=False):
            from sarvis.config import cfg
            original = cfg.llm_backend
            try:
                cfg.llm_backend = "claude"
                avail = b.available_backends()
            finally:
                cfg.llm_backend = original
        self.assertIn("claude", avail)
        self.assertIn("zhipuai", avail)
        self.assertNotIn("openai", avail)
        self.assertNotIn("gemini", avail)
        self.assertNotIn("ollama", avail)

    def test_fallback_chain_puts_primary_first(self):
        b = self.make_brain()
        b.anthropic_client = object()
        b.openai_client = object()
        b.zhipuai_client = None
        b.gemini_client = None
        with patch.object(brain_mod, "_ollama_healthcheck", return_value=False):
            from sarvis.config import cfg
            original = cfg.llm_backend
            try:
                cfg.llm_backend = "openai"
                chain = b._fallback_chain("openai")
            finally:
                cfg.llm_backend = original
        self.assertEqual(chain[0], "openai")
        self.assertIn("claude", chain)


class OpenAICompatibleTests(_BrainNoInitMixin, unittest.TestCase):
    """_think_openai_compatible / _stream_openai_compatible 의 mock 검증."""

    def _mock_completion(self, content):
        resp = MagicMock()
        msg = MagicMock()
        msg.content = content
        choice = MagicMock(message=msg)
        resp.choices = [choice]
        return resp

    def _mock_stream(self, chunks):
        """chunks: list[str] → OpenAI-style streaming generator mock."""
        result = []
        for text in chunks:
            choice = MagicMock()
            choice.delta = MagicMock(content=text)
            ch = MagicMock(choices=[choice])
            result.append(ch)
        return iter(result)

    def test_openai_compatible_strips_emotion_prefix(self):
        b = self.make_brain()
        client = MagicMock()
        client.chat.completions.create.return_value = self._mock_completion(
            "[emotion:happy] 반갑습니다."
        )
        b.history = [{"role": "user", "content": "안녕"}]
        emotion, body = b._think_openai_compatible(client, "fake-model")
        self.assertEqual(emotion, Emotion.HAPPY)
        self.assertNotIn("[emotion:", body)
        # history 에 assistant 가 추가되었어야
        self.assertEqual(b.history[-1]["role"], "assistant")

    def test_openai_compatible_flattens_list_history(self):
        b = self.make_brain()
        client = MagicMock()
        client.chat.completions.create.return_value = self._mock_completion("응답")
        # Claude tool_use 형식의 list content 가 history 에 있어도 평탄화되어야
        b.history = [{"role": "user", "content": [
            {"type": "text", "text": "안녕"},
            {"type": "tool_use", "id": "x", "name": "y", "input": {}},
        ]}]
        b._think_openai_compatible(client, "fake")
        # 호출 시 messages 인자에서 list 가 string 으로 평탄화되어야
        call_kwargs = client.chat.completions.create.call_args.kwargs
        msgs = call_kwargs["messages"]
        # system + flattened user
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")
        self.assertIsInstance(msgs[1]["content"], str)
        self.assertIn("안녕", msgs[1]["content"])

    def test_stream_openai_compatible_emits_chunks_and_final(self):
        b = self.make_brain()
        client = MagicMock()
        client.chat.completions.create.return_value = self._mock_stream([
            "안녕", "하세요. ", "반갑습니다."
        ])
        b.history = [{"role": "user", "content": "안녕"}]

        outputs = list(b._stream_openai_compatible(client, "fake"))
        # 최소 1개의 청크 + 최종 (None, emotion, body)
        chunks = [o for o in outputs if o[0] is not None]
        finals = [o for o in outputs if o[0] is None]
        self.assertGreater(len(chunks), 0)
        self.assertEqual(len(finals), 1)
        _, emotion, body = finals[0]
        self.assertIsInstance(emotion, Emotion)
        self.assertIn("반갑습니다", body)

    def test_stream_openai_compatible_strips_emotion_prefix_in_buf(self):
        b = self.make_brain()
        client = MagicMock()
        # 첫 청크에 [emotion:...] 가 있어도 prefix_buf 에서 제거되어야
        client.chat.completions.create.return_value = self._mock_stream([
            "[emotion:happy] 안녕하세요. 반갑습니다 정말로요!"
        ])
        b.history = [{"role": "user", "content": "안녕"}]
        outputs = list(b._stream_openai_compatible(client, "fake"))
        # 모든 yield 청크에 emotion prefix 가 없어야
        for chunk, _, _ in outputs:
            if chunk is not None:
                self.assertNotIn("[emotion:", chunk)

    def test_stream_handles_empty_choices(self):
        b = self.make_brain()
        client = MagicMock()
        # delta.content 가 None 인 청크가 섞여도 죽지 않아야
        chunks = []
        for text in ["", "안녕", ""]:
            choice = MagicMock()
            choice.delta = MagicMock(content=text or None)
            chunks.append(MagicMock(choices=[choice]))
        client.chat.completions.create.return_value = iter(chunks)
        b.history = [{"role": "user", "content": "x"}]
        outputs = list(b._stream_openai_compatible(client, "fake"))
        # 최종 항목은 (None, Emotion, str)
        self.assertEqual(outputs[-1][0], None)


class ThinkStreamTests(_BrainNoInitMixin, unittest.TestCase):
    def test_no_client_yields_friendly_message(self):
        b = self.make_brain()
        b.client = None
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "openai"
            outs = list(b.think_stream("안녕"))
        finally:
            cfg.llm_backend = original
        self.assertEqual(len(outs), 1)
        chunk, emo, body = outs[0]
        self.assertIsNone(chunk)
        self.assertEqual(emo, Emotion.CONCERNED)
        self.assertIn("OPENAI", body)
        # orphan user 도 롤백되어야
        self.assertEqual(b.history, [])

    def test_stream_exception_yields_friendly_and_rolls_back(self):
        b = self.make_brain()
        b.client = MagicMock()
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "claude"
            with patch.object(Brain, "_stream_claude",
                              side_effect=RuntimeError("Internal Server Error")):
                outs = list(b.think_stream("안녕"))
        finally:
            cfg.llm_backend = original
        # 마지막은 친절 에러
        last = outs[-1]
        self.assertIsNone(last[0])
        self.assertEqual(last[1], Emotion.CONCERNED)
        self.assertIn("⚠", last[2])
        # orphan user 롤백
        self.assertEqual(b.history, [])

    def test_compare_no_keys_returns_friendly(self):
        b = self.make_brain()
        b.anthropic_client = None
        b.openai_client = None
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "compare"
            outs = list(b.think_stream("hi"))
        finally:
            cfg.llm_backend = original
        last = outs[-1]
        self.assertIsNone(last[0])
        self.assertIn("API 키가 없습니다", last[2])

    def test_context_prepended(self):
        b = self.make_brain()
        b.client = MagicMock()
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "claude"
            with patch.object(Brain, "_stream_claude",
                              return_value=iter([(None, Emotion.NEUTRAL, "ok")])):
                list(b.think_stream("질문", context="추가 정보"))
        finally:
            cfg.llm_backend = original
        # history 의 user 메시지에 context 가 포함되어야
        self.assertIn("추가 정보", b.history[0]["content"])
        self.assertIn("질문", b.history[0]["content"])


class HistoryTrimTests(_BrainNoInitMixin, unittest.TestCase):
    def test_trim_history_keeps_last_60(self):
        b = self.make_brain()
        b.history = [{"role": "user", "content": str(i)} for i in range(80)]
        b._trim_history()
        self.assertEqual(len(b.history), 60)
        # 가장 오래된 20개가 잘려서 마지막 60개만 남아야 함
        self.assertEqual(b.history[0]["content"], "20")
        self.assertEqual(b.history[-1]["content"], "79")

    def test_trim_history_noop_when_short(self):
        b = self.make_brain()
        b.history = [{"role": "user", "content": str(i)} for i in range(10)]
        b._trim_history()
        self.assertEqual(len(b.history), 10)

    def test_reset_history(self):
        b = self.make_brain()
        b.history = [{"role": "user", "content": "x"}]
        b.reset_history()
        self.assertEqual(b.history, [])


class FallbackChainTests(_BrainNoInitMixin, unittest.TestCase):
    """think_stream_with_fallback / _client_for / _dispatch_stream."""

    def test_compare_mode_delegates_to_think_stream(self):
        b = self.make_brain()
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "compare"
            with patch.object(Brain, "think_stream",
                              return_value=iter([(None, Emotion.NEUTRAL, "ok")])) as mock_ts:
                outs = list(b.think_stream_with_fallback("안녕"))
            mock_ts.assert_called_once()
        finally:
            cfg.llm_backend = original
        self.assertEqual(outs[-1][2], "ok")

    def test_no_chain_yields_friendly(self):
        b = self.make_brain()
        b.anthropic_client = None
        b.openai_client = None
        b.zhipuai_client = None
        b.gemini_client = None
        b.client = None
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "claude"
            with patch.object(brain_mod, "_ollama_healthcheck", return_value=False):
                outs = list(b.think_stream_with_fallback("안녕"))
        finally:
            cfg.llm_backend = original
        last = outs[-1]
        self.assertIsNone(last[0])
        self.assertIn("사용 가능한 LLM", last[2])

    def test_first_succeeds_no_fallback(self):
        b = self.make_brain()
        b.anthropic_client = MagicMock()
        b.openai_client = MagicMock()
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "claude"
            with patch.object(brain_mod, "_ollama_healthcheck", return_value=False), \
                 patch.object(Brain, "_dispatch_stream",
                              return_value=iter([("hi", None, None),
                                                 (None, Emotion.HAPPY, "안녕")])):
                outs = list(b.think_stream_with_fallback("user"))
        finally:
            cfg.llm_backend = original
        self.assertEqual(outs[0][0], "hi")
        self.assertEqual(outs[-1][1], Emotion.HAPPY)

    def test_fallback_on_exception(self):
        b = self.make_brain()
        b.anthropic_client = MagicMock()
        b.openai_client = MagicMock()
        from sarvis.config import cfg
        original = cfg.llm_backend
        called = {"count": 0}

        def dispatch(backend):
            called["count"] += 1
            if called["count"] == 1:
                # 1차 후보는 raise → 폴백 트리거
                raise RuntimeError("Internal Server Error")
            yield None, Emotion.HAPPY, f"from-{backend}"

        notify = MagicMock()
        try:
            cfg.llm_backend = "claude"
            with patch.object(brain_mod, "_ollama_healthcheck", return_value=False), \
                 patch.object(Brain, "_dispatch_stream", side_effect=dispatch):
                outs = list(b.think_stream_with_fallback("hi", on_fallback=notify))
        finally:
            cfg.llm_backend = original
        self.assertEqual(called["count"], 2)
        notify.assert_called_once()
        self.assertIn("from-", outs[-1][2])

    def test_client_for_returns_correct_client(self):
        b = self.make_brain()
        b.anthropic_client = "A"
        b.openai_client = "O"
        b.zhipuai_client = "Z"
        b.gemini_client = "G"
        self.assertEqual(b._client_for("claude"), "A")
        self.assertEqual(b._client_for("openai"), "O")
        self.assertEqual(b._client_for("zhipuai"), "Z")
        self.assertEqual(b._client_for("gemini"), "G")
        self.assertIsNone(b._client_for("unknown"))

    def test_client_for_ollama_active_backend(self):
        b = self.make_brain()
        b.client = "OLLAMA_CLIENT"
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "ollama"
            self.assertEqual(b._client_for("ollama"), "OLLAMA_CLIENT")
        finally:
            cfg.llm_backend = original

    def test_dispatch_stream_unknown_raises(self):
        b = self.make_brain()
        with self.assertRaises(ValueError):
            list(b._dispatch_stream("nonexistent"))


class RegenerateSafeTtsTests(_BrainNoInitMixin, unittest.TestCase):
    """regenerate_safe_tts — Anthropic / OpenAI / ZhipuAI / Gemini 폴백."""

    def test_empty_input_returns_empty(self):
        b = self.make_brain()
        self.assertEqual(b.regenerate_safe_tts("", "blocklist"), "")
        self.assertEqual(b.regenerate_safe_tts("   ", "blocklist"), "")

    def test_uses_anthropic_when_available(self):
        b = self.make_brain()
        block = SimpleNamespace(text="안전한 응답")
        msg = MagicMock(content=[block])
        b.anthropic_client = MagicMock()
        b.anthropic_client.messages.create.return_value = msg
        result = b.regenerate_safe_tts("원본 텍스트", "blocklist:bad")
        self.assertEqual(result, "안전한 응답")
        b.anthropic_client.messages.create.assert_called_once()

    def test_falls_back_to_openai(self):
        b = self.make_brain()
        b.anthropic_client = None
        choice = MagicMock(message=MagicMock(content="OpenAI 안전"))
        resp = MagicMock(choices=[choice])
        b.openai_client = MagicMock()
        b.openai_client.chat.completions.create.return_value = resp
        result = b.regenerate_safe_tts("원본", "reason")
        self.assertEqual(result, "OpenAI 안전")

    def test_anthropic_exception_returns_empty(self):
        b = self.make_brain()
        b.anthropic_client = MagicMock()
        b.anthropic_client.messages.create.side_effect = RuntimeError("API down")
        result = b.regenerate_safe_tts("원본", "reason")
        self.assertEqual(result, "")

    def test_no_clients_returns_empty(self):
        b = self.make_brain()
        b.anthropic_client = None
        b.openai_client = None
        b.zhipuai_client = None
        b.gemini_client = None
        # _ensure_zhipuai/_ensure_gemini 가 client 를 생성하지 못하도록 모킹
        with patch.object(Brain, "_ensure_zhipuai"), \
             patch.object(Brain, "_ensure_gemini"):
            result = b.regenerate_safe_tts("원본", "reason")
        self.assertEqual(result, "")


class OllamaHealthCheckTests(unittest.TestCase):
    """_ollama_healthcheck / reset_ollama_health_cache."""

    def setUp(self):
        # 테스트 간 캐시 격리
        brain_mod.reset_ollama_health_cache()

    def test_reset_clears_state(self):
        brain_mod._ollama_health_cache["ok"] = True
        brain_mod._ollama_health_cache["checked_at"] = 9999.0
        brain_mod.reset_ollama_health_cache()
        self.assertFalse(brain_mod._ollama_health_cache["ok"])
        self.assertEqual(brain_mod._ollama_health_cache["checked_at"], 0.0)

    def test_healthcheck_cache_hit(self):
        # 미리 캐시를 set 후, ollama import 가 일어나지 않아야
        brain_mod._ollama_health_cache["ok"] = True
        brain_mod._ollama_health_cache["checked_at"] = brain_mod.time.time()
        # ollama 모듈은 호출되지 않아야 — import 자체를 못하게 막아도 통과해야
        result = brain_mod._ollama_healthcheck()
        self.assertTrue(result)

    def test_healthcheck_failure_returns_false(self):
        brain_mod.reset_ollama_health_cache()
        # ollama 모듈이 import 가능하더라도 client.list() 가 raise
        fake_client = MagicMock()
        fake_client.list.side_effect = RuntimeError("connection refused")
        fake_ollama = MagicMock()
        fake_ollama.Client.return_value = fake_client
        with patch.dict(sys.modules, {"ollama": fake_ollama}):
            result = brain_mod._ollama_healthcheck()
        self.assertFalse(result)
        self.assertIsNone(brain_mod._ollama_health_cache["client"])

    def test_healthcheck_success_caches_client(self):
        brain_mod.reset_ollama_health_cache()
        fake_client = MagicMock()
        fake_client.list.return_value = []
        fake_ollama = MagicMock()
        fake_ollama.Client.return_value = fake_client
        with patch.dict(sys.modules, {"ollama": fake_ollama}):
            result = brain_mod._ollama_healthcheck()
        self.assertTrue(result)
        self.assertIs(brain_mod._ollama_health_cache["client"], fake_client)


if __name__ == "__main__":
    unittest.main()
