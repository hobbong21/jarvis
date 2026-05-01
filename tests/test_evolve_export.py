"""사이클 #5 T003: GitHub Issue export 회귀 테스트.

- _read_proposal: PROPOSALS_DIR 안 .md 만 허용, traversal/symlink 차단.
- _resolve_repo: 인자/환경변수 우선순위 + 정규식 검증.
- export_proposal_to_github: dry-run, 누락 repo, 토큰 누락, body 잘림.

실행: python -m unittest tests.test_evolve_export -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import harness_evolve  # noqa: E402


class _IsolatedProposals:
    """PROPOSALS_DIR 을 임시 경로로 우회 + 환경변수 백업."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = harness_evolve.PROPOSALS_DIR
        harness_evolve.PROPOSALS_DIR = Path(self._tmp.name)
        self._env_backup = {
            k: os.environ.get(k) for k in
            ("HARNESS_GITHUB_REPO", "GITHUB_REPO", "GITHUB_TOKEN", "GH_TOKEN")
        }
        # 깨끗한 시작
        for k in self._env_backup:
            os.environ.pop(k, None)
        return self

    def write_proposal(self, name: str, body: str) -> Path:
        p = harness_evolve.PROPOSALS_DIR / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def __exit__(self, *exc):
        harness_evolve.PROPOSALS_DIR = self._orig_dir
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class ResolveRepoTests(unittest.TestCase):
    def test_arg_takes_priority(self):
        with _IsolatedProposals():
            os.environ["HARNESS_GITHUB_REPO"] = "env/from_env"
            self.assertEqual(harness_evolve._resolve_repo("arg/repo"), "arg/repo")

    def test_env_fallback_chain(self):
        with _IsolatedProposals():
            os.environ["GITHUB_REPO"] = "fallback/two"
            self.assertEqual(harness_evolve._resolve_repo(None), "fallback/two")
            os.environ["HARNESS_GITHUB_REPO"] = "primary/one"
            self.assertEqual(harness_evolve._resolve_repo(None), "primary/one")

    def test_invalid_repo_rejected(self):
        with _IsolatedProposals():
            self.assertIsNone(harness_evolve._resolve_repo("no_slash"))
            self.assertIsNone(harness_evolve._resolve_repo("a/b/c"))
            self.assertIsNone(harness_evolve._resolve_repo("https://github.com/a/b"))
            self.assertIsNone(harness_evolve._resolve_repo(""))


class ReadProposalTests(unittest.TestCase):
    def test_valid_path_inside_dir(self):
        with _IsolatedProposals() as ctx:
            ctx.write_proposal("cycle-7.md", "# Title 7\n\nbody")
            r = harness_evolve._read_proposal(
                str(harness_evolve.PROPOSALS_DIR / "cycle-7.md")
            )
            self.assertIsNotNone(r)
            self.assertEqual(r["title"], "Title 7")
            self.assertIn("body", r["body"])

    def test_traversal_blocked(self):
        with _IsolatedProposals() as ctx:
            ctx.write_proposal("cycle-1.md", "# t")
            # 절대 경로 traversal
            self.assertIsNone(harness_evolve._read_proposal("/etc/passwd"))
            # 상대 경로 traversal
            self.assertIsNone(harness_evolve._read_proposal("../../etc/passwd"))

    def test_non_md_rejected(self):
        with _IsolatedProposals() as ctx:
            ctx.write_proposal("note.txt", "# t")
            r = harness_evolve._read_proposal(
                str(harness_evolve.PROPOSALS_DIR / "note.txt")
            )
            self.assertIsNone(r)

    def test_missing_file(self):
        with _IsolatedProposals():
            r = harness_evolve._read_proposal(
                str(harness_evolve.PROPOSALS_DIR / "nope.md")
            )
            self.assertIsNone(r)

    def test_symlink_file_inside_dir_allowed(self):
        with _IsolatedProposals() as ctx:
            real = ctx.write_proposal("real.md", "# real\nbody")
            link = harness_evolve.PROPOSALS_DIR / "alias.md"
            try:
                os.symlink(real, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported on this filesystem")
            r = harness_evolve._read_proposal(str(link))
            self.assertIsNotNone(r)
            self.assertEqual(r["title"], "real")

    def test_symlink_escape_blocked(self):
        with _IsolatedProposals() as ctx:
            ctx.write_proposal("inside.md", "# inside")
            outside = Path(tempfile.mkdtemp()) / "secret.md"
            outside.write_text("# secret", encoding="utf-8")
            link = harness_evolve.PROPOSALS_DIR / "leak.md"
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported on this filesystem")
            r = harness_evolve._read_proposal(str(link))
            self.assertIsNone(r)


class ExportTests(unittest.TestCase):
    def test_dry_run_with_repo_arg(self):
        with _IsolatedProposals() as ctx:
            ctx.write_proposal("cycle-9.md", "# 사이클 9\n\nbody")
            r = harness_evolve.export_proposal_to_github(
                str(harness_evolve.PROPOSALS_DIR / "cycle-9.md"),
                repo="me/repo", dry_run=True,
            )
            self.assertTrue(r["ok"])
            self.assertEqual(r["reason"], "dry_run")
            self.assertEqual(r["repo"], "me/repo")
            self.assertTrue(r["dry_run"])
            self.assertIn("payload_size", r)

    def test_missing_repo_returns_friendly_error(self):
        with _IsolatedProposals() as ctx:
            ctx.write_proposal("cycle-9.md", "# t")
            r = harness_evolve.export_proposal_to_github(
                str(harness_evolve.PROPOSALS_DIR / "cycle-9.md"),
                dry_run=True,
            )
            self.assertFalse(r["ok"])
            self.assertIn("missing_or_invalid_repo", r["reason"])

    def test_missing_token_blocks_real_call(self):
        with _IsolatedProposals() as ctx:
            ctx.write_proposal("cycle-9.md", "# t")
            r = harness_evolve.export_proposal_to_github(
                str(harness_evolve.PROPOSALS_DIR / "cycle-9.md"),
                repo="me/repo", dry_run=False,
            )
            self.assertFalse(r["ok"])
            self.assertIn("missing_github_token", r["reason"])

    def test_invalid_path_returns_invalid(self):
        with _IsolatedProposals():
            r = harness_evolve.export_proposal_to_github(
                "../../etc/passwd", repo="me/repo", dry_run=True,
            )
            self.assertFalse(r["ok"])
            self.assertEqual(r["reason"], "invalid_proposal_path")

    def test_body_truncation(self):
        """GitHub body 한도 안전 마진 (60KB) 초과 시 잘림."""
        with _IsolatedProposals() as ctx:
            big = "# big\n" + ("x" * 70000)
            ctx.write_proposal("cycle-big.md", big)
            r = harness_evolve.export_proposal_to_github(
                str(harness_evolve.PROPOSALS_DIR / "cycle-big.md"),
                repo="me/repo", dry_run=True,
            )
            self.assertTrue(r["ok"])
            # 최대 60000 + "\n\n*(truncated by harness_evolve)*\n" (약 36자)
            self.assertLessEqual(r["payload_size"], 60100)
            self.assertGreater(r["payload_size"], 59000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
