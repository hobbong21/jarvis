"""analysis.py 단위 테스트 — fan-out 휴리스틱 분석기.

architect 사이클 #7 follow-up: 의도/감정/메모리 휴리스틱이 결정론적인지,
parallel_analyze 가 4개 분석기를 모두 한 번에 모아 dict 로 반환하는지 검증.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")

from sarvis import analysis  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class IntentHeuristicTests(unittest.TestCase):
    def test_empty_is_smalltalk(self):
        self.assertEqual(_run(analysis._intent("")), "smalltalk")
        self.assertEqual(_run(analysis._intent("   ")), "smalltalk")

    def test_question_word_prefix(self):
        for q in ("뭐해", "어디 가", "왜 그래", "언제 시작?"):
            self.assertEqual(_run(analysis._intent(q)), "question", q)

    def test_question_endmark(self):
        self.assertEqual(_run(analysis._intent("내일 비가 와?")), "question")
        self.assertEqual(_run(analysis._intent("정말 그럴까")), "question")

    def test_command_verb(self):
        for c in ("시간 알려줘", "음악 재생", "타이머 5분", "기억해줘"):
            self.assertEqual(_run(analysis._intent(c)), "command", c)

    def test_emotion_short_text(self):
        self.assertEqual(_run(analysis._intent("너무 행복해")), "emotion")
        self.assertEqual(_run(analysis._intent("ㅠㅠ 슬퍼")), "emotion")

    def test_smalltalk_default(self):
        self.assertEqual(_run(analysis._intent("그래")), "smalltalk")


class EmotionHintTests(unittest.TestCase):
    def test_happy_dominates(self):
        self.assertEqual(_run(analysis._emotion_hint("기뻐 신나 최고")), "happy")

    def test_angry_detected(self):
        self.assertEqual(_run(analysis._emotion_hint("진짜 짜증나")), "angry")

    def test_anxious_detected(self):
        self.assertEqual(_run(analysis._emotion_hint("너무 불안해")), "anxious")

    def test_neutral_when_none(self):
        self.assertEqual(_run(analysis._emotion_hint("그냥 일이야")), "neutral")


class FaceContextTests(unittest.TestCase):
    def test_no_session(self):
        self.assertEqual(_run(analysis._face_context(None)), "")

    def test_no_vision(self):
        self.assertEqual(_run(analysis._face_context(SimpleNamespace())), "")

    def test_returns_user(self):
        sess = SimpleNamespace(vision=SimpleNamespace(current_user="민수"))
        self.assertEqual(_run(analysis._face_context(sess)), "민수")

    def test_empty_user(self):
        sess = SimpleNamespace(vision=SimpleNamespace(current_user=None))
        self.assertEqual(_run(analysis._face_context(sess)), "")


class MemoryHintTests(unittest.TestCase):
    def test_picks_up_keywords(self):
        out = _run(analysis._memory_hint("내 생일 기억해?"))
        self.assertIn("내", out)
        self.assertIn("생일", out)
        self.assertIn("기억", out)
        self.assertLessEqual(len(out), 3)

    def test_no_keywords(self):
        self.assertEqual(_run(analysis._memory_hint("xyz abc")), [])


class ParallelAnalyzeTests(unittest.TestCase):
    def test_returns_full_dict(self):
        sess = SimpleNamespace(vision=SimpleNamespace(current_user="철수"))
        result = _run(analysis.parallel_analyze("내 생일 알려줘", session=sess))
        self.assertEqual(result["intent"], "command")
        self.assertEqual(result["face"], "철수")
        self.assertIn("내", result["memory_hint"])
        self.assertIn("ms", result)
        self.assertGreaterEqual(result["ms"], 0.0)

    def test_handles_no_session(self):
        result = _run(analysis.parallel_analyze("뭐해?"))
        self.assertEqual(result["intent"], "question")
        self.assertEqual(result["face"], "")


class AnalysisToContextTests(unittest.TestCase):
    def test_empty_dict_returns_empty(self):
        self.assertEqual(analysis.analysis_to_context({}), "")
        self.assertEqual(analysis.analysis_to_context(None), "")

    def test_smalltalk_intent_dropped(self):
        ctx = analysis.analysis_to_context({"intent": "smalltalk"})
        self.assertEqual(ctx, "")

    def test_full_serialization(self):
        ctx = analysis.analysis_to_context({
            "intent": "question",
            "emotion_hint": "happy",
            "face": "민수",
            "memory_hint": ["내", "생일"],
        })
        self.assertIn("의도=question", ctx)
        self.assertIn("감정신호=happy", ctx)
        self.assertIn("식별된사용자=민수", ctx)
        self.assertIn("기억키워드=내,생일", ctx)

    def test_neutral_emotion_dropped(self):
        ctx = analysis.analysis_to_context({
            "intent": "command",
            "emotion_hint": "neutral",
            "face": "",
            "memory_hint": [],
        })
        self.assertEqual(ctx, "의도=command")


if __name__ == "__main__":
    unittest.main()
