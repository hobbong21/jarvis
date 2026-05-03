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
from typing import Dict, Optional, Tuple

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .audio_io import EdgeTTS, WhisperSTT
from .brain import Brain, _friendly_error, _model_switch_friendly
from .config import cfg
from .emotion import Emotion
from .memory import get_memory, extract_user_facts
from .stt_filter import clean_stt_text, build_dynamic_initial_prompt
from .tools import ToolExecutor
from .vision import FaceRegistry, WebVision, compute_face_encoding_from_jpeg
from .owner_auth import OwnerAuth

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


# STT: Whisper 는 모델 다운로드가 오래 걸릴 수 있으므로 백그라운드 스레드로 로드
STT: Optional["WhisperSTT"] = None

def _load_stt():
    global STT
    print("[1/3] STT (Whisper) — 백그라운드 로딩 시작 ...")
    try:
        STT = WhisperSTT()
        print("      Whisper 모델 준비 완료.")
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
            )
        self.brain.tools = self.tools

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
    auth_state: Dict[str, Any] = {
        "face_ok": not _enrolled,
        "voice_ok": not _enrolled,
        "last_face_attempt": 0.0,
        "last_voice_attempt": 0.0,
        "welcome_started": False,
    }

    def _is_authed() -> bool:
        return bool(auth_state["face_ok"] and auth_state["voice_ok"])

    async def _emit_auth_status():
        info = OWNER_AUTH.info()
        await emit(
            type="auth_status",
            enrolled=info["enrolled"],
            face_name=info["face_name"],
            voice_passphrase_len=info["voice_passphrase_len"],
            has_face_encoding=info["has_face_encoding"],
            face_ok=auth_state["face_ok"],
            voice_ok=auth_state["voice_ok"],
            authed=_is_authed(),
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
    PRE_AUTH_ALLOWED_TYPES = {
        "enroll_owner", "auth_reset", "auth_status_request",
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
            ok = OWNER_AUTH.verify_voice(text)
            sim = OWNER_AUTH.voice_similarity_to(text)
            if ok:
                auth_state["voice_ok"] = True
            await emit(
                type="auth_progress",
                face_ok=auth_state["face_ok"],
                voice_ok=auth_state["voice_ok"],
                voice_attempt_text=text,
                voice_attempt_ok=ok,
                voice_similarity=round(sim, 3),
                message=("음성 인증 통과" if ok else "음성 패스프레이즈가 일치하지 않습니다."),
            )
            if _is_authed():
                await emit(type="auth_complete", face_name=OWNER_AUTH.face_name)
                _start_welcome_if_authed()
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    async def _try_face_login(jpeg_bytes: bytes) -> None:
        """0x01 프레임으로 얼굴 매치 시도. 인코딩 없으면 박스 감지로 폴백."""
        if not OWNER_AUTH.is_enrolled() or auth_state["face_ok"]:
            return
        now = time.time()
        if now - auth_state["last_face_attempt"] < 1.0:
            return
        auth_state["last_face_attempt"] = now

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
            # 얼굴 박스가 감지되면 통과 — 보안은 약하지만 시스템이 동작 함.
            if session.vision.face_boxes:
                matched = True
                degraded = True

        if matched:
            auth_state["face_ok"] = True
            await emit(
                type="auth_progress",
                face_ok=True,
                voice_ok=auth_state["voice_ok"],
                face_match_ok=True,
                degraded=degraded,
                message=("얼굴 인증 통과 (간이 모드)" if degraded else "얼굴 인증 통과"),
            )
            if _is_authed():
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

            if mtype == "auth_reset":
                # 등록 해제 + 세션 인증 상태 초기화. 재등록을 위한 명시적 리셋.
                OWNER_AUTH.reset()
                auth_state["face_ok"] = True
                auth_state["voice_ok"] = True
                auth_state["welcome_started"] = False
                await _emit_auth_status()
                await emit(type="auth_reset_ok",
                           message="주인 등록을 초기화했습니다. 다시 등록해주세요.")
                continue

            if mtype == "enroll_owner":
                # body: {face_name, voice_passphrase}. 카메라 프레임은 이미
                # 0x01 로 push_jpeg 됨 → crop_largest_face_jpeg 로 추출.
                face_name = (data.get("face_name") or "").strip()
                passphrase = (data.get("voice_passphrase") or "").strip()
                if not face_name or not passphrase:
                    await emit(
                        type="enroll_owner_result", ok=False,
                        message="이름과 음성 패스프레이즈를 모두 입력해주세요.",
                    )
                    continue
                crop = session.vision.crop_largest_face_jpeg(require_face=True)
                if not crop:
                    await emit(
                        type="enroll_owner_result", ok=False,
                        message="얼굴이 명확히 보이지 않습니다. 카메라를 정면으로 보고 다시 시도해주세요.",
                    )
                    continue
                # 인코딩 계산 (face_recognition 가능 시).
                enc = await asyncio.to_thread(
                    compute_face_encoding_from_jpeg, crop,
                )
                try:
                    OWNER_AUTH.enroll(face_name, passphrase, face_encoding=enc)
                    # FaceRegistry 에도 등록 — 기존 도구가 얼굴 사진을 참조할 수 있도록.
                    try:
                        FACE_REGISTRY.register(face_name, crop)
                    except Exception:
                        traceback.print_exc()
                    # 등록자는 자동 로그인 — 막 본인 얼굴/문구를 셋업했으므로.
                    auth_state["face_ok"] = True
                    auth_state["voice_ok"] = True
                    await emit(
                        type="enroll_owner_result", ok=True,
                        face_name=face_name,
                        has_face_encoding=enc is not None,
                        message=f"주인으로 등록되었습니다, {face_name} 님. 환영합니다.",
                        faces=FACE_REGISTRY.list_people(),
                    )
                    await _emit_auth_status()
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
        # 환각 또는 너무 짧은 잡음 (1글자 이하) 은 silent skip — 사용자에게
        # 안내도 보내지 않는다 (Whisper 가 무음에서 만든 가짜 발화).
        if not cleaned or len(cleaned) < 2:
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
