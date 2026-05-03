"""사이클 #18 — sarvis.owner_auth 단위 테스트."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from sarvis.owner_auth import (
    FACE_DISTANCE_THRESHOLD,
    VOICE_MATCH_THRESHOLD,
    OwnerAuth,
    face_distance,
    normalize_voice,
    voice_similarity,
)


class NormalizeTests(unittest.TestCase):
    def test_strips_whitespace_and_punct(self):
        self.assertEqual(normalize_voice("사비스, 안녕!"), "사비스안녕")
        self.assertEqual(normalize_voice("  Hello, World.  "), "helloworld")

    def test_nfc_normalization(self):
        # NFD (자모 분해) 입력도 NFC 로 합쳐 같은 결과.
        decomposed = "\u1109\u1161\u11ab"  # NFD "산"
        composed = "산"
        self.assertEqual(normalize_voice(decomposed), normalize_voice(composed))

    def test_empty_or_nonstring(self):
        self.assertEqual(normalize_voice(""), "")
        self.assertEqual(normalize_voice(None), "")  # type: ignore[arg-type]
        self.assertEqual(normalize_voice(123), "")  # type: ignore[arg-type]


class VoiceSimilarityTests(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertEqual(voice_similarity("사비스 안녕", "사비스 안녕"), 1.0)

    def test_punct_difference_still_matches(self):
        self.assertEqual(voice_similarity("사비스, 안녕!", "사비스 안녕"), 1.0)

    def test_completely_different_low(self):
        self.assertLess(voice_similarity("안녕하세요", "오늘 날씨"), 0.3)

    def test_one_typo_high(self):
        # STT 한 글자 누락 시뮬레이션.
        self.assertGreater(voice_similarity("사비스 안녕 나야", "사비스 안녕 나"), 0.85)


class FaceDistanceTests(unittest.TestCase):
    def test_identical_zero(self):
        v = [0.1] * 128
        self.assertEqual(face_distance(v, v), 0.0)

    def test_length_mismatch_inf(self):
        self.assertEqual(face_distance([0.1, 0.2], [0.1]), float("inf"))

    def test_empty_inf(self):
        self.assertEqual(face_distance([], [0.1]), float("inf"))


class OwnerAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sarvis_owner_")
        self.path = Path(self.tmp) / "owner.json"
        self.auth = OwnerAuth(str(self.path))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_starts_unenrolled(self):
        self.assertFalse(self.auth.is_enrolled())
        self.assertEqual(self.auth.face_name, "")
        info = self.auth.info()
        self.assertFalse(info["enrolled"])

    def test_enroll_persists_across_instances(self):
        self.auth.enroll("민수", "사비스 안녕 나야 민수다")
        self.assertTrue(self.auth.is_enrolled())
        self.assertEqual(self.auth.face_name, "민수")

        # 새 인스턴스로 다시 로드.
        auth2 = OwnerAuth(str(self.path))
        self.assertTrue(auth2.is_enrolled())
        self.assertEqual(auth2.face_name, "민수")

    def test_enroll_rejects_short_passphrase(self):
        with self.assertRaises(ValueError):
            self.auth.enroll("민수", "안")

    def test_enroll_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            self.auth.enroll("", "사비스 안녕")

    def test_info_does_not_expose_passphrase(self):
        self.auth.enroll("민수", "사비스 안녕 나야")
        info = self.auth.info()
        self.assertNotIn("voice_passphrase_display", info)
        self.assertNotIn("voice_passphrase_norm", info)
        # 길이 힌트만 노출.
        self.assertEqual(info["voice_passphrase_len"], len("사비스 안녕 나야"))

    def test_verify_voice_exact(self):
        self.auth.enroll("민수", "사비스 안녕 나야")
        self.assertTrue(self.auth.verify_voice("사비스 안녕 나야")[0])

    def test_verify_voice_with_punct_and_case(self):
        self.auth.enroll("민수", "Sarvis Hello Me")
        self.assertTrue(self.auth.verify_voice("sarvis, hello me!")[0])

    def test_verify_voice_one_char_typo(self):
        self.auth.enroll("민수", "사비스 안녕 나야 민수")
        # STT 가 한 글자 누락한 경우.
        self.assertTrue(self.auth.verify_voice("사비스 안녕 나야 민")[0])

    def test_verify_voice_rejects_different(self):
        self.auth.enroll("민수", "사비스 안녕 나야 민수")
        self.assertFalse(self.auth.verify_voice("오늘 날씨 어때")[0])

    def test_verify_voice_rejects_empty(self):
        self.auth.enroll("민수", "사비스 안녕 나야")
        self.assertFalse(self.auth.verify_voice("")[0])
        self.assertFalse(self.auth.verify_voice("   ")[0])

    def test_verify_voice_unenrolled_returns_false(self):
        self.assertFalse(self.auth.verify_voice("뭐든지")[0])

    def test_face_encoding_round_trip(self):
        enc = [0.01 * i for i in range(128)]
        self.auth.enroll("민수", "사비스 안녕 나야", face_encoding=enc)
        self.assertTrue(self.auth.has_face_encoding)
        # 자기 자신은 통과.
        self.assertTrue(self.auth.verify_face_encoding(enc))
        # 비슷한 인코딩 (작은 노이즈) 도 통과.
        noisy = [v + 0.005 for v in enc]
        self.assertTrue(self.auth.verify_face_encoding(noisy))
        # 완전 다른 인코딩 거부.
        far = [v + 0.5 for v in enc]
        self.assertFalse(self.auth.verify_face_encoding(far))

    def test_face_verify_without_encoding_returns_false(self):
        # 등록 시 face_encoding 미제공 (face_recognition 미설치 환경 시뮬).
        self.auth.enroll("민수", "사비스 안녕 나야")
        self.assertFalse(self.auth.has_face_encoding)
        self.assertFalse(self.auth.verify_face_encoding([0.0] * 128))

    def test_reset_clears_state_and_file(self):
        self.auth.enroll("민수", "사비스 안녕 나야")
        self.assertTrue(self.path.exists())
        self.auth.reset()
        self.assertFalse(self.auth.is_enrolled())
        self.assertFalse(self.path.exists())
        # 새 인스턴스도 미등록 상태.
        auth2 = OwnerAuth(str(self.path))
        self.assertFalse(auth2.is_enrolled())

    def test_re_enroll_overwrites(self):
        self.auth.enroll("민수", "사비스 안녕 나야")
        # 사이클 #20 이후 fuzzy matching — 새 패스프레이즈는 옛것과 충분히 달라야
        # 회귀가 의미 있다(substring 일치 방지).
        self.auth.enroll("영희", "오늘은 좋은 날씨")
        self.assertEqual(self.auth.face_name, "영희")
        self.assertFalse(self.auth.verify_voice("사비스 안녕 나야")[0])
        self.assertTrue(self.auth.verify_voice("오늘은 좋은 날씨")[0])

    def test_corrupted_file_treated_as_unenrolled(self):
        self.path.write_text("{not json", encoding="utf-8")
        auth = OwnerAuth(str(self.path))
        self.assertFalse(auth.is_enrolled())


if __name__ == "__main__":
    unittest.main()
