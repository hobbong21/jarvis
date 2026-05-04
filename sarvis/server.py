"""S.A.R.V.I.S 웹 서버 — FastAPI + WebSocket

브라우저에서 마이크/카메라를 사용하고, 같은 Brain/Tools 파이프라인을 재사용한다.

실행:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload

또는:
    python server.py
"""
import asyncio
import functools
import base64
import json
import os
import re
import secrets
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .audio_io import EdgeTTS, make_stt
from .brain import Brain, _friendly_error, _model_switch_friendly
from .config import cfg
from .memory import get_memory, extract_user_facts
from .stt_filter import clean_stt_text, build_dynamic_initial_prompt
from .tools import ToolExecutor
from .vision import (
    FaceRegistry,
    WebVision,
    compute_eye_aspect_ratio_from_jpeg,
    compute_face_encoding_from_jpeg,
    is_face_landmarks_supported,
)
from .owner_auth import (
    OwnerAuth,
    detect_blink_in_window,
    random_challenge,
    ENROLL_FACE_ANGLES,
    ENROLL_FACE_LABELS_KO,
    BLINK_WINDOW_SECONDS,
)

# Harness Phase 4 — Fan-out 분석 + Evolution 텔레메트리
from .analysis import parallel_analyze, analysis_to_context
from . import telemetry

# ============================================================
# 전역 — 서버를 즉시 시작하고 Whisper 는 백그라운드에서 로드
# ============================================================
print("=" * 60)
print("  S . A . R . V . I . S   웹 서버 초기화")
print("=" * 60)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


# ─────────────────────────────────────────────────────────────
# 스트리밍 TTS 헬퍼 (기획서 v1.5)
# 응답 텍스트를 첫 문장(head) 와 나머지(tail) 로 쪼개서 두 청크로 병렬 합성하면
# 첫 음성 지연이 줄어 사용자 체감 응답속도가 개선됨.
# ─────────────────────────────────────────────────────────────
# 한국어 종결 어미 + 라틴 종결 부호. 줄바꿈도 분할 후보.
# 문장 끝 후보를 *최소* head 길이 이후에서 가장 빠른 후보로 잡는다.
_SENTENCE_END_RE = re.compile(r"[.!?。…]+|\n+")
# 실제 한국어 응답에서 의미 있는 첫 문장은 보통 15~160자.
# min_head 가 너무 크면 분할 기회를 놓치고, 너무 작으면 의미 없는 짧은 첫 청크.
_TTS_SPLIT_MIN_HEAD = 15
_TTS_SPLIT_MAX_HEAD = 160
_TTS_SPLIT_MIN_TOTAL = 60   # 전체가 60자 미만이면 분할 안 함


def _split_first_sentence(text: str) -> Tuple[str, str]:
    """첫 문장과 나머지로 분리. 분할 부적절 시 (text, '') 반환.

    분할 기준:
      - 전체 길이 ≥ _TTS_SPLIT_MIN_TOTAL
      - 첫 [.!?。…\\n] 위치가 [_TTS_SPLIT_MIN_HEAD, _TTS_SPLIT_MAX_HEAD] 범위
      - 분할 후 head, tail 모두 비어 있지 않음
    """
    if not text or len(text) < _TTS_SPLIT_MIN_TOTAL:
        return text, ""
    for m in _SENTENCE_END_RE.finditer(text):
        cut = m.end()
        if cut < _TTS_SPLIT_MIN_HEAD:
            continue
        if cut > _TTS_SPLIT_MAX_HEAD:
            break  # 더 뒤의 후보도 더 길어지므로 분할 포기
        head = text[:cut].strip()
        tail = text[cut:].strip()
        if head and tail:
            return head, tail
        return text, ""
    return text, ""


# STT: 사이클 #27 — make_stt() 가 OpenAI Whisper API ▸ faster-whisper 폴백을 자동 선택.
# 로컬 모델은 다운로드가 오래 걸릴 수 있으므로 백그라운드 스레드로 로드.
STT = None  # type: Optional[object]

def _load_stt():
    global STT
    print("[1/3] STT — 백그라운드 로딩 시작 ...")
    try:
        STT = make_stt()
        print("      STT 준비 완료.")
    except Exception as e:
        print(f"      STT 초기화 실패: {e}")

threading.Thread(target=_load_stt, daemon=True, name="stt-loader").start()

print("[2/3] TTS (Edge-TTS) ...")
TTS = EdgeTTS()

def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _text_input_log_user(data: dict) -> bool:
    """text_input 메시지의 log_user 플래그 결정.

    프로덕션 경로: 텍스트 입력은 사용자 발화이므로 항상 True.

    테스트 시 monkeypatch 가능 — 음성 흐름(handle_audio)은 respond_internal 을
    거치지 않지만, 만약 향후 누군가 voice 결과를 respond_internal(log_user=False)
    로 라우팅하면 compare 모드 분기(server.respond_internal 의 `and log_user`
    가드)가 자동으로 회피되어야 한다. 이 헬퍼를 patch 하여 회귀 테스트가
    "음성 경로 fake" 를 시뮬레이션할 수 있다 (Task #20).
    """
    return True


print("[3/3] 얼굴 등록부 ...")
FACE_REGISTRY = FaceRegistry(cfg.faces_dir)
# 사이클 #18 — 주인 인증 (얼굴 + 음성 패스프레이즈).
# 미등록 상태에선 게이트 비활성화 → 기존 테스트/사용자 흐름 회귀 0.
# 등록되면 매 WS 연결마다 두 단계 모두 통과해야 메인 기능 사용 가능.
OWNER_AUTH = OwnerAuth("data/owner.json")
# 사이클 #21 — F-04 회의록 + F-10 할 일/캘린더.
from .meeting import MeetingRegistry, build_summary_prompt, parse_summary_json
from .todos import TodoStore, extract_todos_from_text
MEETINGS = MeetingRegistry()
TODOS = TodoStore()
# 사이클 #23 (HA Stage S1) — Harness Agent 자율 진화 계층.
# Observer + Reporter 미니. L0 자율 등급 (Observe-only).
from .ha import (
    Observer as _HAObserver,
    Reporter as _HAReporter,
    Diagnostician as _HADiagnostician,  # 사이클 #24
    Strategist as _HAStrategist,        # 사이클 #25
    Improver as _HAImprover,
    Validator as _HAValidator,
    is_kill_switch_on as _ha_kill_switch_on,
    activate_kill_switch as _ha_kill_switch_activate,
    deactivate_kill_switch as _ha_kill_switch_deactivate,
    KillSwitchActivated as _HAKillSwitchActivated,
)
_known = FACE_REGISTRY.list_people()
if _known:
    print(f"      등록된 얼굴: {', '.join(_known)}")
else:
    print("      등록된 얼굴 없음 (웹에서 + 버튼으로 등록)")

# 멀티모달 명령 로그용 이미지 디렉토리. 텍스트/메타는 memory.commands 테이블,
# 이진 이미지는 이 디렉토리에 <commands.id>.jpg 로 저장.
COMMANDS_DIR = os.environ.get(
    "SARVIS_COMMANDS_DIR",
    os.path.join(os.path.dirname(cfg.faces_dir) or "data", "commands"),
)
os.makedirs(COMMANDS_DIR, exist_ok=True)

# 사이클 #16: 사비스의 학습 지식 첨부 미디어 디렉토리. 텍스트/메타는
# memory.knowledge 테이블, 이진 파일은 여기에 <knowledge.id>.{jpg|webm} 로 저장.
KNOWLEDGE_DIR = os.environ.get(
    "SARVIS_KNOWLEDGE_DIR",
    os.path.join(os.path.dirname(cfg.faces_dir) or "data", "knowledge"),
)
os.makedirs(KNOWLEDGE_DIR, exist_ok=True)

RECORDINGS_DIR = cfg.recordings_dir
os.makedirs(RECORDINGS_DIR, exist_ok=True)

print("[3/3] 설정 완료.")

print("=" * 60)
print("  서버 시작 중 (STT 는 백그라운드에서 로딩). http://localhost:5000")
print("=" * 60)


# ============================================================
# 사용자 세션 — 한 WebSocket 연결당 하나
# ============================================================
class UserSession:
    """WebSocket 한 개에 대응하는 상태 (Brain, ToolExecutor, WebVision)."""

    def __init__(self, username: str):
        self.username = username
        self.brain = Brain()
        self.vision = WebVision()
        self.tools: Optional[ToolExecutor] = None
        self.observing = False
        self._observe_thread: Optional[threading.Thread] = None
        self._observe_stop = threading.Event()
        self._last_observation = ""
        self.on_event = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        # (기획서 v2.0) 장기 메모리.
        # memory_user_id 는 인증 도입 전까지 cfg.memory_user_id 를 공유 (단일 비서 모델).
        # username 은 UI 표시용; 메모리 격리는 별도 키.
        self.memory = get_memory()
        self.memory_user_id = cfg.memory_user_id
        # (P1) 한 세션 내부 동시 호출 직렬화용 락. conv_id 자체는 매 호출마다
        # memory.get_or_start_conversation 으로 재평가 — idle 후 자동 전환.
        self._conv_id_lock = threading.Lock()

        # 사이클 #9 — 3-Pillar 텔레메트리: 현재 turn 의 도구/비전 카운터.
        # process_prompt / handle_audio 진입 시 초기화, _on_tool_event(start) 마다 +1,
        # turn 종료 시 turn_meta 에 합산해 telemetry.log_turn 으로 흘려보낸다.
        self._turn_tool_count = 0
        self._turn_vision_used = False
        self._turn_tool_t0: Optional[float] = None
        self._turn_tool_total_ms: float = 0.0

        self.is_recording = False
        self._recording_label = ""
        self._recording_start_ts: float = 0.0

        if cfg.llm_backend == "claude" and cfg.anthropic_api_key:
            self._attach_tools()

    def reset_turn_counters(self) -> None:
        """매 turn 진입 시 호출 — 도구/비전 카운터 초기화."""
        self._turn_tool_count = 0
        self._turn_vision_used = False
        self._turn_tool_t0 = None
        self._turn_tool_total_ms = 0.0

    def turn_pillar_meta(self) -> dict:
        """turn_meta 에 머지할 3-pillar 카운터 스냅샷."""
        return {
            "tool_count": int(self._turn_tool_count),
            "tool_ms": float(self._turn_tool_total_ms),
            "vision_used": bool(self._turn_vision_used),
        }

    def get_conv_id(self) -> int:
        """현재 대화 conversation id. 30분 idle 후 자동 재시작.

        매 호출마다 memory.get_or_start_conversation 을 재평가해야 idle 윈도우가
        지난 후 새 conversation 으로 자연 전환된다 + 다중 세션이 같은 user_id 의
        가장 최근 active conversation 으로 수렴한다. lock 은 한 세션 내부의
        동시 호출이 두 번 INSERT 하지 않도록 직렬화하기 위함.
        """
        with self._conv_id_lock:
            return self.memory.get_or_start_conversation(self.memory_user_id)

    def _attach_tools(self):
        if self.tools is None:
            self.tools = ToolExecutor(
                vision_system=self.vision,
                anthropic_client=self.brain.get_client(),
                on_event=self._on_tool_event,
                on_timer=self._on_timer,
                face_registry=FACE_REGISTRY,
                on_recording=self._on_recording,
                on_system_cmd=self._on_system_cmd,
            )
        self.brain.tools = self.tools

    def _on_recording(self, action: str, label: str, kind: str = "video"):
        if action == "start":
            if kind == "video":
                self.is_recording = True
                self._recording_label = label
                self._recording_start_ts = time.time()
            self._emit({"type": "recording_cmd", "action": "start", "label": label, "kind": kind})
        elif action == "stop":
            if kind == "video":
                self.is_recording = False
            self._emit({"type": "recording_cmd", "action": "stop", "label": label, "kind": kind})

    def detach_tools(self):
        self.brain.tools = None

    def _emit(self, msg: dict):
        """동기 컨텍스트에서 WebSocket으로 메시지 전송."""
        if self.on_event and self.loop:
            asyncio.run_coroutine_threadsafe(self.on_event(msg), self.loop)

    def _on_tool_event(self, tool_name: str, status: str):
        # 사이클 #9 — turn 단위 도구 카운터 (3-pillar telemetry).
        # status: 'start' / 'end'. ToolExecutor 가 매 호출마다 두 번 발화.
        try:
            if status == "start":
                self._turn_tool_count += 1
                self._turn_tool_t0 = time.monotonic()
                # 카메라 기반 비전 도구 — identify_person 도 카메라 프레임 사용.
                if tool_name in ("see", "observe_action", "identify_person"):
                    self._turn_vision_used = True
            elif status == "end" and self._turn_tool_t0 is not None:
                self._turn_tool_total_ms += (time.monotonic() - self._turn_tool_t0) * 1000.0
                self._turn_tool_t0 = None
        except Exception:
            traceback.print_exc()
        self._emit({"type": "tool_event", "tool": tool_name, "status": status})

    def _on_timer(self, label: str):
        self._emit({"type": "timer_expired", "label": label})

    def _on_system_cmd(self, cmd: dict):
        self._emit(cmd)

    # -------- 행동 모니터링 --------
    def start_observing(self, interval: float = 6.0):
        if self.observing or self.tools is None:
            return
        self.observing = True
        self._observe_stop.clear()
        self._observe_thread = threading.Thread(
            target=self._observe_loop, args=(interval,), daemon=True
        )
        self._observe_thread.start()

    def stop_observing(self):
        self.observing = False
        self._observe_stop.set()

    def _observe_loop(self, interval: float):
        while not self._observe_stop.is_set():
            # 첫 회 즉시 실행 후 interval 대기
            try:
                if self.tools is not None and self.vision.read() is not None:
                    desc = self.tools._t_observe_action(focus="activity")
                    if desc and desc != self._last_observation and "보이지 않" not in desc:
                        self._last_observation = desc
                        self._emit({"type": "observation", "description": desc})
            except Exception as e:
                print(f"[Observer] 오류: {e}")
            self._observe_stop.wait(interval)


# 세션 토큰 → UserSession
ACTIVE: Dict[str, UserSession] = {}


# ============================================================
# FastAPI 앱
# ============================================================
app = FastAPI(title="SARVIS Web")

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# Harness 메타-스킬 랜딩페이지 (개발 방법론 문서). SARVIS 의 런타임 기능이 아니라
# "어떻게 SARVIS 를 발전시킬 것인가" 를 안내하는 보조 시스템.
#
# 보안 주의: /harness 는 *큐레이션된 공개 자산만* 노출한다.
# - web/harness/    : index.html / privacy.html / 배너 PNG (공개 가능)
# - harness/ (루트) : README*.md / CHANGELOG / CONTRIBUTING / LICENSE / .gitignore /
#                     sarvis/*.md → **공개 안 함** (저장소 내부 전용)
HARNESS_PUBLIC_DIR = WEB_DIR / "harness"
if HARNESS_PUBLIC_DIR.exists():
    app.mount(
        "/harness",
        StaticFiles(directory=str(HARNESS_PUBLIC_DIR), html=True),
        name="harness",
    )


# 개발 환경에서는 정적 파일 캐시 비활성 (Replit 미리보기에서 옛 JS/CSS 가 잡혀
# 사용자가 수정 사항을 못 보는 문제 방지). 운영 배포 시에는 캐시 허용.
_IS_DEV = os.getenv("NODE_ENV") != "production"

