"""brain.py 백엔드 디스패치 / 초기화 / 단순 think / 안전 재작성 커버리지.

Task #24: streaming 백엔드와 tool 루프는 test_brain_streaming.py 가 커버한다.
이 파일은 그 외의 미커버 분기 — _init_backend 의 각 backend, _ensure_*,
_think_*_simple, _think_ollama, think_stream 의 openai/zhipuai/gemini/ollama
디스패치, regenerate_safe_tts 의 anthropic/openai/zhipuai/gemini 폴백 — 를
mock SDK 로 커버한다. 외부 호출 없음.
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


class _BrainNoInitMixin:
    def make_brain(self, tool_executor=None):
        with patch.object(Brain, "_init_backend", lambda self: None):
            return Brain(tool_executor=tool_executor)


def _mk_completion(text: str):
    msg = MagicMock()
    msg.content = text
    return MagicMock(choices=[MagicMock(message=msg)])


def _openai_stream(chunks):
    out = []
    for t in chunks:
        choice = MagicMock()
        choice.delta = MagicMock(content=t)
        out.append(MagicMock(choices=[choice]))
    return iter(out)


# ============================================================
# _ensure_* — SDK import + client construction
# ============================================================
class EnsureClientTests(_BrainNoInitMixin, unittest.TestCase):
    def test_ensure_anthropic_skips_when_no_key(self):
        b = self.make_brain()
        b.anthropic_client = None
        from sarvis.config import cfg
        original = cfg.anthropic_api_key
        try:
            cfg.anthropic_api_key = ""
            b._ensure_anthropic()
            self.assertIsNone(b.anthropic_client)
        finally:
            cfg.anthropic_api_key = original

    def test_ensure_anthropic_idempotent(self):
        b = self.make_brain()
        sentinel = object()
        b.anthropic_client = sentinel
        b._ensure_anthropic()
        self.assertIs(b.anthropic_client, sentinel)

    def test_ensure_anthropic_constructs_client(self):
        b = self.make_brain()
        b.anthropic_client = None
        from sarvis.config import cfg
        original = cfg.anthropic_api_key
        try:
            cfg.anthropic_api_key = "sk-test"
            fake_mod = MagicMock()
            instance = object()
            fake_mod.Anthropic.return_value = instance
            with patch.dict(sys.modules, {"anthropic": fake_mod}):
                b._ensure_anthropic()
            self.assertIs(b.anthropic_client, instance)
        finally:
            cfg.anthropic_api_key = original

    def test_ensure_anthropic_swallows_init_error(self):
        b = self.make_brain()
        b.anthropic_client = None
        from sarvis.config import cfg
        original = cfg.anthropic_api_key
        try:
            cfg.anthropic_api_key = "sk-test"
            fake_mod = MagicMock()
            fake_mod.Anthropic.side_effect = RuntimeError("boom")
            with patch.dict(sys.modules, {"anthropic": fake_mod}):
                b._ensure_anthropic()  # 예외 안 나야
            self.assertIsNone(b.anthropic_client)
        finally:
            cfg.anthropic_api_key = original

    def test_ensure_openai_constructs_client(self):
        b = self.make_brain()
        b.openai_client = None
        from sarvis.config import cfg
        original = cfg.openai_api_key
        try:
            cfg.openai_api_key = "sk-oa"
            fake_mod = MagicMock()
            inst = object()
            fake_mod.OpenAI.return_value = inst
            with patch.dict(sys.modules, {"openai": fake_mod}):
                b._ensure_openai()
            self.assertIs(b.openai_client, inst)
        finally:
            cfg.openai_api_key = original

    def test_ensure_openai_no_key_skipped(self):
        b = self.make_brain()
        b.openai_client = None
        from sarvis.config import cfg
        original = cfg.openai_api_key
        try:
            cfg.openai_api_key = ""
            b._ensure_openai()
            self.assertIsNone(b.openai_client)
        finally:
            cfg.openai_api_key = original

    def test_ensure_openai_swallow_error(self):
        b = self.make_brain()
        b.openai_client = None
        from sarvis.config import cfg
        original = cfg.openai_api_key
        try:
            cfg.openai_api_key = "sk-oa"
            fake_mod = MagicMock()
            fake_mod.OpenAI.side_effect = RuntimeError("nope")
            with patch.dict(sys.modules, {"openai": fake_mod}):
                b._ensure_openai()
            self.assertIsNone(b.openai_client)
        finally:
            cfg.openai_api_key = original

    def test_ensure_zhipuai_no_key(self):
        b = self.make_brain()
        b.zhipuai_client = None
        from sarvis.config import cfg
        original = cfg.zhipuai_api_key
        try:
            cfg.zhipuai_api_key = ""
            b._ensure_zhipuai()
            self.assertIsNone(b.zhipuai_client)
        finally:
            cfg.zhipuai_api_key = original

    def test_ensure_zhipuai_constructs_with_base_url(self):
        b = self.make_brain()
        b.zhipuai_client = None
        from sarvis.config import cfg
        original = cfg.zhipuai_api_key
        try:
            cfg.zhipuai_api_key = "zk"
            fake_mod = MagicMock()
            inst = object()
            fake_mod.OpenAI.return_value = inst
            with patch.dict(sys.modules, {"openai": fake_mod}):
                b._ensure_zhipuai()
            self.assertIs(b.zhipuai_client, inst)
            kwargs = fake_mod.OpenAI.call_args.kwargs
            self.assertEqual(kwargs["base_url"], cfg.zhipuai_base_url)
        finally:
            cfg.zhipuai_api_key = original

    def test_ensure_zhipuai_swallow_error(self):
        b = self.make_brain()
        b.zhipuai_client = None
        from sarvis.config import cfg
        original = cfg.zhipuai_api_key
        try:
            cfg.zhipuai_api_key = "zk"
            fake_mod = MagicMock()
            fake_mod.OpenAI.side_effect = RuntimeError("x")
            with patch.dict(sys.modules, {"openai": fake_mod}):
                b._ensure_zhipuai()
            self.assertIsNone(b.zhipuai_client)
        finally:
            cfg.zhipuai_api_key = original

    def test_ensure_gemini_constructs_with_base_url(self):
        b = self.make_brain()
        b.gemini_client = None
        from sarvis.config import cfg
        original = cfg.gemini_api_key
        try:
            cfg.gemini_api_key = "gk"
            fake_mod = MagicMock()
            inst = object()
            fake_mod.OpenAI.return_value = inst
            with patch.dict(sys.modules, {"openai": fake_mod}):
                b._ensure_gemini()
            self.assertIs(b.gemini_client, inst)
            kwargs = fake_mod.OpenAI.call_args.kwargs
            self.assertEqual(kwargs["base_url"], cfg.gemini_base_url)
        finally:
            cfg.gemini_api_key = original

    def test_ensure_gemini_no_key(self):
        b = self.make_brain()
        b.gemini_client = None
        from sarvis.config import cfg
        original = cfg.gemini_api_key
        try:
            cfg.gemini_api_key = ""
            b._ensure_gemini()
            self.assertIsNone(b.gemini_client)
        finally:
            cfg.gemini_api_key = original

    def test_ensure_gemini_swallow_error(self):
        b = self.make_brain()
        b.gemini_client = None
        from sarvis.config import cfg
        original = cfg.gemini_api_key
        try:
            cfg.gemini_api_key = "gk"
            fake_mod = MagicMock()
            fake_mod.OpenAI.side_effect = RuntimeError("nope")
            with patch.dict(sys.modules, {"openai": fake_mod}):
                b._ensure_gemini()
            self.assertIsNone(b.gemini_client)
        finally:
            cfg.gemini_api_key = original

    def test_get_client_returns_anthropic(self):
        b = self.make_brain()
        sentinel = object()
        b.anthropic_client = sentinel
        self.assertIs(b.get_client(), sentinel)


# ============================================================
# _init_backend — per-backend dispatch
# ============================================================
class InitBackendTests(unittest.TestCase):
    """Brain.__init__ → _init_backend 의 각 백엔드 분기."""

    def _make(self, *, ensure_anthropic=None, ensure_openai=None,
              ensure_zhipuai=None, ensure_gemini=None):
        """주어진 _ensure_* 사이드이펙트로 Brain 인스턴스 생성."""
        with patch.object(Brain, "_ensure_anthropic",
                          autospec=True, side_effect=ensure_anthropic
                          or (lambda self: None)), \
             patch.object(Brain, "_ensure_openai",
                          autospec=True, side_effect=ensure_openai
                          or (lambda self: None)), \
             patch.object(Brain, "_ensure_zhipuai",
                          autospec=True, side_effect=ensure_zhipuai
                          or (lambda self: None)), \
             patch.object(Brain, "_ensure_gemini",
                          autospec=True, side_effect=ensure_gemini
                          or (lambda self: None)):
            return Brain()

    def test_compare_mode_loads_both(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "compare"
        try:
            def set_anthr(self):
                self.anthropic_client = "A"

            def set_openai(self):
                self.openai_client = "O"

            b = self._make(ensure_anthropic=set_anthr, ensure_openai=set_openai)
            self.assertEqual(b.client, "A")  # vision 기본은 anthropic
        finally:
            cfg.llm_backend = original

    def test_compare_mode_warns_when_no_keys(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "compare"
        try:
            b = self._make()  # 아무 키도 세팅 안 함
            self.assertIsNone(b.client)
        finally:
            cfg.llm_backend = original

    def test_claude_mode(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "claude"
        try:
            def set_anthr(self):
                self.anthropic_client = "A"
            b = self._make(ensure_anthropic=set_anthr)
            self.assertEqual(b.client, "A")
        finally:
            cfg.llm_backend = original

    def test_claude_mode_no_key_warns(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "claude"
        try:
            b = self._make()
            self.assertIsNone(b.client)
        finally:
            cfg.llm_backend = original

    def test_openai_mode(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "openai"
        try:
            def set_oa(self):
                self.openai_client = "O"
            b = self._make(ensure_openai=set_oa)
            self.assertEqual(b.client, "O")
        finally:
            cfg.llm_backend = original

    def test_openai_mode_no_key_warns(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "openai"
        try:
            b = self._make()
            self.assertIsNone(b.client)
        finally:
            cfg.llm_backend = original

    def test_zhipuai_mode(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "zhipuai"
        try:
            def set_zp(self):
                self.zhipuai_client = "Z"
            b = self._make(ensure_zhipuai=set_zp)
            self.assertEqual(b.client, "Z")
        finally:
            cfg.llm_backend = original

    def test_zhipuai_mode_no_key(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "zhipuai"
        try:
            b = self._make()
            self.assertIsNone(b.client)
        finally:
            cfg.llm_backend = original

    def test_gemini_mode(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "gemini"
        try:
            def set_gm(self):
                self.gemini_client = "G"
            b = self._make(ensure_gemini=set_gm)
            self.assertEqual(b.client, "G")
        finally:
            cfg.llm_backend = original

    def test_gemini_mode_no_key(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "gemini"
        try:
            b = self._make()
            self.assertIsNone(b.client)
        finally:
            cfg.llm_backend = original

    def test_ollama_mode_uses_show(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "ollama"
        try:
            fake_ollama = MagicMock()
            client = MagicMock()
            fake_ollama.Client.return_value = client
            client.show.return_value = {"ok": True}
            with patch.dict(sys.modules, {"ollama": fake_ollama}), \
                 patch.object(Brain, "_ensure_anthropic", lambda self: None):
                b = Brain()
            self.assertIs(b.client, client)
            client.show.assert_called_once()
            client.pull.assert_not_called()
        finally:
            cfg.llm_backend = original

    def test_ollama_mode_pulls_when_missing(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "ollama"
        try:
            fake_ollama = MagicMock()
            client = MagicMock()
            fake_ollama.Client.return_value = client
            client.show.side_effect = RuntimeError("no model")
            with patch.dict(sys.modules, {"ollama": fake_ollama}):
                b = Brain()
            self.assertIs(b.client, client)
            client.pull.assert_called_once()
        finally:
            cfg.llm_backend = original

    def test_unknown_backend_raises(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        cfg.llm_backend = "totally-fake"
        try:
            with self.assertRaises(ValueError):
                Brain()
        finally:
            cfg.llm_backend = original


# ============================================================
# _think_*_simple wrappers
# ============================================================
class ThinkSimpleWrapperTests(_BrainNoInitMixin, unittest.TestCase):
    def test_openai_simple_uses_openai_client(self):
        b = self.make_brain()
        b.openai_client = MagicMock()
        b.openai_client.chat.completions.create.return_value = _mk_completion("hi")
        b.history = [{"role": "user", "content": "안녕"}]
        emo, body = b._think_openai_simple()
        self.assertIsInstance(emo, Emotion)
        self.assertIn("hi", body)

    def test_zhipuai_simple_uses_zhipuai_client(self):
        b = self.make_brain()
        b.zhipuai_client = MagicMock()
        b.zhipuai_client.chat.completions.create.return_value = _mk_completion("z")
        b.history = [{"role": "user", "content": "안녕"}]
        emo, body = b._think_zhipuai_simple()
        self.assertIn("z", body)

    def test_gemini_simple_uses_gemini_client(self):
        b = self.make_brain()
        b.gemini_client = MagicMock()
        b.gemini_client.chat.completions.create.return_value = _mk_completion("g")
        b.history = [{"role": "user", "content": "안녕"}]
        emo, body = b._think_gemini_simple()
        self.assertIn("g", body)

    def test_claude_simple_appends_assistant(self):
        b = self.make_brain()
        b.client = MagicMock()
        # anthropic Messages API: msg.content[0].text
        msg = MagicMock()
        msg.content = [SimpleNamespace(text="[emotion:happy] 안녕!")]
        b.client.messages.create.return_value = msg
        b.history = [{"role": "user", "content": "안녕"}]
        emo, body = b._think_claude_simple()
        self.assertEqual(emo, Emotion.HAPPY)
        self.assertIn("안녕!", body)
        self.assertEqual(b.history[-1]["role"], "assistant")


# ============================================================
# _think_ollama (non-stream)
# ============================================================
class ThinkOllamaTests(_BrainNoInitMixin, unittest.TestCase):
    def test_basic_response_and_history(self):
        b = self.make_brain()
        b.client = MagicMock()
        b.client.chat.return_value = {"message": {"content": "[emotion:neutral] 응답"}}
        b.history = [{"role": "user", "content": "안녕"}]
        emo, body = b._think_ollama()
        self.assertEqual(emo, Emotion.NEUTRAL)
        self.assertIn("응답", body)
        # system prompt 포함
        kwargs = b.client.chat.call_args.kwargs
        self.assertEqual(kwargs["messages"][0]["role"], "system")

    def test_flattens_list_history_with_tool_blocks(self):
        b = self.make_brain()
        b.client = MagicMock()
        b.client.chat.return_value = {"message": {"content": "ok"}}
        b.history = [
            {"role": "user", "content": "안녕"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "응답"},
                {"type": "tool_use", "id": "x", "name": "get_time", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "12:00"},
            ]},
        ]
        b._think_ollama()
        msgs = b.client.chat.call_args.kwargs["messages"]
        for m in msgs[1:]:
            self.assertIsInstance(m["content"], str)
        joined = " ".join(m["content"] for m in msgs[1:])
        self.assertIn("도구 호출", joined)
        self.assertIn("12:00", joined)


# ============================================================
# think() — backend dispatch
# ============================================================
class ThinkDispatchTests(_BrainNoInitMixin, unittest.TestCase):
    def _switch(self, backend):
        from sarvis.config import cfg
        cfg.llm_backend = backend

    def test_dispatches_to_zhipuai(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.client = MagicMock()
            cfg.llm_backend = "zhipuai"
            with patch.object(Brain, "_think_zhipuai_simple",
                              return_value=(Emotion.NEUTRAL, "z")) as m:
                emo, body = b.think("hi")
            m.assert_called_once()
            self.assertEqual(body, "z")
        finally:
            cfg.llm_backend = original

    def test_dispatches_to_gemini(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.client = MagicMock()
            cfg.llm_backend = "gemini"
            with patch.object(Brain, "_think_gemini_simple",
                              return_value=(Emotion.NEUTRAL, "g")) as m:
                emo, body = b.think("hi")
            m.assert_called_once()
            self.assertEqual(body, "g")
        finally:
            cfg.llm_backend = original

    def test_dispatches_to_openai(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.client = MagicMock()
            cfg.llm_backend = "openai"
            with patch.object(Brain, "_think_openai_simple",
                              return_value=(Emotion.NEUTRAL, "o")) as m:
                emo, body = b.think("hi")
            m.assert_called_once()
        finally:
            cfg.llm_backend = original

    def test_dispatches_to_ollama_for_unknown(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.client = MagicMock()
            cfg.llm_backend = "ollama"
            with patch.object(Brain, "_think_ollama",
                              return_value=(Emotion.NEUTRAL, "ol")) as m:
                emo, body = b.think("hi")
            m.assert_called_once()
        finally:
            cfg.llm_backend = original

    def test_compare_uses_anthropic_with_tools(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            tools = MagicMock()
            b = self.make_brain(tool_executor=tools)
            b.anthropic_client = MagicMock()
            b.openai_client = None
            cfg.llm_backend = "compare"
            with patch.object(Brain, "_think_with_tools",
                              return_value=(Emotion.NEUTRAL, "c")) as m:
                emo, body = b.think("hi")
            m.assert_called_once()
            self.assertEqual(body, "c")
        finally:
            cfg.llm_backend = original

    def test_compare_uses_anthropic_simple_when_no_tools(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.anthropic_client = MagicMock()
            b.openai_client = None
            cfg.llm_backend = "compare"
            with patch.object(Brain, "_think_claude_simple",
                              return_value=(Emotion.NEUTRAL, "c")) as m:
                emo, body = b.think("hi")
            m.assert_called_once()
        finally:
            cfg.llm_backend = original

    def test_compare_falls_back_to_openai(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.anthropic_client = None
            b.openai_client = MagicMock()
            cfg.llm_backend = "compare"
            with patch.object(Brain, "_think_openai_simple",
                              return_value=(Emotion.NEUTRAL, "o")) as m:
                emo, body = b.think("hi")
            m.assert_called_once()
        finally:
            cfg.llm_backend = original

    def test_compare_no_keys_returns_friendly(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.anthropic_client = None
            b.openai_client = None
            cfg.llm_backend = "compare"
            emo, body = b.think("hi")
            self.assertEqual(emo, Emotion.CONCERNED)
            self.assertIn("API 키", body)
        finally:
            cfg.llm_backend = original

    def test_context_prepended_into_user_message(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.client = MagicMock()
            cfg.llm_backend = "openai"
            with patch.object(Brain, "_think_openai_simple",
                              return_value=(Emotion.NEUTRAL, "o")):
                b.think("질문", context="컨텍스트")
            self.assertIn("컨텍스트", b.history[0]["content"])
            self.assertIn("질문", b.history[0]["content"])
        finally:
            cfg.llm_backend = original


# ============================================================
# think_stream — openai/zhipuai/gemini/ollama dispatch
# ============================================================
class ThinkStreamDispatchTests(_BrainNoInitMixin, unittest.TestCase):
    def test_dispatch_openai(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.client = MagicMock()
            cfg.llm_backend = "openai"
            with patch.object(Brain, "_stream_openai",
                              return_value=iter([("hi", None, None),
                                                 (None, Emotion.HAPPY, "ok")])):
                outs = list(b.think_stream("질문"))
            self.assertEqual(outs[-1][1], Emotion.HAPPY)
        finally:
            cfg.llm_backend = original

    def test_dispatch_zhipuai(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.client = MagicMock()
            cfg.llm_backend = "zhipuai"
            with patch.object(Brain, "_stream_zhipuai",
                              return_value=iter([(None, Emotion.NEUTRAL, "z")])):
                outs = list(b.think_stream("q"))
            self.assertEqual(outs[-1][2], "z")
        finally:
            cfg.llm_backend = original

    def test_dispatch_gemini(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.client = MagicMock()
            cfg.llm_backend = "gemini"
            with patch.object(Brain, "_stream_gemini",
                              return_value=iter([(None, Emotion.NEUTRAL, "g")])):
                outs = list(b.think_stream("q"))
            self.assertEqual(outs[-1][2], "g")
        finally:
            cfg.llm_backend = original

    def test_dispatch_ollama_default(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.client = MagicMock()
            cfg.llm_backend = "ollama"
            with patch.object(Brain, "_stream_ollama",
                              return_value=iter([(None, Emotion.NEUTRAL, "ol")])):
                outs = list(b.think_stream("q"))
            self.assertEqual(outs[-1][2], "ol")
        finally:
            cfg.llm_backend = original

    def test_compare_falls_back_to_openai_when_no_anthropic(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.anthropic_client = None
            b.openai_client = MagicMock()
            b.openai_client.chat.completions.create.return_value = _openai_stream(
                ["[emotion:neutral] ", "hi"]
            )
            cfg.llm_backend = "compare"
            outs = list(b.think_stream("q"))
            last = outs[-1]
            self.assertIsNone(last[0])
            self.assertEqual(last[1], Emotion.NEUTRAL)
        finally:
            cfg.llm_backend = original

    def test_compare_openai_branch_raises_friendly(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            b = self.make_brain()
            b.anthropic_client = None
            b.openai_client = MagicMock()
            b.openai_client.chat.completions.create.side_effect = RuntimeError(
                "openai down"
            )
            cfg.llm_backend = "compare"
            outs = list(b.think_stream("q"))
            last = outs[-1]
            self.assertEqual(last[1], Emotion.CONCERNED)
            self.assertIn("⚠", last[2])
            # orphan user 롤백
            self.assertEqual(b.history, [])
        finally:
            cfg.llm_backend = original

    def test_dispatch_claude_with_tools_uses_tool_loop(self):
        from sarvis.config import cfg
        original = cfg.llm_backend
        try:
            tools = MagicMock()
            b = self.make_brain(tool_executor=tools)
            b.client = MagicMock()
            cfg.llm_backend = "claude"
            with patch.object(Brain, "_think_with_tools",
                              return_value=(Emotion.HAPPY, "최종")) as m:
                outs = list(b.think_stream("q"))
            m.assert_called_once()
            self.assertEqual(outs[-1][1], Emotion.HAPPY)
            self.assertEqual(outs[-1][2], "최종")
        finally:
            cfg.llm_backend = original


# ============================================================
# regenerate_safe_tts — fallback chain
# ============================================================
class RegenerateSafeTtsTests(_BrainNoInitMixin, unittest.TestCase):
    def test_empty_input_returns_empty(self):
        b = self.make_brain()
        self.assertEqual(b.regenerate_safe_tts("", "any"), "")
        self.assertEqual(b.regenerate_safe_tts("   ", "any"), "")

    def test_uses_anthropic_first(self):
        b = self.make_brain()
        b.anthropic_client = MagicMock()
        msg = MagicMock()
        msg.content = [SimpleNamespace(text="짧은 응답")]
        b.anthropic_client.messages.create.return_value = msg
        out = b.regenerate_safe_tts("원본 텍스트", "차단 사유")
        self.assertEqual(out, "짧은 응답")

    def test_falls_back_to_openai_when_no_anthropic(self):
        b = self.make_brain()
        b.anthropic_client = None
        b.openai_client = MagicMock()
        b.openai_client.chat.completions.create.return_value = _mk_completion("oa안전")
        out = b.regenerate_safe_tts("원본", "사유")
        self.assertEqual(out, "oa안전")

    def test_falls_back_to_zhipuai(self):
        b = self.make_brain()
        b.anthropic_client = None
        b.openai_client = None
        # _ensure_zhipuai 가 client 세팅하도록 모킹
        def set_zp(self):
            self.zhipuai_client = MagicMock()
            self.zhipuai_client.chat.completions.create.return_value = (
                _mk_completion("zp안전")
            )
        with patch.object(Brain, "_ensure_zhipuai", autospec=True,
                          side_effect=set_zp):
            out = b.regenerate_safe_tts("원본", "사유")
        self.assertEqual(out, "zp안전")

    def test_falls_back_to_gemini(self):
        b = self.make_brain()
        b.anthropic_client = None
        b.openai_client = None
        b.zhipuai_client = None

        def set_gm(self):
            self.gemini_client = MagicMock()
            self.gemini_client.chat.completions.create.return_value = (
                _mk_completion("gm안전")
            )
        with patch.object(Brain, "_ensure_zhipuai", autospec=True,
                          side_effect=lambda self: None), \
             patch.object(Brain, "_ensure_gemini", autospec=True,
                          side_effect=set_gm):
            out = b.regenerate_safe_tts("원본", "사유")
        self.assertEqual(out, "gm안전")

    def test_returns_empty_when_all_clients_missing(self):
        b = self.make_brain()
        b.anthropic_client = None
        b.openai_client = None
        b.zhipuai_client = None
        b.gemini_client = None
        with patch.object(Brain, "_ensure_zhipuai", autospec=True,
                          side_effect=lambda self: None), \
             patch.object(Brain, "_ensure_gemini", autospec=True,
                          side_effect=lambda self: None):
            self.assertEqual(b.regenerate_safe_tts("원본", "사유"), "")

    def test_swallows_exceptions(self):
        b = self.make_brain()
        b.anthropic_client = MagicMock()
        b.anthropic_client.messages.create.side_effect = RuntimeError("boom")
        # 예외 시 빈 문자열
        self.assertEqual(b.regenerate_safe_tts("원본", "사유"), "")


# ============================================================
# _ollama_healthcheck — failure path (cache 갱신, raise 안 함)
# ============================================================
class OllamaHealthcheckTests(unittest.TestCase):
    def test_failure_returns_false_and_caches(self):
        brain_mod.reset_ollama_health_cache()
        fake = MagicMock()
        fake.Client.side_effect = RuntimeError("no ollama")
        with patch.dict(sys.modules, {"ollama": fake}):
            self.assertFalse(brain_mod._ollama_healthcheck())
        self.assertFalse(brain_mod._ollama_health_cache["ok"])
        self.assertIsNone(brain_mod._ollama_health_cache["client"])

    def test_success_caches_client(self):
        brain_mod.reset_ollama_health_cache()
        fake = MagicMock()
        client = MagicMock()
        fake.Client.return_value = client
        client.list.return_value = {"models": []}
        with patch.dict(sys.modules, {"ollama": fake}):
            self.assertTrue(brain_mod._ollama_healthcheck())
        self.assertIs(brain_mod._ollama_health_cache["client"], client)
        # 두 번째 호출은 캐시 히트 → Client 재호출 없음
        fake.Client.reset_mock()
        with patch.dict(sys.modules, {"ollama": fake}):
            self.assertTrue(brain_mod._ollama_healthcheck())
        fake.Client.assert_not_called()
        brain_mod.reset_ollama_health_cache()


if __name__ == "__main__":
    unittest.main()
