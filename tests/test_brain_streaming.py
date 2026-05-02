"""brain.py 스트리밍 경로 단위 테스트.

Anthropic Messages API 의 streaming 경로 (`_stream_claude`),
tool-use 루프 (`_think_with_tools`), 그리고 비교 모드(`compare_stream`)는
음성 턴마다 통과하는 핫패스라 회귀가 사용자에게 즉시 보임.

여기서는 외부 SDK 를 호출하지 않고 mock 으로 다음을 검증한다:
  - `_stream_claude` 의 emotion prefix 처리와 최종 tuple 형태
  - `_stream_claude` 가 mid-flight 에서 raise 했을 때 think_stream 이 orphan
    user 턴을 롤백하는지
  - `_think_with_tools` happy path + max_iters 안전장치
  - `compare_stream` 양측 성공 / 한 쪽 실패 케이스
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

from sarvis import brain as brain_mod  # noqa: E402
from sarvis.brain import Brain  # noqa: E402


class _FakeStreamCtx:
    """`anthropic.messages.stream(...)` 가 돌려주는 컨텍스트매니저 mock.

    `text_stream` 은 chunk 문자열을 yield 하는 이터러블 (또는 mid-flight
    예외 발생용 generator) 이다.
    """

    def __init__(self, chunks=None, raise_after=None):
        self._chunks = list(chunks or [])
        self._raise_after = raise_after  # int|None: N개 chunk 후 raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False  # 예외 그대로 전파

    @property
    def text_stream(self):
        for i, c in enumerate(self._chunks):
            if self._raise_after is not None and i >= self._raise_after:
                raise RuntimeError("Internal Server Error")
            yield c
        if self._raise_after is not None and self._raise_after >= len(self._chunks):
            raise RuntimeError("Internal Server Error")


def _make_anthropic_client(stream_ctx):
    """messages.stream(...) 호출 시 주어진 컨텍스트매니저를 돌려주는 mock."""
    client = MagicMock()
    client.messages.stream.return_value = stream_ctx
    return client


def _make_openai_stream(chunks, raise_after=None):
    """OpenAI 스타일 스트리밍 (delta.content) 이터러블 생성."""
    def gen():
        for i, text in enumerate(chunks):
            if raise_after is not None and i >= raise_after:
                raise RuntimeError("OpenAI down")
            choice = MagicMock()
            choice.delta = MagicMock(content=text)
            yield MagicMock(choices=[choice])
        if raise_after is not None and raise_after >= len(chunks):
            raise RuntimeError("OpenAI down")
    return gen()


class _BrainNoInitMixin:
    """Brain 을 _init_backend 없이 인스턴스화."""

    def make_brain(self, tool_executor=None):
        with patch.object(Brain, "_init_backend", lambda self: None):
            return Brain(tool_executor=tool_executor)


class StreamClaudeTests(_BrainNoInitMixin, unittest.TestCase):
    """`_stream_claude` 직접 호출 — mock messages.stream."""

    def test_strips_emotion_prefix_before_yielding(self):
        b = self.make_brain()
        ctx = _FakeStreamCtx(chunks=[
            "[emotion:happy] ", "안녕하세요. ", "반갑습니다.",
        ])
        b.client = _make_anthropic_client(ctx)
        b.history = [{"role": "user", "content": "안녕"}]

        outs = list(b._stream_claude())
        # emit 된 chunk 에는 emotion prefix 가 절대 남지 않아야
        for chunk, _, _ in outs:
            if chunk is not None:
                self.assertNotIn("[emotion:", chunk)
        # 합치면 본문만 (감정 prefix 없음)
        body_concat = "".join(c for c, _, _ in outs if c)
        self.assertIn("안녕하세요", body_concat)
        self.assertIn("반갑습니다", body_concat)

    def test_final_tuple_is_none_emotion_body(self):
        b = self.make_brain()
        ctx = _FakeStreamCtx(chunks=["[emotion:happy] ", "본문 텍스트."])
        b.client = _make_anthropic_client(ctx)
        b.history = [{"role": "user", "content": "x"}]

        outs = list(b._stream_claude())
        # 마지막은 (None, Emotion, body)
        last = outs[-1]
        self.assertIsNone(last[0])
        self.assertIsInstance(last[1], Emotion)
        self.assertEqual(last[1], Emotion.HAPPY)
        self.assertIsInstance(last[2], str)
        self.assertIn("본문 텍스트", last[2])
        self.assertNotIn("[emotion:", last[2])

    def test_appends_assistant_to_history(self):
        b = self.make_brain()
        ctx = _FakeStreamCtx(chunks=["[emotion:neutral] hi"])
        b.client = _make_anthropic_client(ctx)
        b.history = [{"role": "user", "content": "안녕"}]

        list(b._stream_claude())
        self.assertEqual(b.history[-1]["role"], "assistant")
        # 원문(prefix 포함) 이 history 에 보존되어야 — 다음 턴 컨텍스트 보존
        self.assertIn("[emotion:neutral]", b.history[-1]["content"])

    def test_short_response_flushes_prefix_buf(self):
        """MAX_PREFIX 도, ']' 도 채우지 못한 짧은 응답이라도 본문은 yield 되어야."""
        b = self.make_brain()
        # prefix 종료 트리거(']' 또는 '\n' 또는 30자) 없이 끝나는 짧은 응답
        ctx = _FakeStreamCtx(chunks=["짧다"])
        b.client = _make_anthropic_client(ctx)
        b.history = [{"role": "user", "content": "x"}]

        outs = list(b._stream_claude())
        chunks = [c for c, _, _ in outs if c is not None]
        self.assertTrue(any("짧다" in c for c in chunks))
        # 최종 tuple
        self.assertIsNone(outs[-1][0])
        self.assertIn("짧다", outs[-1][2])

    def test_no_emotion_prefix_passes_through(self):
        b = self.make_brain()
        # ']' 도 '['도 없이 prefix 트리거되지 않다가 30자 넘는 시점에 flush
        long_text = "이것은 감정 prefix 없이 그대로 흘러야 하는 긴 응답입니다요."
        ctx = _FakeStreamCtx(chunks=[long_text])
        b.client = _make_anthropic_client(ctx)
        b.history = [{"role": "user", "content": "x"}]

        outs = list(b._stream_claude())
        body = "".join(c for c, _, _ in outs if c)
        self.assertIn("그대로", body)
        # parse_emotion 은 태그 없으면 NEUTRAL
        self.assertEqual(outs[-1][1], Emotion.NEUTRAL)


class StreamClaudeRollbackTests(_BrainNoInitMixin, unittest.TestCase):
    """think_stream 경유 — _stream_claude mid-flight raise 시 orphan user 롤백."""

    def test_midflight_exception_rolls_back_user(self):
        b = self.make_brain()
        b.anthropic_client = MagicMock()
        # 3개 chunk 후 raise
        ctx = _FakeStreamCtx(
            chunks=["[emotion:happy] ", "여기까지 ", "오면"],
            raise_after=2,
        )
        b.client = _make_anthropic_client(ctx)

        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "claude"
            # 도구 없이 — _stream_claude 직행
            b.tools = None
            outs = list(b.think_stream("질문"))
        finally:
            cfg.llm_backend = original

        # 친절 에러로 마무리
        last = outs[-1]
        self.assertIsNone(last[0])
        self.assertEqual(last[1], Emotion.CONCERNED)
        self.assertIn("⚠", last[2])
        # orphan user 가 롤백되었어야 — history 에 user 메시지가 남아있지 않음
        self.assertFalse(
            any(h["role"] == "user" and h["content"] == "질문" for h in b.history),
            f"orphan user not rolled back: {b.history}",
        )


class ThinkWithToolsTests(_BrainNoInitMixin, unittest.TestCase):
    """`_think_with_tools` happy path + max_iters."""

    def _resp(self, *, stop_reason, blocks):
        return SimpleNamespace(stop_reason=stop_reason, content=blocks)

    def _text(self, t):
        return SimpleNamespace(type="text", text=t)

    def _tool(self, name, inputs, block_id="t1"):
        return SimpleNamespace(type="tool_use", id=block_id, name=name, input=inputs)

    def test_happy_path_tool_then_final(self):
        tools = MagicMock()
        tools.definitions.return_value = []
        tools.execute.return_value = "12:00"
        b = self.make_brain(tool_executor=tools)
        b.client = MagicMock()
        b.client.messages.create.side_effect = [
            self._resp(stop_reason="tool_use",
                       blocks=[self._tool("get_time", {}, "abc")]),
            self._resp(stop_reason="end_turn",
                       blocks=[self._text("[emotion:happy] 정오입니다.")]),
        ]
        b.history = [{"role": "user", "content": "지금 몇시?"}]

        emotion, body = b._think_with_tools()
        self.assertEqual(emotion, Emotion.HAPPY)
        self.assertIn("정오", body)
        # 도구 결과가 history 에 들어가야
        tools.execute.assert_called_once_with("get_time", {})
        self.assertTrue(any(
            isinstance(h["content"], list) and any(
                isinstance(item, dict) and item.get("type") == "tool_result"
                for item in h["content"]
            )
            for h in b.history
        ))

    def test_max_iters_safety(self):
        tools = MagicMock()
        tools.definitions.return_value = []
        tools.execute.return_value = "tick"
        b = self.make_brain(tool_executor=tools)
        b.client = MagicMock()
        # 항상 tool_use 만 반환 → 8회 후 안전 종료
        b.client.messages.create.return_value = self._resp(
            stop_reason="tool_use",
            blocks=[self._tool("get_time", {}, "t")],
        )
        b.history = [{"role": "user", "content": "x"}]

        emotion, body = b._think_with_tools()
        self.assertEqual(emotion, Emotion.CONCERNED)
        self.assertIn("도구 호출", body)
        self.assertEqual(b.client.messages.create.call_count, 8)


class CompareStreamTests(_BrainNoInitMixin, unittest.TestCase):
    """`compare_stream` — Claude+OpenAI 병렬 호출."""

    def test_both_sources_succeed(self):
        b = self.make_brain()
        # Claude 쪽
        claude_ctx = _FakeStreamCtx(chunks=[
            "[emotion:happy] ", "Claude 응답입니다.",
        ])
        b.anthropic_client = _make_anthropic_client(claude_ctx)
        # OpenAI 쪽
        b.openai_client = MagicMock()
        b.openai_client.chat.completions.create.return_value = _make_openai_stream([
            "[emotion:neutral] ", "OpenAI 응답입니다.",
        ])

        outs = list(b.compare_stream("안녕"))
        sources = {o[0] for o in outs}
        self.assertEqual(sources, {"claude", "openai"})

        # 각 source 별로 정확히 1개의 final tuple (chunk=None) 이 있어야
        finals = [o for o in outs if o[1] is None]  # chunk is None → final
        self.assertEqual(len(finals), 2)
        finals_by_src = {o[0]: o for o in finals}
        self.assertEqual(finals_by_src["claude"][2], Emotion.HAPPY)
        self.assertIn("Claude", finals_by_src["claude"][3])
        self.assertEqual(finals_by_src["openai"][2], Emotion.NEUTRAL)
        self.assertIn("OpenAI", finals_by_src["openai"][3])

        # emit 된 chunk 에 emotion prefix 가 새지 않아야
        for src, chunk, _, _ in outs:
            if chunk is not None:
                self.assertNotIn("[emotion:", chunk)

        # compare_stream 은 history 를 건드리지 않아야
        self.assertEqual(b.history, [])

    def test_one_side_failure_other_still_completes(self):
        b = self.make_brain()
        # Claude 는 raise, OpenAI 는 정상
        b.anthropic_client = MagicMock()
        b.anthropic_client.messages.stream.side_effect = RuntimeError("Anthropic down")
        b.openai_client = MagicMock()
        b.openai_client.chat.completions.create.return_value = _make_openai_stream([
            "[emotion:happy] ", "OpenAI 살아있음.",
        ])

        outs = list(b.compare_stream("hello"))

        # 양쪽 모두 final tuple 이 와야 (한 쪽이 죽어도 active set 이 비어 종료)
        finals = [o for o in outs if o[1] is None]
        finals_by_src = {o[0]: o for o in finals}
        self.assertIn("claude", finals_by_src)
        self.assertIn("openai", finals_by_src)

        # Claude 측은 친절 에러 (CONCERNED + ⚠)
        self.assertEqual(finals_by_src["claude"][2], Emotion.CONCERNED)
        self.assertIn("⚠", finals_by_src["claude"][3])
        # OpenAI 측은 정상 응답
        self.assertEqual(finals_by_src["openai"][2], Emotion.HAPPY)
        self.assertIn("OpenAI 살아있음", finals_by_src["openai"][3])

    def test_no_anthropic_key_yields_friendly_for_claude(self):
        b = self.make_brain()
        b.anthropic_client = None
        b.openai_client = MagicMock()
        b.openai_client.chat.completions.create.return_value = _make_openai_stream([
            "[emotion:neutral] hello",
        ])
        outs = list(b.compare_stream("hi"))
        finals_by_src = {o[0]: o for o in outs if o[1] is None}
        self.assertEqual(finals_by_src["claude"][2], Emotion.CONCERNED)
        self.assertIn("Anthropic", finals_by_src["claude"][3])
        self.assertEqual(finals_by_src["openai"][2], Emotion.NEUTRAL)

    def test_no_openai_key_yields_friendly_for_openai(self):
        b = self.make_brain()
        ctx = _FakeStreamCtx(chunks=["[emotion:neutral] ok"])
        b.anthropic_client = _make_anthropic_client(ctx)
        b.openai_client = None
        outs = list(b.compare_stream("hi"))
        finals_by_src = {o[0]: o for o in outs if o[1] is None}
        self.assertEqual(finals_by_src["openai"][2], Emotion.CONCERNED)
        self.assertIn("OpenAI", finals_by_src["openai"][3])

    def test_context_is_prepended(self):
        b = self.make_brain()
        ctx = _FakeStreamCtx(chunks=["[emotion:neutral] x"])
        b.anthropic_client = _make_anthropic_client(ctx)
        b.openai_client = None
        list(b.compare_stream("질문", context="추가정보"))
        # anthropic 쪽 호출 인자 확인 — 마지막 user 메시지에 context 가 포함되어야
        kwargs = b.anthropic_client.messages.stream.call_args.kwargs
        last_msg = kwargs["messages"][-1]
        self.assertEqual(last_msg["role"], "user")
        self.assertIn("추가정보", last_msg["content"])
        self.assertIn("질문", last_msg["content"])

    def test_history_flattening_for_openai_side(self):
        b = self.make_brain()
        # Claude tool_use 형식의 list content 가 base_history 에 있어도
        # OpenAI 쪽은 평탄화된 string 으로 보여야
        b.history = [{"role": "user", "content": [
            {"type": "text", "text": "이전 메시지"},
            {"type": "tool_use", "id": "x", "name": "y", "input": {}},
        ]}]
        b.anthropic_client = None  # claude 쪽은 친절 에러
        b.openai_client = MagicMock()
        b.openai_client.chat.completions.create.return_value = _make_openai_stream([
            "[emotion:neutral] ok",
        ])
        list(b.compare_stream("새질문"))
        kwargs = b.openai_client.chat.completions.create.call_args.kwargs
        msgs = kwargs["messages"]
        # system + flattened previous user + new user
        self.assertEqual(msgs[0]["role"], "system")
        # flatten 된 메시지들은 모두 string content
        for m in msgs[1:]:
            self.assertIsInstance(m["content"], str)
        # base_history 에 있던 텍스트 블록과 새 질문이 모두 들어있어야
        joined = " ".join(m["content"] for m in msgs[1:])
        self.assertIn("이전 메시지", joined)
        self.assertIn("새질문", joined)


class StreamOllamaTests(_BrainNoInitMixin, unittest.TestCase):
    """`_stream_ollama` — _stream_claude 와 같은 prefix-strip 로직의 ollama 변종."""

    def _ollama_chunks(self, texts):
        """ollama ChatResponse 풍 chunk 이터러블."""
        out = []
        for t in texts:
            out.append(SimpleNamespace(message=SimpleNamespace(content=t)))
        return iter(out)

    def test_strips_emotion_and_emits_final_tuple(self):
        b = self.make_brain()
        b.client = MagicMock()
        b.client.chat.return_value = self._ollama_chunks([
            "[emotion:happy] ", "오늘 날씨 좋네요. ", "산책 가요!",
        ])
        b.history = [{"role": "user", "content": "안녕"}]

        outs = list(b._stream_ollama())
        for chunk, _, _ in outs:
            if chunk is not None:
                self.assertNotIn("[emotion:", chunk)
        last = outs[-1]
        self.assertIsNone(last[0])
        self.assertEqual(last[1], Emotion.HAPPY)
        self.assertIn("산책", last[2])

    def test_short_response_flushes_prefix_buf(self):
        b = self.make_brain()
        b.client = MagicMock()
        b.client.chat.return_value = self._ollama_chunks(["응"])
        b.history = [{"role": "user", "content": "x"}]

        outs = list(b._stream_ollama())
        chunks = [c for c, _, _ in outs if c is not None]
        self.assertTrue(any("응" in c for c in chunks))
        self.assertIsNone(outs[-1][0])

    def test_handles_dict_chunks_and_empty_content(self):
        b = self.make_brain()
        b.client = MagicMock()
        # ChatResponse 가 dict 로 오는 변종 + 빈 content 가 섞여도 죽지 않아야
        chunks = [
            {"message": {"content": ""}},
            {"message": {"content": "[emotion:neutral] "}},
            {"message": {"content": "안녕하세요 반갑습니다."}},
        ]
        b.client.chat.return_value = iter(chunks)
        b.history = [{"role": "user", "content": "x"}]

        outs = list(b._stream_ollama())
        self.assertIsNone(outs[-1][0])
        self.assertIn("안녕하세요", outs[-1][2])

    def test_flattens_list_history(self):
        b = self.make_brain()
        b.client = MagicMock()
        b.client.chat.return_value = self._ollama_chunks(["[emotion:neutral] ok"])
        # tool_use / tool_result 블록이 섞인 list content 도 평탄화되어야
        b.history = [
            {"role": "user", "content": "안녕"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "응답"},
                {"type": "tool_use", "id": "x", "name": "y", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "결과"},
            ]},
        ]
        list(b._stream_ollama())
        kwargs = b.client.chat.call_args.kwargs
        msgs = kwargs["messages"]
        # system + 평탄화된 메시지들은 모두 string content
        self.assertEqual(msgs[0]["role"], "system")
        for m in msgs[1:]:
            self.assertIsInstance(m["content"], str)
        joined = " ".join(m["content"] for m in msgs[1:])
        self.assertIn("응답", joined)
        self.assertIn("결과", joined)


class ThinkStreamCompareDispatchTests(_BrainNoInitMixin, unittest.TestCase):
    """`think_stream` 의 compare 모드 — Claude 우선, 실패 시 OpenAI 폴백."""

    def test_compare_mode_uses_claude_first(self):
        b = self.make_brain()
        b.anthropic_client = MagicMock()
        b.openai_client = MagicMock()
        b.tools = None  # _stream_claude 직행
        ctx = _FakeStreamCtx(chunks=["[emotion:happy] hi"])
        b.client = None  # think_stream 이 self.client = anthropic_client 로 세팅
        b.anthropic_client.messages.stream.return_value = ctx

        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "compare"
            outs = list(b.think_stream("안녕"))
        finally:
            cfg.llm_backend = original

        self.assertIsNone(outs[-1][0])
        self.assertEqual(outs[-1][1], Emotion.HAPPY)

    def test_compare_mode_falls_back_to_openai_on_claude_error(self):
        b = self.make_brain()
        b.anthropic_client = MagicMock()
        b.anthropic_client.messages.stream.side_effect = RuntimeError("claude down")
        b.openai_client = MagicMock()
        b.openai_client.chat.completions.create.return_value = _make_openai_stream([
            "[emotion:neutral] openai answer",
        ])
        b.tools = None

        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "compare"
            outs = list(b.think_stream("hi"))
        finally:
            cfg.llm_backend = original

        # claude 가 raise → orphan user 롤백 후 친절 에러 yield
        last = outs[-1]
        self.assertIsNone(last[0])
        self.assertEqual(last[1], Emotion.CONCERNED)
        self.assertIn("⚠", last[2])

    def test_compare_mode_no_keys_yields_friendly(self):
        b = self.make_brain()
        b.anthropic_client = None
        b.openai_client = None
        b.tools = None
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            cfg.llm_backend = "compare"
            outs = list(b.think_stream("hi"))
        finally:
            cfg.llm_backend = original
        last = outs[-1]
        self.assertIsNone(last[0])
        self.assertIn("비교 모드", last[2])
        # orphan user 롤백
        self.assertEqual(b.history, [])


if __name__ == "__main__":
    unittest.main()
