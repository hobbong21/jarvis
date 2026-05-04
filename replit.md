# S.A.R.V.I.S — Personal AI Assistant

A multimodal AI assistant inspired by the 4-stage agent pattern (Task Planning → Model Selection → Task Execution → Response Generation). Features face recognition, voice interaction, and tool-augmented intelligence.

## 기획서 업데이트 — v1.6 (2026-05-03) — Harness Agent 보조 문서 우선

**상위 문서** (Ch.17 도구) `attached_assets/Sarvis_기획서_및_개발요구사항_1777803382509.docx`
는 그대로 유효. 추가로 **보조 문서** `attached_assets/Sarvis_Harness_Agent_기획서_1777804423173.docx`
(v1.0, 16장 + 부록 A~D, 약 230 인일) 가 첨부되어 **우선 적용**된다.

**HA(하네스 에이전트) 핵심**: 사람이 쓰는 Harness System(도구) 위에, 그 도구를
사람 대신 운용하는 6대 LLM 에이전트(Observer/Diagnostician/Strategist/
Improver/Validator/Reporter) + Orchestrator + Meta-Evaluator 자율 진화 계층을
얹는다. 5원칙: Observability First, Bounded Autonomy, Reversibility, Multiple
Evaluators, Humble by Default.

**SARVIS 단일 주인 맞춤 5단계 도입 로드맵** (`docs/SARVIS_HA_AGENT_MATRIX.md` 상세):

| 사이클 | Stage | 자율 등급 | 포함 에이전트 | 진입 기준(다음 단계) |
|---|---|---|---|---|
| #23 | S1 Read-Only | L0 | Observer + Reporter(미니) | Observer 정확도 ≥ 85% |
| #24 | S2 Diagnose | L1 | + Diagnostician + Reporter(전체) | 보고 승인률 ≥ 70% |
| **#25 (현재)** | S3 Improve | L1 (모두 사람 승인) | + Strategist + Improver + Validator | 회귀율 ≤ 5% |
| #26 | S4 Auto-Suggest | L2 (가역 변경 자동) | (동일) | 30일 자동 롤백 ≤ 2건 |
| #27 | S5 Constrained Auto | L3 | + 자율성 등급 정책 | 6개월 + 외부 감사 |

**기존 Ch.17 사이클 #23 (HARN-06 성향 슬라이더 + HARN-07 페르소나) 는 사이클
#28+ 으로 후순위 이동.** 이유: HA Stage 진입이 페르소나 튠보다 안전·구조적
선결 조건. 페르소나 튠은 Strategist/Validator 가 검증할 대상이므로 S3 도달
후 통합하는 것이 자연스럽다.

**절대 금지 7원칙** (`docs/SARVIS_HA_AGENT_MATRIX.md` §4 — 인프라 차단):
HA 자기 코드 수정, 사용자 데이터 외부 송출, 결제·삭제 직접 실행, Sarvis 안전
프롬프트 섹션 수정, 자기 모니터링/감사/롤백 비활성화, Meta-Evaluator 입출력
영향, Kill Switch 우회 — 모두 코드/권한 분리로 차단.

## Cycle #25 — HA Stage S3 (Strategist + Improver + Validator, L1 — 사람 승인)

사이클 #24 의 진단을 입력으로 **변경 후보 → 패치 명세 → 위험 검증 → 승인
큐** 까지 자동화. **모든 출력은 ha_proposals(pending)** — 사람 승인 없이
어떤 적용도 일어나지 않는다(L1 유지). 승인되어도 "적용" 은 Stage S4
도입 전까지 발생하지 않으며, WS 응답에 `applied=False` 로 명시한다.

신규 모듈 3종:
- `sarvis/ha/strategist.py` — 8 카테고리 룰 매트릭스(prompt_tweak,
  tool_swap, heuristic_threshold, knowledge_add, ui_hint, model_route,
  monitoring_only, **do_nothing 강제 포함**). 카테고리/근본원인별 후보
  3~5개 + Do Nothing.
- `sarvis/ha/improver.py` — Strategy → PatchSpec(target/before/after/
  reversible/rationale). 텍스트 명세만, 실제 파일 수정 없음.
- `sarvis/ha/validator.py` — target prefix별 기본 위험 + reversibility
  페널티 + 코드 경로 가산 + diag_conf 가산. risk_level low/med/high.
  `auto_approval_blocked=True` 고정 (L1 규칙).

Memory 신규 3 테이블 (`ha_strategies`, `ha_proposals`, `ha_validations`)
+ append-only 트리거 (strategies/validations) — proposals 는 status
갱신 필요로 트리거 제외(주석). 메서드 11종 신규.

server WS 5종 신규 (인증 + Kill Switch 게이트 동일):
`ha_run_strategist` `ha_run_improver` `ha_run_validator`
`ha_proposals_list{status?}` `ha_proposal_decision{proposal_id,
decision, by}`. UI: Strategist/Improver/Validator/제안 큐 버튼 +
승인·거부 인라인 버튼 (위험 등급/점수 배지 + after_text 미리보기).

Reporter `growth_diary` 가 strategies/proposals 를 함께 노출. stage =
"S3 — Improve Suggest", active_agents = 6 (+ Strategist + Improver +
Validator).

테스트 +22 (S3 단위 17 + WS round-trip 5). 회귀 **696/696 통과**.

## Cycle #30 — 카메라 녹화 기능

음성 명령("녹화해"/"녹화 중지")으로 카메라 영상 녹화 시작/중지.
녹화 파일은 `data/recordings/{user_id}/` 에 WebM 형식으로 저장.

**아키텍처**:
- `sarvis/tools.py`: `start_recording`/`stop_recording` 도구 + `on_recording` 콜백
- `sarvis/server.py`: UserSession 녹화 상태, `recording_cmd` WS emit, 0x09 바이너리 수신→파일 저장
- `sarvis/memory.py`: `recordings` 테이블 + `save_recording`/`list_recordings`/`delete_recording` CRUD
- `sarvis/config.py`: `recordings_dir` 설정, system_prompt 녹화 도구 안내
- `web/app.js`: MediaRecorder API, `recording_cmd`/`recording_saved` 이벤트 핸들링, 0x09 바이너리 전송
- `web/index.html` + `web/style.css`: REC 인디케이터 (빨간 점 + 경과 시간)

**음성 녹음** (같은 사이클): `start_audio_recording`/`stop_audio_recording` 도구 추가.
- 카메라 없이 마이크만으로 녹음 가능. 영상 녹화와 동시 사용 가능.
- 바이너리 프로토콜 0x0A (음성), 0x09 (영상). 동일 payload 구조: `[4B duration_ms BE][2B label_len BE][label UTF-8][WebM blob]`
- recordings 테이블 `kind` 컬럼으로 video/audio 구분 (기존 DB 자동 마이그레이션).
- UI: 영상=빨간 REC, 음성=파란 MIC 인디케이터 (동시 표시 가능).
- **보안**: 인증 게이트 적용 (미인증 시 0x09/0x0A 무시). 파일명 밀리초 타임스탬프로 충돌 방지.

## Cycle #24 — HA Stage S2 (Diagnostician, L1 — 진단까지)

사이클 #23 의 Observer 가 만든 `ha_issues(status='open')` 를 입력으로,
**5 Whys + 베이지안 가설 랭킹** 휴리스틱(+옵션 LLM)으로 근본원인·가설 후보·
권장 다음 액션을 첨부. 변경 적용은 여전히 없음 (L1 = Diagnose-only).

신규: `sarvis/ha/diagnostician.py`, Memory `ha_diagnoses` 테이블 + 5 메서드
(`ha_diagnosis_insert`, `ha_diagnoses_for_issue`, `ha_diagnoses_recent`,
`ha_issues_open`, `ha_issue_set_status`), WS 2종 (`ha_run_diagnostician`,
`ha_diagnoses_for_issue`), UI "Diagnostician" 버튼 + 가설 렌더러,
Reporter One-Pager `## Diagnosis` 섹션이 ha_diagnoses 에서 자동 채움.
범주별 룰 트리 5종 (spike/drift/anomaly/cost/underutilization) + fallback.
status 전이: `open → diagnosed`. emit: Diagnostician → Reporter.

P0 보완 (architect 1차): five_whys 1급 산출물(DiagnosisResult/DB/Reporter/
UI) + ha_messages·ha_diagnoses BEFORE UPDATE/DELETE 트리거(RAISE ABORT)
로 DB 레벨 append-only 강제. ha_kill_switch_log 는 open/close UPDATE
설계상 트리거 제외(주석 명시).

테스트 +17 (Diagnostician 13 + WS 2 + 5 Whys 2 + DB 트리거 2). 회귀
**674/674 통과**.

## Cycle #23 — HA Stage S1 Read-Only (Observer + Reporter 미니)

