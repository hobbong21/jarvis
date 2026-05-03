"""F-10 할 일/캘린더 단위 테스트 — todos.py."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sarvis.todos import (
    TodoStore,
    extract_todos_from_text,
    parse_todo_json,
)


class TodoStoreTests(unittest.TestCase):
    def _store(self, td: str) -> TodoStore:
        return TodoStore(Path(td) / "todos.json")

    def test_add_and_persist(self):
        with TemporaryDirectory() as td:
            s = self._store(td)
            it = s.add("우유 사기", due="내일", priority="high", source="voice")
            self.assertIsNotNone(it)
            self.assertEqual(it.title, "우유 사기")
            # 새 인스턴스 로 다시 열어도 살아있어야.
            s2 = self._store(td)
            self.assertEqual(len(s2.items), 1)
            self.assertEqual(s2.items[0].title, "우유 사기")
            self.assertEqual(s2.items[0].priority, "high")

    def test_add_rejects_empty_and_normalizes(self):
        with TemporaryDirectory() as td:
            s = self._store(td)
            self.assertIsNone(s.add(""))
            self.assertIsNone(s.add("   "))
            it = s.add("배포", priority="weird", source="weird")
            self.assertEqual(it.priority, "normal")  # 기본값으로 정규화.
            self.assertEqual(it.source, "manual")

    def test_mark_done_and_remove(self):
        with TemporaryDirectory() as td:
            s = self._store(td)
            a = s.add("a 할 일")
            b = s.add("b 할 일")
            self.assertTrue(s.mark_done(a.id))
            self.assertFalse(s.mark_done("nonexistent"))
            self.assertEqual(len(s.list_active()), 1)
            self.assertEqual(len(s.list_done()), 1)
            self.assertTrue(s.remove(b.id))
            self.assertFalse(s.remove("nonexistent"))
            self.assertEqual(len(s.list_active()), 0)

    def test_priority_sort_order(self):
        with TemporaryDirectory() as td:
            s = self._store(td)
            s.add("low 1", priority="low")
            s.add("high 1", priority="high")
            s.add("normal 1", priority="normal")
            s.add("high 2", priority="high")
            titles = [it.title for it in s.list_active()]
            # high 가 먼저, low 가 마지막.
            self.assertEqual(titles[0:2][0].startswith("high"), True)
            self.assertTrue(titles[-1].startswith("low"))

    def test_corrupt_file_recovery(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "todos.json"
            p.write_text("{this is broken", encoding="utf-8")
            s = TodoStore(p)
            # 손상 파일 → 빈 상태로 복구 + .corrupt.json 백업 생성.
            self.assertEqual(len(s.items), 0)
            self.assertTrue(p.with_suffix(".corrupt.json").exists())


class ParseTodoJsonTests(unittest.TestCase):
    def test_extract_with_fence(self):
        s = '```json\n{"items":[{"title":"우유 사기","due":"오늘","priority":"high"}]}\n```'
        out = parse_todo_json(s)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "우유 사기")
        self.assertEqual(out[0]["priority"], "high")

    def test_no_items(self):
        out = parse_todo_json('{"items": []}')
        self.assertEqual(out, [])

    def test_invalid(self):
        self.assertEqual(parse_todo_json(""), [])
        self.assertEqual(parse_todo_json("not json"), [])
        self.assertEqual(parse_todo_json('{"foo": 1}'), [])

    def test_priority_normalized(self):
        s = '{"items":[{"title":"X","priority":"WEIRD"}]}'
        out = parse_todo_json(s)
        self.assertEqual(out[0]["priority"], "normal")


class ExtractTodosFromTextTests(unittest.TestCase):
    def test_short_text_skipped(self):
        called = []
        def fn(p):
            called.append(p); return ""
        out = extract_todos_from_text("ㅇ", fn)
        self.assertEqual(out, [])
        self.assertEqual(called, [])  # LLM 호출조차 안 함.

    def test_llm_exception_returns_empty(self):
        def fn(p): raise RuntimeError("down")
        out = extract_todos_from_text("내일 오후 3시 회의 잡고 우유 사기", fn)
        self.assertEqual(out, [])

    def test_happy_path(self):
        def fn(p):
            return '{"items":[{"title":"우유 사기","due":"오늘","priority":"normal"}]}'
        out = extract_todos_from_text("오늘 우유 좀 사야겠다", fn)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "우유 사기")


if __name__ == "__main__":
    unittest.main()
