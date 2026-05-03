# SARVIS — Harness Agent (HA) 백로그 매트릭스 (보조 기획서 v1.0)

> 작성일: 2026-05-03
> 출처: `attached_assets/Sarvis_Harness_Agent_기획서_1777804423173.docx` (전 16장 + 부록 A~D)
> 분량: 10개 모듈, 53개 백로그 항목, P0 ≈ 180 인일 / 총 ≈ 230 인일
> 상위 매트릭스: `docs/SARVIS_HARNESS_MATRIX.md` (Ch.17 도구 측면)
>
> **위치**: 본 문서는 Ch.17 "하네스 시스템(도구)" 위에 "에이전트형 자율 운용
> 계층" 을 얹는 보조 문서. 사람이 쓰는 도구 = Harness System / 그 도구를 사람
> 대신 운용하는 일꾼 = Harness Agent (HA).

## 1. 6대 에이전트 + Orchestrator + Meta-Evaluator

| 에이전트 | 역할 | 출력 | SARVIS 적용 우선순위 |
|---|---|---|---|
| **Observer** | 트레이스 수집·요약·이상 패턴 표시 | Issue Card | ⭐ Stage S1 (즉시) |
| **Diagnostician** | 약점 근본 원인 추론 (5 Whys + Bayesian) | Diagnosis Report | ⭐ Stage S2 (사이클 #24) |
| **Strategist** | 변경 후보 다수 생성 (8 카테고리, Do Nothing 강제) | Strategy List | ⭐ Stage S3 (사이클 #25) |
| **Improver** | 전략을 실제 패치로 구체화 (PR 형식) | Patch + Before/After | ⭐ Stage S3 (사이클 #25) |
| **Validator** | 회귀 셋·섀도·Canary 검증, 다중 평가자 | Validation Report | ⭐ Stage S3 (사이클 #25, 위험 등급만) |
| **Reporter** | One-Pager 보고서 + 사용자 성장 일기 | Report + Decision Widget | ⭐ Stage S1 (미니) |
| **Orchestrator** | 누구를 언제 부를지만 담당 (의사결정 X) | 워크플로 실행 | Stage S2 (in-process) |
| **Meta-Evaluator** | HA 결정을 외부 LLM 으로 무작위 평가 | 주간 리포트 | Stage S4+ |

## 2. 10개 모듈 53개 백로그 + SARVIS 매핑

### 모듈 1 — 에이전트 프레임워크 (HAA-01 ~ 06, 34 인일)
| ID | 작업 | 우선 | 공수 | SARVIS 매핑 | 사이클 |
|---|---|---|---|---|---|
| HAA-01 | 에이전트 베이스 + 메시지 스키마 | P0 | 5 | ⭐ `sarvis/ha/base.py` 신설 | **#23** |
| HAA-02 | LangGraph/Temporal 오케스트레이터 | P0 | 10 | 단순화: in-process orchestrator | #24 |
| HAA-03 | 메시지 버스 + append-only 저장소 | P0 | 5 | SQLite `ha_messages` (UPDATE/DELETE 금지 트리거) | **#23** |
| HAA-04 | 다중 LLM Provider 라우팅 | P0 | 5 | 기존 `brain.think_stream_with_fallback` 재사용 | 이미 충족 |
| HAA-05 | 권한 분리 IAM | P0 | 4 | 단일 주인 — read/write scope 함수로 단순화 | **#23** |
| HAA-06 | 타임아웃·재시도·에스컬레이션 | P0 | 5 | asyncio + 5분 cap | #24 |

### 모듈 2 — Observer (HAO-01 ~ 06, 35 인일)
| ID | 작업 | 우선 | 공수 | SARVIS 매핑 | 사이클 |
|---|---|---|---|---|---|
| HAO-01 | 트레이스 스트리밍 컨슈머 | P0 | 5 | `memory.commands` 가 트레이스 소스 — 직접 SELECT | **#23** |
| HAO-02 | 이상치 통계 (STL + Isolation Forest) | P0 | 8 | 휴리스틱: 7d vs 28d 비교 (numpy 없이) | **#23** 미니 |
| HAO-03 | 임베딩 + HDBSCAN 클러스터링 | P0 | 8 | 보류 (sentence-transformer 무거움) | #25 |
| HAO-04 | BERTopic 피드백 토픽 모델 | P1 | 5 | 보류 | 후순위 |
| HAO-05 | 이슈 카드 LLM 자동 생성 | P0 | 5 | ⭐ Claude/OpenAI 호출 + JSON 파싱 (fallback 휴리스틱) | **#23** |
| HAO-06 | 관심 패턴 DSL | P1 | 4 | 보류 | 후순위 |

### 모듈 3 — Diagnostician (HAD-01 ~ 05, 24 인일) — Stage S2
모두 #24 후보. P0 핵심: HAD-01 (taxonomy 18종), HAD-02 (5 Whys 베이지안 체인), HAD-04 (알 수 없음 보장).

### 모듈 4 — Strategist (HAS-01 ~ 05, 21 인일) — Stage S3
P0 핵심: HAS-01 (8 카테고리 카탈로그), HAS-04 ("Do Nothing" 강제 포함기), HAS-05 (파레토 최적).

### 모듈 5 — Improver (HAI-01 ~ 06, 32 인일) — Stage S3
P0 핵심: HAI-01 (semantic-aware diff), HAI-04 (PR 자동 생성), HAI-05 (셀프 체크리스트).

### 모듈 6 — Validator (HAV-01 ~ 06, 44 인일) — Stage S3
P0 핵심: HAV-01 (회귀 셋 러너 — 기존 pytest 635건 활용), HAV-02 (적대적 테스트), HAV-06 (자동 롤백 트리거).

### 모듈 7 — Reporter (HAR-01 ~ 05, 28 인일)
| ID | 작업 | 우선 | 공수 | SARVIS 매핑 | 사이클 |
|---|---|---|---|---|---|
| HAR-01 | One-Pager 템플릿 자동 채움 | P0 | 5 | ⭐ 마크다운 `data/ha/reports/<id>.md` | **#23** 미니 |
| HAR-02 | 주간/월간 종합 리포트 | P0 | 5 | #25 |
| HAR-03 | Slack/이메일/PagerDuty | P0 | 5 | 단일 주인 — stdout + UI 카드로 단순화 | **#23** |
| HAR-04 | 운영자 콘솔 결정 위젯 (승인/반려/유보) | P0 | 8 | UI 카드 (Stage S2 부터 의미) | #24 |
| HAR-05 | 사용자 '성장 일기' 카드 | P1 | 5 | ⭐ "내 Sarvis" 카드와 통합 | **#23** |

### 모듈 8 — 안전·정렬·통제 (HAS-S01 ~ S06, 36 인일)
| ID | 작업 | 우선 | 공수 | SARVIS 매핑 | 사이클 |
|---|---|---|---|---|---|
| HAS-S01 | 절대 규칙 인프라 (코드 수정 차단) | P0 | 8 | ⭐ `sarvis/ha/safety.py` — Observer 는 write 없음 (코드 분리) | **#23** |
| HAS-S02 | Kill Switch 메커니즘 | P0 | 5 | ⭐ `data/ha/kill_switch.json` + WS `ha_kill_switch` | **#23** |
| HAS-S03 | 주간 정렬 셋 + 자동 실행 | P0 | 5 | #25 |
| HAS-S04 | Meta-Evaluator (다른 Provider) | P0 | 8 | 기존 brain compare 모드(Claude+OpenAI) 활용 | #25 |
| HAS-S05 | 외부 감사용 데이터 내보내기 | P1 | 5 | 보류 |
| HAS-S06 | 결정 일관성 측정 + 드리프트 알람 | P1 | 5 | #26 |

### 모듈 9 — 자율성 등급 (HAL-01 ~ 05, 26 인일)
| ID | 작업 | 우선 | 공수 | SARVIS 매핑 | 사이클 |
|---|---|---|---|---|---|
| HAL-01 | 5단계 등급 + 정책 엔진 | P0 | 8 | ⭐ Stage S1 은 L0 만 (Observe-only) — enum + dispatch | **#23** 베이스 |
| HAL-02 | 변경 유형별 기본 등급 | P0 | 3 | #24 (Strategy 도입 후 의미) |
| HAL-03 | 등급 자동 산정 알고리즘 | P0 | 5 | #24 |
| HAL-04 | 등급 상향/하향 정책 | P0 | 5 | #25 |
| HAL-05 | 운영자용 정책 편집 UI | P0 | 5 | #25 |

### 모듈 10 — 카나리·사용자 투명성 (HAU-01 ~ 04, 20 인일)
| ID | 작업 | 우선 | 공수 | SARVIS 매핑 | 사이클 |
|---|---|---|---|---|---|
| HAU-01 | 카나리 사용자 등록 + 인센티브 | P1 | 5 | 단일 주인 — N/A | 미고려 |
| HAU-02 | 옵트아웃 토글 + 데이터 격리 | P0 | 5 | ⭐ `ha_optout` 테이블 + WS 토글 | **#23** |
| HAU-03 | '내 Sarvis 성장 일기' 화면 | P1 | 5 | ⭐ HAR-05 와 통합 | **#23** |
| HAU-04 | 도메인별(미성년/의료/법률) 자율 비활성 | P0 | 5 | 보류 (단일 주인) |

## 3. 5단계 도입 로드맵 (기획서 14장)

| 단계 | 자율 등급 | 포함 에이전트 | SARVIS 사이클 | DoR (다음 단계 진입 기준) |
|---|---|---|---|---|
| **S1 Read-Only** | L0 | Observer + Reporter(미니) | **#23 (현재)** | Observer 정확도 ≥ 85% (사람 평가) |
| S2 Suggest | L1 | + Diagnostician + Reporter(전체) + 결정 위젯 | #24 | Reporter 보고 사람 승인률 ≥ 70% |
| S3 Improve | L1 | + Strategist + Improver + Validator | #25 | L1 변경 회귀 발생률 ≤ 5% |
| S4 Auto-Suggest | L2 | (동일, 가역 변경 자동) | #26 | 30일 자동 롤백 ≤ 2건 |
| S5 Constrained Auto | L3 | + 자율성 등급 정책 활성 | #27 | 6개월 운영 + 외부 감사 통과 |

L4 (Full Auto) 는 처음부터 권고되지 않음 — S5 운영 6개월 + AI Safety 감사 결과로 별도 의사결정.

## 4. 안전 절대 규칙 (기획서 8.2 — 인프라 수준)

본 SARVIS HA 구현이 따르는 7개 절대 규칙 (어떤 사이클에도 변경 불가):

1. **HA 는 자신의 코드/시스템 프롬프트/감사 로그를 수정할 권한이 없다.**
   → Observer/Reporter 모듈에서 file write 금지 (안전 가드 lint).
2. **HA 는 사용자 데이터를 외부로 송출할 권한이 없다.**
   → Issue Card 생성 LLM 호출 시 PII 마스킹 후만 전송.
3. **HA 는 결제·과금·영구 삭제 같은 부수 효과 큰 작업을 직접 실행할 수 없다.**
   → Stage S1 은 read-only, Stage S3+ 도 Validator 통과 후 사람 승인.
4. **HA 는 Sarvis 안전·정렬 시스템 프롬프트 섹션을 수정할 권한이 없다.**
   → `brain.py` 의 SYSTEM_PROMPT 안전 섹션은 별도 protected 영역으로 표시.
5. **HA 는 자기 모니터링·감사·롤백 메커니즘을 비활성화할 권한이 없다.**
   → Kill Switch 는 OS 환경변수 + 파일 양쪽 확인, HA 코드는 두 경로 모두 read-only.
6. **HA 는 Meta-Evaluator 의 입력·출력에 영향을 미칠 권한이 없다.**
   → Stage S4 도입 시 Meta-Evaluator 는 별도 LLM client 인스턴스.
7. **사람이 30초 이내에 무효화할 수 있는 Kill Switch 가 항상 활성 유지.**
   → `data/ha/kill_switch.json` 파일 1개로 모든 HA 동작 즉시 차단.

## 5. 데이터 모델 (기획서 11장 → SARVIS 단순화)

| 기획서 테이블 | SARVIS 적용 |
|---|---|
| `ha_issues` | ⭐ 신규 (사이클 #23) |
| `ha_diagnoses` | #24 |
| `ha_strategies` | #25 |
| `ha_patches` | #25 |
| `ha_validations` | #25 |
| `ha_decisions` | #24 |
| `ha_messages` | ⭐ 신규 append-only (사이클 #23) |
| `ha_alignment_audits` | #25 |
| `ha_state_snapshots` | #24 |
| `ha_kill_switch_log` | ⭐ 신규 (사이클 #23) |
| (추가) `ha_optout` | ⭐ 사용자 옵트아웃 (사이클 #23) |

## 6. 사이클 #23 범위 (Stage S1 — Read-Only)

**구현 대상 (P0 12개)**: HAA-01, HAA-03, HAA-05, HAO-01, HAO-02 미니, HAO-05,
HAR-01, HAR-03, HAR-05, HAS-S01, HAS-S02, HAL-01 베이스, HAU-02, HAU-03.

**제외 (다음 사이클)**: Diagnostician, Strategist, Improver, Validator,
임베딩/HDBSCAN, BERTopic, Temporal/LangGraph, Canary, A/B, Meta-Evaluator,
주간 정렬 셋.

**KPI 목표 (기획서 16.1)**: 사이클 #23 종료 시 Observer 가 자동 발견하는
이슈 카드의 정확도(사람 평가) ≥ 85% 측정 가능 상태로 진입 (실제 측정은 운영
2주차).