@app.middleware("http")
async def _no_cache_static_in_dev(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if _IS_DEV and (path == "/" or path.startswith("/static/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/")
async def index():
    """index.html 을 읽어서 정적 자산 URL 에 파일 mtime 버전 쿼리를 붙여 반환.

    이렇게 하면 web/app.js 가 변경될 때마다 ?v=<mtime> 가 바뀌어 브라우저/프록시
    캐시를 강제로 우회한다 (Replit 미리보기 환경에서 옛 JS 가 잡히는 문제 방지).
    """
    from fastapi.responses import HTMLResponse
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("style.css", "orb.js", "app.js"):
        try:
            v = (WEB_DIR / asset).stat().st_mtime_ns
        except OSError:
            v = time.time_ns()
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={v}")
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ============================================================
# 파일 다운로드 API — 저장된 녹화/사진 파일 서빙
# ============================================================
@app.get("/api/recordings/{rec_id}")
async def download_recording(rec_id: int):
    from .memory import Memory
    from fastapi.responses import JSONResponse
    mem = Memory(cfg.db_path)
    row = mem.get_recording_by_id(rec_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    fp = row.get("file_path", "")
    if not fp or not os.path.isfile(fp):
        return JSONResponse({"error": "file missing"}, status_code=404)
    rec_root = os.path.realpath(RECORDINGS_DIR)
    real_fp = os.path.realpath(fp)
    if not real_fp.startswith(rec_root + os.sep) and real_fp != rec_root:
        return JSONResponse({"error": "access denied"}, status_code=403)
    fname = row.get("filename", os.path.basename(fp))
    kind = row.get("kind", "")
    if kind == "photo":
        media_type = "image/jpeg"
    elif kind == "audio":
        media_type = "audio/webm"
    else:
        media_type = "video/webm"
    return FileResponse(real_fp, filename=fname, media_type=media_type)


# ============================================================
# WebSocket — 메인 대화 채널 (인증 없음)
# ============================================================
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    conn_id = secrets.token_hex(8)  # 연결별 내부 ID
    await ws.accept()

    session = UserSession("S.A.R.V.I.S")
    ACTIVE[conn_id] = session

    session.loop = asyncio.get_event_loop()
    session.on_event = lambda msg: ws.send_json(msg)

    busy = asyncio.Lock()

    async def emit(**kwargs):
        try:
            await ws.send_json(kwargs)
        except Exception:
            pass

    async def emit_bytes(data: bytes):
        try:
            await ws.send_bytes(data)
        except Exception:
            pass

    await emit(
        type="ready",
        username=session.username,
        backend=cfg.llm_backend,
        tools_enabled=session.tools is not None,
        faces=FACE_REGISTRY.list_people(),
    )

    # ── 사이클 #18: 주인 인증 상태 ──────────────────────────────
    # 미등록 → 게이트 비활성화 (face_ok/voice_ok 기본 True). 첫 부팅 사용자가
    # 막히지 않고, 클라이언트는 "주인 등록" UI 만 띄움.
    # 등록 → 두 단계 모두 통과 전엔 모든 명령 차단.
    _enrolled = OWNER_AUTH.is_enrolled()
    # ── 사이클 #20 — 라이브니스 capability probe (세션 시작 1회) ──
    # face_recognition + cv2 가 모두 가용해야 EAR 측정이 가능. 가용한 환경에서는
    # 깜빡임 강제, 미지원 환경에서는 처음부터 우회. EAR 추출이 일시적으로 실패
    # 했다고 라이브니스가 영구 우회되는 보안 결함(architect 지적)을 방지.
    _landmarks_supported = is_face_landmarks_supported()
    auth_state: Dict[str, Any] = {
        "face_ok": not _enrolled,
        "voice_ok": not _enrolled,
        "last_face_attempt": 0.0,
        "last_voice_attempt": 0.0,
        "welcome_started": False,
        # 중복 auth_complete emit 방지 (idempotent guard).
        "completed_emitted": False,
        # ── 사이클 #20 (F-01 보강) — 라이브니스/챌린지 ──
        # 눈 깜빡임 검출용 EAR 시계열. (timestamp, ear) 튜플. 윈도우는
        # 가장 최근 BLINK_WINDOW_SECONDS 만 유지.
        "ear_samples": [],
        # 깜빡임 통과 여부. 라이브니스 미지원(landmarks 없음) 환경에선
        # 처음부터 통과(blink_required=False)로 둔다.
        "blink_ok": (not _enrolled) or (not _landmarks_supported),
        # 세션 시작 시 1회 결정 → 이후 변경하지 않음 (영구 우회 차단).
        "blink_required": _enrolled and _landmarks_supported,
        # 현재 챌린지 — 로그인 시작 시 발급, 검증 후 폐기. 미등록이면 None.
        "current_challenge": (random_challenge() if _enrolled else None),
    }

    def _is_authed() -> bool:
        return bool(
            auth_state["face_ok"]
            and auth_state["voice_ok"]
            and (auth_state["blink_ok"] or not auth_state["blink_required"])
        )

    def _refresh_challenge() -> Optional[str]:
        """등록 상태에서 챌린지 1개 재발급. 미등록이면 None."""
        if not OWNER_AUTH.is_enrolled():
            auth_state["current_challenge"] = None
            return None
        c = random_challenge()
        auth_state["current_challenge"] = c
        return c

    async def _emit_auth_status():
        info = OWNER_AUTH.info()
        await emit(
            type="auth_status",
            enrolled=info["enrolled"],
            face_name=info["face_name"],
            voice_passphrase_len=info["voice_passphrase_len"],
            has_face_encoding=info["has_face_encoding"],
            face_encoding_count=info.get("face_encoding_count", 0),
            face_angles=info.get("face_angles", []),
            schema_version=info.get("schema_version", 1),
            face_ok=auth_state["face_ok"],
            voice_ok=auth_state["voice_ok"],
            blink_ok=auth_state["blink_ok"],
            blink_required=auth_state["blink_required"],
            authed=_is_authed(),
            challenge=auth_state.get("current_challenge"),
            enroll_angles=list(ENROLL_FACE_ANGLES),
            enroll_angle_labels=ENROLL_FACE_LABELS_KO,
        )

    await _emit_auth_status()

    # 환영 인사 (백그라운드) — 고정 문구 + 직접 TTS.
    # ----------------------------------------------------------
    # LLM 호출을 의도적으로 제거: 첫 페이지 로드 시 백엔드(Claude/OpenAI)가
    # 일시적으로 응답하지 못하면 사용자에게 "internal server error" / 빨간
    # 토스트가 보이는 회귀가 있었음. 환영 인사는 가벼운 정적 문자열이면
    # 충분하므로 외부 호출 의존성을 끊어 항상 성공시킨다. 본문 대화는
    # 변경 없이 기존 think_stream + fallback 파이프라인을 사용.
    WELCOME_TEXT = "안녕하세요, 사비스입니다. 무엇을 도와드릴까요?"

    async def welcome():
        await asyncio.sleep(0.5)
        async with busy:
            try:
                await emit(type="state", state="speaking")
                await emit(type="emotion", emotion="neutral")
                # 화면 + 대화 로그에 메시지 1건 표시 (스트리밍과 동일한 형식)
                await emit(type="stream_start")
                await emit(type="stream_chunk", text=WELCOME_TEXT)
                await emit(
                    type="stream_end",
                    text=WELCOME_TEXT,
                    emotion="neutral",
                    is_welcome=True,  # 클라이언트가 자동재생 잠금 해제 후 재생할 수 있도록 표시
                )

                # TTS 합성 (Edge-TTS) — 별도 스레드에서.
                def _noop_regen(orig: str, reason: str) -> str:  # 안전 검증 폴백 (사용 안 됨)
                    return orig
                tts_result = await asyncio.to_thread(
                    TTS.synthesize_bytes_verified, WELCOME_TEXT, _noop_regen,
                )
                if tts_result.get("audio"):
                    await emit_bytes(tts_result["audio"])
            except Exception:
                # 환영 인사는 절대 사용자에게 에러를 노출하지 않는다.
                traceback.print_exc()
            finally:
                await emit(type="state", state="idle")
                await emit(type="emotion", emotion="neutral")

    async def respond_internal(prompt: str, log_user: bool):
        # compare 모드: 텍스트 입력일 때만 — Claude + OpenAI 병렬 A/B
        if cfg.llm_backend == "compare" and log_user:
            await respond_compare(prompt)
            return

        # 사이클 #9 — 3-Pillar telemetry: 매 turn 진입 시 도구/비전 카운터 초기화.
        session.reset_turn_counters()

        await emit(type="state", state="thinking")
        await emit(type="emotion", emotion="thinking")

        # ── Harness 텔레메트리: 턴 메타 초기화 ───────────────────────
        t_turn_start = time.monotonic()  # 사이클 #5 T001: total_ms 계산용
        turn_meta = {
            "turn_id": telemetry.new_turn_id(),
            "ts": time.time(),
            "input_channel": "text",  # 사이클 #4 T001
            "backend": cfg.llm_backend,
            "fallback_used": False,
            "fallback_chain": [cfg.llm_backend],
            "intent": None,
            "emotion": None,
            "tools_used": 0,
            "fanout_ms": 0.0,
            "llm_ms": 0.0,
            "tts_ms": 0.0,
            "total_ms": 0.0,
            "tts_ok": None,
            "tts_reason": None,
            "prompt_len": len(prompt or ""),
            "reply_len": 0,
        }

        try:
            # ── Phase: Fan-out/Fan-in 사전 분석 ─────────────────────
            # (기획서 v2.0) build_context 에 현재 발화를 query 로 전달 → 관련 과거 발언이
            # system prompt 에 자동 주입된다.
            base_ctx = build_context(query=prompt)
            analysis = await parallel_analyze(prompt, session)
            turn_meta["fanout_ms"] = analysis.get("ms", 0.0)
            turn_meta["intent"] = analysis.get("intent")

            extra_ctx = analysis_to_context(analysis)
            ctx = ", ".join(p for p in (base_ctx, extra_ctx) if p)

            if log_user:
                await emit(type="message", role="user", text=prompt)
            # (기획서 v2.0) 사용자 발화를 장기 메모리에 기록. log_user=False 인 경우는
            # 환영 인사처럼 시스템이 만든 prompt 라 메모리 기록 대상 아님.
            user_msg_id: Optional[int] = None
            if log_user:
                try:
                    user_msg_id = session.memory.add_message(
                        session.get_conv_id(), "user", prompt,
                    )
                except Exception:
                    traceback.print_exc()
                # 자동 사실 추출 + recall/learned 신호 emit (UI 헤더 점등)
                await _learn_and_signal(prompt, user_msg_id)

            # 폴백 알림 큐 — 메인 루프에서만 emit (스레드 안전)
            fallback_events = []
            def on_fallback(from_b, to_b, reason):
                fallback_events.append((from_b, to_b, reason))

            # 스트리밍 브릿지 (sync generator → async WebSocket)
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            t_llm_start = time.monotonic()

            def run_stream():
                try:
                    gen = session.brain.think_stream_with_fallback(
                        prompt, ctx, on_fallback=on_fallback,
                    )
                    for item in gen:
                        loop.call_soon_threadsafe(queue.put_nowait, item)
                except Exception as exc:
                    traceback.print_exc()
                    from .emotion import Emotion as _E
                    # 사이클 #6 핫픽스: raw 영문 예외 대신 친절 한국어 안내
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        (None, _E.CONCERNED, _friendly_error(exc, cfg.llm_backend)),
                    )
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

            threading.Thread(target=run_stream, daemon=True).start()

            await emit(type="stream_start")
            final_text = ""
            final_emotion = "neutral"
            announced_fallbacks = 0

            while True:
                item = await queue.get()
                if item is None:
                    break

                # 새 폴백 이벤트가 있으면 사용자에게 알림
                while announced_fallbacks < len(fallback_events):
                    f_from, f_to, _r = fallback_events[announced_fallbacks]
                    announced_fallbacks += 1
                    turn_meta["fallback_used"] = True
                    if f_to not in turn_meta["fallback_chain"]:
                        turn_meta["fallback_chain"].append(f_to)
                    await emit(
                        type="backend_fallback",
                        from_backend=f_from,
                        to_backend=f_to,
                        message=f"⤴ {f_from.upper()} 응답 실패 — {f_to.upper()} 로 자동 전환",
                    )

                chunk, emo, body = item
                if emo is not None:
                    final_text = body or ""
                    final_emotion = emo.value
                    await emit(type="stream_end", text=final_text, emotion=final_emotion)
                elif chunk:
                    await emit(type="stream_chunk", text=chunk)

            turn_meta["llm_ms"] = (time.monotonic() - t_llm_start) * 1000.0
            turn_meta["emotion"] = final_emotion
            turn_meta["reply_len"] = len(final_text)

            # (기획서 v2.0) 어시스턴트 응답을 장기 메모리에 기록.
            if final_text:
                try:
                    session.memory.add_message(
                        session.get_conv_id(), "assistant", final_text,
                        emotion=final_emotion,
                    )
                except Exception:
                    traceback.print_exc()

            # 사이클 #22 (HARN-12): 사용자 피드백 부착 대상 cmd 행 기록 + cmd_id emit.
            # PII 본문은 commands.command_text/response_text 에만 저장 (DB 로컬), telemetry 는
            # 길이만. UI 가 cmd_id 를 받아 응답 메시지 옆 👍/👎 버튼을 그린다.
            if log_user and final_text:
                try:
                    cmd_id = await asyncio.to_thread(
                        session.memory.log_command,
                        session.memory_user_id, prompt,
                        turn_meta.get("input_channel", "text"),
                        None, session.get_conv_id(), "done",
                        {"emotion": final_emotion,
                         "backend": cfg.llm_backend,
                         "intent": turn_meta.get("intent"),
                         "fallback_used": bool(turn_meta.get("fallback_used"))},
                    )
                    await asyncio.to_thread(
                        session.memory.update_command, cmd_id,
                        response_text=final_text,
                    )
                    turn_meta["cmd_id"] = int(cmd_id)
                    await emit(type="turn_logged", cmd_id=int(cmd_id))
                except Exception:
                    traceback.print_exc()

            await emit(type="emotion", emotion=final_emotion)
            await emit(type="state", state="speaking")

            # ── Generate-Verify TTS 게이트 (regen 폴백 포함, 사이클 #3 #1) ─
            # (기획서 v1.5) 스트리밍 TTS: 첫 문장 / 나머지 두 청크 병렬 합성.
            # 사용자 체감 응답속도 ↑ — 첫 청크가 짧아 빠르게 합성·재생되는 동안
            # 나머지가 백그라운드에서 합성 진행. WebSocket 순서 보장으로 재생 순서는 안전.
            t_tts_start = time.monotonic()
            def _tts_regen(orig: str, reason: str) -> str:
                # 별도 스레드에서 호출됨 → brain 재호출은 sync OK
                return session.brain.regenerate_safe_tts(orig, reason)
            head_text, tail_text = _split_first_sentence(final_text)
            tts_chunks_ok = 0
            tts_first_reason: Optional[str] = None
            tts_any_regenerated = False
            if tail_text:
                # 두 청크 분리됨 — 클라이언트에 미리 카운트 알려서 마지막 청크의
                # ttsAudio.onended 에서만 연속 대화 모드 자동 마이크 트리거되도록.
                await emit(type="tts_chunk_count", count=2)
                head_task = asyncio.create_task(asyncio.to_thread(
                    TTS.synthesize_bytes_verified, head_text, _tts_regen,
                ))
                tail_task = asyncio.create_task(asyncio.to_thread(
                    TTS.synthesize_bytes_verified, tail_text, _tts_regen,
                ))
                head_res: Optional[dict] = None
                tail_res: Optional[dict] = None
                try:
                    # head 가 끝나는 즉시 emit (tail 은 백그라운드 합성 계속)
                    head_res = await head_task
                    tts_first_reason = head_res["reason"]
                    tts_any_regenerated = tts_any_regenerated or bool(head_res.get("regenerated"))
                    if head_res["audio"]:
                        await emit_bytes(head_res["audio"])
                        tts_chunks_ok += 1
                    elif not head_res["ok"]:
                        await emit(
                            type="tts_blocked",
                            reason=head_res["reason"],
                            message="음성 합성이 안전 검증에서 차단되었습니다 (텍스트만 표시).",
                        )
                    tail_res = await tail_task
                    tts_any_regenerated = tts_any_regenerated or bool(tail_res.get("regenerated"))
                    if tail_res["audio"]:
                        await emit_bytes(tail_res["audio"])
                        tts_chunks_ok += 1
                    elif not tail_res["ok"]:
                        # tail 만 차단된 경우 — head 는 이미 재생됨, tail 안내만
                        await emit(
                            type="tts_blocked",
                            reason=tail_res["reason"],
                            message="응답 후반부 음성 합성이 차단되었습니다 (텍스트만 표시).",
                        )
                finally:
                    # (P0) head_task 가 예외를 던지면 tail_task 가 누수될 수 있음.
                    # 미완료 task 는 cancel + drain 으로 안전 정리.
                    for _t in (head_task, tail_task):
                        if not _t.done():
                            _t.cancel()
                            try:
                                await _t
                            except (asyncio.CancelledError, Exception):
                                pass
                # 텔레메트리 호환: 둘 중 하나라도 OK 면 ok=True, reason 은 첫 청크 기준
                tts_ok = bool((head_res and head_res["ok"]) or (tail_res and tail_res["ok"]))
                tts_reason = tts_first_reason or (tail_res["reason"] if tail_res else "exception")
                turn_meta["tts_streamed"] = True
            else:
                # 단일 합성 (기존 동작) — count 메시지 안 보냄 → 클라이언트는 기본 0
                res = await asyncio.to_thread(
                    TTS.synthesize_bytes_verified, final_text, _tts_regen,
                )
                tts_any_regenerated = bool(res.get("regenerated"))
                if res["audio"]:
                    await emit_bytes(res["audio"])
                    tts_chunks_ok += 1
                elif not res["ok"]:
                    await emit(
                        type="tts_blocked",
                        reason=res["reason"],
                        message="음성 합성이 안전 검증에서 차단되었습니다 (텍스트만 표시).",
                    )
                tts_ok = res["ok"]
                tts_reason = res["reason"]
                turn_meta["tts_streamed"] = False
            turn_meta["tts_ms"] = (time.monotonic() - t_tts_start) * 1000.0
            turn_meta["tts_ok"] = tts_ok
            turn_meta["tts_reason"] = tts_reason
            turn_meta["tts_regenerated"] = tts_any_regenerated
            turn_meta["tts_chunks"] = tts_chunks_ok
        except Exception as e:
            traceback.print_exc()
            turn_meta["error"] = type(e).__name__
            await emit(type="error", message=_friendly_error(e, cfg.llm_backend))
        finally:
            await emit(type="state", state="idle")
            await emit(type="emotion", emotion="neutral")
            try:
                turn_meta["total_ms"] = (time.monotonic() - t_turn_start) * 1000.0
                turn_meta.update(session.turn_pillar_meta())  # 사이클 #9
                telemetry.log_turn(turn_meta)
            except Exception:
                traceback.print_exc()

    async def respond_compare(prompt: str):
        """A/B 비교 모드 — Claude + OpenAI 동시 스트리밍, TTS 자동재생 안 함.

        사이클 #3 #4: 텔레메트리 기록 추가 (backend="compare", source 별 reply_len 합산).
        """
        # 사이클 #9 — 3-Pillar telemetry: turn 진입 시 카운터 초기화.
        session.reset_turn_counters()
        await emit(type="state", state="thinking")
        await emit(type="emotion", emotion="thinking")
        await emit(type="message", role="user", text=prompt)

        # 텔레메트리 메타 — compare 모드 별도 기록
        turn_meta = {
            "turn_id": telemetry.new_turn_id(),
            "ts": time.time(),
            "input_channel": "text",  # compare 는 텍스트 입력 전용 (사이클 #4 T001)
            "backend": "compare",
            "fallback_used": False,
            "fallback_chain": ["compare:claude+openai"],
            "intent": None,
            "emotion": None,
            "tools_used": 0,
            "fanout_ms": 0.0,
            "llm_ms": 0.0,
            "tts_ms": 0.0,
            "total_ms": 0.0,
            "tts_ok": None,
            "tts_reason": "compare_no_tts",
            "prompt_len": len(prompt or ""),
            "reply_len": 0,
            "compare_sources": [],
        }
        t_turn_start = time.monotonic()  # 사이클 #5 T001
        t_llm_start = t_turn_start

        try:
            # Fan-out 분석 — compare 도 동일하게 실행 (intent 분포 통계용)
            base_ctx = build_context(query=prompt)
            analysis = await parallel_analyze(prompt, session)
            turn_meta["fanout_ms"] = analysis.get("ms", 0.0)
            turn_meta["intent"] = analysis.get("intent")
            extra_ctx = analysis_to_context(analysis)
            ctx = ", ".join(p for p in (base_ctx, extra_ctx) if p)
            # (기획서 v2.0) compare 모드도 사용자 발화를 메모리에 기록.
            user_msg_id_cmp: Optional[int] = None
            try:
                user_msg_id_cmp = session.memory.add_message(
                    session.get_conv_id(), "user", prompt,
                )
            except Exception:
                traceback.print_exc()
            await _learn_and_signal(prompt, user_msg_id_cmp)

            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def run_stream():
                try:
                    for item in session.brain.compare_stream(prompt, ctx):
                        loop.call_soon_threadsafe(queue.put_nowait, item)
                except Exception as exc:
                    traceback.print_exc()
                    from .emotion import Emotion as _E
                    # 사이클 #6 핫픽스: compare 모드도 raw 예외 대신 친절 안내
                    # Task #19: run_stream 가 예외를 흡수해 system 안내로 변환하더라도
                    # 텔레메트리에는 error 타입을 남겨 /api/harness/telemetry 가 백엔드
                    # 장애를 인식할 수 있게 한다 (사용자 UX 와 별개로 진화 입력 보존).
                    turn_meta.setdefault("error", type(exc).__name__)
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        ("system", None, _E.CONCERNED, _friendly_error(exc, "claude")),
                    )
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, None)

            threading.Thread(target=run_stream, daemon=True).start()

            await emit(type="compare_start", sources=["claude", "openai"])
            finals: dict = {}

            while True:
                item = await queue.get()
                if item is None:
                    break
                source, chunk, emo, body = item
                if emo is not None:
                    finals[source] = {"text": body or "", "emotion": emo.value}
                    if source not in turn_meta["compare_sources"]:
                        turn_meta["compare_sources"].append(source)
                    await emit(
                        type="compare_end",
                        source=source,
                        text=body or "",
                        emotion=emo.value,
                    )
                elif chunk:
                    await emit(type="compare_chunk", source=source, text=chunk)

            # (기획서 v2.0) compare 모드의 두 백엔드 응답을 모두 메모리에 기록 —
            # role="assistant" + emotion 에 source 표시(예: "neutral|claude").
            for src, res in finals.items():
                txt = (res.get("text") or "").strip()
                if not txt:
                    continue
                try:
                    session.memory.add_message(
                        session.get_conv_id(), "assistant", txt,
                        emotion=f"{res.get('emotion','neutral')}|{src}",
                    )
                except Exception:
                    traceback.print_exc()

            await emit(type="compare_done")
            await emit(type="emotion", emotion="neutral")

            # reply_len = 두 응답 길이의 합 (PII 본문은 저장 안 함)
            turn_meta["reply_len"] = sum(len(v.get("text", "")) for v in finals.values())
            # emotion = 첫 번째 소스 기준 (대표값)
            for v in finals.values():
                turn_meta["emotion"] = v.get("emotion")
                break
            turn_meta["llm_ms"] = (time.monotonic() - t_llm_start) * 1000.0
        except Exception as e:
            traceback.print_exc()
            turn_meta["error"] = type(e).__name__
            # compare 모드는 두 백엔드 동시 — 일반 안내로 변환 (compare 라벨)
            await emit(type="error", message=_friendly_error(e, "claude"))
        finally:
            await emit(type="state", state="idle")
            try:
                turn_meta["total_ms"] = (time.monotonic() - t_turn_start) * 1000.0
                turn_meta.update(session.turn_pillar_meta())  # 사이클 #9
                telemetry.log_turn(turn_meta)
            except Exception:
                traceback.print_exc()

    def build_context(query: Optional[str] = None) -> str:
        parts = []
        if session.observing:
            parts.append("행동 모니터링 활성")
        if session._last_observation:
            parts.append(f"최근 관찰: {session._last_observation}")
        # (기획서 v2.0) 저장된 사실/관련 과거 발언을 system prompt 에 주입.
        # query 가 있으면 해당 키워드로 과거 메시지 LIKE/의미 검색 — 빈 결과면 아무것도 추가 안 함.
        try:
            mem_block = session.memory.context_block(session.memory_user_id, query=query)
            if mem_block:
                parts.append(mem_block)
                # 클라이언트 헤더 메모리 인디케이터를 잠깐 점등 — recall 신호.
                session._last_recall = True
            else:
                session._last_recall = False
        except Exception:
            traceback.print_exc()
        return ", ".join(parts)

    async def _learn_and_signal(prompt: str, user_msg_id: Optional[int]) -> None:
        """사용자 발화에서 자기소개성 사실을 자동 추출해 facts 에 upsert.
        성공 시 클라이언트로 'memory_event' 신호 → 헤더 인디케이터 점등.
        recall (build_context 가 [기억] 블록 주입) 도 같은 채널로 알린다.
        """
        try:
            if getattr(session, "_last_recall", False):
                await emit(type="memory_event", kind="recall")
                session._last_recall = False
        except Exception:
            traceback.print_exc()
        try:
            pairs = extract_user_facts(prompt or "")
            if not pairs:
                return
            learned = []
            for k, v in pairs:
                try:
                    session.memory.upsert_fact(
                        session.memory_user_id, k, v, source_msg_id=user_msg_id,
                    )
                    learned.append({"key": k, "value": v})
                except Exception:
                    traceback.print_exc()
            if learned:
                await emit(type="memory_event", kind="learned", facts=learned)
        except Exception:
            traceback.print_exc()

    # 사이클 #18 — 인증 통과 전엔 환영 인사를 시작하지 않는다.
    welcome_task: Optional[asyncio.Task] = None

    def _start_welcome_if_authed():
        nonlocal welcome_task
        if auth_state["welcome_started"] or not _is_authed():
            return
        auth_state["welcome_started"] = True
        welcome_task = asyncio.create_task(welcome())

    _start_welcome_if_authed()

    async def _preempt_welcome():
        """사용자 입력이 도착하면 진행 중인 환영 인사를 즉시 취소.

        - 환영 인사가 busy lock 을 점유 중이라 사용자 입력이 폐기되는 회귀 차단.
        - 환영 인사가 본 응답보다 늦게 도착해 대화 순서가 뒤집히는 회귀 차단.
        - 사이클 #18: welcome 이 아직 시작 안 됐을 수도 있다 (미인증 상태) — None 가드.
        """
        if welcome_task is None or welcome_task.done():
            return
        welcome_task.cancel()
        try:
            await welcome_task
        except (asyncio.CancelledError, Exception):
            pass

    # ── 사이클 #18: 인증 게이트 헬퍼 ────────────────────────────────
    # 등록된 상태에서 인증 미완료일 때 허용되는 JSON 메시지 화이트리스트.
    # (등록/리셋/상태 조회 + 모델 카탈로그 같은 무해한 메타 호출만)
    # 사이클 #20 (architect 지적) — `enroll_owner` 는 화이트리스트에서 제외.
    # 이미 등록된 시스템에서는 비인증자가 owner 재등록으로 계정 탈취가 가능했음.
    # 재등록은 명시적 `auth_reset` (등록 정보 삭제) 후에만 허용한다.
    PRE_AUTH_ALLOWED_TYPES = {
        "enroll_owner",  # 미등록 시점에서만 의미 있음 — 핸들러 내부에서 추가 검증.
        "auth_reset", "auth_status_request",
        "auth_new_challenge",  # 사이클 #20 — 챌린지 재발급 요청
        "models_list", "list_faces",
    }

    async def _do_voice_login(audio_bytes: bytes) -> None:
        """0x02 음성 로그인 시도 — STT → 패스프레이즈 매칭.

        성공 시 voice_ok=True 마킹 + auth_progress emit. 인증 완료되면 welcome 시작.
        실패 시 친절 안내. 무음/짧은 발화는 silent skip (재시도 유도).
        """
        if not OWNER_AUTH.is_enrolled():
            return
        if STT is None:
            await emit(
                type="error",
                message="음성 인식이 아직 준비되지 않았습니다. 잠시 후 다시 시도하세요.",
            )
            return
        # 임시 파일 → STT.transcribe.
        suffix = ".webm"
        fd, path = tempfile.mkstemp(prefix="sarvis_login_", suffix=suffix)
        try:
            os.close(fd)
            with open(path, "wb") as f:
                f.write(audio_bytes)
            try:
                text = await asyncio.to_thread(STT.transcribe, path, "")
            except Exception as e:
                print(f"[auth voice login] STT 실패: {e}")
                await emit(type="error", message="음성 인식에 실패했습니다. 다시 말씀해주세요.")
                return
            text = clean_stt_text(text or "")
            if not text or len(text) < 2:
                # 짧은 잡음/환각 → 사용자에게 다시 안내 (silent skip 아님 — 인증 단계는
                # 사용자에게 진행 상태를 명확히 보여줘야 함).
                await emit(
                    type="auth_progress",
                    face_ok=auth_state["face_ok"],
                    voice_ok=auth_state["voice_ok"],
                    voice_attempt_text="",
                    voice_attempt_ok=False,
                    message="음성이 너무 짧거나 명확하지 않습니다. 다시 말씀해주세요.",
                )
                return
            challenge = auth_state.get("current_challenge")
            # 사이클 #20 — 챌린지 강제 (architect 지적 — challenge 활성 시 passphrase
            # OR 우회는 챌린지 도입 목적과 충돌). 챌린지 활성이면 verify_voice 에
            # passphrase 인자를 막기 위해 challenge 만 전달하고, OwnerAuth 내부 OR
            # 로직을 우회하는 strict path 를 사용.
            if challenge:
                # strict — 챌린지 매칭만 허용.
                from difflib import SequenceMatcher as _SM
                from .owner_auth import (
                    normalize_voice as _norm,
                    VOICE_MATCH_THRESHOLD as _TH,
                )
                spoken_n = _norm(text)
                chal_n = _norm(challenge)
                if spoken_n and chal_n and spoken_n == chal_n:
                    sim = 1.0
                elif spoken_n and chal_n:
                    sim = _SM(None, spoken_n, chal_n).ratio()
                else:
                    sim = 0.0
                ok = sim >= _TH
                matched_against = "challenge" if ok else ""
            else:
                # 챌린지 미발급 (미등록 또는 발급 실패) — passphrase 만 평가.
                ok, sim, matched_against = OWNER_AUTH.verify_voice(text, challenge_text=None)

            if ok:
                auth_state["voice_ok"] = True
                # 챌린지는 1회용 — 통과/실패 무관하게 다음 시도를 위해 새로 발급.
                _refresh_challenge()
                msg = "챌린지 문장 일치 — 음성 인증 통과"
            else:
                # 실패 시도 챌린지 폐기 + 재발급 (재시도 표면 축소 — architect 지적).
                _refresh_challenge()
                msg = (
                    f"챌린지 문장과 일치하지 않습니다 (유사도 {sim:.2f}). "
                    f"새로 발급된 문장을 또렷하게 말씀해주세요."
                )
            await emit(
                type="auth_progress",
                face_ok=auth_state["face_ok"],
                voice_ok=auth_state["voice_ok"],
                blink_ok=auth_state["blink_ok"],
                voice_attempt_text=text,
                voice_attempt_ok=ok,
                voice_similarity=round(sim, 3),
                voice_matched_against=matched_against or "",
                challenge=auth_state.get("current_challenge"),
                message=msg,
            )
            if _is_authed() and not auth_state["completed_emitted"]:
                auth_state["completed_emitted"] = True
                await emit(type="auth_complete", face_name=OWNER_AUTH.face_name)
                _start_welcome_if_authed()
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    async def _try_face_login(jpeg_bytes: bytes) -> None:
        """0x01 프레임으로 얼굴 매치 + 라이브니스(눈 깜빡임) 시도.

        사이클 #20 — 단순 인코딩 매칭만으론 인쇄 사진 위조에 취약. 매 프레임
        EAR 를 누적해 윈도우 내 (open → close → open) 깜빡임이 1회 이상
        검출되어야 face_ok = True. `blink_required` 는 세션 시작 시 capability
        probe 로 결정되며, EAR 추출이 일시적으로 실패한다고 영구 우회되지 않음
        (architect 지적). 일반 깜빡임(150~300ms)을 잡기 위해 throttle 0.18s.
        """
        if not OWNER_AUTH.is_enrolled() or auth_state["face_ok"]:
            return
        now = time.time()
        if now - auth_state["last_face_attempt"] < 0.18:
            return
        auth_state["last_face_attempt"] = now

        # 1) 얼굴 매칭 (인코딩 또는 박스 폴백).
        matched = False
        degraded = False
        if OWNER_AUTH.has_face_encoding:
            enc = await asyncio.to_thread(
                compute_face_encoding_from_jpeg, jpeg_bytes,
            )
            if enc and OWNER_AUTH.verify_face_encoding(enc):
                matched = True
        else:
            # 폴백: 등록 시 인코딩이 저장되지 않은 환경 (face_recognition 미설치).
            if session.vision.face_boxes:
                matched = True
                degraded = True

        if not matched:
            return

        # 2) 라이브니스 — EAR 시계열 누적 + 깜빡임 검출.
        if not auth_state["blink_required"]:
            # capability probe 가 미지원 판정한 환경 — 라이브니스 우회.
            auth_state["blink_ok"] = True
            blink_msg = "라이브니스 우회 (landmarks 미지원 환경)"
        else:
            ear = await asyncio.to_thread(
                compute_eye_aspect_ratio_from_jpeg, jpeg_bytes,
            )
            if ear is not None:
                samples = auth_state["ear_samples"]
                samples.append((now, ear))
                # 윈도우 밖 샘플 정리.
                cutoff = now - BLINK_WINDOW_SECONDS
                while samples and samples[0][0] < cutoff:
                    samples.pop(0)
                blinked, stats = detect_blink_in_window(samples)
                if blinked:
                    auth_state["blink_ok"] = True
                    blink_msg = "눈 깜빡임 감지 — 라이브니스 통과"
                else:
                    auth_state["blink_ok"] = False
                    blink_msg = (
                        f"눈을 한 번 깜빡여 주세요 "
                        f"(샘플 {stats['count']}, EAR {stats['min']:.2f}~{stats['max']:.2f})"
                    )
            else:
                # 일시적 landmarks 추출 실패 — blink_required 는 유지, 사용자에게만 안내.
                auth_state["blink_ok"] = False
                blink_msg = "얼굴이 잘 보이게 정면을 봐주세요 (눈 인식 일시 실패)"

        # 라이브니스 통과 전에는 face_ok 도 보류 — 둘 다 통과해야 face_ok=True.
        if auth_state["blink_ok"]:
            auth_state["face_ok"] = True

        await emit(
            type="auth_progress",
            face_ok=auth_state["face_ok"],
            voice_ok=auth_state["voice_ok"],
            blink_ok=auth_state["blink_ok"],
            face_match_ok=True,
            degraded=degraded,
            message=(
                ("얼굴 인증 통과" if not degraded else "얼굴 인증 통과 (간이 모드)")
                + " · " + blink_msg
            ),
        )
        if _is_authed() and not auth_state["completed_emitted"]:
            auth_state["completed_emitted"] = True
            await emit(type="auth_complete", face_name=OWNER_AUTH.face_name)
            _start_welcome_if_authed()

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            # 바이너리 — 카메라 프레임 또는 오디오
            if "bytes" in msg and msg["bytes"] is not None:
                # 첫 바이트 매직: 0x01 = 카메라 프레임(JPEG), 0x02 = 오디오(WebM)
                data = msg["bytes"]
                if not data:
                    continue
                kind = data[0]
                payload = data[1:]
                if kind == 0x01:
                    # 사이클 #28 — 첫 카메라 프레임에서 제스처 콜백 부착(아이딤포턴트).
                    # 콜백은 vision 의 GestureDetector 워커 스레드에서 호출되므로,
                    # 메인 이벤트 루프에 안전하게 emit 을 스케줄.
                    if getattr(session.vision, "_gesture_callback", None) is None:
                        _loop = asyncio.get_event_loop()
                        def _on_gesture(ev, _loop=_loop):
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    emit(type="gesture", name=ev.name,
                                         confidence=float(ev.confidence)),
                                    _loop,
                                )
                            except Exception:
                                pass
                        try:
                            session.vision.attach_gesture_callback(_on_gesture)
                        except Exception as _e:
                            print(f"[server] 제스처 콜백 부착 실패: {_e}")
                    detected = session.vision.push_jpeg(payload)
                    if detected:
                        fw, fh = session.vision.get_frame_size()
                        boxes = [list(b) for b in session.vision.face_boxes]
                        await emit(type="faces", boxes=boxes, fw=fw, fh=fh)
                    # 사이클 #18 — 미인증 상태면 매 프레임을 얼굴 매치에 사용.
                    if OWNER_AUTH.is_enrolled() and not auth_state["face_ok"]:
                        await _try_face_login(payload)
                elif kind == 0x02:
                    # 사이클 #18 — 인증 미완료면 음성 데이터를 로그인 시도로 처리.
                    if OWNER_AUTH.is_enrolled() and not _is_authed():
                        if not auth_state["voice_ok"]:
                            async with busy:
                                await _do_voice_login(payload)
                        continue
                    await _preempt_welcome()
                    if busy.locked():
                        continue
                    async with busy:
                        await handle_audio(payload, emit, emit_bytes, session, build_context, _learn_and_signal)
                elif kind in (0x06, 0x07, 0x08):
                    # 사이클 #16: 학습 지식 첨부 미디어 저장.
                    # 페이로드 포맷은 0x03..0x05 와 동일:
                    #   <caption_len:2 BE><caption_utf8><blob>
                    # caption 은 content (자유 서술) — topic/source/tags 는
                    # 이후 knowledge_update 메시지로 보강할 수 있다.
                    if len(payload) < 2:
                        continue
                    clen = int.from_bytes(payload[:2], "big")
                    if len(payload) < 2 + clen:
                        continue
                    try:
                        caption = payload[2:2 + clen].decode("utf-8", errors="replace")
                    except Exception:
                        caption = ""
                    blob = payload[2 + clen:]
                    if not blob:
                        continue
                    if kind == 0x06:
                        ext, slot = ".jpg", "image"
                    elif kind == 0x07:
                        ext, slot = ".webm", "audio"
                    else:  # 0x08
                        ext, slot = ".webm", "video"
                    try:
                        kid = await asyncio.to_thread(
                            functools.partial(
                                session.memory.add_knowledge,
                                user_id=session.memory_user_id,
                                content=caption,
                                source="user",
                            )
                        )
                        media_path = os.path.join(KNOWLEDGE_DIR, f"{kid}{ext}")
                        await asyncio.to_thread(_write_bytes, media_path, blob)
                        await asyncio.to_thread(
                            functools.partial(
                                session.memory.update_knowledge,
                                kid, **{f"{slot}_path": media_path},
                            )
                        )
                        await emit(
                            type="knowledge_saved", id=kid, content=caption,
                            has_image=(slot == "image"),
                            has_audio=(slot == "audio"),
                            has_video=(slot == "video"),
                        )
                    except Exception as e:
                        print(f"[WS 0x{kind:02x} knowledge media save] {e}")
                        await emit(type="error", message="학습 자료를 저장하지 못했습니다.")
                    continue
                elif kind in (0x03, 0x04, 0x05):
                    # 멀티모달 명령 미디어 저장.
                    # 페이로드 형식: <caption_len:2 BE><caption_utf8><media bytes>
                    #   0x03 = 이미지(JPEG, .jpg, kind=image|multimodal)
                    #   0x04 = 음성(WebM, .webm,  kind=audio)
                    #   0x05 = 영상(WebM, .webm,  kind=video)
                    if len(payload) < 2:
                        continue
                    clen = int.from_bytes(payload[:2], "big")
                    if len(payload) < 2 + clen:
                        continue
                    try:
                        caption = payload[2:2 + clen].decode("utf-8", errors="replace")
                    except Exception:
                        caption = ""
                    blob = payload[2 + clen:]
                    if not blob:
                        continue
                    if kind == 0x03:
                        ext, slot, k = ".jpg", "image", ("multimodal" if caption else "image")
                    elif kind == 0x04:
                        ext, slot, k = ".webm", "audio", "audio"
                    else:  # 0x05
                        ext, slot, k = ".webm", "video", "video"
                    try:
                        # 키워드 인자 사용 — 시그니처 변경에도 깨지지 않도록.
                        cmd_id = await asyncio.to_thread(
                            functools.partial(
                                session.memory.log_command,
                                user_id=session.memory_user_id,
                                command_text=caption,
                                kind=k,
                                status="done",
                            )
                        )
                        media_path = os.path.join(COMMANDS_DIR, f"{cmd_id}{ext}")
                        await asyncio.to_thread(_write_bytes, media_path, blob)
                        upd_kwargs: Dict[str, Any] = {f"{slot}_path": media_path}
                        await asyncio.to_thread(
                            functools.partial(
                                session.memory.update_command, cmd_id, **upd_kwargs,
                            )
                        )
                        await emit(
                            type="command_saved",
                            id=cmd_id, kind=k, caption=caption,
                            has_image=(slot == "image"),
                            has_audio=(slot == "audio"),
                            has_video=(slot == "video"),
                        )
                    except Exception as e:
                        print(f"[WS 0x{kind:02x} command media save] {e}")
                        await emit(type="error", message="명령 미디어를 저장하지 못했습니다.")
                    continue
                elif kind == 0x09:
                    if OWNER_AUTH.is_enrolled() and not _is_authed():
                        continue
                    if not payload or len(payload) < 6:
                        continue
                    try:
                        dur_bytes = payload[:4]
                        duration_ms = int.from_bytes(dur_bytes, "big")
                        label_len = int.from_bytes(payload[4:6], "big")
                        label = payload[6:6 + label_len].decode("utf-8", errors="replace") if label_len else ""
                        blob = payload[6 + label_len:]
                        if not blob:
                            continue
                        ts = time.strftime("%Y%m%d_%H%M%S")
                        ms = int(time.time() * 1000) % 1000
                        safe_label = re.sub(r'[^\w가-힣-]', '_', label)[:30] if label else ""
                        fname = f"{ts}_{ms:03d}_{safe_label}.webm" if safe_label else f"{ts}_{ms:03d}.webm"
                        user_dir = os.path.join(RECORDINGS_DIR, session.memory_user_id)
                        os.makedirs(user_dir, exist_ok=True)
                        fpath = os.path.join(user_dir, fname)
                        await asyncio.to_thread(_write_bytes, fpath, blob)
                        rec_id = await asyncio.to_thread(
                            functools.partial(
                                session.memory.save_recording,
                                user_id=session.memory_user_id,
                                filename=fname,
                                file_path=fpath,
                                kind="video",
                                label=label,
                                duration_ms=duration_ms,
                                size_bytes=len(blob),
                            )
                        )
                        size_mb = len(blob) / (1024 * 1024)
                        dur_s = duration_ms / 1000
                        await emit(
                            type="recording_saved",
                            id=rec_id,
                            filename=fname,
                            label=label,
                            kind="video",
                            duration_s=round(dur_s, 1),
                            size_mb=round(size_mb, 2),
                        )
                        print(f"[녹화 저장] {fname} ({size_mb:.1f}MB, {dur_s:.1f}s)")
                    except Exception as e:
                        print(f"[WS 0x09 recording save] {e}")
                        await emit(type="error", message="녹화 파일을 저장하지 못했습니다.")
                    continue
                elif kind == 0x0B:
                    if OWNER_AUTH.is_enrolled() and not _is_authed():
                        continue
                    if not payload or len(payload) < 2:
                        continue
                    try:
                        label_len = int.from_bytes(payload[:2], "big")
                        label = payload[2:2 + label_len].decode("utf-8", errors="replace") if label_len else ""
                        blob = payload[2 + label_len:]
                        if not blob:
                            continue
                        ts = time.strftime("%Y%m%d_%H%M%S")
                        ms = int(time.time() * 1000) % 1000
                        safe_label = re.sub(r'[^\w가-힣-]', '_', label)[:30] if label else ""
                        fname = f"photo_{ts}_{ms:03d}_{safe_label}.jpg" if safe_label else f"photo_{ts}_{ms:03d}.jpg"
                        user_dir = os.path.join(RECORDINGS_DIR, session.memory_user_id)
                        os.makedirs(user_dir, exist_ok=True)
                        fpath = os.path.join(user_dir, fname)
                        await asyncio.to_thread(_write_bytes, fpath, blob)
                        rec_id = await asyncio.to_thread(
                            functools.partial(
                                session.memory.save_recording,
                                user_id=session.memory_user_id,
                                filename=fname,
                                file_path=fpath,
                                kind="photo",
                                label=label,
                                duration_ms=0,
                                size_bytes=len(blob),
                            )
                        )
                        size_kb = len(blob) / 1024
                        await emit(
                            type="recording_saved",
                            id=rec_id,
                            filename=fname,
                            label=label,
                            kind="photo",
                            duration_s=0,
                            size_mb=round(size_kb / 1024, 2),
                        )
                        print(f"[사진 저장] {fname} ({size_kb:.0f}KB)")
                    except Exception as e:
                        print(f"[WS 0x0B photo save] {e}")
                        await emit(type="error", message="사진을 저장하지 못했습니다.")
                    continue
                elif kind == 0x0A:
                    if OWNER_AUTH.is_enrolled() and not _is_authed():
                        continue
                    if not payload or len(payload) < 6:
                        continue
                    try:
                        duration_ms = int.from_bytes(payload[:4], "big")
                        label_len = int.from_bytes(payload[4:6], "big")
                        label = payload[6:6 + label_len].decode("utf-8", errors="replace") if label_len else ""
                        blob = payload[6 + label_len:]
                        if not blob:
                            continue
                        ts = time.strftime("%Y%m%d_%H%M%S")
                        ms = int(time.time() * 1000) % 1000
                        safe_label = re.sub(r'[^\w가-힣-]', '_', label)[:30] if label else ""
                        fname = f"audio_{ts}_{ms:03d}_{safe_label}.webm" if safe_label else f"audio_{ts}_{ms:03d}.webm"
                        user_dir = os.path.join(RECORDINGS_DIR, session.memory_user_id)
                        os.makedirs(user_dir, exist_ok=True)
                        fpath = os.path.join(user_dir, fname)
                        await asyncio.to_thread(_write_bytes, fpath, blob)
                        rec_id = await asyncio.to_thread(
                            functools.partial(
                                session.memory.save_recording,
                                user_id=session.memory_user_id,
                                filename=fname,
                                file_path=fpath,
                                kind="audio",
                                label=label,
                                duration_ms=duration_ms,
                                size_bytes=len(blob),
                            )
                        )
                        size_mb = len(blob) / (1024 * 1024)
                        dur_s = duration_ms / 1000
                        await emit(
                            type="recording_saved",
                            id=rec_id,
                            filename=fname,
                            label=label,
                            kind="audio",
                            duration_s=round(dur_s, 1),
                            size_mb=round(size_mb, 2),
                        )
                        print(f"[녹음 저장] {fname} ({size_mb:.1f}MB, {dur_s:.1f}s)")
                    except Exception as e:
                        print(f"[WS 0x0A audio recording save] {e}")
                        await emit(type="error", message="녹음 파일을 저장하지 못했습니다.")
                    continue
                continue

            # 텍스트 (JSON)
            text = msg.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue

            mtype = data.get("type")

            # ── 사이클 #18: 인증 게이트 ────────────────────────────────
            # 등록된 상태에서 인증 미완료면 화이트리스트 외 모든 메시지 차단.
            if (
                OWNER_AUTH.is_enrolled()
                and not _is_authed()
                and mtype not in PRE_AUTH_ALLOWED_TYPES
            ):
                await emit(
                    type="auth_required",
                    face_ok=auth_state["face_ok"],
                    voice_ok=auth_state["voice_ok"],
                    message="먼저 주인 인증을 완료해주세요 (얼굴 + 음성).",
                )
                continue

            if mtype == "auth_status_request":
                await _emit_auth_status()
                continue

            if mtype == "auth_new_challenge":
                # 사이클 #20 — 사용자가 챌린지를 다시 받고 싶을 때 (잘 안 들렸을 때 등).
                _refresh_challenge()
                await _emit_auth_status()
                continue

            if mtype == "auth_reset":
                # 등록 해제 + 세션 인증 상태 초기화. 재등록을 위한 명시적 리셋.
                # P0 보안 (architect 2차 지적): auth_reset 도 owner takeover 의
                # 우회 경로였다 — 비인증자가 auth_reset → enroll_owner 연쇄로
                # 계정 탈취 가능. 등록된 시스템에서는 _is_authed() 인증된
                # 본인만 reset 허용.
                if OWNER_AUTH.is_enrolled() and not _is_authed():
                    await emit(
                        type="auth_reset_ok", ok=False,
                        message=(
                            "주인 인증이 완료된 상태에서만 등록을 초기화할 수 "
                            "있습니다. 얼굴/음성 인증을 먼저 완료해주세요."
                        ),
                    )
                    continue
                OWNER_AUTH.reset()
                auth_state["face_ok"] = True
                auth_state["voice_ok"] = True
                auth_state["blink_ok"] = True
                auth_state["blink_required"] = False
                auth_state["ear_samples"] = []
                auth_state["current_challenge"] = None
                auth_state["welcome_started"] = False
                auth_state["completed_emitted"] = False
                await _emit_auth_status()
                await emit(type="auth_reset_ok", ok=True,
                           message="주인 등록을 초기화했습니다. 다시 등록해주세요.")
                continue

            if mtype == "enroll_owner":
                # 사이클 #20 — 5각도 캡처 지원.
                # body: {face_name, voice_passphrase, frames_b64?: [base64 JPEG x N], angles?: [str]}
                # frames_b64 가 있으면 다중 인코딩으로 등록. 없으면 구버전 호환:
                # 현재 카메라 프레임 1장(crop_largest_face_jpeg) 사용.

                # P0 보안 (architect 지적): 이미 주인이 등록된 시스템에서는
                # 비인증자가 enroll_owner 로 계정 탈취 불가. `auth_reset` 으로
                # 명시적 초기화 후에만 재등록 허용.
                if OWNER_AUTH.is_enrolled() and not _is_authed():
                    await emit(
                        type="enroll_owner_result", ok=False,
                        message=(
                            "이미 주인이 등록되어 있습니다. 재등록을 원하시면 "
                            "기존 주인이 인증 후 [주인 재등록] 을 눌러주세요."
                        ),
                    )
                    continue

                face_name = (data.get("face_name") or "").strip()
                passphrase = (data.get("voice_passphrase") or "").strip()
                if not face_name or not passphrase:
                    await emit(
                        type="enroll_owner_result", ok=False,
                        message="이름과 음성 패스프레이즈를 모두 입력해주세요.",
                    )
                    continue

                # P1: frames_b64 입력 검증 — pre-auth DoS 차단.
                MAX_FRAMES = 10
                MAX_FRAME_BYTES = 300_000        # base64 디코드 후 ~300KB
                MAX_TOTAL_BYTES = 2_500_000      # 누적 2.5MB 상한
                raw_frames = data.get("frames_b64") or []
                if not isinstance(raw_frames, list):
                    raw_frames = []
                if len(raw_frames) > MAX_FRAMES:
                    await emit(
                        type="enroll_owner_result", ok=False,
                        message=f"전송된 프레임 수가 너무 많습니다 (최대 {MAX_FRAMES}장).",
                    )
                    continue
                frames_b64: List[str] = []
                total_bytes = 0
                for fb in raw_frames:
                    if not isinstance(fb, str):
                        continue
                    # base64 길이는 원본 바이트의 ~4/3.
                    approx_bytes = (len(fb) * 3) // 4
                    if approx_bytes > MAX_FRAME_BYTES:
                        continue  # 너무 큰 단일 프레임 폐기.
                    total_bytes += approx_bytes
                    if total_bytes > MAX_TOTAL_BYTES:
                        break
                    frames_b64.append(fb)
                angles = data.get("angles") or []
                if not isinstance(angles, list):
                    angles = []

                encs: List[List[float]] = []
                kept_angles: List[str] = []
                primary_crop: Optional[bytes] = None
                failed_angles: List[str] = []

                if isinstance(frames_b64, list) and frames_b64:
                    # 다중 각도 — 각 프레임에서 인코딩 시도.
                    import base64 as _b64
                    for idx, fb in enumerate(frames_b64):
                        if not isinstance(fb, str):
                            continue
                        try:
                            jpeg = _b64.b64decode(fb)
                        except Exception:
                            continue
                        if not jpeg or len(jpeg) < 200:
                            continue
                        enc = await asyncio.to_thread(
                            compute_face_encoding_from_jpeg, jpeg,
                        )
                        angle_label = angles[idx] if idx < len(angles) else f"frame{idx}"
                        if enc:
                            encs.append(enc)
                            kept_angles.append(str(angle_label))
                            if primary_crop is None:
                                primary_crop = jpeg
                        else:
                            failed_angles.append(str(angle_label))
                    if primary_crop is None:
                        # 인코딩이 하나도 안 됐으면 첫 프레임이라도 보존 (Claude Vision 식별용).
                        try:
                            primary_crop = _b64.b64decode(frames_b64[0])
                        except Exception:
                            primary_crop = None

                if not encs:
                    # 구버전 폴백 — 라이브 카메라에서 얼굴 1장 잘라 인코딩.
                    crop = session.vision.crop_largest_face_jpeg(require_face=True)
                    if not crop:
                        await emit(
                            type="enroll_owner_result", ok=False,
                            message=(
                                "얼굴이 명확히 보이지 않습니다. 카메라를 정면으로 "
                                "보고 다시 시도해주세요."
                            ),
                            failed_angles=failed_angles,
                        )
                        continue
                    enc1 = await asyncio.to_thread(
                        compute_face_encoding_from_jpeg, crop,
                    )
                    if enc1:
                        encs = [enc1]
                        kept_angles = ["front"]
                    primary_crop = crop

                try:
                    OWNER_AUTH.enroll(
                        face_name,
                        passphrase,
                        face_encodings=encs if encs else None,
                        face_encoding=encs[0] if encs else None,
                        face_angles=kept_angles or None,
                    )
                    # FaceRegistry 에도 등록 — 기존 도구가 얼굴 사진을 참조할 수 있도록.
                    if primary_crop:
                        try:
                            FACE_REGISTRY.register(face_name, primary_crop)
                        except Exception:
                            traceback.print_exc()
                    # 등록자는 자동 로그인 — 막 본인 얼굴/문구를 셋업했으므로.
                    auth_state["face_ok"] = True
                    auth_state["voice_ok"] = True
                    auth_state["blink_ok"] = True
                    # blink_required 는 세션 시작 시 capability probe 결과를 유지.
                    # (라이브니스 미지원 환경에서 강제로 True 로 바꾸지 않음.)
                    auth_state["blink_required"] = _landmarks_supported
                    auth_state["ear_samples"] = []
                    auth_state["completed_emitted"] = False
                    _refresh_challenge()
                    await emit(
                        type="enroll_owner_result", ok=True,
                        face_name=face_name,
                        has_face_encoding=bool(encs),
                        face_encoding_count=len(encs),
                        kept_angles=kept_angles,
                        failed_angles=failed_angles,
                        message=(
                            f"주인으로 등록되었습니다, {face_name} 님. "
                            f"({len(encs)}개 각도 인식 성공"
                            + (f", {len(failed_angles)}개 실패" if failed_angles else "")
                            + ") 환영합니다."
                        ),
                        faces=FACE_REGISTRY.list_people(),
                    )
                    await _emit_auth_status()
                    if not auth_state["completed_emitted"]:
                        auth_state["completed_emitted"] = True
                        await emit(type="auth_complete", face_name=face_name)
                        _start_welcome_if_authed()
                except ValueError as ve:
                    await emit(type="enroll_owner_result", ok=False, message=str(ve))
                except Exception as e:
                    traceback.print_exc()
                    await emit(type="enroll_owner_result", ok=False,
                               message=f"등록 실패: {e}")
                continue

            if mtype == "text_input":
                await _preempt_welcome()
                if busy.locked():
                    continue
                async with busy:
                    user_text = (data.get("text") or "").strip()
                    if not user_text:
                        continue
                    await respond_internal(
                        user_text, log_user=_text_input_log_user(data),
                    )

            elif mtype == "switch_backend":
                target = data.get("backend", "claude")
                try:
                    await asyncio.to_thread(session.brain.switch_backend, target)
                    if target == "claude":
                        session._attach_tools()
                    else:
                        # openai/ollama/compare 모두 도구 비활성화
                        session.detach_tools()

                    # 키 가용성 검증 후 UI 경고
                    warnings = []
                    b = session.brain
                    if target == "claude" and b.anthropic_client is None:
                        warnings.append("ANTHROPIC_API_KEY 누락")
                    elif target == "openai" and b.openai_client is None:
                        warnings.append("OPENAI_API_KEY 누락")
                    elif target == "zhipuai" and b.zhipuai_client is None:
                        warnings.append("ZHIPUAI_API_KEY 누락")
                    elif target == "gemini" and b.gemini_client is None:
                        warnings.append("GOOGLE_API_KEY 누락")
                    elif target == "compare":
                        if b.anthropic_client is None:
                            warnings.append("Claude 키 없음 — Claude 응답 안 나옴")
                        if b.openai_client is None:
                            warnings.append("OpenAI 키 없음 — GPT 응답 안 나옴")

                    await emit(
                        type="backend_changed",
                        backend=target,
                        tools_enabled=session.brain.tools is not None and target == "claude",
                        warnings=warnings,
                    )
                    for w in warnings:
                        # w 는 항상 한국어 정적 문자열 (위에서 append) — 안전
                        await emit(type="error", message="⚠ " + w)
                except Exception as e:
                    # 사이클 #6 핫픽스: 백엔드 전환 실패도 raw 영문 예외 대신
                    # 친절 한국어 안내 (전환 대상 백엔드 라벨로 분기).
                    await emit(type="error", message=_friendly_error(e, target))

            elif mtype == "switch_model":
                # 사이클 #7 — 백엔드별 모델 변경. body: {backend, model}.
                target_backend = (data.get("backend") or "").strip()
                target_model = (data.get("model") or "").strip()
                try:
                    await asyncio.to_thread(
                        session.brain.switch_model, target_backend, target_model
                    )
                    await emit(
                        type="model_changed",
                        backend=target_backend,
                        model=target_model,
                    )
                except ValueError as e:
                    # 카탈로그 검증 실패 — 이미 한국어 직접 메시지.
                    # _friendly_error 는 API 통신 오류용이라 카탈로그 검증을
                    # "통신 오류" 로 오안내하므로 전용 헬퍼 사용.
                    await emit(type="error", message=_model_switch_friendly(e))
                except Exception as e:
                    # init 실패 등 — friendly_error 로 한국어화.
                    await emit(type="error", message=_friendly_error(e, target_backend))

            elif mtype == "models_list":
                # 사이클 #7 — UI 가 드롭다운 채울 때 사용. 카탈로그 + 현재 선택 모델.
                from .config import MODEL_CATALOG, current_model
                payload = {
                    b: {"models": list(models), "current": current_model(b)}
                    for b, models in MODEL_CATALOG.items()
                }
                await emit(type="models_list", catalog=payload)

            elif mtype == "voices_list":
                # 음성 카탈로그 + 현재 선택 프리셋 id 조회.
                from .config import VOICE_CATALOG, current_voice_preset
                await emit(
                    type="voices_list",
                    catalog=list(VOICE_CATALOG),
                    current=current_voice_preset(),
                )

            elif mtype == "switch_voice":
                # 음성 프리셋 변경. body: {preset: <id>}.
                # cfg.tts_voice/rate/pitch 를 갱신해 다음 합성부터 즉시 반영.
                preset_id = (data.get("preset") or "").strip()
                try:
                    from .config import apply_voice_preset
                    applied = await asyncio.to_thread(apply_voice_preset, preset_id)
                    await emit(
                        type="voice_changed",
                        preset=applied["id"],
                        label=applied["label"],
                        voice=applied["voice"],
                        rate=applied["rate"],
                        pitch=applied["pitch"],
                    )
                except ValueError:
                    # 알려지지 않은 프리셋 id — 친절 한국어 메시지만 노출.
                    await emit(
                        type="error",
                        message="⚠ 음성 변경 실패: 알 수 없는 음성 프리셋입니다.",
                    )
                except Exception:
                    traceback.print_exc()
                    await emit(
                        type="error",
                        message="⚠ 음성 변경 중 오류가 발생했습니다.",
                    )

            elif mtype == "preview_voice":
                # 미리듣기 — 선택한 프리셋(또는 현재 설정) 으로 짧은 샘플 합성.
                # cfg 는 변경하지 않음 (사용자가 OK 눌러야만 switch_voice 로 적용).
                # 오디오는 base64 로 JSON 페이로드에 실어 보냄 — 바이너리 채널은 정상
                # 응답 TTS 만 사용해 충돌 없음.
                preset_id = (data.get("preset") or "").strip()
                sample_text = (data.get("text") or "안녕하세요. 저는 사비스예요. 잘 들리시나요?").strip()
                if len(sample_text) > 120:
                    sample_text = sample_text[:120]
                try:
                    from .config import (
                        get_voice_preset, SUPPORTED_KO_VOICES,
                    )
                    preset = get_voice_preset(preset_id) if preset_id else None
                    voice = preset["voice"] if preset else cfg.tts_voice
                    rate = preset["rate"] if preset else cfg.tts_rate
                    pitch = preset["pitch"] if preset else cfg.tts_pitch

                    # Edge-TTS 가 실제 제공하지 않는 음성이면 합성 시도 자체를 차단.
                    # apply_voice_preset 와 동일한 가드를 미리듣기에도 적용.
                    if voice not in SUPPORTED_KO_VOICES:
                        await emit(
                            type="error",
                            message=(
                                "⚠ 음성 미리듣기 실패: 지원되지 않는 음성"
                            ),
                        )
                        continue

                    import edge_tts
                    import tempfile as _tf
                    async def _do():
                        # 합성 실패 시에도 temp 파일이 leak 되지 않도록 try/finally 로 감싼다.
                        with _tf.NamedTemporaryFile(suffix=".mp3", delete=False) as _f:
                            _path = _f.name
                        try:
                            comm = edge_tts.Communicate(
                                sample_text, voice=voice, rate=rate, pitch=pitch,
                            )
                            await comm.save(_path)
                            with open(_path, "rb") as _rf:
                                return _rf.read()
                        finally:
                            try:
                                os.unlink(_path)
                            except OSError:
                                pass
                    audio_bytes = await _do()
                    if audio_bytes:
                        await emit(
                            type="voice_preview",
                            preset=preset_id,
                            audio_b64=base64.b64encode(audio_bytes).decode("ascii"),
                            mime="audio/mpeg",
                        )
                    else:
                        await emit(
                            type="error",
                            message="⚠ 음성 미리듣기 실패: 빈 오디오",
                        )
                except Exception:
                    traceback.print_exc()
                    await emit(
                        type="error",
                        message="⚠ 음성 미리듣기 중 오류가 발생했습니다.",
                    )

            elif mtype == "reset":
                session.brain.reset_history()
                await emit(type="reset_ack")

            elif mtype == "observe":
                if data.get("on"):
                    if session.tools is None:
                        await emit(
                            type="error",
                            message="행동 인식은 Claude 백엔드에서만 가능합니다.",
                        )
                    else:
                        session.start_observing(interval=float(data.get("interval", 6.0)))
                        await emit(type="observe_state", on=True)
                else:
                    session.stop_observing()
                    await emit(type="observe_state", on=False)

            elif mtype == "register_face":
                # 현재 카메라 프레임에서 얼굴 잘라 등록
                name = (data.get("name") or "").strip()
                if not name:
                    await emit(type="face_register_result", ok=False,
                               message="이름을 입력해 주세요.")
                else:
                    crop = session.vision.crop_largest_face_jpeg(require_face=True)
                    if not crop:
                        await emit(type="face_register_result", ok=False,
                                   message="얼굴이 명확히 보이지 않습니다. 카메라를 정면으로 보고 다시 시도하세요.")
                    else:
                        try:
                            saved = FACE_REGISTRY.register(name, crop)
                            await emit(type="face_register_result", ok=True,
                                       name=saved,
                                       message=f"'{saved}' 등록 완료",
                                       faces=FACE_REGISTRY.list_people())
                        except Exception as e:
                            await emit(type="face_register_result", ok=False,
                                       message=f"등록 실패: {e}")

            elif mtype == "delete_face":
                name = (data.get("name") or "").strip()
                ok = FACE_REGISTRY.delete(name) if name else False
                await emit(type="face_delete_result", ok=ok, name=name,
                           faces=FACE_REGISTRY.list_people())

            elif mtype == "list_faces":
                await emit(type="face_list", faces=FACE_REGISTRY.list_people())

            elif mtype == "command_log":
                # 텍스트만 명령 적재. body: {text, kind?, status?, meta?}
                ctext = (data.get("text") or "").strip()
                if not ctext:
                    await emit(type="error", message="명령 텍스트가 비어있습니다.")
                else:
                    try:
                        cmd_id = await asyncio.to_thread(
                            session.memory.log_command,
                            session.memory_user_id, ctext,
                            (data.get("kind") or "text"),
                            None, None,
                            (data.get("status") or "pending"),
                            data.get("meta"),
                        )
                        await emit(type="command_saved", id=cmd_id,
                                   kind=(data.get("kind") or "text"),
                                   caption=ctext, has_image=False)
                    except ValueError as e:
                        print(f"[WS command_log] {e}")
                        await emit(type="error", message="명령을 저장할 수 없습니다 (잘못된 종류 또는 상태).")

            elif mtype == "commands_recent":
                limit = int(data.get("limit") or 50)
                kind = data.get("kind")
                rows = await asyncio.to_thread(
                    session.memory.recent_commands,
                    session.memory_user_id, limit, kind,
                )
                # image_path 는 클라이언트에 그대로 노출하지 않고 has_image 만 표시.
                items = [{
                    "id": r["id"],
                    "kind": r["kind"],
                    "command_text": r["command_text"],
                    "response_text": r["response_text"],
                    "status": r["status"],
                    "has_image": bool(r["image_path"]),
                    "has_audio": bool(r.get("audio_path")),
                    "has_video": bool(r.get("video_path")),
                    "created_at": r["created_at"],
                    "completed_at": r["completed_at"],
                    "meta": r["meta"],
                } for r in rows]
                await emit(type="commands_recent", items=items)

            elif mtype == "command_get":
                # body: {id, include_image?: bool}
                cid = int(data.get("id") or 0)
                row = await asyncio.to_thread(session.memory.get_command, cid)
                if not row or row["user_id"] != session.memory_user_id:
                    await emit(type="error", message="명령을 찾을 수 없습니다.")
                else:
                    img_b64 = None
                    audio_b64 = None
                    video_b64 = None
                    # 큰 미디어를 base64 로 통째 보내면 WebSocket 프레임/브라우저
                    # 메모리 압력이 커진다 → 8MiB 컷오프, 초과 시 has_* 플래그만.
                    MEDIA_INLINE_MAX = 8 * 1024 * 1024
                    include_media = bool(data.get("include_image")) or bool(data.get("include_media"))
                    if include_media and row.get("image_path") \
                            and os.path.isfile(row["image_path"]):
                        try:
                            if os.path.getsize(row["image_path"]) <= MEDIA_INLINE_MAX:
                                blob = await asyncio.to_thread(_read_bytes, row["image_path"])
                                img_b64 = base64.b64encode(blob).decode("ascii")
                        except OSError as e:
                            print(f"[WS command_get image read] {e}")
                            await emit(type="error", message="이미지를 불러올 수 없습니다.")
                    if data.get("include_audio") and row.get("audio_path") \
                            and os.path.isfile(row["audio_path"]):
                        try:
                            if os.path.getsize(row["audio_path"]) <= MEDIA_INLINE_MAX:
                                blob = await asyncio.to_thread(_read_bytes, row["audio_path"])
                                audio_b64 = base64.b64encode(blob).decode("ascii")
                        except OSError as e:
                            print(f"[WS command_get audio read] {e}")
                            await emit(type="error", message="음성을 불러올 수 없습니다.")
                    if data.get("include_video") and row.get("video_path") \
                            and os.path.isfile(row["video_path"]):
                        try:
                            if os.path.getsize(row["video_path"]) <= MEDIA_INLINE_MAX:
                                blob = await asyncio.to_thread(_read_bytes, row["video_path"])
                                video_b64 = base64.b64encode(blob).decode("ascii")
                        except OSError as e:
                            print(f"[WS command_get video read] {e}")
                            await emit(type="error", message="영상을 불러올 수 없습니다.")
                    await emit(
                        type="command_get",
                        id=row["id"], kind=row["kind"],
                        command_text=row["command_text"],
                        response_text=row["response_text"],
                        status=row["status"],
                        has_image=bool(row["image_path"]),
                        has_audio=bool(row.get("audio_path")),
                        has_video=bool(row.get("video_path")),
                        created_at=row["created_at"],
                        completed_at=row["completed_at"],
                        meta=row["meta"],
                        image_b64=img_b64,
                        audio_b64=audio_b64,
                        video_b64=video_b64,
                    )

            elif mtype == "command_delete":
                cid = int(data.get("id") or 0)
                row = await asyncio.to_thread(session.memory.get_command, cid)
                if not row or row["user_id"] != session.memory_user_id:
                    await emit(type="error", message="명령을 찾을 수 없습니다.")
                else:
                    ok = await asyncio.to_thread(session.memory.delete_command, cid)
                    await emit(type="command_deleted", id=cid, ok=ok)

            # ── 사이클 #16: 멀티모달 학습 지식 (knowledge) ────────────
            elif mtype == "knowledge_add":
                # body: {content, topic?, source?, confidence?, tags?: List[str]}
                content = (data.get("content") or "").strip()
                if not content:
                    await emit(type="error", message="학습 내용이 비어 있습니다.")
                else:
                    try:
                        kid = await asyncio.to_thread(
                            functools.partial(
                                session.memory.add_knowledge,
                                user_id=session.memory_user_id,
                                content=content,
                                topic=str(data.get("topic") or ""),
                                source=str(data.get("source") or "user"),
                                confidence=float(data.get("confidence", 1.0)),
                                tags=data.get("tags") if isinstance(data.get("tags"), list) else None,
                            )
                        )
                        await emit(type="knowledge_saved", id=kid, content=content,
                                   has_image=False, has_audio=False, has_video=False)
                    except ValueError:
                        await emit(type="error", message="학습 자료의 값이 올바르지 않습니다.")
                    except Exception as e:
                        print(f"[WS knowledge_add] {e}")
                        await emit(type="error", message="학습 자료를 저장하지 못했습니다.")

            elif mtype == "knowledge_recent":
                limit = max(1, min(int(data.get("limit") or 20), 200))
                source = data.get("source")
                rows = await asyncio.to_thread(
                    session.memory.recent_knowledge,
                    session.memory_user_id, limit,
                    source if isinstance(source, str) else None,
                )
                items = [{
                    "id": r["id"],
                    "topic": r["topic"],
                    "content": r["content"],
                    "source": r["source"],
                    "confidence": r["confidence"],
                    "tags": r.get("tags", []),
                    "has_image": bool(r.get("image_path")),
                    "has_audio": bool(r.get("audio_path")),
                    "has_video": bool(r.get("video_path")),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                } for r in rows]
                await emit(type="knowledge_recent", items=items)

            elif mtype == "knowledge_search":
                q = (data.get("query") or "").strip()
                limit = max(1, min(int(data.get("limit") or 10), 100))
                rows = await asyncio.to_thread(
                    session.memory.search_knowledge,
                    session.memory_user_id, q, limit,
                ) if q else []
                items = [{
                    "id": r["id"], "topic": r["topic"], "content": r["content"],
                    "source": r["source"], "confidence": r["confidence"],
                    "tags": r.get("tags", []),
                    "has_image": bool(r.get("image_path")),
                    "has_audio": bool(r.get("audio_path")),
                    "has_video": bool(r.get("video_path")),
                    "updated_at": r["updated_at"],
                } for r in rows]
                await emit(type="knowledge_search", query=q, items=items)

            elif mtype == "knowledge_get":
                kid = int(data.get("id") or 0)
                row = await asyncio.to_thread(session.memory.get_knowledge, kid)
                if not row or row["user_id"] != session.memory_user_id:
                    await emit(type="error", message="학습 자료를 찾을 수 없습니다.")
                else:
                    img_b64 = audio_b64 = video_b64 = None
                    MEDIA_INLINE_MAX = 8 * 1024 * 1024
                    for flag, slot, holder in (
                        ("include_image", "image_path", "img"),
                        ("include_audio", "audio_path", "audio"),
                        ("include_video", "video_path", "video"),
                    ):
                        if data.get(flag) and row.get(slot) and os.path.isfile(row[slot]):
                            try:
                                if os.path.getsize(row[slot]) <= MEDIA_INLINE_MAX:
                                    blob = await asyncio.to_thread(_read_bytes, row[slot])
                                    enc = base64.b64encode(blob).decode("ascii")
                                    if holder == "img": img_b64 = enc
                                    elif holder == "audio": audio_b64 = enc
                                    else: video_b64 = enc
                            except OSError as e:
                                print(f"[WS knowledge_get {slot} read] {e}")
                                await emit(type="error", message="학습 자료를 불러올 수 없습니다.")
                    await emit(
                        type="knowledge_get",
                        id=row["id"], topic=row["topic"], content=row["content"],
                        source=row["source"], confidence=row["confidence"],
                        tags=row.get("tags", []),
                        has_image=bool(row.get("image_path")),
                        has_audio=bool(row.get("audio_path")),
                        has_video=bool(row.get("video_path")),
                        created_at=row["created_at"], updated_at=row["updated_at"],
                        image_b64=img_b64, audio_b64=audio_b64, video_b64=video_b64,
                    )

            elif mtype == "knowledge_delete":
                kid = int(data.get("id") or 0)
                row = await asyncio.to_thread(session.memory.get_knowledge, kid)
                if not row or row["user_id"] != session.memory_user_id:
                    await emit(type="error", message="학습 자료를 찾을 수 없습니다.")
                else:
                    ok = await asyncio.to_thread(session.memory.delete_knowledge, kid)
                    await emit(type="knowledge_deleted", id=kid, ok=ok)

            # ── 사이클 #21 (F-04 + F-10) — 회의록/할 일 ────────────────
            # architect P0: 모든 사이클 #21 핸들러는 인증 필수.
            # 단일 주인 시스템이지만, 외부 노출된 WS 에 미인증 접근으로
            # MEETINGS/TODOS(모듈 전역) 가 노출/오염되지 않도록 게이트.
            elif mtype in {
                "meeting_start", "meeting_chunk", "meeting_end",
                "meeting_list", "meeting_get",
                "todo_list", "todo_add", "todo_done", "todo_remove", "todo_extract",
                "feedback_submit", "my_sarvis_summary",  # 사이클 #22
                "profile_get", "profile_save",  # 사이클 #30 개인화
                "storage_list", "storage_delete",  # 저장 공간
                "ha_run_observer", "ha_issues_list", "ha_kill_switch",
                "ha_optout", "ha_growth_diary",          # 사이클 #23
                "ha_run_diagnostician", "ha_diagnoses_for_issue",  # 사이클 #24
                "ha_run_strategist", "ha_run_improver", "ha_run_validator",
                "ha_proposals_list", "ha_proposal_decision",  # 사이클 #25
            } and OWNER_AUTH.is_enrolled() and not _is_authed():
                await emit(type="error", message="주인 인증이 필요합니다.")
                continue

            # ── 사이클 #22 (HARN-12 + HARN-05) — 피드백 / My Sarvis ─────
            elif mtype == "feedback_submit":
                # body: {cmd_id: int, rating: -1|0|+1, comment?: str}
                try:
                    cmd_id = int(data.get("cmd_id") or 0)
                    rating = int(data.get("rating") or 0)
                except (TypeError, ValueError):
                    await emit(type="feedback_result", ok=False,
                               message="cmd_id/rating 형식 오류")
                    continue
                comment = (data.get("comment") or "").strip() or None
                if cmd_id <= 0:
                    await emit(type="feedback_result", ok=False,
                               message="cmd_id 필요")
                    continue
                try:
                    fb = await asyncio.to_thread(
                        session.memory.set_feedback,
                        cmd_id, session.memory_user_id, rating, comment,
                    )
                    await emit(type="feedback_result", ok=True,
                               cmd_id=cmd_id,
                               rating=int(fb.get("rating", rating)),
                               comment=fb.get("comment"))
                except ValueError as ve:
                    await emit(type="feedback_result", ok=False,
                               message=str(ve))
                except Exception as ex:
                    print(f"[feedback_submit] 실패: {ex!r}")
                    await emit(type="feedback_result", ok=False,
                               message="피드백 저장 실패")

            elif mtype == "profile_get":
                try:
                    profile = await asyncio.to_thread(
                        session.memory.get_profile, session.memory_user_id
                    )
                    await emit(type="profile_data", **profile)
                except Exception as ex:
                    print(f"[profile_get] 실패: {ex!r}")
                    await emit(type="error", message="프로필 로드 실패")

            elif mtype == "profile_save":
                try:
                    _TONE_ALLOWED = {"friendly", "formal", "casual", "cute", "professional"}
                    raw_tone = str(data.get("tone", "friendly"))[:30]
                    tone = raw_tone if raw_tone in _TONE_ALLOWED else "friendly"
                    profile = await asyncio.to_thread(
                        functools.partial(
                            session.memory.save_profile,
                            user_id=session.memory_user_id,
                            nickname=str(data.get("nickname", "")).strip()[:100],
                            email=str(data.get("email", "")).strip()[:200],
                            tone=tone,
                            interests=str(data.get("interests", "")).strip()[:500],
                            bio=str(data.get("bio", "")).strip()[:1000],
                        )
                    )
                    await emit(type="profile_saved", **profile)
                except Exception as ex:
                    print(f"[profile_save] 실패: {ex!r}")
                    await emit(type="error", message="프로필 저장 실패")

            elif mtype == "storage_list":
                try:
                    kind_filter = str(data.get("kind", "")).strip()
                    _VALID_KINDS = {"photo", "video", "audio"}
                    kind_filter = kind_filter if kind_filter in _VALID_KINDS else ""
                    lim = min(int(data.get("limit", 100)), 500)
                    recs = await asyncio.to_thread(
                        session.memory.list_recordings_by_kind,
                        session.memory_user_id, kind_filter, lim,
                    )
                    for r in recs:
                        r.pop("file_path", None)
                    await emit(type="storage_list", ok=True, items=recs)
                except Exception as ex:
                    print(f"[storage_list] 실패: {ex!r}")
                    await emit(type="storage_list", ok=False, items=[], message="목록 로드 실패")

            elif mtype == "storage_delete":
                try:
                    rec_id = int(data.get("id") or 0)
                    if rec_id <= 0:
                        await emit(type="storage_deleted", ok=False, message="id 필요")
                        continue
                    ok = await asyncio.to_thread(
                        functools.partial(
                            session.memory.delete_recording,
                            rec_id,
                            user_id=session.memory_user_id,
                        )
                    )
                    await emit(type="storage_deleted", ok=ok, id=rec_id)
                except Exception as ex:
                    print(f"[storage_delete] 실패: {ex!r}")
                    await emit(type="storage_deleted", ok=False, message="삭제 실패")

            elif mtype == "my_sarvis_summary":
                # body: {window_days?: float, default 7}
                try:
                    days = float(data.get("window_days") or 7.0)
                except (TypeError, ValueError):
                    days = 7.0
                window_sec = max(60.0, days * 86400.0)
                try:
                    summary = await asyncio.to_thread(
                        session.memory.my_sarvis_summary,
                        session.memory_user_id, window_sec,
                    )
                    await emit(type="my_sarvis_summary", **summary)
                except Exception as ex:
                    print(f"[my_sarvis_summary] 실패: {ex!r}")
                    await emit(type="error", message="My Sarvis 집계 실패")

            # ── 사이클 #23 (HA Stage S1) — Harness Agent ───────────────
            # 모든 ha_* 핸들러는 Kill Switch 활성 시 즉시 거부 + stdout 로그.
            elif mtype in {"ha_run_observer", "ha_issues_list",
                           "ha_kill_switch", "ha_optout", "ha_growth_diary",
                           "ha_run_diagnostician", "ha_diagnoses_for_issue",
                           "ha_run_strategist", "ha_run_improver",
                           "ha_run_validator", "ha_proposals_list",
                           "ha_proposal_decision"}:
                if mtype != "ha_kill_switch" and _ha_kill_switch_on():
                    print(f"[HA] kill_switch active — {mtype} 거부")
                    await emit(
                        type="ha_blocked", request=mtype,
                        message="HA Kill Switch 활성 — 모든 자율 동작 정지",
                    )
                    continue
                if mtype == "ha_run_observer":
                    try:
                        days = float(data.get("window_days") or 1.0)
                    except (TypeError, ValueError):
                        days = 1.0
                    window_sec = max(60.0, days * 86400.0)
                    use_llm = bool(data.get("use_llm", False))
                    try:
                        obs = _HAObserver(
                            memory=session.memory,
                            brain=session.brain if use_llm else None,
                        )
                        cards = await asyncio.wait_for(
                            asyncio.to_thread(obs.scan, window_sec, use_llm),
                            timeout=15.0,
                        )
                        # Reporter 미니 — One-Pager 즉시 생성
                        rep = _HAReporter(memory=session.memory)
                        for c in cards:
                            try:
                                rep.write_one_pager(c.to_payload())
                            except Exception as ex:
                                print(f"[HA] reporter 실패: {ex!r}")
                        await emit(
                            type="ha_observer_result",
                            ok=True,
                            window_days=days,
                            issue_count=len(cards),
                            issues=[c.to_payload() for c in cards],
                        )
                    except _HAKillSwitchActivated as ex:
                        await emit(type="ha_blocked", request=mtype,
                                   message=str(ex))
                    except asyncio.TimeoutError:
                        await emit(type="ha_observer_result", ok=False,
                                   message="Observer 시간 초과 (15s)")
                    except Exception as ex:
                        print(f"[HA] observer 실패: {ex!r}")
                        await emit(type="ha_observer_result", ok=False,
                                   message=f"Observer 실패: {ex}")

                elif mtype == "ha_issues_list":
                    try:
                        limit = int(data.get("limit") or 20)
                    except (TypeError, ValueError):
                        limit = 20
                    issues = await asyncio.to_thread(
                        session.memory.ha_issues_recent, limit,
                    )
                    await emit(type="ha_issues_list", ok=True,
                               count=len(issues), issues=issues)

                elif mtype == "ha_kill_switch":
                    on = bool(data.get("on"))
                    reason = (data.get("reason") or "manual").strip()[:200]
                    try:
                        if on:
                            await asyncio.to_thread(
                                _ha_kill_switch_activate, "owner", reason,
                            )
                            await asyncio.to_thread(
                                session.memory.ha_kill_switch_log_open,
                                "owner", reason,
                            )
                        else:
                            await asyncio.to_thread(
                                _ha_kill_switch_deactivate, "owner",
                            )
                            await asyncio.to_thread(
                                session.memory.ha_kill_switch_log_close,
                                "owner",
                            )
                        await emit(type="ha_kill_switch", ok=True,
                                   active=_ha_kill_switch_on())
                    except Exception as ex:
                        print(f"[HA] kill_switch 실패: {ex!r}")
                        await emit(type="ha_kill_switch", ok=False,
                                   message="Kill Switch 토글 실패")

                elif mtype == "ha_optout":
                    on = bool(data.get("on"))
                    try:
                        result = await asyncio.to_thread(
                            session.memory.ha_optout_set,
                            session.memory_user_id, on,
                        )
                        await emit(type="ha_optout", ok=True,
                                   opted_out=bool(result))
                    except Exception as ex:
                        print(f"[HA] optout 실패: {ex!r}")
                        await emit(type="ha_optout", ok=False,
                                   message="옵트아웃 토글 실패")

                elif mtype == "ha_run_diagnostician":
                    try:
                        limit = int(data.get("limit") or 20)
                    except (TypeError, ValueError):
                        limit = 20
                    use_llm = bool(data.get("use_llm", False))
                    try:
                        diag = _HADiagnostician(
                            memory=session.memory,
                            brain=session.brain if use_llm else None,
                        )
                        results = await asyncio.wait_for(
                            asyncio.to_thread(diag.run_pending, limit),
                            timeout=15.0,
                        )
                        # Reporter 보강 — 진단 첨부된 이슈에 대해 One-Pager 갱신.
                        rep = _HAReporter(memory=session.memory)
                        for r in results:
                            try:
                                issues = await asyncio.to_thread(
                                    session.memory.ha_issues_recent, 50,
                                )
                                match = next(
                                    (i for i in issues
                                     if i.get("issue_id") == r.issue_id),
                                    None,
                                )
                                if match:
                                    rep.write_one_pager(match)
                            except Exception as ex:
                                print(f"[HA] reporter 보강 실패: {ex!r}")
                        await emit(
                            type="ha_diagnostician_result",
                            ok=True,
                            count=len(results),
                            diagnoses=[r.to_payload() for r in results],
                        )
                    except _HAKillSwitchActivated as ex:
                        await emit(type="ha_blocked", request=mtype,
                                   message=str(ex))
                    except asyncio.TimeoutError:
                        await emit(type="ha_diagnostician_result", ok=False,
                                   message="Diagnostician 시간 초과 (15s)")
                    except Exception as ex:
                        print(f"[HA] diagnostician 실패: {ex!r}")
                        await emit(type="ha_diagnostician_result", ok=False,
                                   message=f"Diagnostician 실패: {ex}")

                elif mtype == "ha_diagnoses_for_issue":
                    issue_id = (data.get("issue_id") or "").strip()
                    if not issue_id:
                        await emit(type="ha_diagnoses_for_issue", ok=False,
                                   message="issue_id 필요")
                        continue
                    try:
                        limit = int(data.get("limit") or 5)
                    except (TypeError, ValueError):
                        limit = 5
                    diags = await asyncio.to_thread(
                        session.memory.ha_diagnoses_for_issue,
                        issue_id, limit,
                    )
                    await emit(type="ha_diagnoses_for_issue", ok=True,
                               issue_id=issue_id, count=len(diags),
                               diagnoses=diags)

                elif mtype == "ha_run_strategist":
                    try:
                        limit = int(data.get("limit") or 5)
                    except (TypeError, ValueError):
                        limit = 5
                    try:
                        strat = _HAStrategist(memory=session.memory)
                        results = await asyncio.wait_for(
                            asyncio.to_thread(strat.run_recent, limit),
                            timeout=10.0,
                        )
                        await emit(type="ha_strategist_result", ok=True,
                                   count=len(results),
                                   strategies=[s.to_payload() for s in results])
                    except _HAKillSwitchActivated as ex:
                        await emit(type="ha_blocked", request=mtype,
                                   message=str(ex))
                    except asyncio.TimeoutError:
                        await emit(type="ha_strategist_result", ok=False,
                                   message="Strategist 시간 초과")
                    except Exception as ex:
                        print(f"[HA] strategist 실패: {ex!r}")
                        await emit(type="ha_strategist_result", ok=False,
                                   message=f"Strategist 실패: {ex}")

                elif mtype == "ha_run_improver":
                    try:
                        limit = int(data.get("limit") or 50)
                    except (TypeError, ValueError):
                        limit = 50
                    try:
                        imp = _HAImprover(memory=session.memory)
                        results = await asyncio.wait_for(
                            asyncio.to_thread(imp.run_recent, limit),
                            timeout=10.0,
                        )
                        await emit(type="ha_improver_result", ok=True,
                                   count=len(results),
                                   proposals=[r.to_payload() for r in results])
                    except _HAKillSwitchActivated as ex:
                        await emit(type="ha_blocked", request=mtype,
                                   message=str(ex))
                    except asyncio.TimeoutError:
                        await emit(type="ha_improver_result", ok=False,
                                   message="Improver 시간 초과")
                    except Exception as ex:
                        print(f"[HA] improver 실패: {ex!r}")
                        await emit(type="ha_improver_result", ok=False,
                                   message=f"Improver 실패: {ex}")

                elif mtype == "ha_run_validator":
                    try:
                        limit = int(data.get("limit") or 50)
                    except (TypeError, ValueError):
                        limit = 50
                    try:
                        val = _HAValidator(memory=session.memory)
                        results = await asyncio.wait_for(
                            asyncio.to_thread(val.run_pending, limit),
                            timeout=10.0,
                        )
                        await emit(type="ha_validator_result", ok=True,
                                   count=len(results),
                                   validations=[r.to_payload() for r in results])
                    except _HAKillSwitchActivated as ex:
                        await emit(type="ha_blocked", request=mtype,
                                   message=str(ex))
                    except asyncio.TimeoutError:
                        await emit(type="ha_validator_result", ok=False,
                                   message="Validator 시간 초과")
                    except Exception as ex:
                        print(f"[HA] validator 실패: {ex!r}")
                        await emit(type="ha_validator_result", ok=False,
                                   message=f"Validator 실패: {ex}")

                elif mtype == "ha_proposals_list":
                    status = data.get("status")
                    try:
                        limit = int(data.get("limit") or 50)
                    except (TypeError, ValueError):
                        limit = 50
                    try:
                        rows = await asyncio.to_thread(
                            session.memory.ha_proposals_list, status, limit,
                        )
                        # 각 proposal 에 가장 최근 validation 첨부.
                        for r in rows:
                            try:
                                vs = await asyncio.to_thread(
                                    session.memory.ha_validations_for_proposal,
                                    r["proposal_id"], 1,
                                )
                                r["latest_validation"] = vs[0] if vs else None
                            except Exception:
                                r["latest_validation"] = None
                        await emit(type="ha_proposals_list", ok=True,
                                   count=len(rows), proposals=rows)
                    except ValueError as ex:
                        await emit(type="ha_proposals_list", ok=False,
                                   message=str(ex))
                    except Exception as ex:
                        print(f"[HA] proposals_list 실패: {ex!r}")
                        await emit(type="ha_proposals_list", ok=False,
                                   message=f"목록 조회 실패: {ex}")

                elif mtype == "ha_proposal_decision":
                    pid = (data.get("proposal_id") or "").strip()
                    decision = (data.get("decision") or "").strip()
                    by = (data.get("by") or "owner").strip()[:64]
                    if not pid or decision not in ("approved", "rejected"):
                        await emit(type="ha_proposal_decision", ok=False,
                                   message="proposal_id + decision(approved/rejected) 필요")
                        continue
                    try:
                        ok = await asyncio.to_thread(
                            session.memory.ha_proposal_decision,
                            pid, decision, by,
                        )
                        await emit(type="ha_proposal_decision", ok=ok,
                                   proposal_id=pid, decision=decision,
                                   by=by, applied=False,
                                   note="L1 — 승인되어도 자동 적용은 발생하지 않음 (Stage S4 도입 전)")
                    except ValueError as ex:
                        await emit(type="ha_proposal_decision", ok=False,
                                   message=str(ex))
                    except Exception as ex:
                        print(f"[HA] proposal_decision 실패: {ex!r}")
                        await emit(type="ha_proposal_decision", ok=False,
                                   message=f"결정 처리 실패: {ex}")

                elif mtype == "ha_growth_diary":
                    try:
                        limit = int(data.get("limit") or 10)
                    except (TypeError, ValueError):
                        limit = 10
                    try:
                        rep = _HAReporter(memory=session.memory)
                        diary = await asyncio.to_thread(rep.growth_diary, limit)
                        await emit(type="ha_growth_diary", ok=True, **diary)
                    except _HAKillSwitchActivated as ex:
                        await emit(type="ha_blocked", request=mtype,
                                   message=str(ex))
                    except Exception as ex:
                        print(f"[HA] growth_diary 실패: {ex!r}")
                        await emit(type="ha_growth_diary", ok=False,
                                   message="성장 일기 생성 실패")

            elif mtype == "meeting_start":
                title = (data.get("title") or "").strip()
                try:
                    m = MEETINGS.start(title)
                    await emit(
                        type="meeting_started",
                        meeting_id=m.meeting_id, title=m.title,
                        started_at=m.started_at,
                    )
                except RuntimeError as ex:
                    # 이미 진행 중인 회의가 있으면 현재 active 정보를 함께 돌려준다.
                    cur = MEETINGS.active
                    await emit(
                        type="meeting_error", message=str(ex),
                        active_meeting_id=cur.meeting_id if cur else None,
                    )

            elif mtype == "meeting_chunk":
                # body: {text?: str, audio_b64?: base64 webm/wav, speaker?: str}
                # 클라이언트가 선택적으로 자체 STT 결과(text) 또는 raw audio 를 보낸다.
                speaker = (data.get("speaker") or "Owner").strip() or "Owner"
                text = (data.get("text") or "").strip()
                if not text and data.get("audio_b64"):
                    if STT is None:
                        await emit(type="meeting_error",
                                   message="STT 모델 로딩 중입니다. 잠시 후 다시 시도해주세요.")
                        continue
                    try:
                        raw = base64.b64decode(data["audio_b64"])
                        # 임시 파일로 STT (sarvis._do_voice_login 과 동일 패턴).
                        import tempfile
                        with tempfile.NamedTemporaryFile(
                            suffix=".webm", delete=False
                        ) as tf:
                            tf.write(raw); audio_path = tf.name
                        try:
                            text = await asyncio.to_thread(STT.transcribe, audio_path, "")
                        finally:
                            try: os.unlink(audio_path)
                            except Exception: pass
                    except Exception as e:
                        traceback.print_exc()
                        await emit(type="meeting_error", message=f"오디오 처리 실패: {e}")
                        continue
                ut = MEETINGS.append_active(text, speaker=speaker)
                if ut is None:
                    await emit(type="meeting_chunk_skipped", reason="empty_or_inactive",
                               text=text)
                else:
                    await emit(
                        type="meeting_chunk_added",
                        meeting_id=MEETINGS.active.meeting_id if MEETINGS.active else None,
                        ts=ut.ts, speaker=ut.speaker, text=ut.text,
                        utterance_count=len(MEETINGS.active.utterances)
                                       if MEETINGS.active else 0,
                    )

            elif mtype == "meeting_end":
                if MEETINGS.active is None:
                    await emit(type="meeting_error", message="진행 중인 회의가 없습니다.")
                    continue

                # LLM 요약 함수 — anthropic_client 직접 호출. 키/클라이언트가 없거나
                # 실패하면 Meeting.summarize 의 fallback 으로 처리.
                def _summarize_with_llm(transcript_md: str) -> Dict[str, Any]:
                    client = session.brain.anthropic_client
                    if client is None:
                        # Anthropic 미사용 환경 — fallback (트랜스크립트 앞부분).
                        return {}
                    prompt = build_summary_prompt(transcript_md)
                    try:
                        resp = client.messages.create(
                            model=getattr(cfg, "claude_model", "claude-sonnet-4-5"),
                            max_tokens=1500,
                            messages=[{"role": "user", "content": prompt}],
                        )
                        text = "".join(
                            getattr(b, "text", "") for b in (resp.content or [])
                        )
                        return parse_summary_json(text)
                    except Exception as ex:
                        print(f"[meeting_end] LLM 요약 실패: {ex}")
                        return {}

                ended = await asyncio.to_thread(MEETINGS.end_active, _summarize_with_llm)
                if ended is None:
                    await emit(type="meeting_error", message="회의 종료 실패")
                    continue
                await emit(
                    type="meeting_ended",
                    meeting_id=ended.meeting_id, title=ended.title,
                    summary=ended.summary, decisions=ended.decisions,
                    action_items=ended.action_items,
                    markdown=ended.to_markdown(),
                    saved_path=str((MEETINGS.base_dir / ended.meeting_id).resolve()),
                )

            elif mtype == "meeting_list":
                items = await asyncio.to_thread(MEETINGS.list_meetings)
                await emit(type="meeting_list", items=items)

            elif mtype == "meeting_get":
                mid = (data.get("meeting_id") or "").strip()
                m = await asyncio.to_thread(MEETINGS.get, mid)
                if m is None:
                    await emit(type="meeting_error", message="회의를 찾을 수 없습니다.")
                else:
                    await emit(type="meeting_get",
                               meeting=m.to_dict(include_transcript=True),
                               markdown=m.to_markdown())

            # ── 사이클 #21 (F-10) — 할 일/캘린더 ──────────────────────────
            elif mtype == "todo_list":
                await emit(
                    type="todo_list",
                    active=[it.as_dict() for it in TODOS.list_active()],
                    done=[it.as_dict() for it in TODOS.list_done()],
                )

            elif mtype == "todo_add":
                title = (data.get("title") or "").strip()
                due = (data.get("due") or "").strip()
                priority = (data.get("priority") or "normal").strip()
                source = (data.get("source") or "manual").strip()
                note = (data.get("note") or "").strip()
                it = TODOS.add(title, due=due, priority=priority,
                               source=source, note=note)
                if it is None:
                    await emit(type="todo_error", message="제목이 비어있습니다.")
                else:
                    await emit(type="todo_added", item=it.as_dict())

            elif mtype == "todo_done":
                item_id = (data.get("id") or "").strip()
                done = bool(data.get("done", True))
                ok = TODOS.mark_done(item_id, done=done)
                await emit(type="todo_done", id=item_id, ok=ok, done=done)

            elif mtype == "todo_remove":
                item_id = (data.get("id") or "").strip()
                ok = TODOS.remove(item_id)
                await emit(type="todo_removed", id=item_id, ok=ok)

            elif mtype == "todo_extract":
                # body: {text: 발화} → LLM 으로 항목 추출 후 자동 추가.
                utterance = (data.get("text") or "").strip()
                if len(utterance) < 4:
                    await emit(type="todo_extract_result", added=[],
                               message="추출할 텍스트가 너무 짧습니다.")
                    continue

                def _llm_call(prompt: str) -> str:
                    client = session.brain.anthropic_client
                    if client is None:
                        return ""
                    try:
                        resp = client.messages.create(
                            model=getattr(cfg, "claude_model", "claude-sonnet-4-5"),
                            max_tokens=600,
                            messages=[{"role": "user", "content": prompt}],
                        )
                        return "".join(
                            getattr(b, "text", "") for b in (resp.content or [])
                        )
                    except Exception as ex:
                        print(f"[todo_extract] LLM 호출 실패: {ex}")
                        return ""

                extracted = await asyncio.to_thread(
                    extract_todos_from_text, utterance, _llm_call,
                )
                added = []
                for raw in extracted:
                    it = TODOS.add(
                        title=raw["title"], due=raw["due"],
                        priority=raw["priority"], source="llm",
                    )
                    if it is not None:
                        added.append(it.as_dict())
                await emit(
                    type="todo_extract_result",
                    added=added,
                    message=(f"{len(added)}개 항목을 자동 추출했습니다."
                             if added else "추출된 항목이 없습니다."),
                )

            elif mtype == "ping":
                await emit(type="pong")

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] 오류: {e}")
        traceback.print_exc()
    finally:
        # 환영 인사 task 가 아직 진행 중이면 깨끗이 취소 (연결 종료 후 emit 시도 방지)
        if not welcome_task.done():
            welcome_task.cancel()
            try:
                await welcome_task
            except (asyncio.CancelledError, Exception):
                pass
        session.stop_observing()
        ACTIVE.pop(conn_id, None)


