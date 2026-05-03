# SARVIS — Harness System 백로그 매트릭스 (기획서 v1.0 Chapter 17)

> 작성일: 2026-05-03
> 출처: `attached_assets/Sarvis_기획서_및_개발요구사항_1777803382509.docx` Ch.17
> 분량: P0 17개 + P1 6개 + P2 2개 = **25개 항목 (≈ 174 인일, MVP P0 ≈ 130 인일)**

## 1. 4축 운영 모델

| 축 | 질문 | 핵심 도구 |
|---|---|---|
| **Observe** | Sarvis는 지금 무엇을 어떻게 하고 있는가? | Behavior Recorder, State Dashboard, 실시간 트레이스 |
| **Evaluate** | 그 동작은 좋았는가? | 자동 메트릭, 사용자 피드백, A/B, 회귀 셋 |
| **Tune** | 다르게 하려면? | 성향 슬라이더, 규칙 편집, 가드레일, 페르소나 |
| **Evolve** | 다음 버전은? | Suggestion Inbox, 우선순위 큐, Version Vault, Canary |

## 2. 25개 백로그 + SARVIS 현재 구현 매핑

| ID | 작업 | 우선 | 공수 | SARVIS 현재 상태 | 다음 사이클 후보 |
|---|---|---|---|---|---|
| HARN-01 | Behavior Trace 스키마 + 수집 SDK | P0 | 8 | △ `memory.commands` 가 명령 로그 부분 보유 (text/image/result) — Trace 풀 스키마 확장 필요 (model_version, plan, tool_calls, latency, cost, confidence, safety_flags) | ⭐ 1순위 |
| HARN-02 | Trace 저장소 (ClickHouse/OpenSearch) | P0 | 8 | ❌ 현재 SQLite. Trace 스케일 작으면 SQLite 유지 가능 | 보류 |
| HARN-03 | PII 자동 마스킹 파이프라인 | P0 | 5 | ❌ | 우선 |
| HARN-04 | 운영자용 State Dashboard | P0 | 10 | ❌ | 중간 (단일 주인 시스템엔 우선순위 ↓) |
| HARN-05 | 사용자용 'My Sarvis' 대시보드 | P0 | 8 | ❌ | ⭐ 단일 주인 SARVIS 에 더 적합 |
| HARN-06 | 성향 슬라이더 UI + 시스템 프롬프트 변환 엔진 | P0 | 8 | ❌ (`brain.py` 시스템 프롬프트 정적) | ⭐ |
| HARN-07 | 페르소나 프리셋 라이브러리 | P0 | 3 | ❌ (F-13 도 동일) | ⭐ 작은 공수 |
| HARN-08 | 자연어→DSL 규칙 변환 + 검증 시뮬레이터 | P0 | 10 | ❌ | 중간 |
| HARN-09 | 가드레일 매트릭스 + 후처리 검증기 | P0 | 8 | △ 부분 (#21 인증 게이트 + 프롬프트 인젝션 방어) | 우선 |
| HARN-10 | 회귀 평가 셋 빌더 + 자동 러너 | P0 | 10 | △ pytest 610건 (단위/통합)이 일부 회귀 셋 역할 | 보강 |
| HARN-11 | LLM-as-Judge 채점 파이프라인 | P0 | 6 | ❌ | 중간 |
| HARN-12 | 사용자 피드백 UI (👍👎/별점/코멘트) | P0 | 4 | ❌ | ⭐ 작은 공수, 즉시 가치 |
| HARN-13 | 암묵 신호 탐지 (정정 발화/반복) | P1 | 8 | ❌ | 후순위 |
| HARN-14 | Suggestion Inbox 칸반 보드 | P0 | 8 | ❌ | 중간 |
| HARN-15 | 건의 자동 분류·우선순위 엔진 | P1 | 6 | ❌ | 후순위 |
| HARN-16 | Bundle/Version 관리 (Git 백엔드) | P0 | 8 | △ Git 자체는 사용 중, 번들 추상화 ❌ | 중간 |
| HARN-17 | Canary/A-B 실험 프레임워크 | P0 | 10 | ❌ | 후순위 (단일 주인엔 의미 ↓) |
| HARN-18 | 자동 롤백 트리거 + 안정 번들 복귀 | P0 | 5 | △ Replit checkpoint 시스템 일부 충당 | 보류 |
| HARN-19 | Audit Trail 불변 로그 저장소 | P0 | 5 | △ 서버 stdout 로그만 | 우선 |
| HARN-20 | RBAC + 4-eye 승인 워크플로우 | P0 | 8 | △ 단일 주인 모델 — 단순화 가능 (주인/게스트) | 단순화 |
| HARN-21 | 이상 패턴 자동 탐지 (드리프트/비용/환각) | P1 | 8 | ❌ | 후순위 |
| HARN-22 | 월간 변화 리포트 자동 생성 | P1 | 5 | ❌ | 후순위 |
| HARN-23 | 데이터 옵트인 + 익명화 학습 데이터셋 | P1 | 8 | ❌ | 후순위 |
| HARN-24 | 외부 감사 포맷 내보내기 (SOC2/ISO) | P2 | 5 | ❌ | 미고려 |
| HARN-25 | 사용자 자기 정의 규칙 라이브러리 (커뮤니티) | P2 | 8 | ❌ | 미고려 |

**범례**: ⭐ 다음 사이클 강력 후보 / △ 부분 충족 / ❌ 미구현 / 보류 = 단일 주인 시스템 특성상 후순위.

## 3. SARVIS (단일 주인 시스템) 맞춤 우선순위

원 기획서는 B2B/플랫폼 모드까지 가정하지만, 현재 SARVIS 는 **단일 주인 개인 비서**. 우선순위 재정렬:

### 사이클 #22 후보 — "Observe" v1 (≈ 1 사이클 분량)
- **HARN-01 (축소판)**: `memory.commands` 스키마에 `model_version`, `latency_ms`,
  `token_usage`, `confidence`, `safety_flags` 컬럼 추가 + 모든 brain 호출에서 자동 기록.
- **HARN-12**: 응답 메시지 옆 👍/👎 버튼 + `feedback` 테이블 (rating/comment).
- **HARN-05 미니**: 우상단 "내 Sarvis" 패널 — 이번 주 명령 수, 만족도 비율,
  자주 쓴 기능 Top 5, 저장 용량.

### 사이클 #23 후보 — "Tune" v1
- **HARN-06**: 성향 8축 슬라이더 (격식/간결성/적극성/감정/창의/유머/전문성/호출 빈도) +
  시스템 프롬프트 동적 합성. `data/personality.json` 영속.
- **HARN-07**: 페르소나 6개 프리셋 (Default/Executive/Buddy/Analyst/Caregiver/Silent Sentry)
  원클릭 적용. 슬라이더 값을 통째로 갈아끼우는 방식.
- **F-13** 자동 충족.

### 사이클 #24 후보 — "Evaluate + Evolve" v1
- **HARN-10 보강**: pytest 회귀 셋에 LLM-as-Judge 시나리오 30건 추가.
- **HARN-11**: `tests/eval/` 신설 + LLM-as-Judge 채점 함수 (별도 LLM 호출로
  Helpfulness/Faithfulness/Safety/Personality Adherence 평가).
- **HARN-14 미니**: 사용자 👎 + AI 자기 분석을 `data/suggestions.json` 으로 누적,
  "내 Sarvis" 패널에 카드 형태로 표시 + 수동 적용.

### 보류 (단일 주인 시스템엔 과잉)
- HARN-02 (ClickHouse), HARN-04 (Grafana 대시보드), HARN-17 (A/B), HARN-18 (Canary),
  HARN-23 (옵트인 학습), HARN-24 (SOC2), HARN-25 (커뮤니티 규칙).

## 4. 데이터 모델 추가 (기획서 17.15)

새 테이블 8종. SARVIS 단일 주인 모델에선 다음만 우선:

| 테이블 | 용도 | SARVIS 적용 |
|---|---|---|
| `traces` | 행동 기록 | `memory.commands` 확장으로 흡수 |
| `evaluations` | 자동/사람 평가 | 신규 (사이클 #24) |
| `behavior_profiles` | 사용자별 성향 | `data/personality.json` (사이클 #23) |
| `rules` | 행동 규칙 | 보류 (HARN-08) |
| `suggestions` | 개선 건의 | `data/suggestions.json` (사이클 #24) |
| `bundles` | 버전 묶음 | Git 자체로 충당 |
| `experiments` | 실험 | 단일 주인엔 N/A |
| `audit_log` | 감사 로그 | 단순화 — `data/audit.log` append-only |

## 5. 단계별 도입 로드맵 (기획서 17.17)

| 단계 | 포함 | 시점 (기획서) | SARVIS 매핑 |
|---|---|---|---|
| v1 — Observe | Trace + Dashboard + Feedback | MVP 동시 | 사이클 #22 |
| v2 — Tune | 성향 슬라이더 + 페르소나 + 규칙 | +2개월 | 사이클 #23 |
| v3 — Evaluate | LLM-as-Judge + 회귀셋 + A/B | +4개월 | 사이클 #24 |
| v4 — Evolve | Suggestion Inbox + Canary + 롤백 | +6개월 | 사이클 #25+ |
| v5 — Self-Improve | 암묵 신호 + 자기 진화 + 옵트인 학습 | +9~12개월 | 후순위 |
