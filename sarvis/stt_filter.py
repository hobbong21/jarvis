"""사이클 #17 — Whisper 한국어 STT 결과의 환각 / 잡음 필터.

Whisper 모델은 유튜브 자막 데이터를 대량 학습했기 때문에 무음/잡음 구간을
"시청해주셔서 감사합니다", "구독, 좋아요 눌러주세요" 같은 한국어 자막 상투구로
환각하는 문제가 매우 흔하다. faster-whisper 의 `condition_on_previous_text=False`
와 `no_speech_threshold` 만으로는 완전히 막을 수 없으므로 결과 텍스트에 대한
패턴 매칭을 한 번 더 거친다.

핵심 함수: `clean_stt_text(raw) -> str`
- 양끝 공백/구두점 제거 후 환각이면 빈 문자열 반환
- 환각이 아니면 정규화된 텍스트 반환

설계 원칙:
- 정규식만 사용 — 결정적, 비용 0, LLM 호출 없음
- 보수적: 의심스러우면 통과시킨다 (false negative 보다 false positive 가 더 나쁨 —
  진짜 발화를 잘못 차단하면 사용자가 답답함을 느낌)
- 패턴은 *전체* 가 환각일 때만 차단. 환각 + 진짜 발화가 섞이면 통과시킴
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Pattern

# 전체 발화가 이 패턴 중 하나와 일치 (전후 구두점/공백/이모지 무시) 하면 환각.
# 유튜브 자막에서 가장 흔히 새어나오는 상투구들.
_FULL_HALLUCINATION_PATTERNS: List[Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in [
        # ─── 유튜브 인사/사인오프 ───
        r"^시청\s*해?\s*주셔서?\s*감사합?니다\.?$",
        r"^시청\s*해?\s*주셔서?\s*고맙습?니다\.?$",
        r"^시청\s*해?\s*주신\s*(여러분|모든\s*분)\s*감사합?니다\.?$",
        r"^영상\s*시청\s*해?\s*주셔서?\s*감사합?니다\.?$",
        r"^구독(\s*과)?\s*좋아요\s*(눌러\s*주세요|부탁\s*드립니다|부탁해요)\.?$",
        r"^좋아요\s*(와|랑|및)?\s*구독\s*(눌러\s*주세요|부탁)\.?$",
        r"^구독\s*부탁\s*드립니다?\.?$",
        r"^좋아요\s*눌러\s*주세요\.?$",
        r"^채널\s*구독\s*(과|및)?\s*좋아요\s*부탁\s*드립니다?\.?$",
        r"^알림\s*설정\s*(까지)?\s*부탁\s*드립니다?\.?$",
        # ─── 영상 인트로/아웃트로 ───
        r"^다음\s*(영상|시간)에서?\s*(만나요|뵙겠습니다|봬요)\.?$",
        r"^다음\s*영상에서\s*뵐?\s*게요\.?$",
        r"^오늘\s*영상\s*(은|도|여기까지)\s*입?니다\.?$",
        r"^오늘\s*영상\s*(여기서|이만)\s*마치겠습?니다?\.?$",
        r"^안녕하세요\s*여러분\.?$",
        r"^여러분\s*안녕하세요\.?$",
        # "안녕하세요 ○○입니다" 자기 소개 환각 — 단, "사비스" 자체는 호출어이므로
        # 사용자가 실제로 말할 가능성이 높아 negative lookahead 로 보호.
        r"^안녕하세요\s*(?!사비스)[가-힣A-Za-z]{2,8}\s*입?니다\.?$",
        r"^오늘은\s*.{2,40}\s*에\s*대해\s*알아보(겠|도록\s*하)?습?니다\.?$",
        r"^지금까지\s*[가-힣]{2,8}(이?었|였)습니다\.?$",
        r"^이상\s*[가-힣]{2,8}(이?었|였)습니다\.?$",
        # ─── 뉴스 사인오프 ───
        r"^MBC\s*뉴스\s*[가-힣A-Za-z]{2,8}(입니다)?\.?$",
        r"^KBS\s*뉴스\s*[가-힣A-Za-z]{2,8}(입니다)?\.?$",
        r"^SBS\s*뉴스\s*[가-힣A-Za-z]{2,8}(입니다)?\.?$",
        r"^YTN\s*[가-힣A-Za-z]{2,8}(입니다)?\.?$",
        r"^[가-힣]{2,5}\s*뉴스의?\s*[가-힣]{2,5}(입니다)?\.?$",
        r"^[가-힣]{2,5}\s*뉴스\s*데스크\.?$",
        # ─── 단독 인사말 — "감사합니다/고맙습니다" 류만 보수적으로 차단 ───
        # "안녕하세요"/"반갑습니다" 같은 인사는 사용자가 실제로 던지는 첫 발화가
        # 될 수 있으므로 단독으론 차단하지 않는다 ("여러분 안녕하세요" 등 컨텍스트
        # 가 붙은 경우만 위 영상 인트로 패턴이 잡음).
        r"^감사합니다\.?$",
        r"^고맙습니다\.?$",
        r"^수고하셨습니다\.?$",
        # ─── 자막 출처 ───
        r"^자막\s*제공[:：].*$",
        r"^자막\s*by\s+.*$",
        r"^자막\s*[가-힣A-Za-z\s]{1,30}$",
        r"^번역\s*[:：]\s*.+$",
        r"^번역\s*by\s+.*$",
        # ─── 영어 환각 (한국어 STT 가 영어 유튜브 자막을 흘려넣는 경우) ───
        r"^thank\s+you(\s+for\s+watching)?\.?!?$",
        r"^thanks?\s+for\s+watching\.?!?$",
        r"^please\s+(subscribe|like)(\s+and\s+(subscribe|like))?\.?$",
        r"^see\s+you\s+(next\s+time|in\s+the\s+next\s+video)\.?$",
        r"^bye(\s+bye)?\.?!?$",
        # ─── URL / 광고 ───
        r"^https?://\S+$",
        r"^www\.\S+$",
        r"^\S+\.(com|net|org|kr)(/\S*)?$",
        # ─── 단일 자모/감탄사만 ───
        r"^[ㄱ-ㅎㅏ-ㅣ]+$",
        r"^[음어아오에으]{1,2}\.?$",
        r"^(흠|헤헤?|히히?|호호?|후후?)\.?$",
        # ─── 동일 문자/짧은 토큰 반복 (잡음 환각) ───
        r"^(네\s*){3,}$",
        r"^(예\s*){3,}$",
        r"^(아\s*){3,}$",
        r"^(어\s*){3,}$",
        r"^(음\s*){3,}$",
        r"^([가-힣A-Za-z]{1,3})(\s+\1){3,}$",  # 같은 1-3자 토큰을 4번 이상 반복
        # ─── 광고/안내 환각 ───
        r"^지금\s*들으시는\s*곡은.*$",
        r"^이번\s*주\s*인기\s*(곡|차트)는.*$",
    ]
]

# 텍스트가 너무 짧으면 (구두점/공백 제거 후) 신뢰할 수 없는 발화로 본다.
# 단, "네", "아니", "응" 같은 짧은 답은 보존해야 하므로 1글자는 통과.
_MIN_LENGTH_AFTER_STRIP = 1

# 양끝에서 떼어낼 문자 — 구두점/공백/일부 한국어 종결 기호.
_TRIM_CHARS = " \t\n\r.,!?·…\"'`~()[]{}<>「」『』"


def _normalize(text: str) -> str:
    """NFC 정규화 + 중복 공백 축약 + 양끝 트리밍."""
    if not isinstance(text, str):
        return ""
    s = unicodedata.normalize("NFC", text)
    # 제어문자 제거
    s = "".join(ch for ch in s if ch.isprintable() or ch in (" ", "\t"))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _strip_for_pattern(text: str) -> str:
    return text.strip(_TRIM_CHARS)


def is_hallucination(text: str) -> bool:
    """텍스트 *전체* 가 알려진 Whisper 한국어 환각 패턴이면 True."""
    s = _strip_for_pattern(_normalize(text))
    if not s:
        return True
    if len(s) < _MIN_LENGTH_AFTER_STRIP:
        return True
    for pat in _FULL_HALLUCINATION_PATTERNS:
        if pat.match(s):
            return True
    return False


def clean_stt_text(text: str) -> str:
    """STT 결과를 정제하고, 환각/잡음으로 판정되면 빈 문자열 반환.

    호출자는 빈 문자열이면 응답 사이클을 silent skip 해야 한다 (사용자에게
    "잘 안 들렸어요" 같은 안내도 보내지 않는 편이 자연스럽다 — Whisper 가
    무음에서 환각한 것이지 사용자가 실제로 말한 게 아니므로).
    """
    if is_hallucination(text):
        return ""
    return _normalize(text)


def build_dynamic_initial_prompt(
    base_prompt: str,
    keywords: List[str],
    max_keywords: int = 12,
    max_total_chars: int = 220,
) -> str:
    """사용자 어휘(이름·관심사·최근 토픽 등) 를 base_prompt 에 덧붙여 한국어
    Whisper 의 고유명사 인식률을 올린다.

    Whisper 의 initial_prompt 는 모델이 다음에 나올 토큰을 예측할 때의 힌트로
    사용되므로, 여기에 사용자가 자주 쓰는 단어를 넣어주면 같은 음성을 더
    정확히 받아쓴다. 너무 길면 오히려 효과가 떨어지므로 12개·220자 컷오프.
    """
    base = (base_prompt or "").strip()
    seen = set()
    pieces: List[str] = []
    for kw in keywords or []:
        if not isinstance(kw, str):
            continue
        k = kw.strip()
        if not k or len(k) > 30 or k in seen:
            continue
        seen.add(k)
        pieces.append(k)
        if len(pieces) >= max_keywords:
            break
    if not pieces:
        return base
    extra = "자주 등장하는 단어: " + ", ".join(pieces) + "."
    combined = (base + " " + extra).strip() if base else extra
    if len(combined) > max_total_chars:
        combined = combined[:max_total_chars].rstrip(", ") + "."
    return combined
