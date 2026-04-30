"""J.A.R.V.I.S 웹 서버 — FastAPI + WebSocket

브라우저에서 마이크/카메라를 사용하고, 같은 Brain/Tools 파이프라인을 재사용한다.

실행:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload

또는:
    python server.py
"""
import asyncio
import json
import secrets
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from audio_io import EdgeTTS, WhisperSTT
from auth import AuthSystem
from brain import Brain
from config import cfg
from emotion import Emotion
from tools import ToolExecutor
from vision import WebVision

# ============================================================
# 전역 — 한 번만 초기화 (Whisper 모델 로딩이 무거움)
# ============================================================
print("=" * 60)
print("  J . A . R . V . I . S   웹 서버 초기화")
print("=" * 60)

WEB_DIR = Path(__file__).parent / "web"

print("[1/3] STT (Whisper) ...")
try:
    STT = WhisperSTT()
except Exception as e:
    print(f"      STT 초기화 실패: {e}")
    STT = None

print("[2/3] TTS (Edge-TTS) ...")
TTS = EdgeTTS()

print("[3/3] Auth ...")
AUTH = AuthSystem(cfg.users_file)

# 세션 토큰 → 사용자명
SESSIONS: Dict[str, str] = {}

print("=" * 60)
print(f"  준비 완료. http://localhost:5000")
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
app = FastAPI(title="JARVIS Web")

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.post("/api/auth/status")
async def auth_status():
    return {"has_users": AUTH.has_users()}


@app.post("/api/auth/register")
async def register(username: str = Form(...), password: str = Form(...)):
    err = AUTH.create_user_detail(username, password)
    if err is not None:
        raise HTTPException(status_code=400, detail=err)
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = username.strip()
    return {"token": token, "username": username.strip()}


@app.post("/api/auth/login")
async def login(username: str = Form(...), password: str = Form(...)):
    if not AUTH.verify(username, password):
        raise HTTPException(status_code=401, detail="인증 실패")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = username.strip()
    return {"token": token, "username": username.strip()}


@app.post("/api/auth/logout")
async def logout(token: str = Form(...)):
    SESSIONS.pop(token, None)
    sess = ACTIVE.pop(token, None)
    if sess:
        sess.stop_observing()
    return {"ok": True}


# ============================================================
# WebSocket — 메인 대화 채널
# ============================================================
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = ""):
    if token not in SESSIONS:
        await ws.close(code=4001, reason="invalid token")
        return

    username = SESSIONS[token]
    await ws.accept()

    session = ACTIVE.get(token)
    if session is None:
        session = UserSession(username)
        ACTIVE[token] = session

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
        username=username,
        backend=cfg.llm_backend,
        tools_enabled=session.tools is not None,
    )

    # 환영 인사 (백그라운드)
    async def welcome():
        await asyncio.sleep(0.5)
        async with busy:
            await respond_internal(
                f"방금 사용자 '{username}'가 시스템에 로그인했어. "
                "짧고 따뜻하게 환영 인사를 해. 도구는 호출하지 마.",
                log_user=False,
            )

    async def respond_internal(prompt: str, log_user: bool):
        await emit(type="state", state="thinking")
        await emit(type="emotion", emotion="thinking")
        try:
            ctx = build_context()
            if log_user:
                await emit(type="message", role="user", text=prompt)

            # 스트리밍 브릿지 (sync generator → async WebSocket)
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def run_stream():
                try:
                    for item in session.brain.think_stream(prompt, ctx):
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

            while True:
                item = await queue.get()
                if item is None:
                    break
                chunk, emo, body = item
                if emo is not None:
                    # 스트림 종료 신호
                    final_text = body or ""
                    final_emotion = emo.value
                    await emit(type="stream_end", text=final_text, emotion=final_emotion)
                elif chunk:
                    await emit(type="stream_chunk", text=chunk)

            await emit(type="emotion", emotion=final_emotion)
            await emit(type="state", state="speaking")
            audio = await asyncio.to_thread(TTS.synthesize_bytes, final_text)
            if audio:
                await emit_bytes(audio)
        except Exception as e:
            traceback.print_exc()
            await emit(type="error", message=str(e))
        finally:
            await emit(type="state", state="idle")
            await emit(type="emotion", emotion="neutral")

    def build_context() -> str:
        parts = [f"로그인 사용자: {username}"]
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
                        session.detach_tools()
                    await emit(
                        type="backend_changed",
                        backend=target,
                        tools_enabled=session.tools is not None and target == "claude",
                    )
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

            elif mtype == "ping":
                await emit(type="pong")

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] 오류: {e}")
        traceback.print_exc()
    finally:
        session.stop_observing()


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
            await emit(type="error", message="STT 모듈이 초기화되지 않았습니다.")
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

        audio = await asyncio.to_thread(TTS.synthesize_bytes, reply)
        if audio:
            await emit_bytes(audio)
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
    return {"ok": True, "backend": cfg.llm_backend, "users": len(SESSIONS)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=5000, reload=False)
