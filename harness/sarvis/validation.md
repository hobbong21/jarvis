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

## 7. 다음 단계 (open items, 사이클 #3 후보)

1. **TTS 재생성 폴백** — 현재는 차단 시 텍스트만 표시. 향후 LLM 에 "더 짧게" 재요청해서 재시도 옵션 추가.
2. **Ollama 폴백 후보화** — 현재 `available_backends()` 는 cfg.llm_backend == "ollama" 일 때만 ollama 포함. Ollama 호스트 헬스체크 + 항상 후보화 옵션.
3. **Telemetry 대시보드 UI** — 현재는 JSON 만. `/harness/dashboard.html` 같은 SPA 페이지 추가.
4. **A/B 비교 모드 텔레메트리** — `respond_compare` 경로에도 동일 로깅 적용.
5. `/harness:evolve` 슬래시 커맨드 — 텔레메트리 N건 누적 시 자동으로 차기 세대 Harness 초안 제안.
6. `references/orchestrator-template.md`, `references/team-examples.md` 등 나머지 참조 문서.
