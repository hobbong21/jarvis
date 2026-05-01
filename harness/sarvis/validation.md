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
| TTS 품질 게이트 | 없음 | `tts-verifier` 스킬 정의 (구현 단계: 다음) |
| 새 사람 온보딩 | 코드 읽기 | `harness/sarvis/*.md` 3장 |
| 회귀 위험 | 매번 재발견 | qa-engineer 체크리스트 7항 |

## 6. 다음 단계 (open items)

1. `tts-verifier` 실제 구현 — backend-engineer + voice-engineer 협업.
2. `/harness:evolve` 진화 트리거 — 라우팅 텔레메트리 수집 후 차기 세대 반영.
3. `references/orchestrator-template.md`, `references/team-examples.md` 등 나머지
   참조 문서 — 필요 시 단계적으로 작성 (현재는 SKILL.md 본문에 핵심 요약 포함).
