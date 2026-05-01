"""auth.py 단위 테스트 — PBKDF2-SHA256 해싱/검증과 AuthSystem CRUD.

architect 사이클 #7 follow-up (커버리지 0% → 100%):
  - hash_password / verify_password 라운드트립
  - 잘못된 형식의 stored 값 처리
  - AuthSystem 사용자 생성/조회/검증/저장/로드
  - create_user_detail 의 한국어 사유 메시지 분기
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")

from auth import AuthSystem, hash_password, verify_password  # noqa: E402


class HashPasswordTests(unittest.TestCase):
    def test_format_is_salt_dollar_hash(self):
        h = hash_password("hunter2")
        self.assertIn("$", h)
        salt, digest = h.split("$")
        self.assertEqual(len(salt), 32)  # token_hex(16) → 32 chars
        # sha256 출력은 64 hex chars
        self.assertEqual(len(digest), 64)

    def test_random_salt_yields_different_hash(self):
        h1 = hash_password("samepw")
        h2 = hash_password("samepw")
        self.assertNotEqual(h1, h2, "salt 가 매번 무작위여야 한다")

    def test_explicit_salt_is_deterministic(self):
        h1 = hash_password("pw", salt="deadbeef")
        h2 = hash_password("pw", salt="deadbeef")
        self.assertEqual(h1, h2)

    def test_verify_roundtrip(self):
        stored = hash_password("correct horse battery staple")
        self.assertTrue(verify_password(stored, "correct horse battery staple"))
        self.assertFalse(verify_password(stored, "wrong"))

    def test_verify_malformed_returns_false(self):
        self.assertFalse(verify_password("noseparator", "anything"))
        self.assertFalse(verify_password("", "anything"))

    def test_verify_empty_password_against_real_hash(self):
        stored = hash_password("real")
        self.assertFalse(verify_password(stored, ""))


class AuthSystemTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "users.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_when_no_file(self):
        a = AuthSystem(str(self.path))
        self.assertFalse(a.has_users())
        self.assertEqual(a.users, {})

    def test_create_then_verify(self):
        a = AuthSystem(str(self.path))
        self.assertTrue(a.create_user("alice", "secret123"))
        self.assertTrue(a.has_users())
        self.assertTrue(a.verify("alice", "secret123"))
        self.assertFalse(a.verify("alice", "wrong"))
        self.assertFalse(a.verify("ghost", "secret123"))

    def test_persistence_across_instances(self):
        a = AuthSystem(str(self.path))
        a.create_user("bob", "passpass")
        # 새 인스턴스로 로드해도 유지되어야 함
        b = AuthSystem(str(self.path))
        self.assertTrue(b.verify("bob", "passpass"))

    def test_create_user_detail_blank_username(self):
        a = AuthSystem(str(self.path))
        msg = a.create_user_detail("   ", "ok1234")
        self.assertIsNotNone(msg)
        self.assertIn("사용자명", msg)

    def test_create_user_detail_short_password(self):
        a = AuthSystem(str(self.path))
        msg = a.create_user_detail("alice", "abc")
        self.assertIsNotNone(msg)
        self.assertIn("4자", msg)

    def test_create_user_detail_blank_password(self):
        a = AuthSystem(str(self.path))
        msg = a.create_user_detail("alice", "")
        self.assertIsNotNone(msg)
        self.assertIn("비밀번호", msg)

    def test_create_user_detail_duplicate(self):
        a = AuthSystem(str(self.path))
        a.create_user("alice", "pwpwpw")
        msg = a.create_user_detail("alice", "other1234")
        self.assertIsNotNone(msg)
        self.assertIn("이미", msg)

    def test_create_user_detail_success_returns_none(self):
        a = AuthSystem(str(self.path))
        self.assertIsNone(a.create_user_detail("carol", "pwpw"))

    def test_username_is_stripped(self):
        a = AuthSystem(str(self.path))
        a.create_user_detail("  dave  ", "1234")
        # 저장된 키는 strip 된 형태
        self.assertIn("dave", a.users)
        # verify 할 때도 strip
        self.assertTrue(a.verify("  dave  ", "1234"))

    def test_corrupt_file_resets_to_empty(self):
        # 손상된 JSON 이 있어도 silently empty 로 시작
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        a = AuthSystem(str(self.path))
        self.assertFalse(a.has_users())

    def test_save_creates_parent_dirs(self):
        nested = Path(self._tmp.name) / "deep" / "nested" / "users.json"
        a = AuthSystem(str(nested))
        self.assertTrue(a.create_user("eve", "1234"))
        self.assertTrue(nested.exists())
        # 저장된 JSON 이 비밀번호 자체를 평문으로 담지 않아야
        raw = json.loads(nested.read_text(encoding="utf-8"))
        self.assertIn("eve", raw)
        self.assertIn("$", raw["eve"]["password"])
        self.assertNotIn("1234", raw["eve"]["password"])


if __name__ == "__main__":
    unittest.main()
