"""사비스 감정 시스템 — 색상 팔레트, 감정 태그 파싱"""
import re
from dataclasses import dataclass
from enum import Enum
from typing import Tuple


class Emotion(Enum):
    NEUTRAL   = "neutral"
    LISTENING = "listening"
    THINKING  = "thinking"
    SPEAKING  = "speaking"
    HAPPY     = "happy"
    CONCERNED = "concerned"
    ALERT     = "alert"


@dataclass
class Palette:
    primary:   Tuple[int, int, int]   # 메인 색상 (RGB)
    secondary: Tuple[int, int, int]   # 보조 색상
    glow:      Tuple[int, int, int]   # 후광 색상
    pulse_rate: float                  # 초당 펄스 횟수
    intensity:  float                  # 회전/입자 속도 0..1


PALETTES = {
    Emotion.NEUTRAL:   Palette((40, 140, 220),  (20, 80, 160),   (0, 180, 255),    0.5, 0.35),
    Emotion.LISTENING: Palette((255, 170, 60),  (200, 100, 30),  (255, 200, 80),   1.6, 0.65),
    Emotion.THINKING:  Palette((0, 200, 255),   (0, 130, 200),   (50, 220, 255),   2.4, 0.9),
    Emotion.SPEAKING:  Palette((100, 240, 255), (40, 180, 220),  (150, 255, 255),  3.2, 1.0),
    Emotion.HAPPY:     Palette((100, 255, 180), (50, 200, 140),  (160, 255, 210),  1.4, 0.75),
    Emotion.CONCERNED: Palette((180, 100, 255), (130, 60, 200),  (220, 160, 255),  1.0, 0.5),
    Emotion.ALERT:     Palette((255, 80, 100),  (200, 40, 60),   (255, 140, 160),  2.8, 1.0),
}


_EMOTION_TAG_RE = re.compile(r"\s*\[emotion:(\w+)\]\s*", re.IGNORECASE)


def parse_emotion(text: str) -> Tuple[Emotion, str]:
    """LLM 응답에서 [emotion:xxx] 태그 추출 후 본문과 분리"""
    m = _EMOTION_TAG_RE.match(text)
    if not m:
        return Emotion.NEUTRAL, text
    name = m.group(1).lower()
    body = text[m.end():].strip()
    try:
        return Emotion(name), body
    except ValueError:
        return Emotion.NEUTRAL, body