async def handle_audio(payload: bytes, emit, emit_bytes, session: UserSession, build_context, learn_and_signal=None):
    """브라우저에서 받은 WebM/Opus 오디오 → STT → Brain → TTS

    사이클 #4 T001: telemetry.log_turn() 추가 (input_channel="audio").
    """
    await emit(type="state", state="listening")
    await emit(type="emotion", emotion="listening")

    # 사이클 #9 — 3-Pillar telemetry: 음성 turn 진입 시 카운터 초기화.
    session.reset_turn_counters()

    # 임시 파일에 저장 후 Whisper 에 경로 전달
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(payload)
        path = f.name

    # 사이클 #4 T001: 음성 경로 텔레메트리
    # 사이클 #6 핫픽스: Brain 인스턴스에는 .cfg 속성이 존재하지 않으므로
    # session/brain 객체 체이닝으로 백엔드 라벨을 얻으면 dict literal 평가 중
    # AttributeError 가 try 진입 전에 raise 되어 핸들러가 죽고 WS 가 끊긴다.
    # 모듈 레벨 cfg 를 직접 사용한다.
    turn_meta = {
        "turn_id": telemetry.new_turn_id(),
        "input_channel": "audio",
        "backend": cfg.llm_backend,
        "fallback_used": False,
        "fallback_chain": [],
    }
    t_turn_start = time.monotonic()

    try:
        await emit(type="state", state="thinking")
        await emit(type="emotion", emotion="thinking")
        if STT is None:
            await emit(type="error", message="음성 인식 모델이 아직 로딩 중입니다. 잠시 후 다시 시도해 주세요.")
            await emit(type="state", state="idle")
            await emit(type="emotion", emotion="neutral")
            turn_meta["stt_ready"] = False
            return
        # 사이클 #17: 사용자 어휘로 STT 프롬프트를 동적으로 보강해 한국어
        # 고유명사/관심 토픽 인식률을 올린다. (facts 의 value + 최근 학습 지식
        # 의 topic 에서 추출 — 비용 거의 0)
        try:
            kw_facts = [
                str(f.get("value", "")).strip()
                for f in session.memory.get_facts(session.memory_user_id, limit=8)
            ]
            kw_topics = [
                str(k.get("topic", "")).strip()
                for k in session.memory.recent_knowledge(session.memory_user_id, limit=8)
            ]
            extra_prompt = build_dynamic_initial_prompt(
                "", [w for w in (kw_facts + kw_topics) if w]
            )
        except Exception:
            extra_prompt = ""
        t_stt = time.monotonic()
        text = await asyncio.to_thread(STT.transcribe, path, extra_prompt)
        turn_meta["stt_ms"] = (time.monotonic() - t_stt) * 1000.0
        text = (text or "").strip()
        turn_meta["stt_text_len_raw"] = len(text)
        # 사이클 #17: Whisper 한국어 환각 패턴 (유튜브 자막 학습 데이터에서
        # 새어나오는 "시청해주셔서 감사합니다" 등) 을 silent drop. 환각이면
        # 사용자에게 안내도 보내지 않는다 — 실제로 말한 게 아니므로.
        cleaned = clean_stt_text(text)
        turn_meta["stt_text_len"] = len(cleaned)
        # 1글자 답변은 화이트리스트("네", "예", "응") 에 속할 때만 살린다.
        # 그 외 1글자는 Whisper 가 노이즈를 한 음절로 환각한 결과일 가능성이 높음.
        # "예.", "네!" 처럼 구두점 붙은 STT 결과도 통과시킨다 (양끝 구두점 strip 후 비교).
        _SHORT_AFFIRMATIVES = {"네", "예", "응"}
        cleaned_for_short_check = cleaned.strip(" .,!?。、")
        if not cleaned or (
            len(cleaned_for_short_check) < 2
            and cleaned_for_short_check not in _SHORT_AFFIRMATIVES
        ):
            if text and not cleaned:
                turn_meta["stt_hallucination_dropped"] = text[:80]
            await emit(type="state", state="idle")
            await emit(type="emotion", emotion="neutral")
            turn_meta["empty_transcription"] = True
            return
        text = cleaned

        await emit(type="message", role="user", text=text)
        # (기획서 v2.0) 음성 turn 도 장기 메모리에 기록 + 자동 사실 추출/recall 신호.
        user_msg_id_audio: Optional[int] = None
        try:
            user_msg_id_audio = session.memory.add_message(
                session.get_conv_id(), "user", text,
            )
        except Exception:
            traceback.print_exc()

        t_llm = time.monotonic()
        # build_context 가 [기억] 블록을 주입하면 session._last_recall=True 가 set 되어
        # 이어지는 learn_and_signal 호출이 recall 인디케이터를 점등한다.
        ctx_for_audio = build_context(query=text)
        if learn_and_signal is not None:
            try:
                await learn_and_signal(text, user_msg_id_audio)
            except Exception:
                traceback.print_exc()
        emotion, reply = await asyncio.to_thread(
            session.brain.think, text, ctx_for_audio
        )
        turn_meta["llm_ms"] = (time.monotonic() - t_llm) * 1000.0
        turn_meta["emotion"] = emotion.value if hasattr(emotion, "value") else str(emotion)
        turn_meta["reply_len"] = len(reply or "")

        await emit(type="message", role="assistant", text=reply)
        # (기획서 v2.0) 어시스턴트 응답도 메모리에 기록.
        if reply:
            try:
                session.memory.add_message(
                    session.get_conv_id(), "assistant", reply,
                    emotion=turn_meta["emotion"],
                )
            except Exception:
                traceback.print_exc()
        await emit(type="emotion", emotion=emotion.value)
        await emit(type="state", state="speaking")

        def _tts_regen_audio(orig: str, reason: str) -> str:
            return session.brain.regenerate_safe_tts(orig, reason)
        t_tts = time.monotonic()
        tts_result = await asyncio.to_thread(
            TTS.synthesize_bytes_verified, reply, _tts_regen_audio,
        )
        turn_meta["tts_ms"] = (time.monotonic() - t_tts) * 1000.0
        turn_meta["tts_ok"] = tts_result["ok"]
        turn_meta["tts_reason"] = tts_result["reason"]
        turn_meta["tts_regenerated"] = bool(tts_result.get("regenerated"))
        if tts_result["audio"]:
            await emit_bytes(tts_result["audio"])
        elif not tts_result["ok"]:
            await emit(
                type="tts_blocked",
                reason=tts_result["reason"],
                message="음성 합성이 안전 검증에서 차단되었습니다 (텍스트만 표시).",
            )
    except Exception as e:
        traceback.print_exc()
        turn_meta["error"] = type(e).__name__
        await emit(type="error", message=_friendly_error(e, cfg.llm_backend))
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
        await emit(type="state", state="idle")
        await emit(type="emotion", emotion="neutral")
        turn_meta["total_ms"] = (time.monotonic() - t_turn_start) * 1000.0
        turn_meta.update(session.turn_pillar_meta())  # 사이클 #9
        try:
            telemetry.log_turn(turn_meta)
        except Exception as _e:
            print(f"[handle_audio] telemetry.log_turn failed: {_e!r}")


