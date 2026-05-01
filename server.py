"""S.A.R.V.I.S 웹 서버 — FastAPI + WebSocket

브라우저에서 마이크/카메라를 사용하고, 같은 Brain/Tools 파이프라인을 재사용한다.

실행:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload

또는:
    python server.py
"""
import asyncio
import json
import os
import secrets
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from audio_io import EdgeTTS, WhisperSTT
from brain import Brain
from config import cfg
from emotion import Emotion
from tools import ToolExecutor
from vision import FaceRegistry, WebVision

# Harness Phase 4 — Fan-out 분석 + Evolution 텔레메트리
from analysis import parallel_analyze, analysis_to_context
import telemetry

# ============================================================
# 전역 — 서버를 즉시 시작하고 Whisper 는 백그라운드에서 로드
# ============================================================
print("=" * 60)
print("  S . A . R . V . I . S   웹 서버 초기화")
print("=" * 60)

WEB_DIR = Path(__file__).parent / "web"

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

print("[3/3] 얼굴 등록부 ...")
FACE_REGISTRY = FaceRegistry(cfg.faces_dir)
_known = FACE_REGISTRY.list_people()
if _known:
    print(f"      등록된 얼굴: {', '.join(_known)}")
else:
    print("      등록된 얼굴 없음 (웹에서 + 버튼으로 등록)")

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

        if cfg.llm_backend == "claude" and cfg.anthropic_api_key:
            self._attach_tools()

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

    # 환영 인사 (백그라운드)
    async def welcome():
        await asyncio.sleep(0.5)
        async with busy:
            await respond_internal(
                "사비스 시스템이 온라인 상태야. "
                "짧고 자신감 있게 준비 완료 인사를 해. 도구는 호출하지 마.",
                log_user=False,
            )

    async def respond_internal(prompt: str, log_user: bool):
        # compare 모드: 텍스트 입력일 때만 — Claude + OpenAI 병렬 A/B
        if cfg.llm_backend == "compare" and log_user:
            await respond_compare(prompt)
            return

        await emit(type="state", state="thinking")
        await emit(type="emotion", emotion="thinking")

        # ── Harness 텔레메트리: 턴 메타 초기화 ───────────────────────
        turn_meta = {
            "turn_id": telemetry.new_turn_id(),
            "ts": time.time(),
            "backend": cfg.llm_backend,
            "fallback_used": False,
            "fallback_chain": [cfg.llm_backend],
            "intent": None,
            "emotion": None,
            "tools_used": 0,
            "fanout_ms": 0.0,
            "llm_ms": 0.0,
            "tts_ms": 0.0,
            "tts_ok": None,
            "tts_reason": None,
            "prompt_len": len(prompt or ""),
            "reply_len": 0,
        }

        try:
            # ── Phase: Fan-out/Fan-in 사전 분석 ─────────────────────
            base_ctx = build_context()
            analysis = await parallel_analyze(prompt, session)
            turn_meta["fanout_ms"] = analysis.get("ms", 0.0)
            turn_meta["intent"] = analysis.get("intent")

            extra_ctx = analysis_to_context(analysis)
            ctx = ", ".join(p for p in (base_ctx, extra_ctx) if p)

            if log_user:
                await emit(type="message", role="user", text=prompt)

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
                    from emotion import Emotion as _E
                    loop.call_soon_threadsafe(
                        queue.put_nowait, (None, _E.CONCERNED, f"오류: {exc}")
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

            await emit(type="emotion", emotion=final_emotion)
            await emit(type="state", state="speaking")

            # ── Generate-Verify TTS 게이트 (regen 폴백 포함, 사이클 #3 #1) ─
            t_tts_start = time.monotonic()
            def _tts_regen(orig: str, reason: str) -> str:
                # 별도 스레드에서 호출됨 → brain 재호출은 sync OK
                return session.brain.regenerate_safe_tts(orig, reason)
            tts_result = await asyncio.to_thread(
                TTS.synthesize_bytes_verified, final_text, _tts_regen,
            )
            turn_meta["tts_ms"] = (time.monotonic() - t_tts_start) * 1000.0
            turn_meta["tts_ok"] = tts_result["ok"]
            turn_meta["tts_reason"] = tts_result["reason"]
            turn_meta["tts_regenerated"] = bool(tts_result.get("regenerated"))

            if tts_result["audio"]:
                await emit_bytes(tts_result["audio"])
            elif not tts_result["ok"]:
                # 합성 차단 — 사용자에게 투명하게 알림 (텍스트는 이미 표시됨)
                await emit(
                    type="tts_blocked",
                    reason=tts_result["reason"],
                    message="음성 합성이 안전 검증에서 차단되었습니다 (텍스트만 표시).",
                )
        except Exception as e:
            traceback.print_exc()
            await emit(type="error", message=str(e))
        finally:
            await emit(type="state", state="idle")
            await emit(type="emotion", emotion="neutral")
            try:
                telemetry.log_turn(turn_meta)
            except Exception:
                traceback.print_exc()

    async def respond_compare(prompt: str):
        """A/B 비교 모드 — Claude + OpenAI 동시 스트리밍, TTS 자동재생 안 함.

        사이클 #3 #4: 텔레메트리 기록 추가 (backend="compare", source 별 reply_len 합산).
        """
        await emit(type="state", state="thinking")
        await emit(type="emotion", emotion="thinking")
        await emit(type="message", role="user", text=prompt)

        # 텔레메트리 메타 — compare 모드 별도 기록
        turn_meta = {
            "turn_id": telemetry.new_turn_id(),
            "ts": time.time(),
            "backend": "compare",
            "fallback_used": False,
            "fallback_chain": ["compare:claude+openai"],
            "intent": None,
            "emotion": None,
            "tools_used": 0,
            "fanout_ms": 0.0,
            "llm_ms": 0.0,
            "tts_ms": 0.0,
            "tts_ok": None,
            "tts_reason": "compare_no_tts",
            "prompt_len": len(prompt or ""),
            "reply_len": 0,
            "compare_sources": [],
        }
        t_llm_start = time.monotonic()

        try:
            # Fan-out 분석 — compare 도 동일하게 실행 (intent 분포 통계용)
            base_ctx = build_context()
            analysis = await parallel_analyze(prompt, session)
            turn_meta["fanout_ms"] = analysis.get("ms", 0.0)
            turn_meta["intent"] = analysis.get("intent")
            extra_ctx = analysis_to_context(analysis)
            ctx = ", ".join(p for p in (base_ctx, extra_ctx) if p)

            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def run_stream():
                try:
                    for item in session.brain.compare_stream(prompt, ctx):
                        loop.call_soon_threadsafe(queue.put_nowait, item)
                except Exception as exc:
                    traceback.print_exc()
                    from emotion import Emotion as _E
                    loop.call_soon_threadsafe(
                        queue.put_nowait, ("system", None, _E.CONCERNED, f"오류: {exc}")
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
            await emit(type="error", message=str(e))
        finally:
            await emit(type="state", state="idle")
            try:
                telemetry.log_turn(turn_meta)
            except Exception:
                traceback.print_exc()

    def build_context() -> str:
        parts = []
        if session.observing:
            parts.append("행동 모니터링 활성")
        if session._last_observation:
            parts.append(f"최근 관찰: {session._last_observation}")
        return ", ".join(parts)

    asyncio.create_task(welcome())

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
                elif kind == 0x02:
                    if busy.locked():
                        continue
                    async with busy:
                        await handle_audio(payload, emit, emit_bytes, session, build_context)
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

            if mtype == "text_input":
                if busy.locked():
                    continue
                async with busy:
                    user_text = (data.get("text") or "").strip()
                    if not user_text:
                        continue
                    await respond_internal(user_text, log_user=True)

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
                        await emit(type="error", message=f"⚠ {w}")
                except Exception as e:
                    await emit(type="error", message=f"전환 실패: {e}")

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

            elif mtype == "ping":
                await emit(type="pong")

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] 오류: {e}")
        traceback.print_exc()
    finally:
        session.stop_observing()
        ACTIVE.pop(conn_id, None)


async def handle_audio(payload: bytes, emit, emit_bytes, session: UserSession, build_context):
    """브라우저에서 받은 WebM/Opus 오디오 → STT → Brain → TTS"""
    await emit(type="state", state="listening")
    await emit(type="emotion", emotion="listening")

    # 임시 파일에 저장 후 Whisper 에 경로 전달
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(payload)
        path = f.name

    try:
        await emit(type="state", state="thinking")
        await emit(type="emotion", emotion="thinking")
        if STT is None:
            await emit(type="error", message="음성 인식 모델이 아직 로딩 중입니다. 잠시 후 다시 시도해 주세요.")
            await emit(type="state", state="idle")
            await emit(type="emotion", emotion="neutral")
            return
        text = await asyncio.to_thread(STT.transcribe, path)
        text = (text or "").strip()
        if not text or len(text) < 2:
            await emit(type="state", state="idle")
            await emit(type="emotion", emotion="neutral")
            return

        await emit(type="message", role="user", text=text)

        emotion, reply = await asyncio.to_thread(
            session.brain.think, text, build_context()
        )
        await emit(type="message", role="assistant", text=reply)
        await emit(type="emotion", emotion=emotion.value)
        await emit(type="state", state="speaking")

        def _tts_regen_audio(orig: str, reason: str) -> str:
            return session.brain.regenerate_safe_tts(orig, reason)
        tts_result = await asyncio.to_thread(
            TTS.synthesize_bytes_verified, reply, _tts_regen_audio,
        )
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
        await emit(type="error", message=str(e))
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
        await emit(type="state", state="idle")
        await emit(type="emotion", emotion="neutral")


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

    import harness_evolve

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
        from brain import Brain
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=5000, reload=False)
