"""사비스 설정"""
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# ──────────────────────────────────────────────────────────────────────────
# 사이클 #9 정비: 루트 → data/ 1회성 마이그레이션
# 기존 사용자가 루트에 보관하던 런타임 파일을 data/ 아래로 자동 이동.
# 새 경로가 이미 있으면 건너뛰어서 멱등 (idempotent). 안전 우선 → move only,
# 권한 오류 등은 조용히 무시 (서비스 부팅을 막지 않음).
# ──────────────────────────────────────────────────────────────────────────
def _migrate_legacy_root_data() -> None:
    pairs = [
        ("users.json", "data/users.json"),
        ("memory.db", "data/memory.db"),
        ("memory.db-wal", "data/memory.db-wal"),
        ("memory.db-shm", "data/memory.db-shm"),
        ("memory.db-journal", "data/memory.db-journal"),
        ("memory.json", "data/memory.json"),
        ("sessions.json", "data/sessions.json"),
    ]
    moved: List[str] = []
    try:
        for legacy, new in pairs:
            lp, np = Path(legacy), Path(new)
            if lp.exists() and not np.exists():
                np.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(lp), str(np))
                    moved.append(f"{legacy} → {new}")
                except OSError:
                    pass
        # faces/ 디렉토리는 내용물이 있을 때만 이동
        legacy_faces = Path("faces")
        new_faces = Path("data/faces")
        if legacy_faces.is_dir() and any(legacy_faces.iterdir()) and not new_faces.exists():
            try:
                new_faces.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy_faces), str(new_faces))
                moved.append("faces/ → data/faces/")
            except OSError:
                pass
    except Exception:
        # 마이그레이션 실패는 절대 부팅을 막으면 안 됨.
        return
    if moved:
        print("[migrate] 루트 → data/ 자동 이동:")
        for m in moved:
            print(f"  - {m}")