# ============================================================
# 헬스체크
# ============================================================
@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "backend": cfg.llm_backend,
        "stt_ready": STT is not None,
        "connections": len(ACTIVE),
    }


# 사이클 #26 — publish cold start 동안 LB health check 안정화.
# /healthz 는 어떤 backend/DB/STT 상태와도 무관하게 uvicorn 이 뜨자마자 즉시 200.
# 일부 LB 가 favicon 404 를 unhealthy 신호로 오인하는 케이스 대비해 빈 favicon 도 제공.
@app.get("/healthz")
async def healthz():
    return {"ok": True}


_FAVICON_BYTES = b""  # 빈 응답 (브라우저는 무시, LB 는 200 으로 간주)


@app.get("/favicon.ico")
async def favicon():
    from fastapi import Response
    return Response(content=_FAVICON_BYTES, media_type="image/x-icon")


# ============================================================
# Harness Evolution — 텔레메트리 요약 + Evolve 엔드포인트
# ============================================================
def _harness_auth_check(request: Request, token: Optional[str]) -> None:
    """공통 인증 게이트 — telemetry/evolve 양쪽에서 사용.

    HARNESS_TELEMETRY_TOKEN 설정 시: 토큰 검증 (query 또는 Bearer)
    미설정 시: loopback 만 허용 (개발 모드)
    """
    from fastapi import HTTPException
    expected = os.environ.get("HARNESS_TELEMETRY_TOKEN", "").strip()
    if expected:
        provided = token or ""
        if not provided:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="invalid token")
    else:
        client_host = (request.client.host if request.client else "") or ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(
                status_code=403,
                detail="HARNESS_TELEMETRY_TOKEN 미설정 — loopback 외 접근 차단. 환경변수를 설정하세요.",
            )


