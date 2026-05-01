"""tts_verifier.py 단위 테스트 — Generate-Verify 게이트.

architect 사이클 #7 follow-up (커버리지 0% → ~95%):
  - 정상 / 빈 / 너무 길음 / 차단어 / 제어문자 / 한국어 비율 케이스
  - 정규화 (NFC + 다중공백) / 문장 단위 자르기
  - 차단어 캐시 reload
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")

import tts_verifier  # noqa: E402
from tts_verifier import (  # noqa: E402
    MAX_LEN,
    _korean_ratio,
    _normalize,
    _truncate_at_sentence,
    reload_blocklist,
    verify_tts_candidate,
)


class NormalizeTests(unittest.TestCase):
    def test_strips_control_chars(self):
        self.assertEqual(_normalize("hi\x00\x01world"), "hiworld")

    def test_keeps_tab_and_newline(self):
        out = _normalize("a\tb\nc")
        self.assertIn("\t", out)
        self.assertIn("\n", out)

    def test_collapses_multi_spaces(self):
        self.assertEqual(_normalize("hi    there"), "hi there")

    def test_strips_outer_whitespace(self):
        self.assertEqual(_normalize("   hi   "), "hi")


class KoreanRatioTests(unittest.TestCase):
    def test_pure_korean_is_one(self):
        self.assertAlmostEqual(_korean_ratio("안녕하세요"), 1.0)

    def test_pure_english_is_zero(self):
        self.assertAlmostEqual(_korean_ratio("hello world"), 0.0)

    def test_empty_is_zero(self):
        self.assertEqual(_korean_ratio(""), 0.0)

    def test_only_spaces_is_zero(self):
        self.assertEqual(_korean_ratio("   "), 0.0)

    def test_mixed(self):
        # "안녕 hi" → 한국어 2 / 의미문자 4 = 0.5
        self.assertAlmostEqual(_korean_ratio("안녕 hi"), 0.5)


class TruncateAtSentenceTests(unittest.TestCase):
    def test_short_unchanged(self):
        self.assertEqual(_truncate_at_sentence("짧은 문장.", 100), "짧은 문장.")

    def test_cuts_at_sentence_end(self):
        text = "첫 번째 문장입니다. 두 번째 문장입니다 그리고 더 길어요 그래야 후반에 잘립니다."
        out = _truncate_at_sentence(text, 25)
        # 첫 종결자 위치(약 12자)는 limit*0.6=15 이하이므로 hard-cut + …
        self.assertTrue(out.endswith("…") or out.endswith("."))
        self.assertLessEqual(len(out), 26)

    def test_cuts_at_late_sentence_keeps_period(self):
        # 종결자가 limit*0.6 이후면 그 종결자에서 자름
        text = "이 문장은 충분히 길어서 limit 의 60% 를 넘은 곳에서 끝나요. 뒤에 더 있음."
        out = _truncate_at_sentence(text, 40)
        self.assertTrue(out.endswith("요.") or out.endswith("."))


class VerifyTTSCandidateTests(unittest.TestCase):
    def setUp(self):
        # 차단어 캐시 격리: 빈 리스트로 강제
        self._patch = patch.object(tts_verifier, "_blocklist_cache", [])
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_empty_text(self):
        v = verify_tts_candidate("")
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "empty")

    def test_whitespace_only(self):
        v = verify_tts_candidate("   \n\t  ")
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "empty")

    def test_normal_korean(self):
        v = verify_tts_candidate("안녕하세요. 사비스입니다.")
        self.assertTrue(v["ok"])
        self.assertEqual(v["reason"], "ok")
        self.assertIn("안녕", v["sanitized"])

    def test_too_long_truncates_to_warning(self):
        # 충분히 긴 텍스트(MAX_LEN 초과) 가 잘리고 경고가 남아야 함
        text = ("이 문장은 충분히 깁니다. " * 200)
        v = verify_tts_candidate(text)
        self.assertTrue(v["ok"])
        self.assertLessEqual(len(v["sanitized"]), MAX_LEN)
        self.assertTrue(any(w.startswith("truncated:") for w in v["warnings"]))

    def test_blocklist_blocks_with_reason(self):
        with patch.object(tts_verifier, "_blocklist_cache", ["secretkey"]):
            v = verify_tts_candidate("이건 안에 secretkey 가 있어요.")
            self.assertFalse(v["ok"])
            self.assertTrue(v["reason"].startswith("blocklist:"))
            self.assertIn("secretkey", v["reason"])
            self.assertEqual(v["sanitized"], "")

    def test_blocklist_case_insensitive(self):
        with patch.object(tts_verifier, "_blocklist_cache", ["AKIA"]):
            v = verify_tts_candidate("키는 akiaXYZ 입니다.")
            self.assertFalse(v["ok"])
            self.assertTrue(v["reason"].startswith("blocklist:"))

    def test_low_korean_ratio_only_warns(self):
        text = "this is a long english only sentence with no Hangul at all really"
        v = verify_tts_candidate(text)
        self.assertTrue(v["ok"])  # 차단하지 않고 경고만
        self.assertTrue(any("low_korean_ratio" in w for w in v["warnings"]))

    def test_short_text_skips_korean_check(self):
        v = verify_tts_candidate("OK")
        self.assertTrue(v["ok"])
        self.assertFalse(any("low_korean_ratio" in w for w in v["warnings"]))


class ReloadBlocklistTests(unittest.TestCase):
    def test_reload_returns_count_and_resets_cache(self):
        # 실제 파일 로드 — data/tts_blocklist.json 이 있어야 함
        with patch.object(tts_verifier, "_blocklist_cache", ["fake"]):
            # 강제로 캐시 무효화 후 재로드
            n = reload_blocklist()
            self.assertGreaterEqual(n, 0)
            # 다시 로드해도 동일
            n2 = reload_blocklist()
            self.assertEqual(n, n2)


if __name__ == "__main__":
    unittest.main()
