"""Fan-out/Fan-in 사전 분석 스케줄러.

한 발화에 대해 의도/감정/시각/메모리 분석을 병렬 수행해 LLM 호출 전 컨텍스트로 합친다.
모든 분석기는 LLM 을 호출하지 않는 로컬 휴리스틱 — 비용/지연 없음.
실패하거나 200ms 타임아웃 초과 시 해당 항목은 빈 결과로 폴백.

반환 dict:
  intent       : str   — "question" | "command" | "emotion" | "smalltalk"
  emotion_hint : str   — "happy" | "sad" | "angry" | "anxious" | "neutral"
  face         : str   — vision 에서 식별된 사용자명 (없으면 "")
  memory_hint  : list[str] — recall 도구가 도움이 될만한 키워드 후보 (최대 3개)
  activity     : str   — action_recognizer 가 분류한 현재 활동 (없으면 "")
  ms           : float — 전체 fan-out 소요 (계측용)
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Dict, List

# ---- 휴리스틱 사전 ----
_QUESTION_END = re.compile(r"(요|까|니|나|냐|죠|지|을까|일까|한가)\s*[?？]?$|[?？]$")
_QUESTION_WORDS = ("뭐", "무엇", "어디", "언제", "누구", "어떻게", "왜", "몇", "얼마", "어느", "어떤")
_COMMAND_VERBS = (
    "해줘", "해 줘", "해주세요", "켜줘", "꺼줘", "보여줘", "찾아줘", "알려줘",
    "기억해", "기억해줘", "기억 해", "타이머", "알람", "재생", "정지", "검색",
    "열어", "닫아", "지워", "추가",
)
_EMOTION_WORDS = {
    "happy": ("좋아", "기뻐", "신나", "행복", "최고", "사랑", "고마", "감사", "ㅎㅎ", "ㅋㅋ"),
    "sad":   ("슬퍼", "우울", "외로", "힘들", "지쳐", "눈물", "허무", "ㅠㅠ", "ㅜㅜ"),
    "angry": ("짜증", "화나", "열받", "싫어", "미워", "짱나", "빡쳐"),
    "anxious": ("불안", "걱정", "두려", "무서", "떨려", "긴장"),
}
_MEMORY_KEYWORDS = (
    "내", "나의", "내가", "기억", "전에", "지난번", "약속", "생일", "이름", "주소",
    "비밀번호", "선호", "좋아하는", "싫어하는", "취미",
)

_TASK_TIMEOUT = 0.2  # 200ms — 휴리스틱이라 충분


async def _intent(text: str) -> str:
    t = text.strip()
    if not t:
        return "smalltalk"
    if any(t.startswith(w) or (" " + w) in t for w in _QUESTION_WORDS):
        return "question"
    if _QUESTION_END.search(t):
        return "question"
    if any(v in t for v in _COMMAND_VERBS):
        return "command"
    # 감정 단어 비중이 높으면 emotion
    score = sum(1 for words in _EMOTION_WORDS.values() for w in words if w in t)
    if score >= 1 and len(t) <= 30:
        return "emotion"
    return "smalltalk"


async def _emotion_hint(text: str) -> str:
    t = text
    best = ("neutral", 0)
    for label, words in _EMOTION_WORDS.items():
        hits = sum(1 for w in words if w in t)
        if hits > best[1]:
            best = (label, hits)
    return best[0]


async def _face_context(session) -> str:
    """vision 에서 현재 식별된 사용자명. session 가 없거나 vision 없으면 빈 문자열."""
    try:
        vision = getattr(session, "vision", None)
        if vision is None:
            return ""
        name = getattr(vision, "current_user", None) or ""
        return str(name) if name else ""
    except (AttributeError, TypeError):
        return ""


async def _activity_context(session) -> str:
    """action_recognizer 가 분류한 활동 라벨/디테일. 없으면 빈 문자열."""
    try:
        ar = getattr(session, "action_recognizer", None)
        if ar is None:
            return ""
        detail = getattr(ar, "get_current_activity_detail", None)
        if callable(detail):
            d = detail() or ""
            if d:
                return d
        label = getattr(ar, "get_current_activity", None)
        if callable(label):
            return label() or ""
        return ""
    except (AttributeError, TypeError):
        return ""


async def _memory_hint(text: str) -> List[str]:
    """기억 키워드 추천. recall 도구 호출 전에 LLM 에 힌트로 전달."""
    t = text.lower()
    hits = [w for w in _MEMORY_KEYWORDS if w in t]
    return hits[:3]


async def _safe(coro, default):
    try:
        return await asyncio.wait_for(coro, timeout=_TASK_TIMEOUT)
    except (asyncio.TimeoutError, Exception):
        return default


async def parallel_analyze(text: str, session=None) -> Dict:
    """5개 분석기를 동시 실행 (asyncio.gather). 각각 200ms 타임아웃."""
    t0 = time.monotonic()
    intent, emo, face, mem, activity = await asyncio.gather(
        _safe(_intent(text), "smalltalk"),
        _safe(_emotion_hint(text), "neutral"),
        _safe(_face_context(session), ""),
        _safe(_memory_hint(text), []),
        _safe(_activity_context(session), ""),
    )
    return {
        "intent": intent,
        "emotion_hint": emo,
        "face": face,
        "memory_hint": mem,
        "activity": activity,
        "ms": (time.monotonic() - t0) * 1000.0,
    }


def analysis_to_context(analysis: Dict) -> str:
    """분석 결과 dict → LLM context 문자열로 직렬화. 빈 항목 생략."""
    if not analysis:
        return ""
    parts = []
    intent = analysis.get("intent")
    if intent and intent != "smalltalk":
        parts.append(f"의도={intent}")
    emo = analysis.get("emotion_hint")
    if emo and emo != "neutral":
        parts.append(f"감정신호={emo}")
    face = analysis.get("face")
    if face:
        parts.append(f"식별된사용자={face}")
    mem = analysis.get("memory_hint") or []
    if mem:
        parts.append(f"기억키워드={','.join(mem)}")
    activity = analysis.get("activity")
    if activity:
        parts.append(f"활동={activity}")
    return ", ".join(parts)
