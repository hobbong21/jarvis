"""사이클 #30 — sarvis.user_storage 단위 테스트.

저장공간 격리, 한도 검사, AI 접근 토글, 검색, kind 화이트리스트, 경로 안전성을 확인.
"""
import tempfile
import unittest
from pathlib import Path

from sarvis.user_storage import (
    ALLOWED_KINDS,
    QuotaExceeded,
    UserStorage,
    _safe_name,
)


class SafeNameTests(unittest.TestCase):
    def test_strips_dangerous_chars(self):
        self.assertEqual(_safe_name("../../etc/passwd"), "etc_passwd")
        self.assertEqual(_safe_name("a/b\\c"), "a_b_c")

    def test_keeps_korean(self):
        self.assertEqual(_safe_name("문서.txt"), "문서.txt")

    def test_truncates_long(self):
        long = "a" * 200
        self.assertEqual(len(_safe_name(long)), 80)

    def test_fallback_on_empty(self):
        self.assertEqual(_safe_name(""), "file")
        self.assertEqual(_safe_name("....."), "file")


class StorageBasicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = UserStorage("민수", root=self.tmp, limit_bytes=1024)

    def test_empty_starts_with_zero_used(self):
        self.assertEqual(self.storage.used_bytes, 0)
        self.assertEqual(self.storage.free_bytes, 1024)

    def test_save_and_read_roundtrip(self):
        fid = self.storage.save_file("hello.txt", b"hello world")
        self.assertEqual(self.storage.read_file(fid), b"hello world")
        self.assertEqual(self.storage.used_bytes, len("hello world"))

    def test_save_creates_metadata(self):
        fid = self.storage.save_file("note.md", b"# title")
        meta = self.storage.get_metadata(fid)
        self.assertEqual(meta["name"], "note.md")
        self.assertEqual(meta["size"], len(b"# title"))
        self.assertEqual(meta["kind"], "upload")
        self.assertTrue(meta["ai_access"])

    def test_rejects_empty_data(self):
        with self.assertRaises(ValueError):
            self.storage.save_file("a.txt", b"")

    def test_rejects_nonbytes_data(self):
        with self.assertRaises(TypeError):
            self.storage.save_file("a.txt", "not-bytes")  # type: ignore[arg-type]

    def test_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            self.storage.save_file("a.txt", b"x", kind="malicious")

    def test_all_known_kinds_accepted(self):
        for kind in ALLOWED_KINDS:
            fid = self.storage.save_file(f"x_{kind}.txt", b"x", kind=kind)
            self.assertIsNotNone(self.storage.get_metadata(fid))

    def test_delete_removes_meta_and_file(self):
        fid = self.storage.save_file("a.txt", b"abc")
        self.assertTrue(self.storage.delete_file(fid))
        self.assertIsNone(self.storage.get_metadata(fid))
        self.assertEqual(self.storage.used_bytes, 0)
        with self.assertRaises(FileNotFoundError):
            self.storage.read_file(fid)

    def test_delete_unknown_returns_false(self):
        self.assertFalse(self.storage.delete_file("nosuch"))

    def test_rename_changes_display_name(self):
        fid = self.storage.save_file("old.txt", b"x")
        self.assertTrue(self.storage.rename(fid, "new.txt"))
        self.assertEqual(self.storage.get_metadata(fid)["name"], "new.txt")

    def test_rename_rejects_empty(self):
        fid = self.storage.save_file("x.txt", b"x")
        with self.assertRaises(ValueError):
            self.storage.rename(fid, "   ")


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = UserStorage("u", root=self.tmp, limit_bytes=10)

    def test_under_limit_ok(self):
        self.storage.save_file("a.txt", b"12345")  # 5
        self.storage.save_file("b.txt", b"12345")  # 10 합계
        self.assertEqual(self.storage.used_bytes, 10)

    def test_exact_limit_ok(self):
        self.storage.save_file("a.txt", b"x" * 10)
        self.assertEqual(self.storage.free_bytes, 0)

    def test_one_byte_over_rejected(self):
        with self.assertRaises(QuotaExceeded):
            self.storage.save_file("big.bin", b"x" * 11)

    def test_quota_check_uses_running_total(self):
        self.storage.save_file("a.txt", b"x" * 8)
        # 남은 2바이트인데 3바이트 → 거부.
        with self.assertRaises(QuotaExceeded):
            self.storage.save_file("b.txt", b"yyy")
        # 거부된 파일은 남기지 않아야 함.
        self.assertEqual(self.storage.used_bytes, 8)

    def test_delete_frees_space(self):
        fid = self.storage.save_file("a.txt", b"x" * 10)
        self.storage.delete_file(fid)
        self.storage.save_file("b.txt", b"y" * 10)  # 다시 들어감
        self.assertEqual(self.storage.used_bytes, 10)


class AIAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = UserStorage("u", root=self.tmp, limit_bytes=1024)

    def test_default_ai_access_true(self):
        fid = self.storage.save_file("a.txt", b"x")
        self.assertTrue(self.storage.get_metadata(fid)["ai_access"])

    def test_save_with_ai_access_false(self):
        fid = self.storage.save_file("a.txt", b"x", ai_access=False)
        self.assertFalse(self.storage.get_metadata(fid)["ai_access"])

    def test_toggle_changes_value(self):
        fid = self.storage.save_file("a.txt", b"x")
        self.storage.set_ai_access(fid, False)
        self.assertFalse(self.storage.get_metadata(fid)["ai_access"])
        self.storage.set_ai_access(fid, True)
        self.assertTrue(self.storage.get_metadata(fid)["ai_access"])

    def test_toggle_unknown_returns_false(self):
        self.assertFalse(self.storage.set_ai_access("nosuch", True))

    def test_ai_call_blocked_when_off(self):
        fid = self.storage.save_file("secret.txt", b"shh", ai_access=False)
        # 사용자 직접 다운로드는 허용.
        self.assertEqual(self.storage.read_file(fid, ai_call=False), b"shh")
        # AI 도구 호출은 거부.
        with self.assertRaises(PermissionError):
            self.storage.read_file(fid, ai_call=True)

    def test_list_ai_only_filters_off(self):
        on = self.storage.save_file("on.txt", b"x", ai_access=True)
        off = self.storage.save_file("off.txt", b"y", ai_access=False)
        ids = {m["file_id"] for m in self.storage.list_files(ai_only=True)}
        self.assertIn(on, ids)
        self.assertNotIn(off, ids)


class ListAndKindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = UserStorage("u", root=self.tmp, limit_bytes=4096)

    def test_list_filters_by_kind(self):
        self.storage.save_file("u.txt", b"a", kind="upload")
        self.storage.save_file("m.bin", b"a", kind="media")
        self.assertEqual(len(self.storage.list_files(kind="upload")), 1)
        self.assertEqual(len(self.storage.list_files(kind="media")), 1)
        self.assertEqual(len(self.storage.list_files(kind="conversation")), 0)

    def test_save_conversation_creates_md_in_conv_dir(self):
        fid = self.storage.save_conversation("# hello\n내용")
        meta = self.storage.get_metadata(fid)
        self.assertEqual(meta["kind"], "conversation")
        self.assertTrue(meta["name"].endswith(".md"))
        # 디스크 — conversations 서브디렉토리에 있어야 함.
        self.assertTrue(any(self.storage.conv_dir.iterdir()))
        self.assertEqual(self.storage.read_file(fid), "# hello\n내용".encode("utf-8"))

    def test_save_conversation_rejects_empty(self):
        with self.assertRaises(ValueError):
            self.storage.save_conversation("   ")

    def test_list_sorted_desc_by_uploaded_at(self):
        import time
        f1 = self.storage.save_file("a.txt", b"x")
        time.sleep(0.01)
        f2 = self.storage.save_file("b.txt", b"x")
        ids = [m["file_id"] for m in self.storage.list_files()]
        self.assertEqual(ids[0], f2)
        self.assertEqual(ids[1], f1)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = UserStorage("u", root=self.tmp, limit_bytes=4096)

    def test_search_by_filename(self):
        self.storage.save_file("project_notes.md", b"unrelated body")
        hits = self.storage.search_files("project")
        self.assertEqual(len(hits), 1)

    def test_search_by_body(self):
        self.storage.save_file("a.md", "오늘 회의에서 결정사항".encode("utf-8"))
        hits = self.storage.search_files("회의")
        self.assertEqual(len(hits), 1)

    def test_search_ai_only_skips_disabled(self):
        self.storage.save_file("hidden.md", b"secret keyword", ai_access=False)
        self.storage.save_file("visible.md", b"secret keyword", ai_access=True)
        hits = self.storage.search_files("secret", ai_only=True)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["name"], "visible.md")

    def test_empty_query_returns_empty(self):
        self.storage.save_file("a.txt", b"x")
        self.assertEqual(self.storage.search_files(""), [])

    def test_max_results_caps(self):
        for i in range(5):
            self.storage.save_file(f"f{i}.txt", b"hit")
        hits = self.storage.search_files("hit", max_results=3)
        self.assertEqual(len(hits), 3)


