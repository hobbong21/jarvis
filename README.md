# S.A.R.V.I.S — Personal AI Assistant

[![tests](https://github.com/hobbong21/jarvis/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/hobbong21/jarvis/actions/workflows/tests.yml)

웹캠으로 사용자를 알아보고, 호출어 또는 푸시투토크로 깨어나 한국어로 대화하는 사비스.
**데스크톱(pygame)** 모드와 **웹(FastAPI)** 모드 모두 지원.
**Microsoft SARVIS의 4단계 agent 패턴**(Task Planning → Model Selection → Task Execution → Response Generation)을 Claude의 native tool_use로 구현.

## 핵심 기능

- 🔐 **로그인** — PBKDF2 해싱 기반 계정 관리
- 🎭 **감정 표현 오브** — LLM이 감정 태그로 직접 결정 (7가지)
- 🎥 **얼굴 인식** — 등록된 사용자 자동 식별 + 자동 인사
- 🎙️ **호출어 감지** — "Sarvis" 부르면 깨어남
- 🛠️ **Agent 도구 시스템** (NEW) — Claude가 7개 도구를 자동 선택/실행
- 🧠 **듀얼 백엔드** — Claude (도구 지원) ↔ Ollama (단순 채팅)
- 🗣️ **한국어 음성** — Whisper STT + Edge-TTS

## Agent 도구

사비스는 사용자 요청을 받으면 적절한 도구를 자동으로 선택해 실행하고, 결과를 종합해 답변합니다.

| 도구 | 용도 | 예시 발화 |
|---|---|---|
| `see` | 카메라 시각 분석 (Claude Vision) | "내가 들고 있는 게 뭐야?" |
| `web_search` | 웹 검색 | "오늘 주요 뉴스 알려줘" |
| `get_weather` | 날씨 조회 (Open-Meteo) | "서울 날씨 어때?" |
| `get_time` | 시간/날짜 | "지금 몇 시야?" |
| `remember` | 장기 기억 저장 | "내 차 비밀번호는 1234야" |
| `recall` | 장기 기억 검색 | "내 차 비밀번호 뭐였지?" |
| `set_timer` | 타이머 (음성 알림) | "10분 뒤에 알려줘" |
| `observe_action` | 카메라에서 사람 행동/자세 인식 (웹 모드) | "내가 뭐 하고 있어?" |

### 동작 예시

```
사용자: "내가 지금 뭘 입고 있어?"
   ↓
[1. Task Planning] 시각적 질문 → 카메라 분석 필요
   ↓
[2. Model Selection] Claude가 'see' 도구 선택
   ↓ UI에 "USING TOOL: SEE" 표시
[3. Task Execution] 카메라 프레임 캡처 → Claude Vision (Haiku) 호출
   ↓ 결과: "흰 셔츠에 검은 재킷을 입은 사람이 보임"
[4. Response Generation] Claude가 자연스럽게 종합
   ↓
사비스: "[emotion:neutral] 흰 셔츠에 검은 재킷이 잘 어울리시네요."
```

## 1. 설치

```bash
pip install -r requirements.txt
```

dlib 설치 문제가 있다면:
- macOS: `brew install cmake` 후 재시도
- Windows: `pip install dlib-bin face_recognition`
- Ubuntu: `sudo apt install build-essential cmake libopenblas-dev`

## 2. 환경변수

```bash
export ANTHROPIC_API_KEY="sk-ant-..."        # https://console.anthropic.com
export PORCUPINE_ACCESS_KEY="..."            # https://console.picovoice.ai (무료)
```

## 3. 얼굴 등록 (선택)

```bash
python face_setup.py
```

## 4. 실행

### 4-A. 데스크톱 모드 (pygame)

```bash
python main.py
```

- 첫 실행 — 계정 생성 (사용자명 + 비밀번호 4자 이상)
- 이후 — 로그인 → 자동 환영 → "Sarvis" 호출 대기

#### 단축키

| 키 | 동작 |
|---|---|
| Q / ESC | 종료 |
| 1 | Claude 백엔드 (도구 활성화) |
| 2 | Ollama 백엔드 (도구 비활성화) |
| R | 대화 히스토리 초기화 |

### 4-B. 웹 모드 (반응형 브라우저 UI)

```bash
python server.py
# 또는: uvicorn server:app --host 0.0.0.0 --port 8000
```

브라우저에서 `http://localhost:8000` 접속.

특징:
- 푸시투토크 (`SPACE` 또는 마이크 버튼) — 1.5초 무음 시 자동 종료
- 카메라 선택 + 라이브 피드 → 매 1초 프레임을 서버로 전송
- **행동 인식 토글** — 활성화하면 6초마다 사비스가 사람의 행동/자세를 자동 묘사
- 모바일/태블릿/데스크톱 모두 반응형

#### 단축키 (웹)

| 키 | 동작 |
|---|---|
| SPACE | 음성 입력 시작/종료 |
| 1 / 2 | Claude / Ollama 전환 |
| R | 대화 히스토리 초기화 |

## 프로젝트 구조

```
sarvis/
├── main.py          # 데스크톱 엔트리: 로그인 → 코어 → pygame UI 루프
├── server.py        # 웹 엔트리: FastAPI + WebSocket
├── config.py        # 설정 + 시스템 프롬프트
├── auth.py          # PBKDF2 인증
├── ui.py            # Pygame UI (로그인 + 메인 + 오브)
├── emotion.py       # 감정 enum + 색상 팔레트 + 태그 파서
├── brain.py         # LLM + tool_use 루프 (4단계 agent)
├── tools.py         # 도구 정의 + 실행기 (see, observe_action, ...)
├── audio_io.py      # 호출어 + STT + TTS
├── vision.py        # 카메라 + 얼굴 인식 + WebVision 어댑터
├── face_setup.py    # 얼굴 등록 스크립트
├── web/             # 웹 모드 정적 자산
│   ├── index.html
│   ├── style.css
│   ├── orb.js       # Canvas 감정 오브
│   └── app.js       # WebSocket 클라이언트 + 마이크/카메라
├── requirements.txt
├── LICENSE          # MIT
├── README.md
└── data/            # 자동 생성 — 모든 런타임 사용자 데이터
    ├── users.json   #   계정
    ├── memory.db    #   장기 기억 (SQLite)
    ├── memory.json  #   도구용 단순 메모리
    └── faces/       #   얼굴 인코딩
```

## 아키텍처 비교

### Microsoft SARVIS (HuggingGPT)
```
사용자 → ChatGPT (라우터) → HuggingFace 모델들 → ChatGPT (종합) → 응답
                          ├─ image-to-text
                          ├─ object-detection
                          ├─ text-to-image
                          └─ ... (수십 개)
```

### 우리 사비스 (Claude tool_use)
```
사용자 → Claude (라우터+종합) → Tool Executor → 응답
                              ├─ see (Claude Vision)
                              ├─ web_search (DuckDuckGo)
                              ├─ get_weather (Open-Meteo)
                              ├─ remember/recall (JSON store)
                              └─ set_timer (threading.Timer)
```

핵심 아이디어는 동일: **LLM이 컨트롤러가 되어 전문 도구들을 조율**.
차이점은 우리는 Claude의 native tool_use를 쓰므로 라우팅 로직을 별도로 구현할 필요가 없고, 한 번의 API 호출 안에서 multi-turn tool use가 가능하다는 점.

## 도구 추가하기

`tools.py`에서:

1. `TOOL_DEFINITIONS` 리스트에 스펙 추가
2. `ToolExecutor`에 `_t_도구명(self, ...)` 메서드 구현
3. 끝.

```python
# 예: 음악 재생 도구
TOOL_DEFINITIONS.append({
    "name": "play_music",
    "description": "Play a song or album",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"]
    }
})

class ToolExecutor:
    def _t_play_music(self, query: str) -> str:
        # spotipy 또는 system command
        ...
        return f"재생 중: {query}"
```

## 개발 방법론 — Harness 적용

SARVIS 의 신규 기능 / 굵직한 변경은 **[Harness](harness/README.md) 메타-스킬**을
사용해 **에이전트 팀 아키텍처**로 분해한 뒤 진행한다. Harness 는 SARVIS 의 런타임
기능이 아니라 *어떻게 SARVIS 를 발전시킬 것인가* 를 통제하는 보조 시스템이다.

### Harness 적용 결과 (요약)

| 패턴 | 상태 | 적용 위치 |
|------|------|----------|
| **Supervisor** | ✅ 구현됨 | `brain.py` — 의도 분류 → 분배 |
| **Pipeline** | ✅ 구현됨 | STT → 의도 → LLM → TTS |
| **Hierarchical** | ✅ 구현됨 | 개발 시 `architect` → 도메인 리드 → 엔지니어 위임 트리 (.claude/agents/) |
| **Expert Pool** | 🟡 부분 | 백엔드 수동 전환은 동작. 자동 폴백은 미구현. |
| **Fan-out / Fan-in** | 🟡 부분 | 감정·얼굴·메모리는 호출되지만 명시적 병렬 fan-out 스케줄러 없음. |
| **Generate-Verify** | ⏳ 목표 | `tts-verifier` 스킬 명세만 존재. 실제 코드 미구현. |

상세 차이는 [`harness/sarvis/architecture.md`](harness/sarvis/architecture.md) 와
[`harness/sarvis/validation.md`](harness/sarvis/validation.md) 의 open items 참조.

### 산출물 위치

| 경로 | 내용 |
|-----|------|
| `harness/` | Harness 플러그인 원본 (README EN/KO/JA, CHANGELOG, 랜딩페이지) |
| `harness/sarvis/analysis.md` | Phase 1 — SARVIS 도메인 분석 |
| `harness/sarvis/architecture.md` | Phase 2 — 6패턴 합성 결정 |
| `harness/sarvis/validation.md` | Phase 6 — 트리거 / 산출물 검증 결과 |
| `.claude/skills/harness/SKILL.md` | Harness 메타-스킬 (트리거 정의) |
| `.claude/skills/tts-verifier/SKILL.md` | Phase 4 산출 — TTS 품질 게이트 |
| `.claude/agents/_orchestrator.md` | 런타임 오케스트레이션 정책 |
| `.claude/agents/{architect,voice,vision,backend,frontend,qa,security}-*.md` | 개발 에이전트 정의 |

### 신규 기능 추가 절차

1. `architect` 에이전트가 영향 분석 + 6패턴 중 적용 패턴 선정 (`harness/sarvis/architecture.md` 의 표 갱신).
2. 위임 트리에 따라 leaf 엔지니어 (voice / vision / backend / frontend) 에 작업 명세.
3. `qa-engineer` 7항 체크리스트 통과.
4. `security-reviewer` 5항 점검.
5. 변경 사항 + 패턴 선정 근거를 `replit.md` 에 기록.

### 랜딩페이지 (개발 문서)

- `http://<host>:5000/harness/` — Harness 소개 및 6패턴 시각화.
- `http://<host>:5000/harness/privacy.html` — 개인정보처리방침.

### 트리거 (Claude Code 환경)

```
하네스 구성해줘
하네스 적용해줘
build a harness for <도메인>
ハーネスを構成して
```

> Harness 는 [revfactory/harness](https://github.com/revfactory/harness) (Apache-2.0)
> 의 사양을 따른다. 본 프로젝트에 동봉된 사본은 `harness/LICENSE` 참고.

---

## 다음 아이디어

- **표정 인식** — DeepFace로 사용자 감정 → 사비스 emotion에 반영
- **MCP 서버 연결** — Claude API의 MCP 통합으로 캘린더, 메일, 슬랙
- **음성 자동 감지** — 호출어 없이 VAD로 발화 자동 감지
- **다중 사용자** — 카메라 식별 → 사용자별 메모리 분리
- **프록시 서버 모드** — Microsoft SARVIS의 `/tasks` `/results` 같은 REST API 노출

## 라이선스

MIT — `LICENSE` 참고