@app.get("/api/harness/telemetry")
async def harness_telemetry_summary(
    request: Request,
    limit: Optional[int] = None,
    token: Optional[str] = None,
):
    """Harness Evolution — 라우팅/지연/품질 집계. PII 없음."""
    _harness_auth_check(request, token)
    return telemetry.summarize(limit=limit)


@app.post("/api/harness/evolve")
async def harness_evolve_endpoint(
    request: Request,
    token: Optional[str] = None,
    min_turns: Optional[int] = None,
):
    """/harness:evolve — 누적 텔레메트리로 차세대 Harness 사이클 초안 자동 제안.

    트리거 조건: telemetry total >= MIN_TURNS (기본 10, 운영은 100+ 권장).
    Anthropic 또는 OpenAI 클라이언트가 있어야 함.
    결과 markdown 은 harness/sarvis/proposals/cycle-{n}.md 에 저장.

    보안 (사이클 #3 architect P2): min_turns 파라미터는 **상향만 허용**.
    하향 우회로 트리거 조건을 약화시킬 수 없음.
    """
    _harness_auth_check(request, token)

    from . import harness_evolve

    # min_turns clamp: 외부 입력은 절대 MIN_TURNS 미만으로 못 내림.
    effective_min = harness_evolve.MIN_TURNS
    if min_turns is not None and min_turns > effective_min:
        effective_min = int(min_turns)

    # 임시 Brain 인스턴스에서 클라이언트만 차용 (또는 활성 세션 중 첫 번째)
    anthropic_client = None
    openai_client = None
    if ACTIVE:
        first_session = next(iter(ACTIVE.values()), None)
        if first_session and getattr(first_session, "brain", None):
            anthropic_client = first_session.brain.anthropic_client
            openai_client = first_session.brain.openai_client

    # 활성 세션이 없으면 새 Brain 인스턴스 생성 (도구 없음)
    if anthropic_client is None and openai_client is None:
        from .brain import Brain
        try:
            tmp = Brain()
            anthropic_client = tmp.anthropic_client
            openai_client = tmp.openai_client
        except Exception as e:
            traceback.print_exc()
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail=f"brain_init_failed: {e}")

    result = await asyncio.to_thread(
        harness_evolve.propose_next_cycle,
        anthropic_client,
        openai_client,
        effective_min,
    )
    # markdown 본문은 응답에 포함하되, 파일 경로도 안내
    return result


