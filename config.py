"""사비스 설정"""
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    # ============ LLM 백엔드 ============
    # "claude" | "openai" | "ollama" | "zhipuai" | "gemini" | "compare" (Claude + OpenAI 병렬 A/B)
    llm_backend: str = os.getenv("SARVIS_BACKEND", "openai")

    # Claude API
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    claude_model: str = "claude-sonnet-4-6"
    # 비전 도구는 빠른 Haiku를 사용 (가격/지연 절감)
    vision_model: str = "claude-haiku-4-5"

    # OpenAI API
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = "gpt-4o-mini"

    # Ollama (로컬, 도구 사용 비활성화)
    # 모델 변경: OLLAMA_MODEL 환경변수로 (예: llama3.2:3b, qwen2.5:14b, gemma2:9b)
    ollama_host: str = os.getenv("OLLAMA_HOST") or "http://localhost:11434"
    ollama_model: str = os.getenv("OLLAMA_MODEL") or "qwen2.5:7b"

    # ZhipuAI (智谱 GLM, OpenAI 호환). 키는 ZHIPUAI_API_KEY 우선,
    # 없으면 OLLAMA_API_KEY 폴백 (초기 설정 시 잘못된 이름으로 저장된 경우 호환).
    zhipuai_api_key: str = os.getenv("ZHIPUAI_API_KEY") or os.getenv("OLLAMA_API_KEY") or ""
    zhipuai_base_url: str = os.getenv("ZHIPUAI_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4"
    zhipuai_model: str = os.getenv("ZHIPUAI_MODEL") or "glm-4-flash"

    # Google Gemini (OpenAI 호환 엔드포인트). 키는 GOOGLE_API_KEY 우선,
    # 없으면 GEMINI_API_KEY 폴백. 기본 모델은 빠르고 저렴한 gemini-2.5-flash.
    gemini_api_key: str = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
    gemini_base_url: str = os.getenv("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai/"
    gemini_model: str = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"

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

    # ============ 장기 메모리 (기획서 v2.0) ============
    # SARVIS 는 단일 사용자 데스크톱 비서 모델. 인증이 추가되기 전까지는
    # 모든 WS 연결이 동일한 메모리 user_id 를 공유 (= 한 사람의 비서로 사용).
    # 다중 사용자 환경에서는 SARVIS_MEMORY_USER 를 디바이스/계정별로 분리할 것.
    memory_user_id: str = os.getenv("SARVIS_MEMORY_USER", "default")
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
- identify_person: 카메라에 보이는 사람이 누구인지 식별 ("나 누구야?", "이 사람 알아?", "누가 보여?")
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
