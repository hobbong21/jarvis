# Phase 6 — Validation: SARVIS Harness

> Phase 1~5 산출물이 실제로 잡히고 동작하는지 확인하는 결과 문서.

## 1. 트리거 검증

| 트리거 | 기대 동작 | 결과 |
|-------|---------|------|
| "하네스 구성해줘" | `.claude/skills/harness/SKILL.md` 매칭 | ✅ frontmatter `triggers` 포함 |
| "build a harness" | 동일 | ✅ |
| "ハーネスを構成して" | 동일 | ✅ |
| "tts 검증해줘" | `.claude/skills/tts-verifier/SKILL.md` 매칭 | ✅ |

## 2. 산출 파일 점검

| 경로 | 존재 |
|-----|------|
| `harness/README.md` (+ KO/JA) | ✅ |
| `harness/CHANGELOG.md` | ✅ |
| `harness/CONTRIBUTING.md` | ✅ |
| `harness/LICENSE` | ✅ |
| `harness/index.html` (+ privacy.html) | ✅ |
| `harness/harness_*.png` (4개) | ✅ |
| `web/static/harness/*` (서빙용 미러) | ✅ |
| `.claude/skills/harness/SKILL.md` | ✅ |
| `.claude/skills/harness/references/agent-design-patterns.md` | ✅ |
| `.claude/skills/tts-verifier/SKILL.md` | ✅ |
| `.claude/agents/_orchestrator.md` | ✅ |
| `.claude/agents/{architect,voice,vision,backend,frontend,qa,security}-*.md` | ✅ (7개) |
| `harness/sarvis/{analysis,architecture,validation}.md` | ✅ |

## 3. 라우팅 점검

| URL | 응답 |
|-----|------|
| `GET /` | SARVIS 메인 (기존) |
| `GET /api/health` | JSON 헬스 (기존) |
| `WS /ws` | 음성/텍스트 채널 (기존) |
| `GET /harness/` | Harness 랜딩페이지 |
| `GET /harness/privacy.html` | 개인정보처리방침 |
| `GET /harness/harness_banner.png` | 배너 이미지 |
| `GET /harness/README.md` | 영문 README (정적) |
| `GET /api/harness/telemetry` | Evolution 집계 JSON (✅ 신규) |

## 4. 회귀 점검 (기존 SARVIS 기능)

- 모듈 import 시간 < 1.5s — cv2 lazy 패턴 영향 없음 (`vision.py` 미수정).
- `/` 캐시 헤더 / mtime 쿼리 — 미수정.
- WS — 미수정.
- 백엔드 라우팅 — 미수정.

## 5. With-Harness vs Without-Harness 비교

| 측면 | Without | With |
|------|---------|------|
| 신규 기능 추가 절차 | 즉흥 결정 | architect → leaf 위임 트리 강제 |
| 패턴 선택 근거 | 암묵 | `architecture.md` 6패턴 표 |
| TTS 품질 게이트 | 없음 | `tts_verifier.py` 구현 완료 — 모든 TTS 호출이 검증 통과해야 합성 |
| LLM 백엔드 장애 대응 | 사용자 수동 전환 | 자동 폴백 체인 (`think_stream_with_fallback`) + 투명 알림 |
| 사전 분석 | 순차 호출 | `parallel_analyze` (intent/emotion/face/memory) — asyncio.gather, 200ms 타임아웃 |
| 진화 데이터 | 없음 | `data/harness_telemetry.jsonl` + `/api/harness/telemetry` 집계 |
| 새 사람 온보딩 | 코드 읽기 | `harness/sarvis/*.md` 3장 |
| 회귀 위험 | 매번 재발견 | qa-engineer 체크리스트 7항 |

## 6. Phase 4 — 사이클 #2 결과 (Generate-Verify, Expert Pool, Fan-out, Telemetry)

| 항목 | 결과 | 비고 |
|------|------|------|
| `tts_verifier.verify_tts_candidate()` 단위 테스트 | ✅ PASS | empty / blocklist / 길이 자동 truncate / 한국어 비율 |
| `Brain.available_backends()` / `_fallback_chain()` | ✅ PASS | claude+openai 환경에서 ['openai','claude'] 체인 확인 |
| `parallel_analyze("지금 몇 시야?")` | ✅ intent=question | 4 분석 ~1ms 완료 |
| `parallel_analyze("타이머 5분 켜줘")` | ✅ intent=command | |
| `parallel_analyze("너무 슬퍼 ㅠㅠ")` | ✅ intent=emotion, hint=sad | |
| `telemetry.log_turn()` PII 차단 | ✅ | `text` 키는 `text_len` 으로만 저장 |
| `GET /api/harness/telemetry` | ✅ 200 | 집계 JSON 반환 (총수/백엔드/폴백률/평균지연) |
| `/harness/index.html` 정적 라우팅 | ✅ 200 | 마운트 충돌 없음 |
| `python -c "import server"` smoke | ✅ | 모든 import 정상 |

## 6.1 Architect 코드 리뷰 결과 (사이클 #2 종료시)

