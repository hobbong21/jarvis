"""음성 입출력 — 호출어 감지, 음성 녹음/인식, 음성 합성"""
import asyncio
import os
import tempfile
import threading
import time
from typing import Callable

# numpy / sounddevice 는 사용하는 함수 안에서만 임포트 (배포 cold start 지연 방지)
from config import cfg


# ============================================================
# 1. 호출어 감지 (Picovoice Porcupine)
# ============================================================
class WakeWordListener:
    """'자비스' 같은 호출어를 항상 듣고 있다가 콜백 실행"""

    def __init__(self, on_wake: Callable[[], None]):
        import pvporcupine
        from pvrecorder import PvRecorder

        if not cfg.porcupine_access_key:
            raise ValueError(
                "PORCUPINE_ACCESS_KEY 환경변수가 필요합니다.\n"
                "https://console.picovoice.ai/ 에서 무료로 발급받으세요."
            )
        self.porcupine = pvporcupine.create(
            access_key=cfg.porcupine_access_key,
            keywords=cfg.wake_keywords,
        )
        self.recorder = PvRecorder(frame_length=self.porcupine.frame_length)
        self.on_wake = on_wake
        self._running = False
        self._paused = False
        self._thread = None

    def start(self):
        self._running = True
        self.recorder.start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def pause(self):
        """STT나 TTS 중에는 일시 중지"""
        self._paused = True
        try:
            self.recorder.stop()
        except Exception:
            pass

    def resume(self):
        try:
            self.recorder.start()
        except Exception:
            pass
        self._paused = False

    def _loop(self):
        while self._running:
            if self._paused:
                time.sleep(0.1)
                continue
            try:
                pcm = self.recorder.read()
                if self.porcupine.process(pcm) >= 0:
                    self.on_wake()
            except Exception as e:
                # 일시 중지 중 발생하는 read 오류 무시
                if self._running and not self._paused:
                    print(f"[WakeWord] {e}")

    def stop(self):
        self._running = False
        try:
            self.recorder.stop()
            self.recorder.delete()
            self.porcupine.delete()
        except Exception:
            pass


# ============================================================
# 2. 음성 녹음 (침묵 감지 기반)
# ============================================================
class SpeechRecorder:
    SAMPLE_RATE = 16000

    def record(self):
        """말하는 동안 녹음. 일정 시간 침묵하면 종료."""
        import numpy as np
        try:
            import sounddevice as sd
        except Exception as e:
            raise RuntimeError(f"sounddevice 미설치: {e}")

        chunks = []
        silence_count = 0
        chunk_dur = 0.1
        chunk_size = int(self.SAMPLE_RATE * chunk_dur)
        max_chunks = int(cfg.max_recording / chunk_dur)
        silence_limit = int(cfg.silence_duration / chunk_dur)
        speaking = False

        with sd.InputStream(
            samplerate=self.SAMPLE_RATE, channels=1, dtype="float32"
        ) as stream:
            for _ in range(max_chunks):
                data, _ = stream.read(chunk_size)
                chunks.append(data.copy())
                vol = float(np.sqrt(np.mean(data ** 2)))
                if vol > cfg.silence_threshold:
                    speaking = True
                    silence_count = 0
                elif speaking:
                    silence_count += 1
                    if silence_count >= silence_limit:
                        break

        return np.concatenate(chunks).flatten().astype(np.float32)


# ============================================================
# 3. STT (Faster-Whisper)
# ============================================================
class WhisperSTT:
    def __init__(self):
        from faster_whisper import WhisperModel

        device = cfg.whisper_device
        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        print(f"      Whisper 로딩: {cfg.whisper_model} ({device}/{compute_type})")
        self.model = WhisperModel(
            cfg.whisper_model, device=device, compute_type=compute_type
        )

    def transcribe(self, audio) -> str:
        """numpy 배열 또는 파일 경로(str) 모두 지원 (faster-whisper 가 내부적으로 디코드)."""
        try:
            segments, _ = self.model.transcribe(
                audio,
                language=cfg.whisper_language,
                beam_size=5,
                vad_filter=True,
            )
            return " ".join(s.text for s in segments).strip()
        except Exception as e:
            print(f"[STT] 트랜스크립션 실패: {type(e).__name__}: {e}")
            return ""


# ============================================================
# 4. TTS (Edge-TTS + pygame 재생)
# ============================================================
class EdgeTTS:
    def __init__(self):
        # pygame 은 웹 서버 모드에서 사용하지 않으므로 __init__ 에서 임포트하지 않는다.
        # speak() (로컬 재생) 에서만 지연 임포트한다.
        self._pygame = None
        self._pygame_checked = False

    def _ensure_pygame(self):
        """pygame 을 처음 speak() 호출 시에만 임포트 (웹 모드에서는 호출되지 않음)."""
        if not self._pygame_checked:
            self._pygame_checked = True
            try:
                import pygame
                if not pygame.mixer.get_init():
                    pygame.mixer.init(frequency=24000)
                self._pygame = pygame
            except Exception:
                self._pygame = None

    def speak(self, text: str):
        """텍스트를 합성하여 재생 (블로킹)"""
        if not text.strip():
            return
        self._ensure_pygame()
        if self._pygame is None:
            return
        path = self._synthesize(text)
        try:
            self._pygame.mixer.music.load(path)
            self._pygame.mixer.music.play()
            while self._pygame.mixer.music.get_busy():
                self._pygame.time.Clock().tick(20)
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    def _synthesize(self, text: str) -> str:
        return asyncio.run(self._synthesize_async(text))

    async def _synthesize_async(self, text: str) -> str:
        import edge_tts
        communicate = edge_tts.Communicate(
            text, voice=cfg.tts_voice, rate=cfg.tts_rate, pitch=cfg.tts_pitch
        )
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        await communicate.save(path)
        return path

    def synthesize_bytes(self, text: str) -> bytes:
        """텍스트 → MP3 바이트 (웹 서버가 클라이언트에 푸시할 때 사용)."""
        if not text or not text.strip():
            return b""
        path = self._synthesize(text)
        try:
            with open(path, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass
