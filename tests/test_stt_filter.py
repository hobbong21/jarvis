"""사이클 #17 — Whisper 한국어 STT 환각/잡음 필터 테스트."""

from __future__ import annotations

import unittest

from sarvis.stt_filter import (
    build_dynamic_initial_prompt,
    clean_stt_text,
    is_hallucination,
)


class HallucinationDetectionTests(unittest.TestCase):
    """Whisper 가 무음에서 자주 만들어내는 한국어 자막 환각 패턴 차단."""

    def test_youtube_thanks_dropped(self) -> None:
        for s in [
            "시청해주셔서 감사합니다",
            "시청 해 주셔서 감사합니다.",
            "시청해주셔서 감사합니다!",
            "시청해 주셔서 고맙습니다",
        ]:
            with self.subTest(s=s):
                self.assertTrue(is_hallucination(s))
                self.assertEqual(clean_stt_text(s), "")

    def test_subscribe_like_dropped(self) -> None:
        for s in [
            "구독과 좋아요 눌러주세요",
            "구독 좋아요 부탁드립니다",
            "좋아요 눌러주세요",
            "채널 구독과 좋아요 부탁드립니다",
        ]:
            with self.subTest(s=s):
                self.assertEqual(clean_stt_text(s), "")

    def test_next_video_dropped(self) -> None:
        for s in [
            "다음 영상에서 만나요",
            "다음 영상에서 뵙겠습니다",
            "다음 시간에 만나요.",
        ]:
            with self.subTest(s=s):
                self.assertEqual(clean_stt_text(s), "")

    def test_news_signoff_dropped(self) -> None:
        for s in ["MBC 뉴스 김철수입니다", "KBS 뉴스 박영희", "SBS 뉴스 이영수입니다."]:
            with self.subTest(s=s):
                self.assertEqual(clean_stt_text(s), "")

    def test_bare_thanks_dropped(self) -> None:
        # "감사합니다" 단독은 무음 환각의 대표 패턴 — 진짜 사용자가 감사 인사를
        # 하려면 보통 앞에 다른 말을 붙임 ("도와주셔서 감사합니다" 등). 단독은 컷.
        for s in ["감사합니다", "감사합니다.", "고맙습니다"]:
            with self.subTest(s=s):
                self.assertEqual(clean_stt_text(s), "")

    def test_jamo_only_dropped(self) -> None:
        for s in ["ㅋ", "ㅎㅎ", "ㅠㅠ", "ㅏㅏㅏ"]:
            with self.subTest(s=s):
                self.assertEqual(clean_stt_text(s), "")

    def test_repetition_spam_dropped(self) -> None:
        self.assertEqual(clean_stt_text("네 네 네 네"), "")
        self.assertEqual(clean_stt_text("아 아 아 아"), "")

    def test_short_filler_dropped(self) -> None:
        for s in ["음", "어", "아", "음."]:
            with self.subTest(s=s):
                self.assertEqual(clean_stt_text(s), "")


class GenuineSpeechPreservedTests(unittest.TestCase):
    """진짜 사용자 발화는 절대 잘리지 않아야 한다 (false positive 방지)."""

    def test_short_yes_no_preserved(self) -> None:
        for s in ["네", "아니", "응", "맞아", "좋아"]:
            with self.subTest(s=s):
                self.assertEqual(clean_stt_text(s), s)

    def test_normal_questions_preserved(self) -> None:
        cases = [
            "오늘 날씨 어때",
            "사비스 안녕",
            "내일 오전 9시에 알람 맞춰줘",
            "방금 전에 내가 뭐라고 했지",
            "감사합니다 사비스",  # 단독 "감사합니다" 가 아니므로 통과
            "시청해주셔서 감사하다는 말도 좀 그렇지",  # 환각 상투구가 안에 있어도 전체가 환각이 아님
        ]
        for s in cases:
            with self.subTest(s=s):
                self.assertNotEqual(clean_stt_text(s), "")

    def test_normalization_strips_extra_whitespace(self) -> None:
        self.assertEqual(clean_stt_text("  안녕   사비스  "), "안녕 사비스")

    def test_non_string_returns_empty(self) -> None:
        self.assertEqual(clean_stt_text(None), "")  # type: ignore[arg-type]
        self.assertEqual(clean_stt_text(123), "")  # type: ignore[arg-type]


class DynamicInitialPromptTests(unittest.TestCase):

    def test_appends_keywords_to_base(self) -> None:
        out = build_dynamic_initial_prompt("기본 프롬프트.", ["민수", "강아지"])
        self.assertIn("기본 프롬프트", out)
        self.assertIn("민수", out)
        self.assertIn("강아지", out)

    def test_empty_keywords_returns_base(self) -> None:
        self.assertEqual(build_dynamic_initial_prompt("기본", []), "기본")
        self.assertEqual(build_dynamic_initial_prompt("기본", None), "기본")  # type: ignore[arg-type]

    def test_empty_base_with_keywords(self) -> None:
        out = build_dynamic_initial_prompt("", ["서울"])
        self.assertIn("서울", out)
        self.assertNotEqual(out.strip(), "")

    def test_dedupes_and_skips_blanks(self) -> None:
        out = build_dynamic_initial_prompt("", ["민수", "민수", "  ", "", "강아지"])
        # "민수" 가 한 번만 나와야 함
        self.assertEqual(out.count("민수"), 1)
        self.assertIn("강아지", out)

    def test_caps_keyword_count(self) -> None:
        kws = [f"단어{i}" for i in range(50)]
        out = build_dynamic_initial_prompt("", kws, max_keywords=5)
        # 최대 5개만 포함
        included = sum(1 for i in range(50) if f"단어{i}" in out)
        self.assertLessEqual(included, 5)

    def test_caps_total_length(self) -> None:
        out = build_dynamic_initial_prompt(
            "", ["가나다라마바사아자차"] * 30, max_keywords=30, max_total_chars=80,
        )
        self.assertLessEqual(len(out), 81)  # 마침표 1자 여유

    def test_skips_overlong_keyword(self) -> None:
        long_kw = "가" * 50
        out = build_dynamic_initial_prompt("", [long_kw, "정상"])
        self.assertNotIn(long_kw, out)
        self.assertIn("정상", out)

    def test_non_string_keyword_skipped(self) -> None:
        out = build_dynamic_initial_prompt("", [123, None, "정상"])  # type: ignore[list-item]
        self.assertIn("정상", out)


if __name__ == "__main__":
    unittest.main()
