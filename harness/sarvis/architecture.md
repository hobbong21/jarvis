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
| 백엔드 선택 | **Expert Pool** | ✅ | `Brain.think_stream_with_fallback()` — 1차 백엔드 실패 시 가용 백엔드로 자동 재시도. 사용자에게 `backend_fallback` WS 이벤트로 투명 알림. 친절 한국어 에러 + 수동 전환 버튼은 그대로 유지(체인 모두 실패 시). **사이클 #3 #2**: Ollama 헬스체크(60s 캐시, timeout 1.2s + 1회 재시도) 통과 시 항상 후보 포함. |
| 부가 분석 | **Fan-out / Fan-in** | ✅ | `analysis.parallel_analyze()` — intent/emotion/face/memory 4개 분석을 `asyncio.gather` 로 동시 실행 (각 200ms 타임아웃). 결과는 LLM 컨텍스트에 합류. **사이클 #3 #4**: compare 모드도 동일 fan-out 실행 + 텔레메트리 기록. |
| TTS 직전 | **Generate-Verify** | ✅ | `tts_verifier.verify_tts_candidate()` — 길이/한국어 비율/금칙어/제어문자 검증 + 자동 sanitize. 차단 시 `tts_blocked` WS 이벤트로 알림. `data/tts_blocklist.json` 사전 분리. **사이클 #3 #1**: 차단 시 `Brain.regenerate_safe_tts()` 1회 LLM 재작성 → 재검증 → 통과 시 합성. |
| 진화 관측 | **Telemetry & Feedback** | ✅ | `telemetry.log_turn()` — 턴별 메타(백엔드/폴백/지연/intent/TTS결과) JSONL 저장. `GET /api/harness/telemetry` 로 집계 조회. PII(본문) 미수집. **사이클 #3 #3**: `web/harness/dashboard.html` SPA — 토큰 입력(Bearer 헤더 only) + 5초 자동 새로고침 + Evolve 버튼. |
| 진화 자동 제안 | **Self-Evolution** | ✅ | **사이클 #3 #5**: `harness_evolve.propose_next_cycle()` + `POST /api/harness/evolve` — 누적 텔레메트리 (≥`MIN_TURNS`) 를 LLM 에 보내 차세대 사이클 markdown 초안 자동 생성, `harness/sarvis/proposals/cycle-{n}.md` 저장. min_turns 외부 입력은 상향만 허용. |
| 신규 기능 추가 (개발 시) | **Hierarchical Delegation** | ✅ | `architect` → 7개 leaf 에이전트 위임 트리는 본 저장소에 정의됨. |

**목표 합성형 (달성)**: `Supervisor[Pipeline(Fan-out/Fan-in → Expert-Pool → Generate-Verify(+regen))] + Telemetry feedback loop + Self-Evolution proposer`.

모든 핵심 패턴 ✅ + 자기진화 루프 ✅. 추가 개선 항목은 §5 와 `validation.md` 의 open items 참조.

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
| `verifier.tts` | TTS 직전 한국어/길이/금칙 검증 | 후보 텍스트 | pass/fail + 사유 | `tts_verifier.py` + `audio_io.synthesize_bytes_verified()` |
| `analyzer.fanout` | intent/emotion/face/memory 병렬 분석 | 사용자 텍스트 + session | 컨텍스트 dict | `analysis.parallel_analyze()` |
| `evolution.telemetry` | 턴 메타데이터 수집 + 집계 | turn_meta dict | JSONL + summary API | `telemetry.py` |

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
