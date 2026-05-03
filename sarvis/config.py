"""사비스 설정"""
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


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
    # Whisper 모델: tiny / base / small / medium / large-v3 / large-v3-turbo (faster-whisper)
    # 한국어 정확도는 large-v3 가 가장 좋음 (CPU 전용이면 SARVIS_WHISPER_MODEL=medium 또는
    # large-v3-turbo 로 오버라이드 권장).
    whisper_model: str = os.getenv("SARVIS_WHISPER_MODEL", "large-v3")
    whisper_device: str = os.getenv("SARVIS_WHISPER_DEVICE", "auto")
    whisper_language: str = os.getenv("SARVIS_WHISPER_LANGUAGE", "ko")
    # initial_prompt: Whisper 가 한국어 자연어 + 음성 비서 명령 어휘를 우선 학습하도록
    # 호출어/명령 동사/존대 마무리 표현을 함께 노출. stt_filter 가 사용자별 어휘를
    # 추가로 동적 주입한다.
    whisper_initial_prompt: str = os.getenv(
        "SARVIS_WHISPER_PROMPT",
        "다음은 사비스 한국어 음성 비서와의 자연스러운 대화입니다. "
        "사비스, 알려줘, 보여줘, 들려줘, 켜줘, 꺼줘, 검색해줘, 찾아줘, 시작해, 멈춰, "
        "기억해, 잊어버려, 부탁해, 안녕하세요, 감사합니다.",
    )
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
    # 사이클 #9 정비: 런타임 데이터는 모두 data/ 아래로 통일.
    faces_dir: str = os.getenv("SARVIS_FACES_DIR", "data/faces")

    # ============ 행동인식 (action.py) ============
    # mediapipe + ultralytics 미설치 시 자동 비활성. env 로 개별 토글.
    action_enabled: bool = os.getenv("SARVIS_ACTION_ENABLED", "1") not in ("0", "false", "False")
    gesture_wake_enabled: bool = os.getenv("SARVIS_GESTURE_WAKE", "1") not in ("0", "false", "False")
    fall_detect_enabled: bool = os.getenv("SARVIS_FALL_DETECT", "1") not in ("0", "false", "False")
    activity_recognize_enabled: bool = os.getenv("SARVIS_ACTIVITY", "1") not in ("0", "false", "False")
    # 워커 처리 fps. 60fps 메인 루프와 독립.
    action_target_fps: int = int(os.getenv("SARVIS_ACTION_FPS", "10"))
    # 손 들기: N프레임 sustained → wake. 10fps × 0.5초 = 5프레임 (=의도적 동작).
    gesture_wake_sustain_frames: int = 5
    gesture_wake_cooldown_s: float = 4.0
    # 넘어짐: 어깨 정규Y 가 윈도우 안에서 이만큼 떨어지면 급강하로 판단.
    fall_velocity_threshold: float = 0.35
    # 수평 자세 sustained 길이 (10fps → 0.8초 == 안정적 누움).
    fall_horizontal_frames: int = 8
    fall_cooldown_s: float = 30.0
    # 활동 분류 주기 (초). 너무 잦으면 YOLO 부하 ↑, 너무 드물면 반응 ↓.
    activity_interval_s: float = 4.0
    # YOLO 모델 (자동 다운로드). yolov8n=빠름, yolov8s=조금 정확, yolov8m=정확한대신 무거움.
    yolo_model: str = os.getenv("SARVIS_YOLO_MODEL", "yolov8n.pt")

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
- 정중하지만 친근한 말투. "주인님" 같은 호칭은 쓰지 마.
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