# ============================================================
# 사이클 #9 — Harness 자가 개선 액션 API
# ============================================================
# 모두 _harness_auth_check 동일 게이트(token 또는 loopback) 적용.

@app.get("/api/harness/actions")
async def harness_actions_list(
    request: Request,
    token: Optional[str] = None,
):
    """현재 적용 가능한 액션 목록 + 권장값.

    응답: {actions: [...], recommendations: [...], summary_total: N}
    actions[*] = list_actions() — name/label/category/bounds/current/can_revert.
    recommendations[*] = recommend_actions(summary).
    """
    _harness_auth_check(request, token)
    from . import harness_actions
    summary = telemetry.summarize()
    return {
        "actions": harness_actions.list_actions(),
        "recommendations": harness_actions.recommend_actions(summary),
        "summary_total": summary.get("total", 0),
    }


@app.post("/api/harness/actions/apply")
async def harness_actions_apply(
    request: Request,
    token: Optional[str] = None,
):
    """액션 적용. body: {name, value, source?='dashboard'}."""
    _harness_auth_check(request, token)
    from fastapi import HTTPException
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=400, detail="missing 'name'")
    if "value" not in body:
        raise HTTPException(status_code=400, detail="missing 'value'")
    source = body.get("source") if isinstance(body.get("source"), str) else "dashboard"
    from . import harness_actions
    try:
        entry = harness_actions.apply_action(name, body["value"], source=source)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown action: {name}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "entry": entry, "actions": harness_actions.list_actions()}