(상세는 `docs/SARVIS_DEVELOPMENT_REPORT.md` 사이클 #23.)

**아키텍처**: `sarvis/ha/` 신설 — `base.py`(HAMessage append-only + HAAgent
read/write scope), `safety.py`(Kill Switch + 7원칙 가드), `observer.py`
(트레이스 스캔 + 휴리스틱 anomaly + 선택적 LLM 패턴 인식), `reporter.py`
(One-Pager 마크다운 + 사용자 성장 일기).

**메모리 (`sarvis/memory.py`)**: 신규 4 테이블.
- `ha_messages(msg_id PK, schema_version, from_agent, to_agent, payload JSON,
  signature HMAC, created_at)` — append-only(코드 레벨 가드, UPDATE/DELETE 거부).
- `ha_issues(issue_id PK, category, severity, evidence_traces JSON, signal,
  narrative, confidence, status, created_at)`.
- `ha_kill_switch_log(id, activated_by, activated_at, deactivated_at, reason)`.
- `ha_optout(user_id PK, opted_out_at)` — 옵트아웃 사용자는 Observer 입력에서
  자동 제외(JOIN-제외 + LEFT JOIN NULL).

**서버 (`sarvis/server.py`)**: 신규 WS 5종(인증 게이트):
- `ha_run_observer{window_days?}` — 즉시 1회 스캔.
- `ha_issues_list{limit?}` — 최근 이슈 카드.
- `ha_kill_switch{on}` — 운영자 Kill Switch.
- `ha_optout{on}` — 사용자 옵트아웃 토글.
- `ha_growth_diary` — 변경 이력(현 단계는 issues + 만족도 추세).
Kill Switch 활성 시 모든 ha_* WS 거부 + stdout 로그.

**UI**: prod-dock "내 Sarvis 성장 일기 (HA)" 카드 — 실행 버튼, 이슈 카드 N개,
옵트아웃 토글, Kill Switch 토글.

**전 사이클 (#22) 알려진 한계**: compare 모드 응답에서는 `turn_logged` 가
발화되지 않아 👍/👎 불가. 사이클 #25(Validator) 가 compare 결과를 정식
이슈 카드로 흡수하면 자연 해소 예상.

## Cycle #22 — Observe v1 (HARN-12 피드백 + HARN-05 미니 "내 Sarvis")

Harness 첫 사이클. 사용자가 응답 품질을 명시적으로 표현하고(👍/👎),
사비스가 "내가 어떻게 쓰이고 있는지" 한눈에 보여주는 최소 기능 셋.

**메모리 (`sarvis/memory.py`)**
- 신규 테이블 `command_feedback(id, command_id FK→commands ON DELETE CASCADE,
  user_id, rating ∈ {-1,0,+1}, comment ≤1000자, created_at, updated_at,
  UNIQUE(command_id))` — `_SCHEMA_SQL` + 레거시 DB 마이그레이션 양쪽에 정의.
- `set_feedback(cmd_id, user_id, rating, comment=None)` — rating 검증
  (ValueError), command 존재 확인 (ValueError), comment 1000자 자동 절단,
  UPSERT(`ON CONFLICT(command_id) DO UPDATE`).
- `get_feedback(cmd_id) → dict | None`.
- `my_sarvis_summary(user_id, window_sec=7d) → dict` — 윈도우 내 명령 수,
  오류 수, 종류별 top-5, 만족도(up/(up+down) %), 최근 부정 피드백 5건,
  DB 파일 크기(MB). window_sec ≥ 60s clamp.

**서버 (`sarvis/server.py`)**
- `respond_internal` 끝에서 `log_command(channel) + update_command(response_text)`
  하여 turn 별 cmd_id 확보 → `emit(type="turn_logged", cmd_id=...)`.
  meta 에 `{emotion, backend, intent, fallback_used}` 적재.
- 신규 WS 메시지 2종 (인증 게이트 set 에 추가):
  - `feedback_submit{cmd_id, rating, comment?}` → `feedback_result{ok, cmd_id, rating, comment}`.
  - `my_sarvis_summary{window_days?}` → `my_sarvis_summary{...}`.

**UI (`web/{index.html, app.js, style.css}`)**
- 응답 버블 finalize 시 placeholder `.fb-row[data-pending=1]` 슬롯 생성 →
  `turn_logged` 수신 시 마지막 placeholder 에 👍/👎 버튼 attach. 클릭 시
  `feedback_submit` 송신, `feedback_result` 로 상태/색상 갱신.
- prod-dock 에 "내 Sarvis (Ch.17)" 카드 추가 — 만족도/명령 수/오류 수/저장 MB/
  자주 쓴 종류 pill/최근 아쉬웠던 답변 5건. 패널 열 때 + 피드백 후 자동 갱신.

**테스트**
- `tests/test_feedback.py` (19건): set/get/UPSERT/검증/취소(0)/긴 comment 절단/
  존재 안 하는 cmd/CASCADE/empty summary/카운팅/top kinds/만족도/오류 수/
  최근 부정/타 사용자 격리/오래된 행 제외/window clamp/recent_negative 5건 제한.
- `tests/test_server_endpoints.py::FeedbackAndMySarvisWSTests` (6건):
  turn_logged + cmd_id 발화, feedback round-trip, invalid cmd_id, invalid rating,
  my_sarvis_summary 기본/custom window.
- 전체 회귀 **635/635 PASS** (이전 610 → +25, 28.66s).

**알려진 한계 (다음 사이클 후보)**
- **compare 모드(Claude+OpenAI A/B)** 는 `respond_compare` 가 별도 경로라 `turn_logged` 를
  발화하지 않음. compare_end 는 finalizeStreamBubble 을 호출하지 않으므로 placeholder
  누적은 없으나, A/B 답변 각각에 👍/👎 를 달려면 compare 경로에 cmd_id 발화 추가 필요.
- HARN-01 trace 컬럼 확장(요청·응답 토큰 수 등)은 이번 사이클에서 미실시 — `commands.meta`
  에 `{emotion, backend, intent, fallback_used}` 만 적재. 다음 사이클에서 트레이스 보강.

## Cycle #21 — 회의록(F-04) + 할 일/캘린더(F-10)

기획서 P0/P1 미구현 우선순위 두 항목을 한 사이클로 묶어 추가.

**F-04 회의록 자동 기록·요약**
- `sarvis/meeting.py` — `Meeting`, `Utterance`, `MeetingRegistry`. 잡음
  필터(빈/단음절 거부), 동시 회의 1개 강제(이중 시작 시 RuntimeError), 종료
  후 chunk 무시, 마크다운 산출물(요약/결정사항/액션아이템 테이블/타임스탬프 트랜스크립트).
- LLM 요약은 의존성 주입(`brain_summarize_fn`) → 단위 테스트 가능. 실제 호출은
  `server.py` 가 `session.brain.anthropic_client` 직접 사용 + `parse_summary_json`
  으로 코드펜스/잡음 강건 파싱. LLM 실패/형식이상 시 fallback (트랜스크립트 앞 3줄).
- WS 핸들러: `meeting_start`, `meeting_chunk` (text 또는 audio_b64 → STT),
  `meeting_end` (요약+저장), `meeting_list`, `meeting_get`. 영속화는
  `data/meetings/<id>/{meeting.json,meeting.md}`.

**F-10 할 일/캘린더**
- `sarvis/todos.py` — `TodoStore`(원자적 rename 쓰기, 손상 파일 .corrupt.json 백업),
  `TodoItem`(priority high/normal/low, source voice/manual/meeting/llm),
  우선순위+최신순 정렬, 자유 발화에서 LLM 으로 항목 자동 추출(`extract_todos_from_text`).
- WS 핸들러: `todo_list`, `todo_add`, `todo_done`, `todo_remove`, `todo_extract`
  (LLM 추출 + 자동 추가). 영속화는 `data/todos.json`.

**UI**
- 우하단 floating dock(📋) — 회의 시작/종료/요약 카드 + 할 일 추가/완료/자동추출 카드.
- 기존 app.js 의 ws 인스턴스를 `window.__ws/__sendWS` 로 노출 → 패널이 추가
  message 리스너만 부착하도록 minimal-invasive 통합.

**테스트**
- `tests/test_meeting.py` (14건): 잡음 필터, 종료 후 거부, summarize 정상/깨진/예외
  fallback, 영속화 round-trip, 마크다운 섹션, registry 동시 1개 강제, JSON 파서.
- `tests/test_todos.py` (12건): persist, 정규화, 우선순위 정렬, mark_done/remove,
  손상 파일 복구, parse_todo_json 펜스, extract 짧은 텍스트/예외/정상.
- 26/26 PASS (0.35s).

## Cycle #20 — Owner Authentication 보강 (F-01: 5각도 + 라이브니스 + 챌린지)

기획서 F-01("주인 인증") 의 보안 갭 보강. 사이클 #18 의 단일-각도 + 패스프레이즈만 매칭 한계를 다음 3축으로 강화.

**구성:**
- `sarvis/owner_auth.py` (재작성):
  - **5각도 다중 인코딩** — `face_encodings: List[List[float]]` 저장, `verify_face_encoding` = `min(distance) ≤ 0.55`. 구버전 `face_encoding` 단일 필드도 자동 호환 (schema_version 1→2 마이그레이션).
  - **챌린지 풀** — `VOICE_CHALLENGE_POOL` (10개 한국어 자연 문장), `random_challenge()` = `secrets.choice` (예측 불가).
  - **`verify_voice(text, challenge_text=)`** = (ok, similarity, matched_against∈{passphrase,challenge,''}). 패스프레이즈 OR 챌린지 둘 중 하나 ≥ 0.78 시 통과 — 녹음 재생 공격 대응.
  - **`detect_blink_in_window(ear_samples)`** — stateless. open(EAR≥0.24) → close(≤0.18) → open 전이 1회 이상이면 blinked. 윈도우 6.0s, 최소 4 프레임. 히스테리시스 적용.
- `sarvis/vision.py`:
  - **`compute_eye_aspect_ratio_from_jpeg`** — `face_recognition.face_landmarks(model="large")` → 양 눈 6점 → Soukupová & Čech EAR 평균. 미지원 환경엔 None 반환 (호출자 폴백).
- `sarvis/server.py`:
  - `auth_state` 확장: `ear_samples`, `blink_ok`, `blink_required`, `current_challenge`. `_is_authed` = `face_ok ∧ voice_ok ∧ (blink_ok ∨ ¬blink_required)`.
  - `_try_face_login`: 인코딩 매치 → EAR 누적 → 깜빡임 검출. 통과해야 face_ok=True. landmarks 미지원 시 자동 우회(degraded).
  - `_do_voice_login`: 현재 챌린지 전달, `matched_against` UI 노출, 통과 시 `_refresh_challenge`(1회용).
  - `enroll_owner` 핸들러: `frames_b64` (5장 base64 JPEG) + `angles` 수신 → 각각 인코딩 → 저장. 미전송 시 구버전(라이브 1장 캡처) 폴백. `kept_angles` / `failed_angles` 결과 리포트.
  - 신규 메시지: `auth_new_challenge` (챌린지 재발급), `auth_status` 확장 필드 (`blink_ok`, `blink_required`, `challenge`, `enroll_angles`, `enroll_angle_labels`, `face_encoding_count`, `schema_version`).
- `web/index.html` + `web/style.css` + `web/app.js`:
  - 등록: [5각도 얼굴 캡처 시작] → 각도별 안내 + 1.4s 후 자동 캡처 → 5칸 진행 그리드(current/done/failed) → [등록 완료] → `frames_b64` 일괄 전송. `captureCameraJpegBase64` (canvas, 480px 다운샘플, JPEG q=0.85).
  - 로그인: 챌린지 박스 (보라톤) + ↻ 새로고침 버튼. 안내 문구에 "한 번 눈을 깜빡여 주세요" 라이브니스 명시. `auth_progress` 의 `voice_matched_against=='challenge'` 시 "챌린지 ✓" 라벨, 얼굴 매치 후 라이브니스 대기 시 "얼굴 ✓ · 깜빡임 대기" 표시.

**보안 효과:**
- 단일 정면 사진(인쇄/스마트폰 화면) 위조 → 깜빡임 미발생으로 차단.
- 사전 녹음 패스프레이즈 재생 → 매번 바뀌는 챌린지 문장 미일치로 차단(50% 이상 비율 — 챌린지가 일치 안 하면 패스프레이즈와도 70%+ 어긋나야 함).
- 머리 각도 변화에 견고 (5각도 인코딩 중 최소 거리 사용).

**Architect 리뷰 후 P0 보안 패치 (cycle #20 내):**
- `enroll_owner`: 등록된 시스템에서는 `_is_authed()` 본인만 재등록 허용 (계정 탈취 차단).
- `auth_reset`: 등록된 시스템에서는 인증 본인만 reset 허용 — 비인증자가 `auth_reset → enroll_owner` 연쇄로 owner takeover 하던 우회 경로 차단.
- `_do_voice_login`: 챌린지 활성 시 challenge **strict** 매칭만 허용 (passphrase OR 우회 제거). 통과/실패 모두 챌린지 1회용 폐기.
- `blink_required`: 세션 시작 시 `is_face_landmarks_supported()` capability probe 1회로 결정 → 이후 변경 금지. EAR 일시 추출 실패는 `blink_ok=False` 안내만.
- `frames_b64` 입력 검증: 최대 10장, 단일 300KB, 누적 2.5MB.
- `completed_emitted` 가드: `auth_complete` 중복 emit 방지.

**미적용 (사이클 #21 후보):**
- 음성 화자 임베딩 (resemblyzer/d-vector) — `librosa`/`numba` ~150MB 의존성. 인터페이스 자리만 비워둠.
- 데드맨 타임아웃(자리 비움 자동 잠금) + 다중 사용자 다중 owner.
- E2E 회귀 테스트: 미인증 `auth_reset → enroll_owner` 연쇄가 거부되는지 자동화 검증.

---

## Cycle #18 — Owner Authentication (Face + Voice Passphrase)

서비스 시작 시 주인 인증을 강제하는 로그인 시스템.

**구성:**
- `sarvis/owner_auth.py` (신규) — `OwnerAuth`. `data/owner.json` 에 얼굴 인코딩(128차원, 옵션) + 음성 패스프레이즈(NFC + 구두점 제거 정규화) 저장. `verify_voice` = difflib SequenceMatcher ≥ 0.78. `verify_face_encoding` = face_recognition 호환 거리 ≤ 0.55.
- `sarvis/vision.py` — `compute_face_encoding_from_jpeg(jpeg)` 헬퍼 (face_recognition + cv2 lazy load, 0.5x 다운샘플, 가장 큰 얼굴 선택, 실패 시 None).
- `sarvis/server.py` — WS 핸들러 인증 게이트:
  - 미등록 → 게이트 OFF (회귀 0). 클라이언트는 등록 UI 표시.
  - 등록 → 매 연결마다 `auth_state{face_ok, voice_ok}` 모두 통과 전엔 화이트리스트 (`enroll_owner`/`auth_reset`/`auth_status_request`/`models_list`/`list_faces`) 외 모든 메시지 차단 (`auth_required` 안내).
  - `welcome_task` 는 `_start_welcome_if_authed()` 로 인증 완료 후 시작.
  - 0x01 프레임 (미인증) → `_try_face_login` (1초 throttle, 인코딩 없으면 박스 감지 폴백 + degraded 표시).
  - 0x02 음성 (미인증) → `_do_voice_login` (STT → `verify_voice` → `auth_progress` emit, len<2 silent skip 대신 안내).
  - JSON `enroll_owner`: 카메라 프레임에서 얼굴 캡처 → 인코딩 계산 → `OwnerAuth.enroll` + `FaceRegistry.register` (도구 호환). 등록자 자동 로그인.
  - JSON `auth_reset`: 등록 해제 + 세션 초기화.
- `web/index.html` + `web/style.css` + `web/app.js` — 풀스크린 인증 오버레이:
  - 미등록 → 이름 + 패스프레이즈 입력 폼 + 카메라 미리보기 → "등록하기".
  - 등록됨 → 얼굴 자동 매치 + "🎙 음성 패스프레이즈 말하기" 버튼 (8초 자동 정지) → 두 단계 ✓ 시 페이드 아웃.
  - "주인 재등록" 버튼.
  - WS 이벤트: `auth_status` / `auth_progress` / `auth_complete` / `auth_required` / `auth_reset_ok` / `enroll_owner_result`.

**보안 메모:**
- 음성 인증은 1차로 STT 패스프레이즈 매칭 — 같은 문구를 누가 말해도 통과 가능. 사이클 #19 에서 화자 임베딩(resemblyzer)으로 업그레이드 예정.
- `face_recognition` 미설치: 인코딩 저장 안 됨 → 폴백 (얼굴 박스 감지) + UI "(간이)" 표시.
- `data/owner.json` 평문 저장. 패스프레이즈는 fuzzy 매칭 위해 정규화 텍스트 보관 (해시 X).

**테스트:** `tests/test_owner_auth.py` — 12 케이스 (정규화, 유사도, 등록 영속성, 검증, 리셋, 손상 파일 처리). PYTHONPATH=. 직접 실행 (unittest discover hang 회피).


## Architecture

- **Backend**: FastAPI + WebSockets (`server.py`) on port 5000
- **Frontend**: HTML5 Canvas / Vanilla JS / CSS in the `web/` directory
- **AI Brain**: Claude (Anthropic) or Ollama as LLM backend (`brain.py`)
- **STT**: Faster-Whisper for speech-to-text
- **TTS**: Edge-TTS (Microsoft) for text-to-speech
- **Vision**: OpenCV + optional face_recognition for webcam analysis

## Key Files

- `server.py` — FastAPI web server (entry point for web mode)
- `brain.py` — LLM controller (Claude tool_use loop + Ollama simple chat)
- `tools.py` — Tool definitions and executor (web search, weather, timer, memory, vision)
- `audio_io.py` — Speech recording, Whisper STT, Edge-TTS
- `vision.py` — VisionSystem (desktop) + WebVision (web, browser-pushed frames)
- `config.py` — Centralized configuration via environment variables
- `auth.py` — User authentication with PBKDF2 hashing
- `emotion.py` — 7 assistant emotional states
- `web/` — Frontend assets (HTML, CSS, JS, orb animation)
- `data/` — **All runtime/user data** (`memory.db`, `users.json`, `memory.json`, `faces/`, `commands/` (멀티모달 명령 이미지), `harness_actions.jsonl`, `harness_telemetry.jsonl`, `tts_blocklist.json`). The repo's source-tree top level is therefore code-only. Path overridable via env (`SARVIS_MEMORY_DB`, `SARVIS_USERS_FILE`, `SARVIS_FACES_DIR`, `SARVIS_COMMANDS_DIR`, `SARVIS_TOOL_MEMORY`); each path's owner module auto-creates parent dirs on first write.

### 음성 인식·대화 자연스러움 강화 — 사이클 #17

- **`sarvis/stt_filter.py` 신규**: Whisper 한국어 환각 필터. 유튜브 자막 학습에서 새어나오는 무음 환각 ("시청해주셔서 감사합니다", "구독과 좋아요 눌러주세요", "다음 영상에서 만나요", "MBC 뉴스 …", 단독 "감사합니다", 자모만, "네 네 네 네" 같은 반복) 을 정규식으로 silent drop. `clean_stt_text(text) -> str` (빈 문자열 = 환각). 보수적: 환각 상투구가 *전체* 일 때만 차단.
- **동적 STT 프롬프트**: `build_dynamic_initial_prompt(base, keywords)` 가 facts.value + 최근 knowledge.topic 키워드를 base prompt 에 덧붙여 (12개·220자 컷오프) Whisper 의 사용자 고유명사 인식률을 끌어올린다. `WhisperSTT.transcribe(audio, extra_prompt="")` 시그니처 확장.
- **handle_audio**: STT 결과를 `clean_stt_text` 통과시킨 뒤 빈 문자열이면 응답 사이클 silent skip (텔레메트리에 `stt_hallucination_dropped` 기록 — 실제로 말한 게 아니므로 사용자에게 안내도 보내지 않음).
- **시스템 프롬프트 강화**: "자연스러운 대화" 섹션 추가 — 짧은 호응 변주("네/아하/그렇구나"), 사용자 발화 그대로 따라 읽지 말 것, 모호한 인식이면 추측 대신 되묻기, 비서로서 능동적 제안, [기억] 적극 활용.

### 멀티모달 학습 지식 (knowledge) — 사이클 #16

- `memory.db` 의 `knowledge` 테이블에 사비스가 학습한 내용을 영구 저장. 컬럼: `id, user_id, conv_id, topic, content, source(user|conversation|tool|web|inferred), confidence(0~1), image_path, audio_path, video_path, tags_json, created_at, updated_at`.
- 첨부 미디어는 `data/knowledge/<id>.{jpg|webm}` 파일로 저장 (BLOB 회피, `SARVIS_KNOWLEDGE_DIR` 로 경로 오버라이드 가능).
- `Memory.add_knowledge / update_knowledge / get_knowledge / recent_knowledge / search_knowledge / delete_knowledge` 6개 API. `delete_knowledge` 는 첨부 파일 모두 best-effort 정리.
- **활용 연결**: `Memory.context_block()` 가 매 답변마다 학습 지식 카드(query 가 있으면 search, 없으면 recent) 를 LLM 프롬프트의 `[기억]` 블록에 자동 주입. 첨부 미디어는 `[이미지/음성/영상 첨부]` 마커로 표시되어 LLM 이 인지함.
- `/ws` 신규 메시지: JSON `knowledge_add`, `knowledge_recent`, `knowledge_search`, `knowledge_get` (옵셔널 base64 이미지/음성/영상; 8MiB 컷오프), `knowledge_delete`. 바이너리 매직 `0x06/0x07/0x08` = `<caption_len:2 BE><caption_utf8><blob>` 포맷 (이미지/음성/영상).

### 멀티모달 명령 로그 (commands)

- `memory.db` 의 `commands` 테이블에 사용자가 사비스에게 시킨 일을 영구 저장. 컬럼: `id, user_id, conv_id, kind(text|voice|image|audio|video|multimodal), command_text, image_path, audio_path, video_path, response_text, status(pending|done|error), meta_json, created_at, completed_at`.
- 미디어(이미지/음성/영상) 이진 데이터는 `data/commands/<id>.{jpg|webm}` 파일로 저장하고 경로만 DB 에 기록 (BLOB 회피).
- `Memory.log_command / update_command / get_command / recent_commands / delete_command` 5개 API 로 접근. `delete_command` 는 image/audio/video 파일을 모두 best-effort 정리.
- 기존 DB(audio_path/video_path 미존재) 는 `_migrate_add_column_if_missing` 으로 자동 ALTER — 다운타임 없는 인플레이스 마이그레이션.
- `/ws` 신규 메시지: JSON `command_log` (텍스트만), `commands_recent` (목록 + has_image/has_audio/has_video), `command_get` (단건 + 옵셔널 base64 이미지/음성/영상; `include_image`/`include_audio`/`include_video` 플래그), `command_delete`. 바이너리 매직 `0x03/0x04/0x05` = `<caption_len:2 BE><caption_utf8><blob>` 포맷으로 캡션과 이미지/음성/영상을 함께 적재.

## Environment Variables

- `ANTHROPIC_API_KEY` — Required for Claude backend (set as a secret)
- `OPENAI_API_KEY` — Required for OpenAI backend and `compare` mode (set as a secret)
- `GOOGLE_API_KEY` — Required for Gemini backend (set as a secret). `GEMINI_API_KEY` is honoured as a legacy fallback. Default model `gemini-2.5-flash` (override with `GEMINI_MODEL`).
- `SARVIS_BACKEND` — `"openai"` (default), `"claude"`, `"ollama"`, `"zhipuai"`, `"gemini"`, or `"compare"`
- `OLLAMA_HOST` — Ollama server URL (default `http://localhost:11434`). Use a tunnel URL (e.g. cloudflared) when SARVIS runs on Replit but Ollama runs on a local machine.
- `OLLAMA_MODEL` — Ollama model tag (default `qwen2.5:7b`). Examples: `llama3.2:3b`, `qwen2.5:14b`, `gemma2:9b`.
- `PORCUPINE_ACCESS_KEY` — Optional, for desktop wake-word detection

## Running

The workflow runs `python -m sarvis.server` on port 5000. The app auto-loads the Whisper model on startup.

## CI — GitHub Actions

`.github/workflows/tests.yml` runs the full 137-test unittest suite on every push and PR to `main`.

- Runner: `ubuntu-latest`, Python 3.11, pip cache keyed on `requirements.txt` + `requirements-dev.txt`.
- System packages: `libsndfile1`, `portaudio19-dev`, `ffmpeg` (for sounddevice / faster-whisper imports).
- Dev dependencies (`requirements-dev.txt`) layer `coverage>=7.4.0` on top of `requirements.txt`.
- Command: `coverage run -m unittest discover -s tests -v` (env: `SARVIS_SKIP_CV2_PRELOAD=1` prevents `vision._bg_preload_cv2` from importing `cv2` during test collection), followed by `coverage report` + `coverage xml`.
- **Coverage threshold**: `coverage report --fail-under=60` enforces a baseline. Current branch coverage ≈ **73.4%** (3631 stmts / 1224 branches). 사이클 #7 에서 `auth` (0→100%), `tts_verifier` (0→91%), `analysis` (18→96%), `tools` (11→62%), `vision` (18→53%), `brain` (18→52%), `audio_io` (17→40%) 모듈에 단위 테스트를 추가. 사이클 #8 에서 `tests/test_server_endpoints.py` (24개) 추가 — `server.py` 14.7% → 56.5%, REST(/health, /api/harness/*) + WebSocket(/ws, /api/harness/ws) 핸들러를 fakeserver(`fastapi.testclient.TestClient` + Brain/TTS/FaceRegistry/parallel_analyze fake) 로 커버. **Task #14** 에서 `HandleAudioTests` 4개를 같은 파일에 추가 — `server.handle_audio` 본문(STT 정상 / 빈 transcription / TTS 차단 / Brain 예외) 을 회귀 검사해 `server.py` 56.5% → **61.8%** 로 상향. **Task #22** 에서 `VoiceInputAdjacentTests` 12개 추가 — WS 바이너리 디스패처(0x01/0x02/0x03..0x05/unknown/empty), busy lock & welcome 선점, 연결 끊김 시 임시 .webm 정리, 3-Pillar 텔레메트리 키, 자동 사실 학습/recall(memory_event), 메모리 예외 견고성을 회귀 검사해 `server.py` 61.8% → **67.1%**, 프로젝트 71.8% → **73.4%**.
- **Coverage upload**: `codecov/codecov-action@v4` posts `coverage.xml` to Codecov so each PR gets a delta comment. `secrets.CODECOV_TOKEN` is optional for public repos but recommended; `fail_ci_if_error: false` keeps upload flakes from breaking CI.
- **Coverage artifact**: `coverage.xml` + `.coverage` are uploaded as `coverage-report` artifact (14-day retention) for offline inspection.
- Two badges at the top of `README.md`: `tests` (workflow status) + `codecov` (coverage %). Branch protection on `main` should require the `unittest` job to pass before merge.
- Local config: `.coveragerc` (branch coverage on, omits `tests/`, `harness/`, `scripts/`, `web/`, `tools_local/`, `face_setup.py`, `main.py`, `ui.py`). Local artifacts (`.coverage`, `coverage.xml`, `htmlcov/`) are gitignored.

## Development Methodology — Harness

SARVIS uses **[Harness](harness/README.md)** (a Claude Code Team-Architecture Factory plugin, Apache-2.0) as its **meta development system**. Harness is *not* a runtime feature — it is the architectural rule book that decides how SARVIS evolves.

**Target composition** (per Harness Phase 2): `Supervisor[Pipeline(Fan-out → Expert-Pool → Generate-Verify)]` plus `Hierarchical Delegation` for development.

**Current implementation status** (cycle #5 complete, 2026-05-01):
- ✅ **Supervisor + Pipeline + Hierarchical** — `brain.py`, `.claude/agents/`.
- ✅ **Expert Pool** — `Brain.think_stream_with_fallback()` + `_ollama_healthcheck()`.
- ✅ **Fan-out / Fan-in** — `analysis.parallel_analyze()` (4-way 200ms timeout).
- ✅ **Generate-Verify** — `tts_verifier` + `synthesize_bytes_verified()` + `Brain.regenerate_safe_tts()`.
- ✅ **Telemetry & Feedback Loop** — `log_turn()` to JSONL (no PII). Real-time `WS /api/harness/ws` push + 5s polling fallback. **Cycle #5**: `summarize().latency` exposes `avg/p50/p95/p99/count` for `fanout_ms`/`llm_ms`/`tts_ms`/`total_ms` (nearest-rank percentile, pure Python). `respond_internal` / `respond_compare` now also record `total_ms`. Dashboard shows a new "응답시간 분포" table.
- ✅ **Self-Evolution Proposer + Export** — `propose_next_cycle()` writes `harness/sarvis/proposals/cycle-{n}.md`. **Cycle #5**: `export_proposal_to_github()` + `POST /api/harness/evolve/export` posts the proposal as a GitHub Issue. Path-traversal blocked (`PROPOSALS_DIR` allowlist), `repo` from arg or env (`HARNESS_GITHUB_REPO`/`GITHUB_REPO`), token from env (`GITHUB_TOKEN`/`GH_TOKEN`) **only** — never accepted in body. `issue_url` is verified against the `https://github.com/` scheme allowlist on both server and client. Dashboard adds a "GitHub Issue 로 내보내기" button (with dry-run option) inside the evolve result.
- ✅ **Regression Tests** *(new in cycle #5)* — `tests/test_telemetry.py` (12) + `tests/test_evolve_export.py` (12). Pure stdlib `unittest` (no extra deps). Run via `python -m unittest discover tests`. Covers: summarize keyset equivalence (empty vs non-empty), nearest-rank percentile correctness, PII sanitization (str/list/tuple/dict all → `*_len`), pub-sub callback isolation + idempotent subscribe, GitHub export (traversal block, missing repo/token, body truncation, dry-run, env priority).

Cycle #5 added/changed:
- `telemetry.py` — `_percentile`, `_latency_stats`, `LATENCY_KEYS`, `summarize().latency` (consistent empty/non-empty). Sanitize collections via `len()`. Fixed file-handle leak in `_rotate_if_needed`.
- `server.py` — `total_ms` recorded in `respond_internal` / `respond_compare`. New `POST /api/harness/evolve/export` endpoint.
- `harness_evolve.py` — `export_proposal_to_github()` with traversal-safe `_read_proposal()`, `_resolve_repo()`, `https://github.com/` `issue_url` allowlist, 60KB body cap, urllib + `asyncio.to_thread` (20s timeout).
- `web/harness/dashboard.html` — "응답시간 분포" table; dynamic GitHub export button, dry-run checkbox, repo input, scheme-checked link.
- `tests/__init__.py`, `tests/test_telemetry.py`, `tests/test_evolve_export.py` — 24 unit tests.

Remaining work (cycle #6 candidates) is tracked in `harness/sarvis/validation.md` §10.

Key locations:
- `harness/` — Original Harness plugin assets (READMEs EN/KO/JA, CHANGELOG, landing page source, banner images, plus `harness/sarvis/` SARVIS-specific Phase outputs). **Repo-internal — not publicly served.**
- `web/harness/` — Curated public landing assets only (`index.html`, `privacy.html`, 4 banner PNGs). This is what the `/harness/` route serves; markdown / LICENSE / .gitignore / sarvis/* are deliberately kept out of the public mount.
- `harness/sarvis/{analysis,architecture,validation}.md` — Phase 1/2/6 outputs of applying Harness to SARVIS itself.
- `.claude/skills/harness/SKILL.md` — Harness meta-skill with triggers (`하네스 구성해줘`, `build a harness`, `ハーネスを構成して`).
- `.claude/skills/harness/references/agent-design-patterns.md` — The six team patterns and a decision tree.
- `.claude/skills/tts-verifier/SKILL.md` — Phase 4 generated skill (Generate-Verify gate before TTS).
- `.claude/agents/_orchestrator.md` — Supervisor policy mirroring `brain.py`.
- `.claude/agents/{architect,voice-engineer,vision-engineer,backend-engineer,frontend-engineer,qa-engineer,security-reviewer}.md` — Development team roles with explicit input/output/forbidden rules.

Procedure for new SARVIS features:
1. `architect` agent picks one (or a composition) of the six patterns and updates `harness/sarvis/architecture.md`.
2. Delegate to leaf engineers per the table above.
3. `qa-engineer` 7-item checklist must pass.
4. `security-reviewer` 5-item checklist must pass.
5. Record the change + rationale in `replit.md` (this file).

## Features

- **Dual Mode**: Desktop (pygame) or Web (FastAPI + WebSocket)
- **Agentic Tools**: web_search, get_weather, get_time, remember/recall, set_timer, see (vision)
- **Voice I/O**: Browser microphone → Whisper STT → Claude → Edge-TTS → browser audio
- **Camera**: Browser webcam → JPEG frames → Claude Vision analysis
- **Emotion Orb**: Canvas animation reflecting assistant's emotional state — selectable visual styles (ORBITAL / PULSE / REACTOR / NEURAL), all preserving the 7 emotion palettes. Choice persists in `localStorage('orbStyle')` and applies to both orbs in compare mode.
- **Conversation-First UI** *(2026-05-01)*: The default desktop layout is a centered chat (`.chat-main`) with the dialogue log + integrated mic/text/SEND input bar. The orb panel (`.orb-pane`), vision panel (`.side-pane`: camera + face register), and mode panel (backend picker) toggle via three top-bar buttons (`오브 / 비전 / 모드`). Layout uses CSS grid with `grid-template-columns: 0px 1fr 0px` plus `.show-orb` / `.show-vision` modifier classes (smooth transition). Per-user preference persists in `localStorage('panelState')`. **First-visit default is `{orb:true}`** (cycle #5 follow-up) so the emotion-reactive SARVIS orb is visible immediately on desktop without requiring the user to discover the toggle. Existing users with a saved `panelState` are unaffected (backward compatible).
- **Mobile Voice-Only Fullscreen** *(2026-05-01)*: On `≤640px`, the layout collapses to a single fullscreen orb view — `chat-main`, `side-pane`, `mobile-tabs`, and `mode-panel` are all `display:none`, while `.orb-pane` is forced visible (`opacity:1 !important`, `pointer-events:auto !important`) with the orb sized to `min(78vw, 70dvh)` so the SARVIS face dominates. The bottom `.mobile-input-bar` hides the text input + send button entirely and shows only a single large 78px circular mic button (`.mobile-mic-btn`) with a glowing accent border that transitions to red while recording. All interaction is voice-in / voice-out; assistant replies still appear as a centered subtitle (`.orb-reply`) directly below the orb (max `30dvh` scrollable, streaming cursor preserved). Tablet (641–960px) keeps the standard desktop grid behavior.
- **Static Spoken Welcome** *(2026-05-01)*: The first-load greeting (`server.py welcome()`) uses a fixed Korean string — *"안녕하세요, 사비스입니다. 무엇을 도와드릴까요?"* — and synthesizes Edge-TTS directly, bypassing `think_stream_with_fallback`. This eliminates the regression where a transient LLM outage on page-load caused a red "internal server error" toast. The welcome `stream_end` carries `is_welcome=true` so the client can distinguish it from response audio. The browser's autoplay policy is handled by `_unlockAudioOnGesture()` in `web/app.js`: the welcome MP3 buffer is queued in a FIFO (`_pendingTtsQueue`, max 3) and played on the first `pointerdown`/`keydown`/`touchstart`. If that gesture targets an input control (mic / send / text input — including mobile equivalents), the queued welcome is discarded and `_suppressNextWelcomeAudio` blocks any in-flight welcome bytes so the user's own utterance isn't overlapped. On the server side, `_preempt_welcome()` is invoked before each `text_input` and `0x02` audio frame — it cancels the welcome task immediately so user input can never be dropped by the welcome's `busy` lock and never arrives out-of-order. The welcome task is also cancelled in the WS `finally` block (clean disconnect). On client reconnect (`connectWS`), all welcome flags + queue are cleared (TDZ-safe try/catch) so a stale `_suppressNextWelcomeAudio` from a previous session can't drop a new welcome.
- **Auth**: Local username/password with session tokens
- **Friendly Error Surface**: When an LLM backend fails (credit exhausted, auth failure, rate limit, network error), `_friendly_error()` in `brain.py` converts raw provider exceptions into Korean guidance messages that name the exact alternative-backend buttons to press. Raw stack traces, request IDs, and provider payloads are kept server-side only. `think_stream` rolls back the orphan user history entry on any failure so the next call doesn't hit consecutive-user errors. ZhipuAI (GLM-4) gets a dedicated branch that surfaces `身份验证失败` (401) with a link to `open.bigmodel.cn/usercenter/proj-mgmt/apikeys` and a credit-exhausted branch pointing to `open.bigmodel.cn` 财务中心 → 充值.
- **ZhipuAI (GLM-4) Backend** *(2026-05-01, cycle #6)*: Adds `zhipuai` as a fourth LLM backend alongside Claude/OpenAI/Ollama. Reuses the OpenAI SDK with `base_url=https://open.bigmodel.cn/api/paas/v4` (no extra package). `config.py` reads `ZHIPUAI_API_KEY` first then falls back to `OLLAMA_API_KEY` (legacy compatibility). Default model is `glm-4-flash`, overridable via `ZHIPUAI_MODEL`. `brain.py` factors OpenAI/ZhipuAI into shared `_think_openai_compatible()` / `_stream_openai_compatible()` helpers so both backends benefit from a single bug fix surface. `regenerate_safe_tts()` now falls back to ZhipuAI when neither Claude nor OpenAI keys are present. UI: mode panel adds `4·GLM` button; hotkeys are `1·Claude / 2·OpenAI / 3·Ollama / 4·GLM / 5·Compare`. Backend label in the header maps `zhipuai → GLM` for UI consistency. OpenAI + ZhipuAI clients both pin `timeout=20.0` to prevent worker pinning under provider stalls.
- **Cycle #6 Hardening** *(2026-05-01)*: Cross-domain leaf-agent review (architect / voice / vision / backend / frontend / qa / security) found and fixed 9 P0/P1 issues alongside the ZhipuAI rollout. Highlights: TTS verifier `MAX_LEN` raised 600 → 800 (GLM Korean replies trend longer); `vision._bg_preload_cv2` now respects `SARVIS_SKIP_CV2_PRELOAD=1` so test/CI imports don't pull `cv2`; tests/test_telemetry.py adds `ZhipuAIBackendTests` (28 tests total, all passing). Each fix is tagged with the originating leaf agent in the source comment.
- **Cycle #6 Hotfix — Friendly Error Everywhere** *(2026-05-01)*: Fixed two regressions surfacing as "internal server error" toasts in production. **(P0)** `handle_audio()` initialised `turn_meta["backend"]` from `session.brain.cfg.llm_backend`, but `Brain` instances expose no `cfg` attribute — the dict-literal evaluation raised `AttributeError` before the surrounding `try` opened, killing the WS handler. Replaced with the module-level `cfg.llm_backend`. **(P1)** Three other emit sites (`respond_internal` outer except, `respond_compare` outer except, `handle_audio` outer except, both `run_stream` thread excepts, and the `switch_backend` failure path) leaked raw English exceptions to the user via `str(e)` / `f"오류: {e}"`. They now route through `brain._friendly_error(e, backend)` so the user always sees Korean guidance with the correct alternative-backend buttons. Three regression tests guard the call sites: `BrainCfgRegressionTests` blocks `self.cfg` from re-entering `Brain.__init__` and any `session.brain.cfg` access from re-entering `server.py`, and `test_server_never_emits_raw_str_exception` AST-walks `server.py` to fail on any `emit(type="error", message=str(e))` or unsanitised f-string. 21/21 tests passing.
- **Continuous Conversation Mode** *(기획서 v1.5, 2026-05-01)*: Header toggle (`#continuous-toggle` in topbar `meta`, ghost style with `aria-pressed` cyan glow) lets the user enable a hands-free flow — after a voice turn, the assistant's TTS finishes → mic auto-restarts after 600ms → 30 seconds of silence auto-disables the mode and notifies. Implementation is fully client-side (`web/app.js`); the server is unaware. Persistence via `localStorage('sarvis-continuous')`. Safety rails: (a) `_lastTurnWasVoice` flag — text-input turns never trigger auto-restart so keyboard-first users are not hijacked; the welcome MP3's `onended` is also blocked (welcome is treated as a non-voice turn). (b) `compareMode` blocks auto-restart so dual-backend reviews stay manual. (c) Both `ttsAudio.onended` AND server `state="idle"` trigger `maybeAutoStartListening()` (dual fallback) — covers TTS autoplay-blocked / TTS-disabled cases. The auto-start path is idempotent (each call cancels the prior pending timer). The 30-second idle timer is cancelled the moment VAD detects the first speech frame (not the bare `stopRecording()` call), so it only fires on true silence. Connection failures are bounded by `CONTINUOUS_MAX_FAILS=2` to prevent runaway retries; `connectWS()` resets `_lastTurnWasVoice` so a reconnect mid-conversation never auto-hijacks the mic.
- **Barge-in** *(기획서 v1.5, 2026-05-01)*: User input during the assistant's speech immediately interrupts playback. `interruptTts()` in `web/app.js` (a) pauses `ttsAudio` and rewinds `currentTime=0`, (b) flushes `_pendingTtsQueue`, (c) stops the amplitude RAF loop, (d) sets `_suppressNextWelcomeAudio=true` so a queued welcome MP3 is discarded, (e) zeroes `_remainingTtsChunks`, (f) cancels pending continuous-mode auto-start, and (g) raises `_ignoreTtsBytesUntilNextTurn=true` so any tail TTS the server is still synthesising arrives and is dropped. Trigger sites: `toggleRecording()` (mic / SPACE), `textForm` submit, `mobileTextForm` submit. The latch is released the moment a new turn begins (`state="thinking"` or `"speaking"`), which also resets `_remainingTtsChunks=0` so the new turn's chunk count starts clean.
- **Long-term Memory v2.0 stage 1** *(기획서 v2.0, 2026-05-01)*: SQLite-based persistent memory across sessions. `memory.py` (new) defines a thread-safe `Memory` class — fresh connection per call, autocommit, `PRAGMA journal_mode=WAL`, `foreign_keys=ON` — over **6 tables** (`users`, `conversations`, `messages`, `facts`, `events`, `routines`) with cascading FKs. Singleton accessor `get_memory()` reads `cfg.memory_path` (default `./memory.db`). 13 public APIs: `start_conversation` / `get_or_start_conversation` (30-min idle window auto-restart) / `end_conversation` / `add_message` / `recent_messages` / `search_messages` (LIKE; v2.0 stage 2 will swap for embeddings) / `upsert_fact` / `get_fact` / `list_facts` / `delete_fact` / `add_event` / `list_events` / `context_block` (renders `[기억]` block with stored facts + recall snippets, ≤120-char truncation per line). `_format_role_label()` decodes `emotion="<emo>|<source>"` → `(assistant|claude)` so compare-mode source survives recall. `config.py` adds `memory_user_id` (env `SARVIS_MEMORY_USER`, default `"default"`) — single-user desktop model where `username` stays UI-only and the memory isolation key is separate. `UserSession` integrates `self.memory` + `self.memory_user_id` + a `_conv_id_lock` (intra-session serialization; `get_conv_id()` re-evaluates `get_or_start_conversation` every call so idle expiry / multi-session convergence both work). All four response paths record both sides: `respond_internal` (user prompt + assistant final), `handle_audio` (transcribed user + assistant final), `respond_compare` (user prompt + **both** backend finals tagged via `emotion="<emo>|<source>"`). `build_context(query)` injects the memory block into the prompt. `tests/test_memory.py` adds **25 tests** (CRUD, FK cascades, LIKE escaping, context block, source label preservation, multi-session convergence, idle expiry) — total **68/68 passing**. `.gitignore` excludes `memory.db*` + `chromadb/`. Stage 2 (semantic search via ChromaDB + `ko-sroberta-multitask`) is the next cycle.
- **Streaming TTS** *(기획서 v1.5, 2026-05-01)*: First-sentence split for perceived latency. `_split_first_sentence()` in `server.py` cuts the response into a head (≥15 chars, ≤160 chars, ending at `.!?。…` or `\n`) and a tail when the total is ≥60 chars; otherwise a single chunk is sent (no protocol change). When split, the server emits `{"type":"tts_chunk_count","count":2}` JSON before the binary frames, then runs `head_task` and `tail_task` as parallel `asyncio.create_task` jobs and awaits `head_task` first to push the head bytes the moment they're ready. A `try/finally` around both tasks `cancel + drain` any unfinished sibling if the other raises (no leaked threads). The client's `ttsAudio.onended` decrements `_remainingTtsChunks` and only triggers `maybeAutoStartListening()` on the last chunk; if a head is `tts_blocked` (no audio), `state="idle"` from the server force-resets the counter so continuous-mode auto-start still fires. Eight regression tests (`tests/test_streaming_tts.py`) lock down the splitter (short text, no terminator, question mark, newline, too-far candidate, too-short first sentence). 43/43 tests passing.
- **Cycle #9 — Repo cleanup + auto-migration** *(2026-05-01, follow-up)*: User reported "파일구조가 너무 복잡해". Root level reduced from 19 .py + miscellaneous files (junk `=4.8.0`, empty `tools_local/`, empty `.agents/`, unreferenced `attached_assets/`, obsolete `PUSH_GUIDE.md`, orphan `sessions.json`) to a code-only top level. **All runtime/user data unified under `data/`**: `memory.db` (`memory.py` `_ensure_schema` now `os.makedirs(parent, exist_ok=True)`), `users.json` (`auth.py.save()` + `config.users_file` default), `memory.json` (`tools.py` default), `faces/` (`vision.py FaceRegistry` + `FaceMemory` defaults + `parents=True`). Each new default is env-overridable (`SARVIS_MEMORY_DB`, `SARVIS_USERS_FILE`, `SARVIS_TOOL_MEMORY`, `SARVIS_FACES_DIR`). **Backward-compat auto-migration**: `config._migrate_legacy_root_data()` runs once at module import and moves `users.json`, `memory.db` (+`-wal`/`-shm`/`-journal`), `memory.json`, `sessions.json`, `faces/` from root → `data/` *only when the new path doesn't already exist* (idempotent). Failures are silent so a permission error never blocks boot. `.gitignore` updated with both new (`data/...`) and legacy root paths. `tests/test_migrate_legacy.py` adds 5 cases (move when new missing, idempotent when new exists, faces dir moved only with contents, clean no-op, SQLite sidecar files all move). **142/142 tests passing**.
- **Cycle #9 — 3-Pillar visibility + Harness self-improvement actions** *(2026-05-01)*: Two-track release responding to the user mandate that SARVIS hinges on three pillars (음성 / 카메라 공통 이미지 분석 / 즉각 실행) AND that the harness should be able to repair its own problems. **(T1) 3-Pillar telemetry**: `telemetry.PILLAR_KEYS` + `_voice_pillar` / `_vision_pillar` / `_action_pillar` compute 0–100 scores from the rolling JSONL — voice = `audio_ratio*60 + (1-empty_rate)*25 + (1-tts_fail_rate)*15`, vision = `vision_use_ratio*70 + 30 - latency_penalty` (penalty kicks in when vision-tool p50 > 4 s), action = `speed_score*0.7 + (1-error_rate)*20 + tool_use*10`. `summarize()` exposes a `pillars` key in **both** empty and non-empty paths (P1 keyset equivalence preserved); pillar `notes` are promoted into `insights` with a `[pillar]` prefix. `UserSession` now tracks `_turn_tool_count` / `_turn_tool_total_ms` / `_turn_vision_used` via `reset_turn_counters()` + `turn_pillar_meta()`, merged into `log_turn` from all three turn entry points (`respond_internal` / `respond_compare` / `handle_audio`). **(T2) `harness_actions.py`** — a safe-by-construction self-improvement layer. The `Action` dataclass exposes a 4-entry catalog (`silence_threshold` 0.005–0.030 / `silence_duration` 0.8–2.5 s / `max_recording` 5–30 s / `tts_rate` ±30 % with Edge-TTS `+5%` parser/formatter), all bounds-clamped on apply, with single-step revert (`_previous` pointer) and JSONL audit trail at `data/harness_actions.jsonl` (rotated at 2000 lines). `recommend_actions(summary)` derives conservative suggestions from telemetry — empty-rate > 20 % drops `silence_threshold` by 25 %, `tts_failure_rate > 10 %` bumps `tts_rate` by +5 %, `p50_total_ms > 5 s` shaves 2 s off `max_recording`. **(T3) REST API + dashboard panel**: 4 new endpoints behind the existing `_harness_auth_check` (`HARNESS_TELEMETRY_TOKEN` or loopback) — `GET /api/harness/actions` (catalog + recommendations + summary_total), `POST /api/harness/actions/apply` (`{name, value, source?}`), `POST /api/harness/actions/revert` (`{name, source?}`), `GET /api/harness/actions/audit?n=`. The dashboard renders the 3 score cards (음성/비전/즉각 실행) with metric breakdowns + pillar notes, a yellow "권장 액션" stack with one-click apply + reason, the manual-tuning catalog with apply/revert buttons + bounds + current value, and an audit `<details>` strip; `refreshActions()` + `refreshAudit()` bootstrap on page load and refresh every 30 s. **Architect P1 fixes (3)**: (P1#1) `_on_tool_event` whitelist now includes `identify_person` so face-recognition turns count toward Vision; (P1#2) `_vision_pillar` docstring rewritten to match the actual `latency_penalty` formula (no spec/code drift); (P1#3) `harness_actions._state_lock` (RLock) wraps `Action.apply` / `Action.revert` so concurrent admin requests can't corrupt the `_previous` pointer — concurrent-revert tests guarantee exactly one of N parallel reverts succeeds. **Tests**: `Cycle9PillarTests` (8) + `harness_actions` tests — Catalog (3) / BoundsClamp (3) / ApplyRevert (3) / TtsRateFormat (3) / AuditLog (3) / Recommend (4) / Concurrency (2) / RouteAst+VisionWhitelist (2) → **137/137 passing**. Live smoke (`/api/harness/actions` apply silence_duration 1.5 s → 1.7 s → revert) confirmed end-to-end.
- **Cycle #8 — Korean STT quality / Always-on emotion + memory badges / Auto-learned facts** *(2026-05-01)*: Three user-driven UX upgrades. **(T1) Korean STT quality**: default `whisper_model` raised `small` → `medium` (env `SARVIS_WHISPER_MODEL` override). `WhisperSTT.transcribe()` now passes Korean-tuned options — `initial_prompt` (env `SARVIS_WHISPER_PROMPT`, default mentions wake-word + common honorifics), `temperature=0.0`, `condition_on_previous_text=False`, `compression_ratio_threshold=2.4`, `no_speech_threshold=0.5`, `vad_parameters={"min_silence_duration_ms": 500}` — collectively reducing hallucinations and mis-transcriptions on Korean audio. CPU/`int8` keeps medium model ~770 MB (acceptable on Replit). **(T2) Always-visible header badges**: emotion was previously buried in the `orb-pane` (toggle, off by default on small screens). Header `meta` now hosts `#emotion-mini` (emoji + label, `data-emotion` drives color/glow per state) and `#memory-mini` (svg + `MEM ON`); both kept compact (label hidden < 720px). `setEmotion` mirrors to `updateEmotionMini`; `flashMemory(kind, label, ms)` flashes `recall` (green) on context-block hit and `learned` (amber) on auto-fact upsert. `addLog('system', …)` introduced for inline `MEMORY` log entries with italic amber styling. **(T3) Auto-learned facts (zero LLM cost)**: `memory.extract_user_facts(text)` returns `[(key, value), …]` from 11 conservative Korean self-introduction regex patterns (name / nickname / job / location / birthday / favorite / hobby / language) with `_strip_trailing_particles` cleanup of `이에요|예요|입니다|이야|야|이다|라고|…`, `_FACT_VALUE_BANLIST` blocking `사비스` / fillers, and length guards (4–400 char input, 1–60 char value). `server._learn_and_signal(prompt, msg_id)` runs after every user `add_message` in all three paths (single chat, compare mode, voice/handle_audio); it upserts via `memory.upsert_fact` and emits `{type: "memory_event", kind: "learned"|"recall"}` to flash the header badge + log a `MEMORY` line (`기억에 저장: name=민수, …`). `build_context()` sets `session._last_recall=True` whenever it injects a `[기억]` block — consumed by the next `_learn_and_signal` call. Voice path reorders so `build_context` runs before `_learn_and_signal` for accurate recall signaling. **Tests**: `AutoFactExtractionTests` (15 cases — name×3 endings, location, hobby, job, birthday, nickname, the `떡볶이야` greedy/lazy regression, banlist, length guards, `None`/`""`) + question false-positive sanity check (`내 이름은 뭐야?` / `내 이름은?` → `[]`). All **106/106 tests passing**. Architect P0 review: no critical issues (race on `_last_recall` is UI-only / aesthetic). Whisper medium boot adds ~10–15 s background load — non-blocking (existing thread).
- **Cycle #7 — Model picker / Insights / Semantic search foundation** *(2026-05-01)*: Three-pronged release based on user request. **(T1) Per-backend model picker**: `config.MODEL_CATALOG` enumerates per-backend candidate models (`claude` / `openai` / `ollama` / `zhipuai` / `gemini`); `config.current_model(backend)` returns the active selection; `Brain.switch_model(backend, model)` validates against the catalogue and refreshes the active backend init when applicable (compare mode raises — multi-backend by definition). WS protocol adds `models_list` (returns `{backend: {models, current}}`) and `switch_model {backend, model}` (emits `model_changed` on success). The mode panel renders a `<select.model-select>` that updates as the backend changes (`updateModelSelectFor()`); `change` sends `switch_model`. New models are added by editing `MODEL_CATALOG` only — single source of truth. **(T2) SARVIS self-improvement insights** in the harness dashboard: `telemetry._per_backend_stats()` returns `{count, avg_llm_ms, p50_llm_ms, tts_failure_rate, fallback_rate, tts_regen_rate}` per backend; `telemetry._build_insights()` derives actionable bullets — fastest / slowest backend (p50 LLM, ≥5-turn floor), high-fallback warning (>10%), TTS-failure warning (>5% warn / >20% err), top blocked TTS reason (excluding `ok`/`success` sentinels), low-volume notice (<20 turns). `summarize()` exposes `per_backend` + `insights` keys in **both** empty and non-empty paths (P1 keyset equivalence preserved). `dashboard.html` renders the insights panel + the per-backend comparison table above existing distributions. **(T3) ChromaDB semantic search foundation** *(opt-in)*: `memory.SemanticIndex` class wraps `chromadb` + `sentence-transformers (jhgan/ko-sroberta-multitask)` with cosine top-k. Activated only when both packages are installed AND `SARVIS_SEMANTIC=1`; otherwise a `_NullSemanticIndex` no-op is injected so all existing flows continue. `Memory(path=…)` with a non-default DB path also auto-injects the null index — **prevents test runs from contaminating the production `chromadb/` directory** (architect P0 fix). `Memory.add_message` now indexes user-role messages out-of-band; `Memory.search_messages` prefers semantic top-k and falls back to LIKE on empty / failure. Defaults remain LIKE-only because the encoder model is ~400 MB. **Architect P1 fix**: `switch_model` validation `ValueError` is no longer routed through `_friendly_error` (which would mis-label catalogue rejections as a generic "통신 오류"); `brain._model_switch_friendly()` formats the already-Korean message directly. AST regression test asserts `server.py` retains a `except ValueError` branch for `switch_model`. **Tests**: `Cycle7InsightsTests` (5) + `Cycle7ModelSwitchTests` (5) + `Cycle7SemanticIndexTests` (4) → **89/89 passing**.
- **Gemini Backend** *(2026-05-01)*: Adds `gemini` as a fifth LLM backend alongside Claude / OpenAI / Ollama / GLM. Reuses the OpenAI SDK against Google's OpenAI-compatible endpoint `https://generativelanguage.googleapis.com/v1beta/openai/` (no extra package). `config.py` reads `GOOGLE_API_KEY` first then falls back to `GEMINI_API_KEY`. Default model `gemini-2.5-flash`, override with `GEMINI_MODEL`. `brain.py` reuses the existing `_think_openai_compatible()` / `_stream_openai_compatible()` helpers — `_ensure_gemini` / `_think_gemini_simple` / `_stream_gemini` are thin adapters. All branch sites updated: `_init_backend`, `think`, `think_stream`, `available_backends`, `_client_for`, `_dispatch_stream`, `switch_backend` whitelist, `_ALT_BUTTONS["gemini"]`, `_friendly_error` (auth / quota Korean branches), `regenerate_safe_tts` (last-resort fallback after Anthropic → OpenAI → ZhipuAI). UI: mode panel adds `5·GEMINI`, COMPARE moves to `6·COMPARE`; hotkeys are `1·Claude / 2·OpenAI / 3·Ollama / 4·GLM / 5·Gemini / 6·Compare`. **Architect P1 follow-up**: `Brain.think()`'s outer `except` previously returned `f"AI 통신 오류가 발생했어요. {e}"`, leaking raw English provider errors into the audio path (the only call site that uses `think()` instead of `think_stream`). It now routes through `_friendly_error(e, cfg.llm_backend)`. `tests/test_telemetry.py` adds 5 Gemini cases (counter, keyset equality, `_ALT_BUTTONS` membership, friendly_error Korean wording, and a dynamic regression that mocks `_think_gemini_simple` to raise `"401 Unauthorized: invalid api key xyz123"` and asserts the user-facing reply contains zero raw English tokens). 73/73 tests passing.

## Cycle #19 — UI Redesign (Claude-inspired Warm Minimal)

다크 사이파이 → 따뜻한 미니멀 AI 비서 톤. 감정 인터페이스 표현력 강화.

**디자인 원칙:**
- 배경 `#FAF9F7` (warm cream) + 액센트 `#C96442` (Claude 테라코타).
- 사이파이 그리드/HUD 스캔/코너 데코 전부 제거 → 풍부한 여백.
- 본문 sans (Pretendard), 큰 라벨/제목 serif (Source Serif Pro fallback).
- 칩(emotion-mini, memory-mini, panel-toggle, ghost) 통일된 알약(pill) 모양 + 부드러운 보더.

**감정 인터페이스 핵심 개선 (`AI 감정 변화 인터페이스 퀄리티`):**
- `web/orb.js` `PALETTES` — 11개 감정 (neutral/listening/thinking/speaking/happy/surprised/sad/angry/concerned/alert/error) 각각 muted 의미 컬러 매핑. (예: thinking=violet/slow pulse, listening=warm amber, speaking=sage green, sad=dusty blue.)
- `web/app.js` — `EMOTION_LABELS_KO` 한국어 자연어 레이블 ("차분함", "기쁨", "생각 중", "듣는 중" 등). `EMOTION_GLYPHS` 도 이모지 → 정제된 유니코드 글리프 (◔/◉/◌/✦) 로 교체. `setEmotion`/`updateEmotionMini` 가 한국어 표시.
- `#emotion-label` (orb 아래 큰 글자) — 26px serif + 600ms ease 컬러 트랜지션으로 부드럽게 변화.
- `.emotion-mini[data-emotion=...]` — 7가지 톤별 칩 색상. 380ms 부드러운 보간.

**파일:**
- `web/style.css` — `:root` 새 토큰 시스템 (warm 팔레트 + serif/sans/mono 폰트 + shadow tier). 끝부분에 100+ 줄 폴리시 override (HUD 데코 제거, topbar/입력/오브 패널/인증 카드 라이트 톤). 백업: `web/style.css.bak`.
- `web/orb.js` — `PALETTES` Claude 톤 재설계. 백업: `web/orb.js.bak`.
- `web/app.js` — `EMOTION_GLYPHS`/`EMOTION_LABELS_KO` 추가, `setEmotion`/`updateEmotionMini` 한국어 출력.
- `web/index.html` — `theme-color #FAF9F7`, `apple-mobile-web-app-status-bar-style default` (라이트).
- 사이클 #18 인증 오버레이 (`.auth-card`, `.auth-step`, `.auth-btn`) 다크 → 라이트 톤 일괄 재작업 — `var(--bg-card)` + `var(--shadow-lg)` + `var(--accent)` primary.

**호환성:**
- `OwnerAuth` / `FaceRegistry` / WS 프로토콜 변경 없음. 기존 12/12 인증 테스트 영향 없음.
- 비교 모드 (`compare-mode`), `setOrbEmotion`/`setSubEmotion` 흐름 보존.
- `EMOTION_LABELS_KO` 에 누락 키는 fallback 으로 raw key 표시 (안전).

**검증:**
- 워크플로 RUNNING, GET / 200, WS 연결 정상.
- 시각 검증 — 스크린샷 도구 페이지 타임아웃 (서버 응답은 정상). 사용자 측 브라우저 새로고침으로 확인 권장.
