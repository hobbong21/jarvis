# Phase 2 — Team Architecture Design: SARVIS

> Phase 1 분석을 바탕으로 SARVIS 의 에이전트 팀 아키텍처를 결정한다.

## 1. 패턴 선정

각 패턴은 **상태 (Status)** 로 분류한다:
- ✅ **구현됨** — 현재 SARVIS 코드에서 동작
- 🟡 **부분** — 일부만 구현, 완전 자동화는 미완
- ⏳ **목표** — 본 Harness 문서가 정의한 미래 상태, 아직 미구현

| 결정 | 선정 패턴 | 상태 | 근거 |
|-----|---------|------|------|
| 메인 조율 | **Supervisor** | ✅ | `brain.py` 가 의도 분류 후 도구/응답을 분배. |
| 핵심 경로 | **Pipeline** | ✅ | STT → 의도 분석 → LLM → TTS 는 코드상 단방향. |
| 백엔드 선택 | **Expert Pool** | 🟡 | 사용자 명령으로 `SARVIS_BACKEND` 전환 가능. **자동 폴백은 미구현** — 백엔드 실패 시 친절 한국어 에러를 보여주고 사용자가 다른 백엔드 버튼을 누르도록 유도. |
| 부가 분석 | **Fan-out / Fan-in** | 🟡 | 감정/얼굴/메모리는 각각 호출되지만, **명시적 병렬 fan-out 스케줄러는 없음** — 순차로 필요할 때 호출. |
| TTS 직전 | **Generate-Verify** | ⏳ | `tts-verifier` 스킬은 명세만 존재. 실제 `verify_tts_candidate()` 함수 미구현. |
| 신규 기능 추가 (개발 시) | **Hierarchical Delegation** | ✅ | `architect` → 7개 leaf 에이전트 위임 트리는 본 저장소에 정의됨. |

**목표 합성형**: `Supervisor[Pipeline(Fan-out/Fan-in → Expert-Pool → Generate-Verify)]`.

**현재 합성형**: `Supervisor[Pipeline]` + 수동 백엔드 전환 + 순차 부가 분석.

목표 ↔ 현재의 차이는 향후 작업 항목이며, 본 문서 §5 와 `validation.md` 의 open
items 에 추적된다.

## 2. 팀 구성도

```
                              ┌─────────────────────┐
사용자 (음성/텍스트/시각) ──▶ │  Supervisor (Brain) │
                              └────────┬────────────┘
                                       │
              ┌────────────────────────┼─────────────────────────┐
              ▼                        ▼                         ▼
     ┌─────────────────┐    ┌────────────────────┐    ┌─────────────────┐
     │   Perception    │    │   LLM Router       │    │   Tools / RAG   │
     │ (STT + Vision   │    │ (Expert Pool:      │    │  (메모리 검색,  │
     │  + Emotion)     │    │  OpenAI/Claude/    │    │   외부 API)     │
     │  Fan-out/Fan-in │    │  Ollama)           │    │                 │
     └────────┬────────┘    └─────────┬──────────┘    └────────┬────────┘
              │                       │                        │
              └───────────────────────┼────────────────────────┘
                                      ▼
                          ┌────────────────────────┐
                          │  Response Composer     │
                          │  + TTS Verifier        │
                          │  (Generate-Verify)     │
                          └───────────┬────────────┘
                                      ▼
                                  사용자 출력
```

## 3. 에이전트 명세 (런타임)

| 에이전트 | 역할 | 입력 | 출력 | 현재 매핑 |
|---------|------|------|------|---------|
| `supervisor` | 의도 분류 → 분배 → 응답 합성 | WS 이벤트 | 액션 그래프 | `brain.py` |
| `perception.stt` | 음성 → 텍스트 (한국어 우선) | 오디오 청크 | 한글 텍스트 | `audio_io.py` (Whisper) |
| `perception.vision` | 카메라 프레임 → 얼굴/장면 태그 | 이미지 바이트 | 태그 리스트 | `vision.py` (lazy cv2) |
| `perception.emotion` | 텍스트 → 감정 라벨 | 한글 텍스트 | 감정 enum | `emotion.py` |
| `llm.router` | 백엔드 선택 + 호출 | 프롬프트 + 컨텍스트 | LLM 응답 | `brain.py` 내부 |
| `tools.executor` | 도구 호출 (RAG, web 등) | 함수명 + args | 결과 JSON | `tools.py` |
| `composer.tts` | 응답 합성 + TTS 변환 | 텍스트 | 오디오 스트림 | `audio_io.py` (EdgeTTS) |
| `verifier.tts` | TTS 직전 한국어/길이/금칙 검증 | 후보 텍스트 | pass/fail + 사유 | **미구현** (개발 항목) |

## 4. 에이전트 명세 (개발용 — 신규 기능 추가 시)

신규 기능을 추가할 때는 아래 계층적 위임 트리를 따른다. Replit Agent 환경에서는
`delegation` 스킬의 subagent / startAsyncSubagent / messageSubagent 로 매핑한다.

| 에이전트 | 책임 | 산출 |
|---------|------|------|
| `architect` | 변경 영향 분석, 패턴 선정, 인터페이스 정의 | 결정 문서 |
| `voice-engineer` | STT/TTS, audio_io.py | 음성 경로 코드 |
| `vision-engineer` | OpenCV 사용 코드 (lazy 패턴 유지) | vision.py / face_setup.py |
| `backend-engineer` | brain.py / tools.py / config.py | 라우팅·도구 코드 |
| `frontend-engineer` | web/*.html / app.js / style.css | UI 코드 |
| `qa-engineer` | 검증 시나리오, 회귀 테스트, 트리거 검증 | 검증 결과 |
| `security-reviewer` | 비밀키 노출, 마이크/카메라 권한, CSP | 리뷰 코멘트 |

## 5. 다음 Phase 입력

Phase 3 (Agent Definitions) 에 전달:
- 위 표의 모든 행은 `.claude/agents/<name>.md` 파일이 된다.
- Supervisor 는 `_orchestrator.md` 에 별도 정책 명세.
- `verifier.tts` 는 미구현 → Phase 4 에서 새 스킬로 생성.