@app.post("/api/harness/actions/revert")
async def harness_actions_revert(
    request: Request,
    token: Optional[str] = None,
):
    """직전 적용 한 단계 되돌리기. body: {name, source?='dashboard'}."""
    _harness_auth_check(request, token)
    from fastapi import HTTPException
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=400, detail="missing 'name'")
    source = body.get("source") if isinstance(body.get("source"), str) else "dashboard"
    from . import harness_actions
    try:
        entry = harness_actions.revert_action(name, source=source)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown action: {name}")
    if entry is None:
        return {"ok": False, "reason": "no_previous_value", "actions": harness_actions.list_actions()}
    return {"ok": True, "entry": entry, "actions": harness_actions.list_actions()}


@app.get("/api/harness/actions/audit")
async def harness_actions_audit(
    request: Request,
    token: Optional[str] = None,
    limit: Optional[int] = 50,
):
    """최근 감사 로그 N개 (apply/revert 모두)."""
    _harness_auth_check(request, token)
    from . import harness_actions
    n = max(1, min(500, int(limit or 50)))
    return {"audit": harness_actions.recent_audit(n)}


@app.post("/api/harness/evolve/export")
async def harness_evolve_export_endpoint(
    request: Request,
    token: Optional[str] = None,
):
    """사이클 #5 T003: 자동 생성된 사이클 제안서를 GitHub Issue 로 export.

    Request body (JSON):
      {
        "path": "harness/sarvis/proposals/cycle-5.md",  # 필수, PROPOSALS_DIR 안만 허용
        "repo": "owner/name",          # 선택, 미지정 시 HARNESS_GITHUB_REPO 환경변수
        "labels": ["harness"],         # 선택
        "dry_run": false               # 선택, true 면 GitHub 호출 없이 payload 검증만
      }

    인증: telemetry/evolve 와 동일 게이트 (token query 또는 Bearer header).
    GitHub 토큰: GITHUB_TOKEN/GH_TOKEN 환경변수 우선 (요청 본문에선 받지 않음 — 누출 방지).

    응답: {ok, reason, issue_url, issue_number, repo, title, dry_run}
    """
    _harness_auth_check(request, token)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    path = body.get("path")
    if not isinstance(path, str) or not path:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="missing 'path' (proposal markdown)")

    repo = body.get("repo") if isinstance(body.get("repo"), str) else None
    labels = body.get("labels") if isinstance(body.get("labels"), list) else None
    dry_run = bool(body.get("dry_run"))

    from . import harness_evolve
    result = await asyncio.to_thread(
        harness_evolve.export_proposal_to_github,
        path, repo, None, labels, dry_run,
    )
    return result


