---
name: harness
description: |
  Team-Architecture Factory — 도메인 설명을 받아 에이전트 팀과 그들이 사용할 스킬로
  변환한다. 6가지 사전 정의된 팀 패턴(파이프라인 / 팬아웃-팬인 / 전문가 풀 /
  생성-검증 / 감독자 / 계층적 위임) 중 하나를 선택해 `.claude/agents/` 와
  `.claude/skills/` 하위 산출물을 생성한다.
triggers:
  - "하네스 구성"
  - "하네스 설계"
  - "하네스 적용"
  - "build a harness"
  - "design an agent team"
  - "ハーネスを構成"
layer: L3 Meta-Factory
sub_layer: Team-Architecture Factory
runtime: claude-code
upstream_docs:
  - harness/README.md
  - harness/README_KO.md
  - harness/CHANGELOG.md
---

# Harness — Team-Architecture Factory

> 출처: [revfactory/harness](https://github.com/revfactory/harness) (Apache-2.0).
> 이 SKILL.md 는 첨부된 README 를 기반으로 정리된 **Replit 환경용 운영 사양**이다.
> 원본 플러그인 트리거 (`/plugin install harness@harness`) 와 동등한 의미 트리거를
> 한국어/영어/일본어 자연어로 받는다.

## 1. 언제 호출되는가

다음과 같은 한 문장이면 충분하다:

```
하네스 구성해줘
이 프로젝트에 맞는 에이전트 팀 설계해줘
build a harness for <도메인>
ハーネスを構成して
```

호출 즉시 6단계 워크플로우(아래)가 시작되며, 결과물은 항상 `.claude/agents/` 와
`.claude/skills/` 두 경로에 떨어진다 — 즉, **재사용 가능한 산출물**이지
단발성 답변이 아니다.

## 2. 6 Phase 워크플로우

| Phase | 이름 | 산출 | 핵심 질문 |
|-------|------|------|-----------|
| 1 | **Domain Analysis** | `harness/<domain>/analysis.md` | 무엇을 하는 도메인인가? 단발 vs 반복? 협업 단위가 있는가? |
| 2 | **Team Architecture Design** | `harness/<domain>/architecture.md` | 6패턴 중 어느 것이 맞는가? 에이전트 팀 vs 단일 서브 에이전트? |
| 3 | **Agent Definitions** | `.claude/agents/*.md` | 각 에이전트의 책임/입출력/금지사항은? |
| 4 | **Skill Generation** | `.claude/skills/<skill>/SKILL.md` (+ `references/`) | 에이전트가 실제로 무엇을 호출하나? Progressive Disclosure 로 어떻게 분할? |
| 5 | **Integration & Orchestration** | `.claude/agents/_orchestrator.md` | 메시지 프로토콜 / 핸드오프 / 에러 정책은? |
| 6 | **Validation & Testing** | `harness/<domain>/validation.md` | 트리거가 실제로 잡히는가? With-skill vs Without-skill 비교는? |

각 Phase 완료 시 다음 Phase 시작 전에 사용자 확인을 받는 것이 기본 (드라이런 모드는
예외 — 한 번에 6단계를 모두 초안 생성).

## 3. 6가지 팀 아키텍처 패턴

| 패턴 | 적합한 상황 | SARVIS 예시 |
|------|------------|-------------|
| **파이프라인 (Pipeline)** | 순차 의존, 이전 단계 출력이 다음 단계 입력 | 음성 입력 → STT → LLM → TTS → 출력 |
| **팬아웃/팬인 (Fan-out / Fan-in)** | 병렬 독립 분석, 결과 병합 | 한 발화에 대해 의도/감정/얼굴인식 동시 분석 후 응답 합성 |
| **전문가 풀 (Expert Pool)** | 상황별 1명 선택 호출 | 백엔드 라우팅 (OpenAI / Claude / Ollama) — 비용/지연/언어로 선택 |
| **생성-검증 (Generate-Verify)** | 산출물 생성 후 품질 게이트 | TTS 출력 전 한국어 부적절 표현/길이 검증 |
| **감독자 (Supervisor)** | 중앙이 동적으로 분배 | 멀티턴 대화 매니저가 의도에 따라 도구 호출/메모리 갱신 분배 |
| **계층적 위임 (Hierarchical)** | 상위가 하위에게 재귀 위임 | 신규 기능 추가 시 아키텍트 → 백엔드/프론트/QA 리드 → 각 엔지니어 |

## 4. 산출물 규약

```
프로젝트/
├── .claude/
│   ├── agents/                  # 에이전트 정의 (한 파일 = 한 에이전트)
│   │   ├── _orchestrator.md     # 팀 오케스트레이션 정책
│   │   └── <role>.md
│   └── skills/
│       └── <skill>/
│           ├── SKILL.md         # Frontmatter + 트리거 + Progressive Disclosure
│           └── references/      # 깊은 참조 (지연 로드)
└── harness/
    └── <domain>/
        ├── analysis.md          # Phase 1
        ├── architecture.md      # Phase 2 + 패턴 선정 근거
        └── validation.md        # Phase 6 검증 결과
```

## 5. 모드

| 모드 | 도구 | 권장 |
|------|------|------|
| **에이전트 팀** (기본) | TeamCreate + SendMessage + TaskCreate | 2명 이상, 협업 필요 |
| **서브 에이전트** | Agent 도구 직접 호출 | 단발성, 통신 불필요 |

에이전트 팀 모드는 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` 환경 변수를 요구한다.
Replit Agent 환경에서는 동등 기능을 `delegation` 스킬로 매핑한다 (subagent /
startAsyncSubagent / messageSubagent).

## 6. 진화 메커니즘 (Harness Evolution)

생성된 하네스가 실제 프로젝트에서 사용된 후 `/harness:evolve` 트리거로 초기 ↔ 최종
아키텍처의 델타를 팩토리로 되먹인다. 다음 세대 같은 도메인 생성은 "출시 상태에 더
가까운 초안"에서 시작한다.

```
초기 하네스 ──▶ 실 프로젝트 사용 ──▶ 출시 하네스
                                          │
                                          ▼  /harness:evolve
                                    ┌───────────────┐
                                    │   팩토리      │◀── 더 나은 다음 세대 초안
                                    └───────────────┘
```

## 7. 참조

본 SARVIS 사본에 **현재 포함된** 참조 파일:

- `references/agent-design-patterns.md` — 6패턴 상세 + 결정 트리 (✅)

원본 [revfactory/harness](https://github.com/revfactory/harness) 에는 추가로 다음 참조가
있으나 본 사본에는 미포함이며, 필요 시 원본 저장소에서 단계적으로 가져온다:

- `references/orchestrator-template.md` — 오케스트레이터 보일러플레이트 (원본 전용)
- `references/team-examples.md` — 5가지 실전 팀 구성 (원본 전용)
- `references/skill-writing-guide.md` — Progressive Disclosure 작성 규약 (원본 전용)
- `references/qa-agent-guide.md` — 검증 에이전트 통합 (원본 전용)

## 8. 본 프로젝트(SARVIS) 적용 결과

`harness/sarvis/` 디렉토리 참조 — Phase 1~6 산출물이 SARVIS 도메인에 맞춰 작성되어
있다. 새 기능을 추가할 때는 그 문서의 "감독자 (메인 에이전트)" 와 "전문가 풀
(백엔드 라우팅)" 패턴을 따른다.
