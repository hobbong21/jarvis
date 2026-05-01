"""기획서 v2.0 — 장기 메모리 (SQLite) 회귀 테스트."""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from memory import Memory, extract_user_facts


class MemorySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmpdir.name, "mem.db")
        self.mem = Memory(self.path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_schema_creates_all_tables(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self.path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        names = {r[0] for r in rows}
        for t in (
            "conversations", "messages", "facts",
            "observations", "routines", "timers_events",
        ):
            self.assertIn(t, names)


class ConversationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mem = Memory(os.path.join(self.tmpdir.name, "mem.db"))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_start_and_add_messages(self) -> None:
        cid = self.mem.start_conversation("u1")
        self.assertIsInstance(cid, int)
        m1 = self.mem.add_message(cid, "user", "안녕")
        m2 = self.mem.add_message(cid, "assistant", "안녕하세요")
        self.assertGreater(m2, m1)

    def test_add_message_rejects_invalid_role(self) -> None:
        cid = self.mem.start_conversation("u1")
        with self.assertRaises(ValueError):
            self.mem.add_message(cid, "robot", "x")

    def test_get_recent_messages_chronological_order(self) -> None:
        cid = self.mem.start_conversation("u1")
        for i in range(5):
            self.mem.add_message(cid, "user", f"메시지{i}")
        rows = self.mem.get_recent_messages("u1", limit=10)
        self.assertEqual([r["content"] for r in rows],
                         ["메시지0", "메시지1", "메시지2", "메시지3", "메시지4"])

    def test_get_or_start_reuses_recent(self) -> None:
        c1 = self.mem.get_or_start_conversation("u1")
        self.mem.add_message(c1, "user", "hi")
        c2 = self.mem.get_or_start_conversation("u1", idle_window_sec=3600)
        self.assertEqual(c1, c2)

    def test_get_or_start_creates_new_after_idle(self) -> None:
        c1 = self.mem.get_or_start_conversation("u1")
        self.mem.add_message(c1, "user", "hi")
        c2 = self.mem.get_or_start_conversation("u1", idle_window_sec=0.0)
        self.assertNotEqual(c1, c2)


class FactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mem = Memory(os.path.join(self.tmpdir.name, "mem.db"))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_upsert_creates_then_updates(self) -> None:
        f1 = self.mem.upsert_fact("u1", "회사", "ACME")
        f2 = self.mem.upsert_fact("u1", "회사", "Globex")
        self.assertEqual(f1, f2)  # same id
        rows = self.mem.get_facts("u1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], "Globex")

    def test_get_facts_orders_by_updated_desc(self) -> None:
        self.mem.upsert_fact("u1", "a", "1")
        time.sleep(0.01)
        self.mem.upsert_fact("u1", "b", "2")
        time.sleep(0.01)
        self.mem.upsert_fact("u1", "a", "1updated")
        rows = self.mem.get_facts("u1")
        self.assertEqual(rows[0]["key"], "a")  # most recently updated first

    def test_facts_isolated_per_user(self) -> None:
        self.mem.upsert_fact("u1", "취미", "독서")
        self.mem.upsert_fact("u2", "취미", "운동")
        u1 = self.mem.get_facts("u1")
        u2 = self.mem.get_facts("u2")
        self.assertEqual(u1[0]["value"], "독서")
        self.assertEqual(u2[0]["value"], "운동")

    def test_delete_fact(self) -> None:
        self.mem.upsert_fact("u1", "키", "값")
        self.assertTrue(self.mem.delete_fact("u1", "키"))
        self.assertFalse(self.mem.delete_fact("u1", "키"))
        self.assertEqual(self.mem.get_facts("u1"), [])


class SearchAndForgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mem = Memory(os.path.join(self.tmpdir.name, "mem.db"))
        self.cid = self.mem.start_conversation("u1")
        self.mem.add_message(self.cid, "user", "내일 오후 3시에 회의가 있어")
        self.mem.add_message(self.cid, "assistant", "회의 일정 기억할게요")
        self.mem.add_message(self.cid, "user", "차 비밀번호는 1234")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_search_messages_keyword(self) -> None:
        rows = self.mem.search_messages("u1", "회의")
        self.assertEqual(len(rows), 2)
        contents = {r["content"] for r in rows}
        self.assertIn("내일 오후 3시에 회의가 있어", contents)

    def test_search_empty_query_returns_empty(self) -> None:
        self.assertEqual(self.mem.search_messages("u1", ""), [])
        self.assertEqual(self.mem.search_messages("u1", "   "), [])

    def test_forget_removes_messages_and_facts(self) -> None:
        self.mem.upsert_fact("u1", "차_비번", "1234")
        result = self.mem.forget("u1", "1234")
        self.assertGreaterEqual(result["facts"], 1)
        self.assertGreaterEqual(result["messages"], 1)
        # 잊혀진 후 검색 결과 0
        self.assertEqual(self.mem.search_messages("u1", "1234"), [])

    def test_forget_empty_query_noop(self) -> None:
        r = self.mem.forget("u1", "")
        self.assertEqual(r, {"facts": 0, "messages": 0})


class ContextBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mem = Memory(os.path.join(self.tmpdir.name, "mem.db"))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_empty_user_returns_empty_string(self) -> None:
        self.assertEqual(self.mem.context_block("nobody"), "")

    def test_facts_appear_in_block(self) -> None:
        self.mem.upsert_fact("u1", "이름", "지훈")
        block = self.mem.context_block("u1")
        self.assertIn("[기억]", block)
        self.assertIn("이름: 지훈", block)

    def test_query_recalls_appear(self) -> None:
        cid = self.mem.start_conversation("u1")
        self.mem.add_message(cid, "user", "프로젝트 X 의 마감일은 다음 주 금요일")
        block = self.mem.context_block("u1", query="프로젝트 X")
        self.assertIn("관련 과거 발언", block)
        self.assertIn("프로젝트 X", block)

    def test_long_recall_truncated(self) -> None:
        cid = self.mem.start_conversation("u1")
        long_text = "프로젝트 X " + ("긴 설명 " * 50)
        self.mem.add_message(cid, "user", long_text)
        block = self.mem.context_block("u1", query="프로젝트 X")
        self.assertIn("…", block)


class ObservationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mem = Memory(os.path.join(self.tmpdir.name, "mem.db"))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_add_and_query(self) -> None:
        self.mem.add_observation("u1", "face", {"name": "지훈", "confidence": 0.92})
        self.mem.add_observation("u1", "object", {"label": "노트북"})
        all_rows = self.mem.get_observations("u1")
        self.assertEqual(len(all_rows), 2)
        face_rows = self.mem.get_observations("u1", type_="face")
        self.assertEqual(len(face_rows), 1)
        self.assertEqual(face_rows[0]["data"]["name"], "지훈")


class TimersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mem = Memory(os.path.join(self.tmpdir.name, "mem.db"))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_upcoming_within_window(self) -> None:
        now = time.time()
        self.mem.add_timer_event("u1", "회의", now + 60)
        self.mem.add_timer_event("u1", "다음달", now + 86400 * 40)
        rows = self.mem.upcoming_timers("u1", within_sec=3600)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "회의")


class CompareSourcePreservationTests(unittest.TestCase):
    """compare 모드의 emotion='|source' 인코딩이 context_block recall 에 노출되는지."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mem = Memory(os.path.join(self.tmpdir.name, "mem.db"))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_recall_label_includes_source(self) -> None:
        cid = self.mem.start_conversation("u1")
        self.mem.add_message(cid, "user", "프로젝트 X 일정 알려줘")
        self.mem.add_message(cid, "assistant", "프로젝트 X 일정은 다음 주", emotion="neutral|claude")
        self.mem.add_message(cid, "assistant", "프로젝트 X 마감 임박", emotion="neutral|openai")
        block = self.mem.context_block("u1", query="프로젝트 X", max_recalls=10)
        self.assertIn("(assistant|claude)", block)
        self.assertIn("(assistant|openai)", block)

    def test_recall_label_no_source_for_normal_message(self) -> None:
        cid = self.mem.start_conversation("u1")
        self.mem.add_message(cid, "user", "오늘 날씨 어때")
        self.mem.add_message(cid, "assistant", "오늘 날씨는 맑음", emotion="neutral")
        block = self.mem.context_block("u1", query="날씨")
        # 일반 응답은 (assistant) 만, 파이프 없음
        self.assertIn("(assistant)", block)
        self.assertNotIn("(assistant|", block)


class MultiSessionConvergenceTests(unittest.TestCase):
    """다중 WS 세션이 같은 user_id 면 idle window 안에서 같은 conversation 으로 수렴해야 함."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.mem = Memory(os.path.join(self.tmpdir.name, "mem.db"))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_two_sessions_converge_after_first_message(self) -> None:
        # 세션 A: 첫 호출로 conversation 생성 + 메시지 기록
        a = self.mem.get_or_start_conversation("u1", idle_window_sec=3600)
        self.mem.add_message(a, "user", "첫 메시지")
        # 세션 B: 같은 user_id 로 호출 → 가장 최근 active conversation 을 발견해야 함
        b = self.mem.get_or_start_conversation("u1", idle_window_sec=3600)
        self.assertEqual(a, b)

    def test_idle_window_expiry_creates_new(self) -> None:
        a = self.mem.get_or_start_conversation("u1", idle_window_sec=3600)
        self.mem.add_message(a, "user", "오래된 메시지")
        # 즉시 매우 짧은 윈도우로 호출 → 새 conversation 생성
        b = self.mem.get_or_start_conversation("u1", idle_window_sec=0.0)
        self.assertNotEqual(a, b)


class CascadeDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmpdir.name, "mem.db")
        self.mem = Memory(self.path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_messages_cascade_when_conversation_deleted(self) -> None:
        cid = self.mem.start_conversation("u1")
        self.mem.add_message(cid, "user", "hi")
        import sqlite3
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM conversations WHERE id=?", (cid,))
            conn.commit()
            n = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conv_id=?", (cid,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)


class AutoFactExtractionTests(unittest.TestCase):
    """Cycle #8 — 한국어 자기소개 패턴 → facts 자동 추출 회귀."""

    def test_extracts_name_with_yo_ending(self):
        self.assertIn(("name", "민수"), extract_user_facts("내 이름은 민수야"))

    def test_extracts_name_with_imnida_ending(self):
        self.assertIn(("name", "김철수"), extract_user_facts("저는 김철수입니다"))

    def test_extracts_name_with_yeyo_ending(self):
        self.assertIn(("name", "영희"), extract_user_facts("나는 영희예요"))

    def test_extracts_location(self):
        self.assertIn(("location", "서울"), extract_user_facts("나는 서울에 살아"))

    def test_extracts_hobby(self):
        self.assertIn(("hobby", "등산"), extract_user_facts("내 취미는 등산이에요"))

    def test_extracts_favorite_with_double_i_ending(self):
        # '떡볶이야' 가 '떡볶'+'이야' 로 잘못 잘리는 회귀 (greedy + 어미 strip)
        self.assertIn(("favorite", "떡볶이"), extract_user_facts("내가 좋아하는 건 떡볶이야"))

    def test_extracts_job(self):
        self.assertIn(("job", "개발자"), extract_user_facts("저는 개발자로 일하고 있어요"))

    def test_extracts_birthday(self):
        self.assertIn(("birthday", "5월 1일"), extract_user_facts("제 생일은 5월 1일이에요"))

    def test_extracts_nickname(self):
        self.assertIn(("nickname", "지니"), extract_user_facts("나를 지니라고 불러줘"))

    def test_no_match_for_question(self):
        self.assertEqual(extract_user_facts("오늘 날씨 어때"), [])

    def test_no_match_for_short_greeting(self):
        self.assertEqual(extract_user_facts("사비스, 안녕"), [])

    def test_banlist_blocks_assistant_name_as_user_name(self):
        # '내 이름은 사비스야' — banlist 가 어시스턴트 이름 박제 방지.
        out = extract_user_facts("내 이름은 사비스야")
        self.assertNotIn(("name", "사비스"), out)

    def test_too_long_input_returns_empty(self):
        self.assertEqual(extract_user_facts("나는 " + "가" * 500 + "에 살아"), [])

    def test_too_short_returns_empty(self):
        self.assertEqual(extract_user_facts("응"), [])

    def test_non_string_returns_empty(self):
        self.assertEqual(extract_user_facts(None), [])
        self.assertEqual(extract_user_facts(""), [])


if __name__ == "__main__":
    unittest.main()