# ============================================================
# 사이클 #4 T002 — Harness 텔레메트리 실시간 WebSocket 스트림
# ============================================================
def _harness_ws_auth_ok(ws: WebSocket, token: Optional[str]) -> bool:
    """WebSocket 용 인증 게이트 — telemetry/evolve 와 동일 정책.

    HARNESS_TELEMETRY_TOKEN 설정 시: query token 또는 Authorization Bearer 검증.
    미설정 시: loopback 에서만 허용 (개발 모드).
    """
    expected = os.environ.get("HARNESS_TELEMETRY_TOKEN", "").strip()
    if expected:
        provided = (token or "").strip()
        if not provided:
            auth = ws.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        return bool(provided) and secrets.compare_digest(provided, expected)
    # loopback only
    client_host = (ws.client.host if ws.client else "") or ""
    return client_host in ("127.0.0.1", "::1", "localhost")


@app.websocket("/api/harness/ws")
async def harness_ws_endpoint(ws: WebSocket, token: Optional[str] = None):
    """텔레메트리 실시간 푸시. 연결 시 summary 1회 + 이후 새 turn 마다 push.

    메시지 형식:
      {"type": "summary", "summary": {...}}              # 연결 직후 1회
      {"type": "turn", "meta": {...}, "summary": {...}}  # 새 턴 발생 시
      {"type": "ping", "ts": <epoch>}                    # 25초 keepalive
    """
    if not _harness_ws_auth_ok(ws, token):
        await ws.close(code=4401)  # 4xxx = 앱 정책 거부
        return

    await ws.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    # 텔레메트리 구독 — 동기 콜백에서 asyncio.Queue 에 안전하게 put.
    # 사이클 #4 architect P1: QueueFull 은 put_nowait 가 *루프 스레드*에서
    # 실행될 때 raise 됨. 그래서 try/except 는 반드시 그 안에서 잡아야 한다
    # (call_soon_threadsafe 호출자 측에서는 잡히지 않음).
    def _enqueue_safe(item: Dict) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            # 클라가 느려 큐가 막히면 그냥 드롭 (다음 summary 가 보정).
            pass

    def _on_turn(meta: Dict):
        try:
            loop.call_soon_threadsafe(_enqueue_safe, {"type": "turn", "meta": meta})
        except RuntimeError:
            # 이벤트 루프 종료 후 콜백이 들어오면 무시.
            pass

    telemetry.subscribe(_on_turn)

    keepalive_task: Optional[asyncio.Task] = None
    try:
        # 1) 연결 직후 현재 summary 1회 송신.
        try:
            await ws.send_json({"type": "summary", "summary": telemetry.summarize()})
        except Exception:
            return

        # 2) keepalive ping 25초 주기.
        async def _keepalive():
            while True:
                await asyncio.sleep(25)
                try:
                    await queue.put({"type": "ping", "ts": time.time()})
                except Exception:
                    break
        keepalive_task = asyncio.create_task(_keepalive())

        # 3) 메인 루프 — 큐에서 꺼내 송신. turn 메시지엔 갱신된 summary 첨부.
        while True:
            msg = await queue.get()
            if msg["type"] == "turn":
                msg["summary"] = telemetry.summarize()
            try:
                await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError):
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[harness_ws] error: {e!r}")
    finally:
        telemetry.unsubscribe(_on_turn)
        if keepalive_task:
            keepalive_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("sarvis.server:app", host="0.0.0.0", port=5000, reload=False)
