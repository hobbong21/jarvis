"""F-04 회의록 단위 테스트 — meeting.py.

- append_chunk: 빈/잡음 거부, 정상 누적, 종료 후 거부.
- summarize: 정상 JSON 흐름, 깨진 LLM 응답 fallback, 예외 발생 시 fallback.
- 영속화 round-trip: save → load → 동등성.
- transcript_md / to_markdown: 핵심 섹션 포함.
- MeetingRegistry: 동시 회의 1개 강제 + list 정렬.
- parse_summary_json: 코드 펜스/앞뒤 잡음 강건성.
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sarvis.meeting import (
    Meeting,
    MeetingRegistry,
    Utterance,
    parse_summary_json,
)


def _fake_summary_fn_ok(transcript: str) -> dict:
    return {
        "summary": "결제 모듈 도입을 결정했고 다음 주 금요일까지 PoC 를 진행한다.",
        "decisions": ["결제 SDK A 채택", "PoC 마감 다음 주 금요일"],
        "action_items": [
            {"owner": "민수", "task": "PoC 코드 초안", "due": "다음 주 금요일"},
            {"owner": "지수", "task": "결제 보안 검토", "due": ""},
        ],
    }


def _fake_summary_fn_broken(transcript: str) -> dict:
    # 형식이 어긋난 응답 — Meeting.summarize 가 정규화로 흡수해야.
    return {"summary": "  ", "decisions": "이건 리스트가 아님", "action_items": "string"}


def _fake_summary_fn_raises(transcript: str) -> dict:
    raise RuntimeError("LLM down")


class MeetingCoreTests(unittest.TestCase):
    def _new_meeting(self) -> Meeting:
        return Meeting(meeting_id="test-001", title="테스트 회의", started_at=time.time())

    def test_append_chunk_filters_noise(self):
        m = self._new_meeting()
        self.assertIsNone(m.append_chunk(""))
        self.assertIsNone(m.append_chunk("  "))
        self.assertIsNone(m.append_chunk("아"))
        self.assertIsNone(m.append_chunk("어"))
        self.assertIsNone(m.append_chunk("."))
        ut = m.append_chunk("결제 모듈을 도입합시다")
        self.assertIsInstance(ut, Utterance)
        self.assertEqual(ut.text, "결제 모듈을 도입합시다")
        self.assertEqual(len(m.utterances), 1)

    def test_append_chunk_rejected_after_end(self):
        m = self._new_meeting()
        m.append_chunk("발언 1")
        m.end()
        self.assertIsNone(m.append_chunk("종료 후 발언"))
        self.assertEqual(len(m.utterances), 1)

    def test_summarize_normalizes_ok_response(self):
        m = self._new_meeting()
        m.append_chunk("결제 모듈 도입 검토")
        m.append_chunk("다음주까지 PoC 진행")
        out = m.summarize(_fake_summary_fn_ok)
        self.assertIn("결제", out["summary"])
        self.assertEqual(len(m.decisions), 2)
        self.assertEqual(len(m.action_items), 2)
        self.assertEqual(m.action_items[0]["owner"], "민수")
        self.assertEqual(m.status, "summarized")
        self.assertIsNotNone(m.ended_at)

    def test_summarize_handles_broken_response(self):
        m = self._new_meeting()
        m.append_chunk("뭔가 발언")
        m.summarize(_fake_summary_fn_broken)
        # 빈 summary → fallback 으로 채워졌어야.
        self.assertTrue(m.summary)
        # 잘못된 타입은 빈 list 로 정규화.
        self.assertEqual(m.decisions, [])
        self.assertEqual(m.action_items, [])

    def test_summarize_handles_exception(self):
        m = self._new_meeting()
        m.append_chunk("발언")
        m.summarize(_fake_summary_fn_raises)
        # 예외 → "(요약 실패 — ...)" 로 채워지거나 fallback. 어느 쪽이든 truthy.
        self.assertTrue(m.summary)
        self.assertEqual(m.status, "summarized")

    def test_to_markdown_contains_core_sections(self):
        m = self._new_meeting()
        m.append_chunk("발언 1")
        m.summarize(_fake_summary_fn_ok)
        md = m.to_markdown()
        self.assertIn("# 회의록 — 테스트 회의", md)
        self.assertIn("## 요약", md)
        self.assertIn("## 핵심 결정사항", md)
        self.assertIn("## 액션 아이템", md)
        self.assertIn("## 트랜스크립트", md)
        # 테이블 헤더가 액션 아이템에 포함.
        self.assertIn("| 담당자 |", md)

    def test_save_and_load_roundtrip(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            m = self._new_meeting()
            m.append_chunk("회의 시작")
            m.append_chunk("두 번째 발언")
            m.summarize(_fake_summary_fn_ok)
            m.save(base)
            loaded = Meeting.load(m.meeting_id, base)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.title, m.title)
            self.assertEqual(len(loaded.utterances), 2)
            self.assertEqual(loaded.summary, m.summary)
            self.assertEqual(loaded.decisions, m.decisions)


class MeetingRegistryTests(unittest.TestCase):
    def test_start_then_end_flow(self):
        with TemporaryDirectory() as td:
            reg = MeetingRegistry(Path(td))
            m = reg.start("프로젝트 킥오프")
            self.assertIsNotNone(reg.active)
            reg.append_active("안녕하세요 시작합시다")
            reg.append_active("안건은 결제 모듈입니다")
            ended = reg.end_active(_fake_summary_fn_ok)
            self.assertIsNotNone(ended)
            self.assertIsNone(reg.active)
            # list 에 보여야.
            lst = reg.list_meetings()
            self.assertEqual(len(lst), 1)
            self.assertEqual(lst[0]["meeting_id"], m.meeting_id)
            self.assertEqual(lst[0]["status"], "summarized")
            # transcript 는 list 응답에서 제외 (lightweight).
            self.assertNotIn("utterances", lst[0])

    def test_double_start_raises(self):
        with TemporaryDirectory() as td:
            reg = MeetingRegistry(Path(td))
            reg.start("회의 1")
            with self.assertRaises(RuntimeError):
                reg.start("회의 2")

    def test_append_after_end_silent(self):
        with TemporaryDirectory() as td:
            reg = MeetingRegistry(Path(td))
            reg.start("회의 X")
            reg.append_active("발언")
            reg.end_active(_fake_summary_fn_ok)
            # active 가 None 이 됐으니 append_active 는 None 반환 (회귀 X).
            self.assertIsNone(reg.append_active("종료 후 발언"))


class ParseSummaryJsonTests(unittest.TestCase):
    def test_clean_json(self):
        out = parse_summary_json('{"summary": "ok", "decisions": ["a"]}')
        self.assertEqual(out["summary"], "ok")

    def test_with_code_fence(self):
        s = '```json\n{"summary": "ok"}\n```'
        out = parse_summary_json(s)
        self.assertEqual(out["summary"], "ok")

    def test_with_prefix_noise(self):
        s = '여기 결과입니다:\n{"summary": "ok"}\n끝.'
        out = parse_summary_json(s)
        self.assertEqual(out["summary"], "ok")

    def test_invalid(self):
        self.assertEqual(parse_summary_json(""), {})
        self.assertEqual(parse_summary_json("그냥 텍스트"), {})
        self.assertEqual(parse_summary_json("{nope"), {})


if __name__ == "__main__":
    unittest.main()
