"""사비스 메인 — 로그인 → 코어(도구 포함) → Pygame UI 루프"""
import threading
import time
from typing import List, Optional

import pygame

from .auth import AuthSystem
from .audio_io import EdgeTTS, SpeechRecorder, WakeWordListener, WhisperSTT
from .brain import Brain
from .config import cfg
from .emotion import Emotion
from .tools import ToolExecutor
from .ui import SarvisUI
from .vision import VisionSystem


class SarvisCore:
    """오디오/뇌/비전/도구를 묶는 코어"""

    def __init__(self, logged_in_user: str):
        print("=" * 60)
        print("  S . A . R . V . I . S   초기화")
        print("=" * 60)

        self.logged_in_user = logged_in_user

        print("[1/6] 비전 시스템 ...")
        self.vision = VisionSystem()

        print("[2/6] 두뇌 (LLM) ...")
        self.brain = Brain()
        print(f"      백엔드: {cfg.llm_backend}")

        print("[3/6] 도구 시스템 (agent tools) ...")
        self.tools: Optional[ToolExecutor] = None
        # Claude 백엔드일 때만 도구 활성화
        if cfg.llm_backend == "claude":
            self._attach_tools()
            print(f"      등록된 도구: {len(self.tools.definitions())}개")
        else:
            print("      Ollama 백엔드 — 도구 사용 불가")

        print("[4/6] STT (Whisper) ...")
        self.stt = WhisperSTT()
        self.recorder = SpeechRecorder()

        print("[5/6] TTS (Edge-TTS) ...")
        self.tts = EdgeTTS()

        print("[6/6] 호출어 감지 (Porcupine) ...")
        self.wake = WakeWordListener(on_wake=self._on_wake)

        # ===== UI에 노출되는 공유 상태 =====
        self.state = "idle"
        self.emotion = Emotion.NEUTRAL
        self.current_tool: Optional[str] = None
        self.chat_log: List[dict] = []
        self._busy = threading.Event()
        self._first_seen = None

        print("=" * 60)
        print(f"  '{logged_in_user}'님으로 로그인. 'Sarvis' 라고 호출하세요.")
        print("=" * 60)

    def _attach_tools(self):
        """ToolExecutor 생성 및 brain에 연결 (Claude 백엔드 진입 시)"""
        if self.tools is None:
            self.tools = ToolExecutor(
                vision_system=self.vision,
                anthropic_client=self.brain.get_client(),
                on_event=self._on_tool_event,
                on_timer=self._on_timer_expired,
            )
        self.brain.tools = self.tools

    def detach_tools(self):
        """도구 비활성화 (Ollama 전환 시)"""
        self.brain.tools = None

    def reconnect_tools(self):
        """백엔드 전환 후 도구 재연결"""
        self._attach_tools()

    # -------- 라이프사이클 --------
    def start(self):
        self.wake.start()
        threading.Thread(target=self._welcome, daemon=True).start()

    def shutdown(self):
        self.wake.stop()
        self.vision.release()

    # -------- 콜백들 --------
    def _on_tool_event(self, tool_name: str, status: str):
        """ToolExecutor가 도구 실행 시작/종료 시 호출"""
        self.current_tool = tool_name if status == "start" else None

    def _on_timer_expired(self, label: str):
        """타이머 만료 시 사비스가 음성 알림"""
        prompt = f"방금 설정해둔 타이머 '{label}'이 만료됐어. 사용자에게 짧게 알려줘."
        threading.Thread(
            target=self._respond, args=(prompt, False), daemon=True
        ).start()

    def _on_wake(self):
        if self._busy.is_set():
            return
        threading.Thread(target=self._handle_conversation, daemon=True).start()

    # -------- 대화 처리 --------
    def _handle_conversation(self):
        self._busy.set()
        self.wake.pause()
        try:
            self.state = "listening"
            self.emotion = Emotion.LISTENING
            audio = self.recorder.record()

            self.state = "thinking"
            self.emotion = Emotion.THINKING
            text = self.stt.transcribe(audio)
            if not text or len(text.strip()) < 2:
                return

            self._add_log("user", text)
            print(f"\n[YOU]    {text}")

            ctx = self._build_context()
            emotion, reply = self.brain.think(text, context=ctx)
            self._add_log("assistant", reply)
            self.emotion = emotion
            print(f"[SARVIS] [{emotion.value}] {reply}\n")

            self.state = "speaking"
            self.tts.speak(reply)
        except Exception as e:
            print(f"[오류] {e}")
            self.emotion = Emotion.CONCERNED
        finally:
            self.state = "idle"
            self.emotion = Emotion.NEUTRAL
            self.current_tool = None
            self.wake.resume()
            self._busy.clear()

    def _respond(self, prompt: str, log_user_msg: bool = True):
        """내부 트리거 응답 (자동 인사, 타이머 등)"""
        if self._busy.is_set():
            return
        self._busy.set()
        self.wake.pause()
        try:
            self.state = "thinking"
            self.emotion = Emotion.THINKING
            if log_user_msg:
                self._add_log("user", prompt)

            ctx = self._build_context()
            emotion, reply = self.brain.think(prompt, context=ctx)
            self._add_log("assistant", reply)
            self.emotion = emotion
            print(f"[SARVIS] [{emotion.value}] {reply}\n")

            self.state = "speaking"
            self.tts.speak(reply)
        except Exception as e:
            print(f"[자동 응답 오류] {e}")
            self.emotion = Emotion.CONCERNED
        finally:
            self.state = "idle"
            self.emotion = Emotion.NEUTRAL
            self.current_tool = None
            self.wake.resume()
            self._busy.clear()

    def _welcome(self):
        time.sleep(1.0)
        prompt = (
            f"방금 사용자 '{self.logged_in_user}'가 시스템에 로그인했어. "
            "짧고 따뜻하게 환영 인사를 해. 도구는 이번엔 호출하지 마."
        )
        self._respond(prompt, log_user_msg=False)

    def _build_context(self) -> str:
        parts = [f"로그인 사용자: {self.logged_in_user}"]
        cam_user = self.vision.current_user
        if cam_user:
            parts.append(f"카메라에 보이는 사람: {cam_user}")
        else:
            parts.append("카메라에 등록된 사람 없음")
        return ", ".join(parts)

    def _add_log(self, role: str, text: str):
        self.chat_log.append({"role": role, "text": text})
        if len(self.chat_log) > 100:
            self.chat_log = self.chat_log[-100:]

    def maybe_auto_greet(self):
        cam_user = self.vision.current_user
        if (
            cam_user
            and cam_user != self._first_seen
            and not self._busy.is_set()
        ):
            self._first_seen = cam_user
            prompt = (
                f"방금 사용자 '{cam_user}'가 카메라 앞에 나타났어. "
                "자연스럽게 짧게 인사해. 도구는 호출하지 마."
            )
            threading.Thread(
                target=self._respond, args=(prompt, False), daemon=True
            ).start()


