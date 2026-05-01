---
name: tts-verifier
description: |
  TTS 변환 직전, 응답 후보 텍스트를 검증한다. 한국어 비율, 길이, 금칙 표현,
  민감 정보 누출을 점검하고 통과/재생성 신호를 반환한다.
triggers:
  - "tts 검증"
  - "verify tts"
  - 함수 호출: composer.tts() 직전 자동 게이트
project: SARVIS
generated_by: harness (Phase 4)
---

# TTS Verifier (Generate-Verify 패턴)

## 입력
- `text: str` — TTS 변환 후보.
- `context: dict` — 발화 의도, 백엔드, 사용자 언어.

## 검증 규칙

| # | 규칙 | 임계 | 실패 시 |
|---|------|------|--------|
| 1 | 길이 (문자) | 1 ≤ len ≤ 600 | 너무 길면 요약 재생성, 비면 폴백 메시지 |
| 2 | 한국어 비율 | ≥ 0.6 (한국어 의도일 때) | 백엔드에 한국어 재요청 |
| 3 | 금칙 패턴 | 욕설/혐오 사전 매칭 0건 | 정중한 표현으로 재생성 |
| 4 | 시크릿 누출 | `sk-[a-zA-Z0-9]{20,}` 등 0건 | 즉시 차단 + 알림 |
| 5 | URL 안전성 | 사용자에게 안전한 도메인만 | 차단 또는 사유 첨부 |
| 6 | 빈 응답 | 비어있지 않음 | "다시 한 번 말씀해 주실래요?" 폴백 |

## 출력
```json
{
  "ok": true | false,
  "reasons": ["length_too_long", "low_korean_ratio", ...],
  "suggested_action": "regenerate" | "shorten" | "block" | "fallback_message"
}
```

## 재생성 정책
- 최대 1회 재시도.
- 재시도 실패 시 사용자에게 텍스트로만 출력 (TTS 생략) + 한국어 안내.

## 구현 위치 (제안)
- `audio_io.py` 의 TTS 호출 직전 단일 함수 `verify_tts_candidate(text, context) -> VerifyResult`.
- 사전은 `data/tts_blocklist.json` 로 외부화.

## 검증 (트리거 테스트)
```
- "tts 검증해줘" → 본 스킬 잡힘
- 정상 응답 후보 → ok=True
- 600자 초과 후보 → suggested_action="shorten"
- API 키 패턴 포함 → suggested_action="block"
```

> 본 스킬은 Harness Phase 4 가 SARVIS 도메인에 대해 자동 생성한 산출물의 예시이다.
> 실제 코드는 backend-engineer + voice-engineer 위임으로 구현 예정 (현재 미구현).
