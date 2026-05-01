"""audio_io.py 단위 테스트 — EdgeTTS 의 verify+regen 게이트 분기.

architect 사이클 #7 follow-up:
  - synthesize_bytes_verified 의 ok / blocklist / regen 성공 / regen 실패 / regen 예외 분기
  - synthesize_bytes 가 verified 결과의 audio 만 노출하는지
  - speak() 가 빈 입력은 즉시 반환하는지

EdgeTTS 의 _synthesize 는 외부 네트워크/edge_tts 패키지에 의존하므로
임시 파일을 만들어 반환하는 식으로 mock 한다.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")

from audio_io import EdgeTTS  # noqa: E402


def _fake_synthesize_factory(payload: bytes = b"FAKE_MP3"):
    """_synthesize 호출 시 payload 가 든 임시 파일 경로를 반환하는 mock."""
    def _impl(self, text):
        f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        f.write(payload)
        f.close()
        return f.name
    return _impl


class SynthesizeBytesVerifiedTests(unittest.TestCase):
    def setUp(self):
        self.tts = EdgeTTS()

    def test_blocked_text_returns_empty_audio(self):
        # 빈 텍스트 → verifier 가 empty 로 차단
        result = self.tts.synthesize_bytes_verified("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["audio"], b"")
        self.assertEqual(result["reason"], "empty")
        self.assertFalse(result["regenerated"])

    def test_blocklist_with_no_regen_callback(self):
        with patch("tts_verifier._blocklist_cache", ["secret"]):
            result = self.tts.synthesize_bytes_verified("이건 secret 키")
        self.assertFalse(result["ok"])
        self.assertEqual(result["audio"], b"")
        self.assertTrue(result["reason"].startswith("blocklist:"))

    def test_ok_path_returns_audio(self):
        with patch("tts_verifier._blocklist_cache", []), \
             patch.object(EdgeTTS, "_synthesize", _fake_synthesize_factory(b"AUDIO")):
            result = self.tts.synthesize_bytes_verified("안녕하세요.")
        self.assertTrue(result["ok"])
        self.assertEqual(result["audio"], b"AUDIO")
        self.assertEqual(result["reason"], "ok")
        self.assertFalse(result["regenerated"])
        self.assertGreater(result["length"], 0)

    def test_regen_success(self):
        # 원본은 차단, 콜백이 안전 텍스트 반환 → 합성 성공
        def regen(orig, reason):
            return "안전한 한국어 응답."

        with patch("tts_verifier._blocklist_cache", ["bad"]), \
             patch.object(EdgeTTS, "_synthesize", _fake_synthesize_factory(b"REGEN")):
            result = self.tts.synthesize_bytes_verified("이건 bad 단어 포함",
                                                       regen_callback=regen)
        self.assertTrue(result["ok"])
        self.assertEqual(result["audio"], b"REGEN")
        self.assertTrue(result["regenerated"])

    def test_regen_callback_returns_blocked_text(self):
        # 콜백이 또 차단되는 텍스트를 주면 최종 실패 (regen_failed:...)
        def regen(orig, reason):
            return "여전히 bad 단어 포함"

        with patch("tts_verifier._blocklist_cache", ["bad"]):
            result = self.tts.synthesize_bytes_verified("원본 bad", regen_callback=regen)
        self.assertFalse(result["ok"])
        self.assertEqual(result["audio"], b"")
        self.assertTrue(result["reason"].startswith("regen_failed:"))
        self.assertTrue(result["regenerated"])

    def test_regen_callback_raises_does_not_propagate(self):
        def regen(orig, reason):
            raise RuntimeError("콜백 폭발")

        with patch("tts_verifier._blocklist_cache", ["bad"]):
            result = self.tts.synthesize_bytes_verified("bad", regen_callback=regen)
        # 예외는 격리 — 원래 차단 사유가 그대로 노출
        self.assertFalse(result["ok"])
        self.assertEqual(result["audio"], b"")
        self.assertTrue(result["reason"].startswith("blocklist:"))

    def test_regen_callback_returns_empty(self):
        def regen(orig, reason):
            return ""

        with patch("tts_verifier._blocklist_cache", ["bad"]):
            result = self.tts.synthesize_bytes_verified("bad here", regen_callback=regen)
        self.assertFalse(result["ok"])
        self.assertEqual(result["audio"], b"")

    def test_synthesize_bytes_returns_audio_only(self):
        with patch("tts_verifier._blocklist_cache", []), \
             patch.object(EdgeTTS, "_synthesize", _fake_synthesize_factory(b"X")):
            audio = self.tts.synthesize_bytes("안녕")
        self.assertEqual(audio, b"X")

    def test_synthesize_bytes_blocked_returns_empty(self):
        audio = self.tts.synthesize_bytes("")
        self.assertEqual(audio, b"")

    def test_long_text_truncated_warning_in_result(self):
        long = "이 문장은 충분히 깁니다. " * 200
        with patch("tts_verifier._blocklist_cache", []), \
             patch.object(EdgeTTS, "_synthesize", _fake_synthesize_factory(b"L")):
            result = self.tts.synthesize_bytes_verified(long)
        self.assertTrue(result["ok"])
        self.assertTrue(any(w.startswith("truncated:") for w in result["warnings"]))


class SpeakTests(unittest.TestCase):
    def test_blank_text_no_pygame_init(self):
        tts = EdgeTTS()
        # 빈 텍스트면 _ensure_pygame 도 호출되지 않아야 — 즉시 return
        with patch.object(EdgeTTS, "_ensure_pygame") as ensure:
            tts.speak("")
            tts.speak("   ")
            ensure.assert_not_called()

    def test_no_pygame_silent_return(self):
        tts = EdgeTTS()
        # pygame 이 None 이면 _synthesize 호출 없이 return
        tts._pygame_checked = True
        tts._pygame = None
        with patch.object(EdgeTTS, "_synthesize") as synth:
            tts.speak("hi")
            synth.assert_not_called()


if __name__ == "__main__":
    unittest.main()
