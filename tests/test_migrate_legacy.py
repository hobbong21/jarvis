"""사이클 #9 정비: 루트 → data/ 자동 마이그레이션 회귀 테스트.

`config._migrate_legacy_root_data` 가:
  1) 새 경로가 없을 때 레거시 파일을 옮긴다.
  2) 새 경로가 이미 있으면 레거시를 건드리지 않는다 (멱등).
  3) faces/ 디렉토리는 내용물이 있을 때만 옮긴다.
  4) 권한/IO 오류는 부팅을 막지 않는다 (silent ignore).
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path


class MigrateLegacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd_prev = os.getcwd()
        os.chdir(self.tmp)
        # config 모듈은 import 시점에 한 번 마이그레이션을 돌리므로,
        # 테스트는 함수를 직접 import 해서 호출한다.
        from sarvis.config import _migrate_legacy_root_data  # noqa: WPS433
        self.run_migration = _migrate_legacy_root_data

    def tearDown(self):
        os.chdir(self.cwd_prev)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_moves_legacy_files_when_new_missing(self):
        Path("users.json").write_text('{"u": "x"}', encoding="utf-8")
        Path("memory.db").write_bytes(b"sqlite-bytes")
        self.run_migration()
        self.assertFalse(Path("users.json").exists())
        self.assertFalse(Path("memory.db").exists())
        self.assertEqual(
            Path("data/users.json").read_text(encoding="utf-8"), '{"u": "x"}'
        )
        self.assertEqual(Path("data/memory.db").read_bytes(), b"sqlite-bytes")

    def test_idempotent_when_new_already_exists(self):
        Path("data").mkdir(exist_ok=True)
        Path("data/users.json").write_text("new", encoding="utf-8")
        Path("users.json").write_text("legacy", encoding="utf-8")
        self.run_migration()
        # 새 파일은 그대로, 레거시는 그 자리에 남아있어야 한다.
        self.assertEqual(Path("data/users.json").read_text(encoding="utf-8"), "new")
        self.assertTrue(Path("users.json").exists())
        self.assertEqual(Path("users.json").read_text(encoding="utf-8"), "legacy")

    def test_faces_dir_moved_only_with_contents(self):
        # 빈 faces/ → 이동 안 함
        Path("faces").mkdir()
        self.run_migration()
        self.assertTrue(Path("faces").exists())
        self.assertFalse(Path("data/faces").exists())

        # 내용물 추가 후 → 이동
        (Path("faces") / "_index.json").write_text("{}", encoding="utf-8")
        self.run_migration()
        self.assertFalse(Path("faces").exists())
        self.assertTrue((Path("data/faces") / "_index.json").exists())

    def test_no_legacy_no_op(self):
        # 깨끗한 상태에서 호출해도 아무 일도 일어나지 않아야 한다.
        self.run_migration()
        self.assertFalse(Path("data").exists() and any(Path("data").iterdir()))

    def test_sqlite_sidecar_files_also_move(self):
        Path("memory.db").write_bytes(b"main")
        Path("memory.db-wal").write_bytes(b"wal")
        Path("memory.db-shm").write_bytes(b"shm")
        self.run_migration()
        self.assertEqual(Path("data/memory.db").read_bytes(), b"main")
        self.assertEqual(Path("data/memory.db-wal").read_bytes(), b"wal")
        self.assertEqual(Path("data/memory.db-shm").read_bytes(), b"shm")


if __name__ == "__main__":
    unittest.main()