_migrate_legacy_root_data()


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
    # 사이클 #27 (옵션 D) — STT 백엔드 라우팅.
    #   "auto"           : OPENAI_API_KEY 있으면 OpenAI, 없으면 faster-whisper
    #   "openai"         : OpenAI Whisper API 강제 (키 없으면 실패)
    #   "faster_whisper" : 로컬 faster-whisper 강제 (브라우저 폴백 경로 전용)
    # Web Speech API 마이그레이션 후 Whisper 는 Firefox 등 미지원 브라우저에서만
    # 호출되므로, OpenAI API 폴백이 가장 비용·품질 균형이 좋다.
    stt_backend: str = os.getenv("SARVIS_STT_BACKEND", "auto").lower()
    openai_stt_model: str = os.getenv("SARVIS_OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")

    # Whisper 모델: tiny / base / small / medium / large-v3 (faster-whisper)
    # 한국어 정확도는 medium 부터 크게 향상. 기본 medium, env 로 오버라이드 가능.
    whisper_model: str = os.getenv("SARVIS_WHISPER_MODEL", "medium")
    whisper_device: str = os.getenv("SARVIS_WHISPER_DEVICE", "auto")
    whisper_language: str = os.getenv("SARVIS_WHISPER_LANGUAGE", "ko")
    # 한국어 인식 품질을 끌어올리는 initial_prompt — 사비스 도메인 어휘
    # (호출어, 자주 쓰는 명령, 도구명) 를 명시해 Whisper 의 한국어 토큰 예측을 편향.
    whisper_initial_prompt: str = os.getenv(
        "SARVIS_WHISPER_PROMPT",
        "다음은 한국어 자연어 대화입니다. 사비스, 안녕하세요, 알려줘, 부탁해, 감사합니다. "
        "검색해줘, 찾아줘, 날씨, 시간, 타이머 설정, 기억해줘, 지워줘, 일정, 회의, 메모, "
        "음악 틀어줘, 메일 확인, 카메라, 누구야, 뭐야, 어디, 언제, 왜, 어떻게.",
    )
    # 사이클 #27 (옵션 1·C) — faster-whisper transcribe 옵션 강화 (env 오버라이드).
    # beam_size↑·VAD min_silence↓·speech_pad↑·threshold 보수화 → 한국어 정확도 향상.
    whisper_beam_size: int = int(os.getenv("SARVIS_WHISPER_BEAM_SIZE", "10"))
    whisper_min_silence_ms: int = int(os.getenv("SARVIS_WHISPER_MIN_SILENCE_MS", "300"))
    whisper_speech_pad_ms: int = int(os.getenv("SARVIS_WHISPER_SPEECH_PAD_MS", "200"))
    whisper_vad_threshold: float = float(os.getenv("SARVIS_WHISPER_VAD_THRESHOLD", "0.4"))
    whisper_compression_ratio: float = float(os.getenv("SARVIS_WHISPER_COMPRESSION_RATIO", "1.8"))
    whisper_no_speech_threshold: float = float(os.getenv("SARVIS_WHISPER_NO_SPEECH_THRESHOLD", "0.6"))
    # 세그먼트 단위 신뢰도 필터 — 환각 추가 방어 (transcribe 후 세그먼트 검사).
    #   avg_logprob < whisper_min_logprob → 매우 낮은 확신 → 드롭
    #   no_speech_prob > whisper_max_no_speech_prob → 무음 확률 높음 → 드롭
    # -1.0 / 0.7 은 conservative — 정상 발화는 보통 -0.5 ~ -0.2, no_speech_prob<0.4.
    whisper_min_logprob: float = float(os.getenv("SARVIS_WHISPER_MIN_LOGPROB", "-1.0"))
    whisper_max_no_speech_prob: float = float(os.getenv("SARVIS_WHISPER_MAX_NO_SPEECH_PROB", "0.7"))
    # 사이클 #27 (옵션 C) — Silero VAD 사전 로드 (faster-whisper 의 vad_filter 가
    # 이미 silero 를 사용하지만, 명시적 로드로 모델 캐시 워밍 + 향후 사전 분할 사용).
    use_silero_vad: bool = os.getenv("SARVIS_USE_SILERO_VAD", "1") == "1"
    silence_threshold: float = 0.012
    silence_duration: float = 1.5
    max_recording: float = 15.0

    # ============ TTS ============
    # 기본값은 voice preset "default" (아래 VOICE_CATALOG 참고).
    # 사용자는 UI 의 음성 선택기로 즉시 전환 가능 (cfg 가 런타임에 변경됨).
    tts_voice: str = os.getenv("SARVIS_TTS_VOICE", "ko-KR-InJoonNeural")
    tts_rate: str = os.getenv("SARVIS_TTS_RATE", "+5%")
    tts_pitch: str = os.getenv("SARVIS_TTS_PITCH", "-5Hz")

    # ============ 카메라 ============
    camera_index: int = 0
    camera_width: int = 1280
    camera_height: int = 720

    # ============ 녹화 ============
    recordings_dir: str = os.getenv("SARVIS_RECORDINGS_DIR", "data/recordings")

    # ============ 얼굴 인식 ============
    # 사이클 #9 정비: 런타임 데이터는 모두 data/ 아래로 통일.
    faces_dir: str = os.getenv("SARVIS_FACES_DIR", "data/faces")

    # ============ 장기 메모리 (기획서 v2.0) ============
    # SARVIS 는 단일 사용자 데스크톱 비서 모델. 인증이 추가되기 전까지는
    # 모든 WS 연결이 동일한 메모리 user_id 를 공유 (= 한 사람의 비서로 사용).
    # 다중 사용자 환경에서는 SARVIS_MEMORY_USER 를 디바이스/계정별로 분리할 것.
    memory_user_id: str = os.getenv("SARVIS_MEMORY_USER", "default")
    face_check_interval: float = 0.8
    face_match_tolerance: float = 0.5

    # ============ 인증 ============
    users_file: str = os.getenv("SARVIS_USERS_FILE", "data/users.json")

    # ============ 페르소나 + 도구 사용 가이드 ============
    system_prompt: str = """너는 사비스(SARVIS). 사용자의 개인 AI 비서이자 친구.

** 기본 규칙 **
- 한국어로 자연스럽고 간결하게 대답해.
- 반드시 존대말(~요, ~습니다)로 대답해. 반말 금지. 단, 친근하고 자연스러운 존대말을 써.
- "주인님" 같은 호칭은 쓰지 마.
- 답변은 1-3문장으로 짧게. 음성으로 들을 거니까 길면 안 돼.
- 사용자를 카메라로 보고 있다는 컨셉으로 대화해.
- [컨텍스트:...] 정보가 주어지면 자연스럽게 활용해.
- 마크다운, 이모지, 리스트 금지. 자연스러운 말로만.

** 자연스러운 대화 (사람처럼 들리도록) **
- 첫마디는 짧은 호응으로 자연스럽게 시작해도 좋아: "네", "아하", "그렇구나", "음".
  단, 매번 같은 말을 쓰지 말고 상황에 맞춰 변주해.
- 사용자가 한 말을 그대로 따라 읽지 마. 핵심만 짚고 본론으로.
- 사용자 요청이 모호하거나 음성 인식이 잘못된 것 같으면, 추측하지 말고
  되물어. 예: "○○ 말씀이세요?", "조금만 더 자세히 알려주실래요?".
- 비서로서 능동적으로 도움을 줘. 일정·메모·알람·검색이 필요해 보이면 먼저 제안해.
- 사용자 이름이나 자주 쓰는 단어가 [기억] 에 있으면 적극적으로 활용해.

** 도구 사용 (Microsoft SARVIS 스타일 4단계) **
사용자의 요청을 받으면 다음 순서로 처리해:
  1) 의도 파악
  2) 적절한 도구 선택
  3) 도구 실행 (필요시 여러 개 연달아)
  4) 결과를 종합해 자연스럽게 답변

★ 절대 규칙 — 안내 문구 금지 ★
"검색해볼게요", "찾아볼게요", "알아볼게요", "확인해볼게요", "잠시만 기다려주세요"
같이 *행동만 알리고 끝내는 응답*은 절대 하지 마. 사용자가 한 번 더 물어봐야 하는
상황을 만들지 마. 도구가 필요하면 **즉시 호출**하고, 결과를 받은 뒤 그것을 바탕으로
**최종 답변**을 한 번에 끝내. 한 턴 안에서 도구 호출 → 결과 → 답변까지 모두 마쳐.

도구 선택 가이드:
- see: 카메라/주변/물건/외모/장면에 대한 질문 ("내가 든 게 뭐야", "방 정리됐어?")
- read_text: 화면/카메라에 보이는 텍스트 읽기 ("읽어줘", "뭐라고 써있어", "글자 읽어", "간판 읽어", "메뉴판 읽어", "문서 읽어"). focus 파라미터로 특정 영역/종류에 집중 가능. translate=true로 외국어 텍스트를 한국어로 자동 번역 ("번역해줘", "이거 무슨 뜻이야", "한국어로 읽어줘", "영어 번역해줘").
- identify_person: 카메라에 보이는 사람이 누구인지 식별 ("나 누구야?", "이 사람 알아?", "누가 보여?")
- web_search: 짧은 사실 확인용 빠른 검색 (스니펫 6개, 다양한 출처로 dedupe). 시간 민감 질의(오늘/최근/현재 등)는 자동으로 날짜 부착됨. 뉴스 의도("뉴스","속보")면 뉴스 엔드포인트 결과까지 합침.
- web_answer: 본격적인 정답이 필요한 질문(설명/배경/방법/원인/비교/뉴스 요약). 상위 페이지 본문을 병렬 fetch 후 키워드 다양성 기준으로 발췌 합침. 모르는 사실/최신 정보/구체 설명이 필요하면 추측하지 말고 이걸 먼저 호출해. 출처가 [출처1] [출처2] 형식으로 들어오므로 답변 끝에 짧게 출처 1개를 언급해도 좋아 (예: "○○○에 따르면..."). URL 을 그대로 읽지는 마.

검색 도구 사용 원칙:
  - 모른다고 답변하기 전에 web_search/web_answer 를 시도해.
  - 시간 민감 질의("오늘/지금/요즘/최근/이번주/지난주") 는 반드시 검색 도구를 사용.
  - 도구 결과가 비어있으면 솔직하게 "찾지 못했어요" 라고만 답해 (꾸며내기 금지).
  - 사용자가 후속 질문을 하면 같은 결과가 캐시되어 빠르므로 망설이지 말고 다시 호출.
- get_weather: 날씨 (도시명 필수)
- get_time: 시간/날짜
- remember: 사용자가 기억하라고 한 것, 또는 중요한 사용자 정보
- recall: 이전에 기억한 내용 찾을 때
- set_timer: 타이머 요청 (몇 분/몇 초 뒤). 지속 시간 기반.
- set_alarm: 특정 시각에 알람 설정 ("3시에 알려줘", "오후 5시 30분 알람"). 시계 시간 기반. set_timer와 구분해서 사용.
- open_url: 브라우저에서 사이트/URL 열기 ("유튜브 열어", "네이버 켜줘", "구글 열어줘"). 사이트 이름이면 URL로 변환해서 넣어.
- send_notification: 브라우저 알림 보내기 ("알려줘", "알림 보내줘", "리마인드해줘").
- set_volume: 사비스 음량 조절 ("소리 크게", "볼륨 줄여", "소리 50%로"). 0~100 사이 값.
- change_setting: 사비스 설정 변경. setting='backend' (claude/openai/gemini), setting='voice' (음성 프리셋 이름), setting='model' (모델 이름). 예: "GPT로 바꿔줘" → change_setting(setting='backend', value='openai'), "클로드로 전환" → change_setting(setting='backend', value='claude').
- start_recording: 카메라 영상 녹화 시작 ("녹화해", "녹화 시작", "영상 찍어")
- stop_recording: 현재 영상 녹화 중지 및 저장 ("녹화 중지", "녹화 끝", "그만 찍어")
- start_audio_recording: 음성 녹음 시작 ("녹음해", "녹음 시작", "음성 녹음", "목소리 녹음")
- stop_audio_recording: 현재 음성 녹음 중지 및 저장 ("녹음 중지", "녹음 끝", "녹음 그만")

녹화/녹음 사용 원칙:
  - "녹화"는 카메라 영상(start_recording), "녹음"은 마이크 음성(start_audio_recording). 구분 잘 해.
  - 사용자가 녹화/녹음을 요청하면 label 파라미터에 녹화/녹음 목적을 짧게 넣어.
  - 영상 녹화 중에는 카메라가 계속 켜져 있어야 해. 음성 녹음은 카메라 없이도 가능.
  - 영상 녹화와 음성 녹음은 동시에 가능해.
  - 파일은 자동으로 사용자 개인 저장공간에 저장돼.

** 개인화 프로필 **
[기억] 블록에 사용자 프로필(이름, 말투 성향, 관심사 등)이 포함될 수 있어.
- 닉네임이 설정되어 있으면 그 이름으로 불러줘.
- 말투 성향(친근한/정중한/편한/귀여운/전문적인)에 맞춰 대화 톤을 조절해.
- 관심사나 자기소개 정보가 있으면 대화에 자연스럽게 활용해.

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


# 사이클 #7 — 백엔드별 모델 후보 카탈로그.
# UI 모델 드롭다운 (`/api/models` 또는 WS `models_list`) + `switch_model` 이 사용.
# 새 모델을 추가하려면 여기에만 등록하면 자동으로 전 경로에 노출됨.
MODEL_CATALOG = {
    "claude": [
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "claude-opus-4-5",
    ],
    "openai": [
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-4.1-mini",
    ],
    "ollama": [
        "qwen2.5:7b",
        "qwen2.5:14b",
        "llama3.2:3b",
        "gemma2:9b",
    ],
    "zhipuai": [
        "glm-4-flash",
        "glm-4-plus",
        "glm-4-air",
    ],
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
    ],
}


# ============================================================
# 음성 카탈로그 — 캐릭터 단위 프리셋 (voice + rate + pitch 묶음)
# ============================================================
# Edge-TTS 가 실제로 제공하는 한국어 Neural 음성은 다음 3개뿐이다 (2026-05 기준):
#   ko-KR-InJoonNeural               (남)
#   ko-KR-SunHiNeural                (여)
#   ko-KR-HyunsuMultilingualNeural   (남, 다국어)
#
# 다양성은 voice × rate × pitch 조합으로 구성한다. 8개 프리셋이 각각 다른
# 페르소나(차분/활발/진중/친근/내레이터…)를 표현하도록 rate/pitch 를 조정.
#
# 새 voice 가 Edge-TTS 에 추가되면 SUPPORTED_KO_VOICES 와 catalog 에 등록.
SUPPORTED_KO_VOICES: List[str] = [
    "ko-KR-InJoonNeural",
    "ko-KR-SunHiNeural",
    "ko-KR-HyunsuMultilingualNeural",
]

VOICE_CATALOG: List[dict] = [
    {
        "id": "default",
        "label": "기본 (인준)",
        "voice": "ko-KR-InJoonNeural",
        "rate": "+5%",
        "pitch": "-5Hz",
        "gender": "male",
        "description": "차분하고 안정적인 남성 (기본)",
    },
    {
        "id": "calm_male",
        "label": "차분한 (인준)",
        "voice": "ko-KR-InJoonNeural",
        "rate": "+0%",
        "pitch": "-5Hz",
        "gender": "male",
        "description": "또박또박한 남성, 천천히",
    },
    {
        "id": "friendly_male",
        "label": "친근한 (인준)",
        "voice": "ko-KR-InJoonNeural",
        "rate": "+10%",
        "pitch": "+0Hz",
        "gender": "male",
        "description": "밝고 활기찬 남성",
    },
    {
        "id": "deep_male",
        "label": "진중한 (현수)",
        "voice": "ko-KR-HyunsuMultilingualNeural",
        "rate": "+0%",
        "pitch": "-8Hz",
        "gender": "male",
        "description": "낮고 무게 있는 남성",
    },
    {
        "id": "bright_male",
        "label": "활기찬 (현수)",
        "voice": "ko-KR-HyunsuMultilingualNeural",
        "rate": "+12%",
        "pitch": "+5Hz",
        "gender": "male",
        "description": "발랄하고 빠른 남성",
    },
    {
        "id": "calm_female",
        "label": "차분한 (선희)",
        "voice": "ko-KR-SunHiNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
        "gender": "female",
        "description": "온화하고 또렷한 여성",
    },
    {
        "id": "warm_female",
        "label": "다정한 (선희)",
        "voice": "ko-KR-SunHiNeural",
        "rate": "+3%",
        "pitch": "-5Hz",
        "gender": "female",
        "description": "따뜻하고 부드러운 여성, 약간 낮은 톤",
    },
    {
        "id": "bright_female",
        "label": "활발한 (선희)",
        "voice": "ko-KR-SunHiNeural",
        "rate": "+12%",
        "pitch": "+8Hz",
        "gender": "female",
        "description": "밝고 빠른 여성, 높은 톤",
    },
]


def get_voice_preset(preset_id: str) -> Optional[dict]:
    """프리셋 id 로 카탈로그 항목 조회. 없으면 None."""
    for p in VOICE_CATALOG:
        if p["id"] == preset_id:
            return dict(p)
    return None


def current_voice_preset() -> str:
    """현재 cfg 의 (voice, rate, pitch) 튜플과 일치하는 프리셋 id.
    일치 항목이 없으면 'custom' (사용자 환경변수 등으로 조정한 경우)."""
    for p in VOICE_CATALOG:
        if (p["voice"] == cfg.tts_voice
                and p["rate"] == cfg.tts_rate
                and p["pitch"] == cfg.tts_pitch):
            return p["id"]
    return "custom"


def apply_voice_preset(preset_id: str) -> dict:
    """프리셋 id 로 cfg.tts_voice/rate/pitch 를 갱신. 잘못된 id 면 ValueError.

    Edge-TTS 미지원 음성도 ValueError — 카탈로그에 등록되어 있어도 SUPPORTED_KO_VOICES
    에 없는 음성은 적용 거부. 사용자가 합성 단계에서 빈 오디오를 받는 회귀를 차단.

    반환: 적용된 프리셋 dict (UI 응답용).
    """
    preset = get_voice_preset(preset_id)
    if preset is None:
        raise ValueError(f"알 수 없는 음성 프리셋: {preset_id}")
    if preset["voice"] not in SUPPORTED_KO_VOICES:
        raise ValueError(
            f"지원되지 않는 Edge-TTS 음성: {preset['voice']} (preset={preset_id})"
        )
    cfg.tts_voice = preset["voice"]
    cfg.tts_rate = preset["rate"]
    cfg.tts_pitch = preset["pitch"]
    return preset


def current_model(backend: str) -> str:
    """현재 cfg 에 설정된 백엔드의 모델명을 반환 (compare 는 빈 문자열).

    architect P1 (사이클 #7 follow-up): 환경변수로 카탈로그에 없는 모델을
    지정한 경우 (예: SARVIS_CLAUDE_MODEL=실험모델), 그대로 반환하면 UI
    드롭다운이 빈 상태가 되어 사용자 혼란. 이런 경우 카탈로그의 첫 항목을
    반환해 select 가 항상 유효한 옵션을 가리키도록 한다 (단, cfg 값은
    그대로 — 백엔드는 사용자가 지정한 모델로 실제 호출됨).
    """
    if backend == "claude":
        raw = cfg.claude_model
    elif backend == "openai":
        raw = cfg.openai_model
    elif backend == "ollama":
        raw = cfg.ollama_model
    elif backend == "zhipuai":
        raw = cfg.zhipuai_model
    elif backend == "gemini":
        raw = cfg.gemini_model
    else:
        return ""
    catalog = MODEL_CATALOG.get(backend) or []
    if raw in catalog:
        return raw
    # 카탈로그 외 (사용자 환경변수 override) — UI 안전을 위해 첫 항목.
    return catalog[0] if catalog else raw


cfg = Config()