# ============================================================
# 엔트리 포인트
# ============================================================
def main():
    ui = SarvisUI()
    auth = AuthSystem(cfg.users_file)

    # 로그인
    user = ui.run_login(auth)
    if not user:
        ui.quit()
        print("로그인 취소. 종료합니다.")
        return

    print(f"\n[Auth] '{user}'님 로그인 성공.\n")

    try:
        core = SarvisCore(logged_in_user=user)
    except Exception as e:
        print(f"[초기화 오류] {e}")
        ui.quit()
        return

    core.start()

    running = True
    try:
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_q, pygame.K_ESCAPE):
                        running = False
                    elif event.key == pygame.K_1:
                        try:
                            core.brain.switch_backend("claude")
                            core.reconnect_tools()
                            print("→ Claude 백엔드로 전환 (도구 활성화)")
                        except Exception as e:
                            print(f"전환 실패: {e}")
                    elif event.key == pygame.K_2:
                        try:
                            core.brain.switch_backend("ollama")
                            core.detach_tools()
                            print("→ Ollama 백엔드로 전환 (도구 비활성화)")
                        except Exception as e:
                            print(f"전환 실패: {e}")
                    elif event.key == pygame.K_r:
                        core.brain.reset_history()
                        core.chat_log.clear()
                        print("→ 대화 히스토리 초기화")

            frame = core.vision.read()
            if frame is not None:
                core.vision.update_face_recognition(frame)
                core.maybe_auto_greet()

            ui.render_main(
                frame=frame,
                state=core.state,
                emotion=core.emotion,
                logged_user=core.logged_in_user,
                camera_user=core.vision.current_user,
                chat_log=core.chat_log,
                backend=cfg.llm_backend,
                current_tool=core.current_tool,
            )

            pygame.display.flip()
            ui.tick(60)
    finally:
        print("\n사비스 종료 중...")
        core.shutdown()
        ui.quit()


if __name__ == "__main__":
    main()
