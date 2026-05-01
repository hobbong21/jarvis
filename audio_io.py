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
    """'사비스' 같은 호출어를 항상 듣고 있다가 콜백 실행"""

    def __init__(self, on_wake: Callable[[], None]):
        import os as _os
        import pvporcupine
        from pvrecorder import PvRecorder

        if not cfg.porcupine_access_key:
            raise ValueError(
                "PORCUPINE_ACCESS_KEY 환경변수가 필요합니다.\n"
                "https://console.picovoice.ai/ 에서 무료로 발급받으세요."
            )

        # 커스텀 .ppn 파일 자동 탐색 ("sarvis"는 Porcupine 내장 키워드가 아님)
        keyword_path = cfg.wake_keyword_path
        if not keyword_path:
            default_paths = [
                _os.path.join(_os.path.dirname(__file__), "sarvis.ppn"),
                _os.path.join(_os.path.dirname(__file__), "wake", "sarvis.ppn"),
            ]
            for p in default_paths:
                if _os.path.exists(p):
                    keyword_path = p
                    break

        if keyword_path and _os.path.exists(keyword_path):
            self.porcupine = pvporcupine.create(
                access_key=cfg.porcupine_access_key,
                keyword_paths=[keyword_path],
            )
            print(f"[WakeWord] 커스텀 키워드 사용: {keyword_path}")
        else:
            raise FileNotFoundError(
                "'Sarvis' 호출어 파일(sarvis.ppn)이 없습니다.\n"
                "1. https://console.picovoice.ai/ppn 에서 'sarvis' 키워드 학습\n"
                "2. 다운로드한 .ppn 파일을 프로젝트 루트에 'sarvis.ppn'으로 저장\n"
                "   또는 SARVIS_KEYWORD_PATH 환경변수에 경로 지정"
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
        """numpy 배열 또는 파일 경로(str) 모두 지원 (faster-whisper 가 내부적으로 디코드).

        한국어 인식 품질을 끌어올리는 옵션:
        - initial_prompt: 한국어 패턴/호출어 힌트 (오인식 감소)
        - temperature=0.0: 결정적 디코딩 (환각 감소)
        - condition_on_previous_text=False: 이전 발화에 휘둘리는 환각 차단
        - compression_ratio_threshold/no_speech_threshold: 무음/잡음 컷오프
        - vad_parameters: 짧은 무음에서 자르지 않게 완화
        """
        try:
            segments, _ = self.model.transcribe(
                audio,
                language=cfg.whisper_language,
                beam_size=5,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                initial_prompt=cfg.whisper_initial_prompt or None,
                temperature=0.0,
                condition_on_previous_text=False,
                compression_ratio_threshold=2.4,
                no_speech_threshold=0.5,
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
        """텍스트 → MP3 바이트. 검증 게이트 통과한 경우만 합성."""
        result = self.synthesize_bytes_verified(text)
        return result["audio"]

    def synthesize_bytes_verified(self, text: str, regen_callback=None) -> dict:
        """Generate-Verify 게이트 적용 합성.

        Args:
          text          : 합성할 원본 텍스트
          regen_callback: Optional[Callable[[str, str], str]] —
                          차단 시 1회만 호출. (original_text, reason) → 안전 재작성 텍스트.
                          반환값이 비어있거나 다시 차단되면 최종 실패.

        반환:
          audio    : bytes  — MP3 (실패 시 b"")
          ok       : bool
          reason   : str    — verifier slug ("ok"/"empty"/"too_long"/"blocklist:..." 등)
          warnings : list[str]
          length   : int    — 실제 합성에 사용된 텍스트 길이
          regenerated: bool — regen_callback 으로 재작성된 텍스트가 사용됐는지
        """
        from tts_verifier import verify_tts_candidate

        verdict = verify_tts_candidate(text or "")
        regenerated = False

        if not verdict["ok"]:
            original_reason = verdict["reason"]
            print(f"[TTS-Verify] 차단: {original_reason} (len={len(text or '')})")

            # 재생성 폴백 시도 (1회) — architect 사이클 #2 P2 → 사이클 #3 #1 처리
            if regen_callback is not None:
                try:
                    regen_text = regen_callback(text or "", original_reason)
                except Exception as e:
                    print(f"[TTS-Verify] 재생성 콜백 예외: {type(e).__name__}: {e}")
                    regen_text = ""

                if regen_text and regen_text.strip():
                    regen_verdict = verify_tts_candidate(regen_text)
                    if regen_verdict["ok"]:
                        print(f"[TTS-Verify] 재생성 성공 ({original_reason} → ok, "
                              f"len {len(text or '')} → {len(regen_text)})")
                        verdict = regen_verdict
                        regenerated = True
                    else:
                        print(f"[TTS-Verify] 재생성도 차단: {regen_verdict['reason']}")
                        return {
                            "audio": b"",
                            "ok": False,
                            "reason": f"regen_failed:{original_reason}->{regen_verdict['reason']}",
                            "warnings": verdict.get("warnings", []),
                            "length": 0,
                            "regenerated": True,
                        }

            if not verdict["ok"]:
                return {
                    "audio": b"",
                    "ok": False,
                    "reason": original_reason,
                    "warnings": verdict.get("warnings", []),
                    "length": 0,
                    "regenerated": False,
                }

        sanitized = verdict["sanitized"]
        if verdict.get("warnings"):
            print(f"[TTS-Verify] 경고와 함께 합성: {verdict['warnings']}")

        path = self._synthesize(sanitized)
        try:
            with open(path, "rb") as f:
                audio = f.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        return {
            "audio": audio,
            "ok": True,
            "reason": "ok",
            "warnings": verdict.get("warnings", []),
            "length": len(sanitized),
            "regenerated": regenerated,
        }
