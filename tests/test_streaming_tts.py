"""기획서 v1.5 스트리밍 TTS 헬퍼 회귀 테스트.

`server._split_first_sentence` 가 의도된 분할 정책을 지키는지 검증.
정책:
  - 전체 길이 < 60 → 분할 안 함
  - 첫 문장 종결([.!?。…\n]) 위치가 [25, 160] 범위면 분할
  - 분할 결과 head/tail 모두 비어 있지 않아야
"""
import os
import sys
import unittest

# server 모듈 import 시 cv2 prefetch / Whisper 로딩 회피.
os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sarvis.server import _split_first_sentence  # noqa: E402


class SplitFirstSentenceTests(unittest.TestCase):
    def test_short_text_not_split(self):
        """전체가 60자 미만이면 분할 안 함."""
        text = "안녕하세요. 반갑습니다."
        head, tail = _split_first_sentence(text)
        self.assertEqual(head, text)
        self.assertEqual(tail, "")

    def test_long_text_split_on_first_sentence(self):
        """충분히 긴 텍스트는 첫 종결 부호에서 분할."""
        text = (
            "오늘 회의는 오후 3시에 시작합니다. "
            "이어서 발표 자료를 검토하고, 마지막으로 다음 주 일정에 대해 논의할 예정입니다."
        )
        head, tail = _split_first_sentence(text)
        self.assertTrue(head)
        self.assertTrue(tail)
        self.assertIn("회의", head)
        self.assertNotIn("회의", tail)
        # head + tail 합치면 원문 핵심이 보존되어야 (공백·strip 차이 무시)
        self.assertIn("발표 자료", tail)

    def test_split_with_question_mark(self):
        """? 도 종결로 인정."""
        text = (
            "어떤 백엔드를 사용하시겠어요? "
            "Claude, OpenAI, GLM, Ollama 중 선택할 수 있고 핫키 1~4 로 즉시 전환됩니다."
        )
        head, tail = _split_first_sentence(text)
        self.assertTrue(head.endswith("?") or head.endswith("? "))
        self.assertIn("Claude", tail)

    def test_split_on_newline(self):
        """줄바꿈도 분할 후보."""
        text = (
            "첫 줄은 충분히 길어야 분할 후보가 됩니다 정말로요\n"
            "두 번째 줄에 본문이 이어집니다. 충분한 길이의 추가 설명입니다."
        )
        head, tail = _split_first_sentence(text)
        self.assertTrue(head)
        self.assertTrue(tail)
        self.assertIn("두 번째 줄", tail)

    def test_first_sentence_too_short_not_split(self):
        """첫 종결이 25자 미만이면 첫 문장은 건너뛰고 다음 후보 탐색."""
        text = "네. " + ("이건 충분히 긴 두 번째 문장입니다 정말로 길어야 합니다. " * 3)
        head, tail = _split_first_sentence(text)
        # "네. " 는 25자 미만이라 건너뛰고, 다음 종결 부호에서 분할되어야 함
        self.assertNotEqual(head, "네.")
        self.assertTrue(tail)

    def test_no_sentence_end_no_split(self):
        """종결 부호가 없는 긴 문장은 분할 안 함 (max_head 이내에 후보 없음)."""
        text = "이 문장은 마침표가 전혀 없는 아주 긴 한국어 문장이지만 분할 가능한 후보가 전혀 없으므로 그대로 단일 합성됩니다 정말로요"
        head, tail = _split_first_sentence(text)
        self.assertEqual(head, text)
        self.assertEqual(tail, "")

    def test_first_candidate_too_far_no_split(self):
        """첫 후보가 max_head(160자) 너머면 분할 포기."""
        prefix = "가" * 200
        text = prefix + ". 짧은 두 번째 문장."
        head, tail = _split_first_sentence(text)
        self.assertEqual(head, text)
        self.assertEqual(tail, "")

    def test_empty_input(self):
        head, tail = _split_first_sentence("")
        self.assertEqual(head, "")
        self.assertEqual(tail, "")


if __name__ == "__main__":
    unittest.main()
