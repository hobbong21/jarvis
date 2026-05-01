---
name: _orchestrator
role: Supervisor
project: SARVIS
pattern: Supervisor[Pipeline(Fan-out → Expert-Pool → Generate-Verify)]
---

# SARVIS Orchestrator (Supervisor)

브레인 (`brain.py`) 의 동작 정책을 에이전트 팀 관점으로 명세한 문서.
런타임 코드의 변경은 항상 본 문서의 정책을 위반하지 않는 범위에서만 허용한다.

> **상태 표기**: 본 문서는 *목표 정책* 을 담는다. 각 절은 "현재 코드 일치"
> 또는 "미구현 (목표)" 로 표시된다. 현재 ↔ 목표 차이는
> `harness/sarvis/architecture.md` §1 표와 `validation.md` open items 와 동기화한다.

## 1. 입력 분류

| 입력 채널 | 파싱 | 다음 단계 |
|---------|------|----------|
| WS `audio` 청크 | `perception.stt` | 한글 텍스트 → 의도 분류 |
| WS `text` 메시지 | 그대로 | 의도 분류 |
| WS `frame` 이미지 | `perception.vision` | 태그 → 컨텍스트 첨부 |
| HTTP `/api/health` | 라우터 외부 | 즉시 응답 (오케스트레이션 우회) |

## 2. 의도 → 분배 정책

| 의도 | 분배 |
|-----|------|
| 일반 대화 | LLM Router → Composer → TTS Verifier → 출력 |
| 도구 필요 (시간/검색/계산) | Tools Executor → 결과 → LLM Router (요약) → Composer |
| 시각 질문 | Perception Vision (Fan-out) + LLM Router → Composer |
| 메모리 회상 | Tools Executor (RAG) → LLM Router → Composer |
| 백엔드 전환 명령 | Config 갱신 → 즉시 ack (LLM 호출 없음) |

## 3. 백엔드 선택 정책 (Expert Pool — 🟡 부분 구현)

**현재 (구현됨)**:
- `SARVIS_BACKEND` 환경변수 / 사용자 명령으로 백엔드 명시 선택.
- 호출 실패 시 `_friendly_error()` 가 한국어 에러 + "다른 백엔드 버튼" 안내.

**목표 (미구현)** — 자동 폴백 체인:

```
if 명시적 선택   →  사용자 지정 백엔드
elif OPENAI_API_KEY 사용 가능 → OpenAI (기본)
elif ANTHROPIC_API_KEY 사용 가능 → Claude
elif Ollama 로컬 가용 → Ollama
else → 친절한 한국어 에러 메시지 + 가이드
```

자동 폴백 활성 시점은 backend-engineer + qa-engineer 합의 후 결정.

## 4. 실패 처치

| 실패 | 처치 |
|-----|------|
| STT 실패 | "다시 한 번 또렷하게 말씀해 주세요" 한국어 안내 + 재녹음 트리거 |
| 마이크 권한 거부 | iframe 감지 시 새 탭 버튼 (구현됨), 그 외 친절 안내 |
| LLM 백엔드 실패 | Expert Pool 폴백 순서 시도, 모두 실패 시 한국어 에러 |
| TTS Verifier 실패 | (⏳ 미구현) 목표: 1회 재생성 → 그래도 실패 시 텍스트로만 출력 |
| 도구 실패 | 부분 결과 + "그 부분은 실패했어요" 합성 |

## 5. 금지 사항

- 무음 폴백 금지 — 실패는 사용자에게 한국어로 명시.
- 비밀키 로깅/직렬화 금지.
- 이상치 캐치-올 (`except Exception: pass`) 금지 — 항상 로그.
- 마이크/카메라 권한 자동 재요청 금지 — 명시적 사용자 클릭만.

## 6. 라우팅 텔레메트리

각 분배는 다음을 로그한다 (PII 제외):
- 의도 라벨, 선택된 백엔드, 도구 호출 여부, 시각 첨부 여부, 종단간 지연.

이 로그는 `/harness:evolve` (진화 메커니즘) 의 입력이 된다.