class IsolationTests(unittest.TestCase):
    def test_two_users_have_separate_storage(self):
        tmp = tempfile.mkdtemp()
        a = UserStorage("alice", root=tmp, limit_bytes=1024)
        b = UserStorage("bob", root=tmp, limit_bytes=1024)
        a.save_file("a.txt", b"alice-data")
        self.assertEqual(b.list_files(), [])
        self.assertEqual(a.used_bytes, len(b"alice-data"))
        self.assertEqual(b.used_bytes, 0)

    def test_persistence_across_instances(self):
        tmp = tempfile.mkdtemp()
        s1 = UserStorage("u", root=tmp, limit_bytes=1024)
        fid = s1.save_file("p.txt", b"persist")
        s2 = UserStorage("u", root=tmp, limit_bytes=1024)
        self.assertEqual(s2.read_file(fid), b"persist")
        self.assertEqual(s2.used_bytes, len(b"persist"))


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = UserStorage("u", root=self.tmp, limit_bytes=1024)

    def test_path_traversal_in_filename_neutralized(self):
        fid = self.storage.save_file("../../etc/passwd", b"x")
        # 디스크 파일은 사용자 디렉토리 안에만 생성되어야 함.
        path = self.storage._disk_path(self.storage._meta[fid])
        self.assertTrue(self.storage.user_dir in path.parents)

    def test_face_name_traversal_neutralized(self):
        # face_name 에 traversal 문자가 들어와도 root 밖으로 나가면 안 됨.
        s = UserStorage("../evil", root=self.tmp, limit_bytes=1024)
        # 정규화된 디렉토리는 root 안에 있어야 함.
        self.assertTrue(Path(self.tmp) in s.user_dir.parents)

    def test_empty_face_name_rejected(self):
        with self.assertRaises(ValueError):
            UserStorage("   ", root=self.tmp)

    def test_nonpositive_limit_rejected(self):
        with self.assertRaises(ValueError):
            UserStorage("u", root=self.tmp, limit_bytes=0)


class RegisterExternalTests(unittest.TestCase):
    """사이클 #32 — 외부 디스크 파일 참조 등록 (녹화/녹음 통합용)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = UserStorage("u", root=self.tmp, limit_bytes=10 ** 6)
        # 외부 디렉토리 (녹화 폴더 시뮬)
        self.ext_dir = Path(self.tmp) / "external_recordings"
        self.ext_dir.mkdir()
        self.ext_file = self.ext_dir / "video.webm"
        self.ext_file.write_bytes(b"fake video bytes" * 100)  # 1600 bytes

    def test_register_external_adds_meta_without_copy(self):
        fid = self.storage.register_external(
            "운동 영상.webm", str(self.ext_file), kind="media",
        )
        meta = self.storage.get_metadata(fid)
        self.assertEqual(meta["name"], "운동 영상.webm")
        self.assertEqual(meta["kind"], "media")
        self.assertEqual(meta["size"], self.ext_file.stat().st_size)
        # 데이터 복사 없음 — files/ 디렉토리에 새 파일이 생기지 않아야.
        copies = list(self.storage.files_dir.glob("*"))
        self.assertEqual(copies, [])
        # 원본은 그대로.
        self.assertTrue(self.ext_file.exists())

    def test_external_read_file_returns_actual_data(self):
        fid = self.storage.register_external("v.webm", str(self.ext_file), kind="media")
        data = self.storage.read_file(fid, ai_call=False)
        self.assertEqual(data, self.ext_file.read_bytes())

    def test_external_quota_check(self):
        small = UserStorage("u2", root=self.tmp, limit_bytes=100)
        with self.assertRaises(QuotaExceeded):
            small.register_external("big.bin", str(self.ext_file), kind="media")

    def test_external_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            self.storage.register_external("x", str(self.ext_file), kind="weird")

    def test_external_rejects_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            self.storage.register_external("x", str(self.ext_dir / "nope.bin"))

    def test_delete_external_keeps_disk_file(self):
        fid = self.storage.register_external("v.webm", str(self.ext_file))
        self.assertTrue(self.storage.delete_file(fid))
        # 메타는 삭제됨.
        self.assertIsNone(self.storage.get_metadata(fid))
        # 그러나 외부 디스크 파일은 보존.
        self.assertTrue(self.ext_file.exists())

    def test_delete_internal_removes_disk_file(self):
        # 대조: 일반 save_file 로 들어간 파일은 삭제 시 디스크에서도 사라져야.
        fid = self.storage.save_file("internal.txt", b"hello")
        path = self.storage._disk_path(self.storage._meta[fid])
        self.assertTrue(path.exists())
        self.storage.delete_file(fid)
        self.assertFalse(path.exists())

    def test_external_blocked_when_ai_access_off(self):
        fid = self.storage.register_external(
            "secret.webm", str(self.ext_file), ai_access=False,
        )
        with self.assertRaises(PermissionError):
            self.storage.read_file(fid, ai_call=True)
        # 사용자 다운로드는 허용.
        self.assertEqual(
            self.storage.read_file(fid, ai_call=False),
            self.ext_file.read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
