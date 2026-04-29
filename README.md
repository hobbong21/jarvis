# J.A.R.V.I.S — Personal AI Assistant

웹캠으로 사용자를 알아보고, "Jarvis" 호출어로 깨어나 한국어로 대화하는 데스크톱 자비스.
**Microsoft JARVIS의 4단계 agent 패턴**(Task Planning → Model Selection → Task Execution → Response Generation)을 Claude의 native tool_use로 구현.

## 핵심 기능

- 🔐 **로그인** — PBKDF2 해싱 기반 계정 관리
- 🎭 **감정 표현 오브** — LLM이 감정 태그로 직접 결정 (7가지)
- 🎥 **얼굴 인식** — 등록된 사용자 자동 식별 + 자동 인사
- 🎙️ **호출어 감지** — "Jarvis" 부르면 깨어남
- 🛠️ **Agent 도구 시스템** (NEW) — Claude가 7개 도구를 자동 선택/실행
- 🧠 **듀얼 백엔드** — Claude (도구 지원) ↔ Ollama (단순 채팅)
- 🗣️ **한국어 음성** — Whisper STT + Edge-TTS

## Agent 도구

자비스는 사용자 요청을 받으면 적절한 도구를 자동으로 선택해 실행하고, 결과를 종합해 답변합니다.

| 도구 | 용도 | 예시 발화 |
|---|---|---|
| `see` | 카메라 시각 분석 (Claude Vision) | "내가 들고 있는 게 뭐야?" |
| `web_search` | 웹 검색 | "오늘 주요 뉴스 알려줘" |
| `get_weather` | 날씨 조회 (Open-Meteo) | "서울 날씨 어때?" |
| `get_time` | 시간/날짜 | "지금 몇 시야?" |
| `remember` | 장기 기억 저장 | "내 차 비밀번호는 1234야" |
| `recall` | 장기 기억 검색 | "내 차 비밀번호 뭐였지?" |
| `set_timer` | 타이머 (음성 알림) | "10분 뒤에 알려줘" |

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
자비스: "[emotion:neutral] 흰 셔츠에 검은 재킷이 잘 어울리시네요."
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

```bash
python main.py
```

- 첫 실행 — 계정 생성 (사용자명 + 비밀번호 4자 이상)
- 이후 — 로그인 → 자동 환영 → "Jarvis" 호출 대기

### 단축키

| 키 | 동작 |
|---|---|
| Q / ESC | 종료 |
| 1 | Claude 백엔드 (도구 활성화) |
| 2 | Ollama 백엔드 (도구 비활성화) |
| R | 대화 히스토리 초기화 |

## 프로젝트 구조

```
jarvis/
├── main.py          # 엔트리: 로그인 → 코어 → UI 루프
├── config.py        # 설정 + 시스템 프롬프트
├── auth.py          # PBKDF2 인증
├── ui.py            # Pygame UI (로그인 + 메인 + 오브)
├── emotion.py       # 감정 enum + 색상 팔레트 + 태그 파서
├── brain.py         # LLM + tool_use 루프 (4단계 agent)
├── tools.py         # NEW — 도구 정의 + 실행기
├── audio_io.py      # 호출어 + STT + TTS
├── vision.py        # 카메라 + 얼굴 인식
├── face_setup.py    # 얼굴 등록 스크립트
├── requirements.txt
├── LICENSE          # MIT
├── README.md
├── users.json       # 자동 생성 — 계정
├── memory.json      # 자동 생성 — 장기 기억
└── faces/           # 자동 생성 — 얼굴 인코딩
```

## 아키텍처 비교

### Microsoft JARVIS (HuggingGPT)
```
사용자 → ChatGPT (라우터) → HuggingFace 모델들 → ChatGPT (종합) → 응답
                          ├─ image-to-text
                          ├─ object-detection
                          ├─ text-to-image
                          └─ ... (수십 개)
```

### 우리 자비스 (Claude tool_use)
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

## 다음 아이디어

- **표정 인식** — DeepFace로 사용자 감정 → 자비스 emotion에 반영
- **MCP 서버 연결** — Claude API의 MCP 통합으로 캘린더, 메일, 슬랙
- **음성 자동 감지** — 호출어 없이 VAD로 발화 자동 감지
- **다중 사용자** — 카메라 식별 → 사용자별 메모리 분리
- **프록시 서버 모드** — Microsoft JARVIS의 `/tasks` `/results` 같은 REST API 노출

## 라이선스

MIT — `LICENSE` 참고