| 항목 | 등급 | 처리 |
|------|------|------|
| `think_stream_with_fallback` 가 전역 `cfg.llm_backend` 변경 → 동시성 위험 | P1 | ✅ 수정 — 인스턴스 단위 `self.client` 임시 바인딩으로 변경, cfg 미변경. `_client_for(backend)`/`_dispatch_stream(backend)` 분리 |
| `⚠` 시작 휴리스틱으로 fallback 트리거 → 정상 응답 오탐 위험 | P1 | ✅ 수정 — 휴리스틱 제거. _stream_* 가 raise 한 경우에만 다음 후보로 진행 (구조화된 실패 신호) |
| `/api/harness/telemetry` 무인증 노출 | P2 | ✅ 수정 — `HARNESS_TELEMETRY_TOKEN` 환경변수 설정 시 토큰 검증 (Bearer/query), 미설정 시 loopback 만 허용 |
| TTS sanitize 1회 재시도 부재 | P2 | ⏳ 사이클 #3 후보 — 현재는 보수적 차단 우선 |
| 히스토리 롤백 로직 검증 | OK | user 턴은 1회만 추가, 후보 실패 시 assistant 부분 추가분만 pop |
| finally 텔레메트리 보장 | OK | `turn_meta` try 이전 초기화 + finally `log_turn()` |

## 7. Phase 5 — 사이클 #3 결과 (TTS regen / Ollama 헬스 / Dashboard / Compare 텔레 / Evolve)

| 항목 | 결과 | 비고 |
|------|------|------|
| `audio_io.synthesize_bytes_verified(text, regen_callback)` | ✅ | 차단 시 1회 콜백 호출 → 재검증 → 통과 시 합성. `regenerated` 플래그 반환. |
| `Brain.regenerate_safe_tts(original, reason)` | ✅ | Anthropic 우선, OpenAI 폴백. 짧고 안전한 재작성 LLM 호출. |
| `_ollama_healthcheck()` 60s 캐시 + 1회 재시도 | ✅ | timeout 1.2s × 2 = 최대 2.4초 / 60s. ollama 미설치 환경에서는 ok=False (정상). |
| `available_backends()` ollama 후보 포함 | ✅ | 헬스체크 통과 시 `cfg.llm_backend != "ollama"` 여도 폴백 후보. |
| `web/harness/dashboard.html` SPA | ✅ | 토큰 Bearer-only, 5초 자동 새로고침, Evolve 버튼. 외부 라이브러리 0. |
| `respond_compare` 텔레메트리 | ✅ | `backend="compare"`, `compare_sources` 키, `reply_len` 합산, fan-out 분석 포함. |
| `POST /api/harness/evolve` | ✅ | total < MIN_TURNS 시 거부. `min_turns` 외부 입력 **상향만** 허용 (clamp). |
| `harness_evolve.propose_next_cycle()` | ✅ 실측 | OpenAI 호출 → cycle-N.md markdown 생성 (생성 백엔드/시각/total 헤더 자동 추가). |
| `python -c "import server"` smoke | ✅ | 모든 import 정상. |

## 7.1 Architect 코드 리뷰 결과 (사이클 #3 종료시)

| 항목 | 등급 | 처리 |
|------|------|------|
| `dashboard.html` 가 토큰을 query param + Bearer 양쪽에 전송 → URL/액세스 로그에 평문 노출 | P1 | ✅ 수정 — Bearer 헤더만 사용, query param 송신 제거. |
| `/api/harness/evolve` 의 `min_turns` 외부 입력 하향 허용 → 트리거 조건 우회 | P2 | ✅ 수정 — `effective_min = max(MIN_TURNS, 외부값)` clamp. 하향 불가 검증 (`?min_turns=0` → 응답에 `min_turns=10` 반영). |
| `_ollama_healthcheck` timeout 0.3s → false-negative | P2 | ✅ 수정 — 1.2s + 1회 재시도 (총 최대 2.4s, 60s 1회만). |
| TTS regen 콜백 별도 스레드 호출 안전성 | OK | `regenerate_safe_tts` 는 history 미사용 / 도구 미사용 / 1회 호출 → 재진입 안전. |
| Compare 모드 fan-out 부작용 | OK | parallel_analyze 200ms 타임아웃 + 실패 시 빈 dict → compare 응답 흐름에 영향 없음. |
| dashboard innerHTML XSS | OK | markdown 출력은 `<pre>` 내 `&lt;/&gt;/&amp;` escape 적용. 통계 데이터는 모두 백엔드 통제. |

## 8. 다음 단계 (open items, 사이클 #4 후보)

1. **Ollama 자동 모델 풀** — 헬스체크 통과 시 미설치 모델 자동 pull (백그라운드).
2. **Telemetry export to GitHub Issue** — 사이클 #N+1 제안서를 자동으로 PR/Issue 로 생성.
3. **WebSocket 텔레메트리 스트림** — 대시보드가 polling 대신 WS 로 실시간 갱신.
4. **regen 폴백 통계** — `tts_regenerated` 플래그를 summarize 에 추가 (재생성률 메트릭).
5. **proposals 자동 적용** — 승인된 cycle-N.md 의 acceptance 항목을 task 트리로 변환.
6. `references/orchestrator-template.md`, `references/team-examples.md` 등 나머지 참조 문서.
