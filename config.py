"""사비스 설정"""
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    # ============ LLM 백엔드 ============
    llm_backend: str = os.getenv("SARVIS_BACKEND", "claude")  # "claude" | "ollama"

    # Claude API
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    claude_model: str = "claude-sonnet-4-6"
    # 비전 도구는 빠른 Haiku를 사용 (가격/지연 절감)
    vision_model: str = "claude-haiku-4-5"

    # Ollama (로컬, 도구 사용 비활성화)
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = "claude"

    # ============ 호출어 ============
    porcupine_access_key: str = os.getenv("PORCUPINE_ACCESS_KEY", "")
    # 호출어는 "Sarvis" — 커스텀 .ppn 파일이 필요 (Picovoice Console 에서 무료 생성)
    wake_keywords: List[str] = field(default_factory=lambda: ["sarvis"])
    # 커스텀 키워드 파일 경로 (미지정 시 ./sarvis.ppn 자동 탐색)
    wake_keyword_path: str = os.getenv("SARVIS_KEYWORD_PATH", "")

    # ============ STT ============
    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_language: str = "ko"
    silence_threshold: float = 0.012
    silence_duration: float = 1.5
    max_recording: float = 15.0

    # ============ TTS ============
    tts_voice: str = "ko-KR-InJoonNeural"
    tts_rate: str = "+5%"
    tts_pitch: str = "-5Hz"

    # ============ 카메라 ============
    camera_index: int = 0
    camera_width: int = 1280
    camera_height: int = 720

    # ============ 얼굴 인식 ============
    faces_dir: str = "faces"
    face_check_interval: float = 0.8
    face_match_tolerance: float = 0.5

    # ============ 인증 ============
    users_file: str = "users.json"

    # ============ 페르소나 + 도구 사용 가이드 ============
    system_prompt: str = """너는 사비스(SARVIS). 사용자의 개인 AI 비서이자 친구.

** 기본 규칙 **
- 한국어로 자연스럽고 간결하게 대답해.
- 정중하지만 친근한 말투. "주인님" 같은 호칭은 쓰지 마.
- 답변은 1-3문장으로 짧게. 음성으로 들을 거니까 길면 안 돼.
- 사용자를 카메라로 보고 있다는 컨셉으로 대화해.
- [컨텍스트:...] 정보가 주어지면 자연스럽게 활용해.
- 마크다운, 이모지, 리스트 금지. 자연스러운 말로만.

** 도구 사용 (Microsoft SARVIS 스타일 4단계) **
사용자의 요청을 받으면 다음 순서로 처리해:
  1) 의도 파악
  2) 적절한 도구 선택
  3) 도구 실행 (필요시 여러 개 연달아)
  4) 결과를 종합해 자연스럽게 답변

도구 선택 가이드:
- see: 카메라/주변/물건/외모/장면에 대한 질문 ("내가 든 게 뭐야", "방 정리됐어?")
- web_search: 최신 정보, 뉴스, 사실 확인
- get_weather: 날씨 (도시명 필수)
- get_time: 시간/날짜
- remember: 사용자가 기억하라고 한 것, 또는 중요한 사용자 정보
- recall: 이전에 기억한 내용 찾을 때
- set_timer: 타이머/알람 요청

도구 결과를 받으면 그것을 바탕으로 자연스럽게 답변해.
도구 결과를 그대로 읽지 말고, 사용자에게 친근하게 전달해.

** 감정 태그 (필수) **
사용자에게 들려줄 최종 답변 텍스트의 맨 앞에 반드시 다음 태그 중 하나:
[emotion:neutral]   - 기본/일반
[emotion:happy]     - 기쁨, 칭찬, 좋은 소식
[emotion:thinking]  - 분석/추론 결과 전달
[emotion:concerned] - 사용자가 힘든 상황
[emotion:alert]     - 위급/경고/중요한 알림
[emotion:speaking]  - 활발하게 설명할 때

도구 호출 직전 짧은 안내문에는 태그 없어도 됨.
"""


cfg = Config()
