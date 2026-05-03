"""음성 입출력 — 호출어 감지, 음성 녹음/인식, 음성 합성"""
import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable, Optional, Tuple

# numpy / sounddevice 는 사용하는 함수 안에서만 임포트 (배포 cold start 지연 방지)
from .config import cfg


# ============================================================
# TTS 오디오 출력 검증 — 합성 결과가 실제로 들리는 음성인지 확인
# ============================================================
_AUDIO_VERIFY_ATTEMPTS = 2          # 합성+검증 총 시도 횟수
_MIN_AUDIO_DURATION_S = 0.05        # 50ms 미만 = 사실상 빈 파일/무음


def _verify_audio_bytes(audio: bytes) -> Tuple[bool, str]:
    """합성된 MP3 바이트가 재생 가능한 음성을 담고 있는지 ffprobe 로 검증.

    반환값: (ok, reason_slug)
      - ("ok",              True ): 정상
      - ("synth_empty",     False): 0바이트
      - ("ffprobe_missing", True ): ffprobe 미설치 → 검증 스킵하고 통과 (graceful degrade)
      - ("synth_corrupt",   False): ffprobe 가 파일 파싱 실패
      - ("synth_silent",    False): 오디오 스트림이 없거나 길이가 ~0
    """
    if not audio:
        return False, "synth_empty"

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        # 검증 도구가 없는 환경(서버리스/슬림 컨테이너) 에서는 차단하지 않고 통과.
        # 호출자에게 warning 으로만 노출.
        return True, "ffprobe_missing"

    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=duration,codec_type",
                "-of", "default=noprint_wrappers=1:nokey=0",
                "-i", "pipe:0",
            ],
            input=audio,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[TTS-AudioVerify] ffprobe 호출 실패: {type(e).__name__}: {e}")
        return False, "synth_corrupt"

    if proc.returncode != 0:
        return False, "synth_corrupt"

    out = proc.stdout.decode("utf-8", errors="ignore")
    if "codec_type=audio" not in out:
        return False, "synth_silent"

    duration = 0.0
    for line in out.splitlines():
        if line.startswith("duration="):
            try:
                duration = float(line.split("=", 1)[1])
            except ValueError:
                duration = 0.0
            break
    if duration < _MIN_AUDIO_DURATION_S:
        return False, "synth_silent"

    return True, "ok"


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
            _pkg_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            default_paths = [
                _os.path.join(_pkg_root, "sarvis.ppn"),
                _os.path.join(_pkg_root, "wake", "sarvis.ppn"),
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
# 3. STT — 라우팅 (OpenAI Whisper API ▸ faster-whisper 폴백)
# ============================================================
def _build_stt_prompt(extra_prompt: str = "") -> str:
    base = (cfg.whisper_initial_prompt or "").strip()
    extra = (extra_prompt or "").strip()
    if base and extra:
        return base + " " + extra
    return extra or base


class OpenAIWhisperSTT:
    """사이클 #27 (옵션 D) — OpenAI Whisper API 백엔드.

    Web Speech API 마이그레이션 후 폴백 경로(Firefox 등) 에서만 호출됨.
    로컬 모델보다 정확도·환각 내성 모두 우수. 분당 비용은 발생.
    numpy 배열은 미지원 — 호출자는 항상 파일 경로(str) 를 넘긴다 (server.py 기준).
    """

    def __init__(self):
        from openai import OpenAI

        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY 미설정")
        self.client = OpenAI()
        self.model = cfg.openai_stt_model
        print(f"      STT 백엔드: OpenAI ({self.model})")

    def transcribe(self, audio, extra_prompt: str = "") -> str:
        if not isinstance(audio, str) or not os.path.exists(audio):
            print("[STT/openai] 파일 경로만 지원 (numpy 미지원)")
            return ""
        prompt = _build_stt_prompt(extra_prompt).strip()
        try:
            with open(audio, "rb") as f:
                resp = self.client.audio.transcriptions.create(
                    model=self.model,
                    file=f,
                    language=cfg.whisper_language,
                    prompt=prompt or None,
                    temperature=0.0,
                )
            return (getattr(resp, "text", "") or "").strip()
        except Exception as e:
            print(f"[STT/openai] 트랜스크립션 실패: {type(e).__name__}: {e}")
            return ""


# ============================================================
# 음성 에너지 사전 게이트 — Whisper 호출 전 빠른 무음 감지
# ============================================================
def _audio_is_near_silent(audio_path: str,
                          *,
                          rms_threshold: float = 0.005,
                          min_speech_ratio: float = 0.04) -> bool:
    """파일이 사실상 무음/잡음만 담고 있으면 True (Whisper 호출 스킵 신호).

    Whisper 는 무음/저레벨 입력에서 한국어 자막 환각 ("시청해주셔서 감사합니다")
    을 생성하기 쉽다. transcribe 전에 numpy 로 RMS 와 speech-frame 비율을
    빠르게 계산해 명백한 무음을 차단한다.

    검사 기준:
      - 전체 RMS 가 rms_threshold 미만 → 무음
      - speech 프레임 비율이 min_speech_ratio 미만 → 잡음만

    파일 디코드/numpy 미설치 등 예외 발생 시 False (Whisper 에 위임).
    """
    if not isinstance(audio_path, str) or not os.path.exists(audio_path):
        return False
    try:
        import numpy as np
        # webm/ogg/mp3 등 압축 포맷은 ffmpeg 디코드 필요 — soundfile 만으론 부족.
        # 시도 순서: soundfile → wave (PCM only). 디코드 실패하면 False (Whisper 가 처리).
        samples = None
        try:
            import soundfile as sf
            data, _sr = sf.read(audio_path, dtype="float32", always_2d=False)
            if hasattr(data, "ndim") and data.ndim > 1:
                data = data.mean(axis=1)
            samples = data
        except Exception:
            pass
        if samples is None:
            try:
                import wave
                with wave.open(audio_path, "rb") as wf:
                    nframes = wf.getnframes()
                    raw = wf.readframes(min(nframes, 16000 * 30))  # 최대 30초
                    sw = wf.getsampwidth()
                if sw == 2:
                    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    samples = arr
            except Exception:
                return False
        if samples is None or len(samples) == 0:
            return False

        rms = float(np.sqrt(np.mean(samples ** 2)))
        if rms < rms_threshold:
            return True

        # speech-frame 비율: 100ms 청크별 RMS 가 임계 위인 비율
        chunk = max(1, len(samples) // 80)  # ≈100ms@16kHz, 안전 분모
        if chunk < 80:
            return False
        chunks = samples[: (len(samples) // chunk) * chunk].reshape(-1, chunk)
        chunk_rms = np.sqrt((chunks ** 2).mean(axis=1))
        speech_ratio = float((chunk_rms > rms_threshold).mean())
        return speech_ratio < min_speech_ratio
    except Exception:
        return False


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
        print(f"      STT 백엔드: faster-whisper {cfg.whisper_model} ({device}/{compute_type})")
        self.model = WhisperModel(
            cfg.whisper_model, device=device, compute_type=compute_type
        )
        # 사이클 #27 (옵션 C) — Silero VAD 사전 로드.
        # faster-whisper 가 내부적으로 silero 를 호출하므로 모델 캐시를 워밍해두면
        # 첫 트랜스크립션 시 추가 다운로드 지연이 사라진다.
        self._silero = None
        if cfg.use_silero_vad:
            try:
                from silero_vad import load_silero_vad
                self._silero = load_silero_vad()
                print("      Silero VAD 사전로드 OK")
            except Exception as e:
                print(f"      Silero VAD 로드 스킵: {type(e).__name__}: {e}")

    def transcribe(self, audio, extra_prompt: str = "") -> str:
        """numpy 배열 또는 파일 경로(str) 모두 지원 (faster-whisper 가 내부적으로 디코드).

        강화:
        - beam_size·VAD threshold/min_silence/speech_pad 모두 cfg 로 외부화
        - compression_ratio·no_speech threshold 보수화 → 환각 감소
        - 파일 입력 시 사전 무음 게이트 — 무음 파일에는 Whisper 호출 자체를 스킵
        - 세그먼트별 avg_logprob / no_speech_prob 검사로 저신뢰 환각 추가 차단
        """
        # 사전 무음 게이트 — 파일 경로일 때만 (numpy 입력은 SpeechRecorder 가 이미 검증)
        if isinstance(audio, str) and _audio_is_near_silent(audio):
            return ""

        initial = _build_stt_prompt(extra_prompt)
        try:
            segments, _ = self.model.transcribe(
                audio,
                language=cfg.whisper_language,
                beam_size=cfg.whisper_beam_size,
                vad_filter=True,
                vad_parameters={
                    "threshold": cfg.whisper_vad_threshold,
                    "min_silence_duration_ms": cfg.whisper_min_silence_ms,
                    "speech_pad_ms": cfg.whisper_speech_pad_ms,
                },
                initial_prompt=initial or None,
                temperature=0.0,
                condition_on_previous_text=False,
                compression_ratio_threshold=cfg.whisper_compression_ratio,
                no_speech_threshold=cfg.whisper_no_speech_threshold,
            )
            # 세그먼트 신뢰도 필터 — 환각 추가 방어막.
            #   avg_logprob < cfg.whisper_min_logprob → 매우 낮은 확신 (드롭)
            #   no_speech_prob > cfg.whisper_max_no_speech_prob → 무음 가능성 높음 (드롭)
            kept: list = []
            min_lp = cfg.whisper_min_logprob
            max_nsp = cfg.whisper_max_no_speech_prob
            for s in segments:
                avg_lp = getattr(s, "avg_logprob", None)
                nsp = getattr(s, "no_speech_prob", None)
                txt = (getattr(s, "text", "") or "").strip()
                if not txt:
                    continue
                if avg_lp is not None and avg_lp < min_lp:
                    continue
                if nsp is not None and nsp > max_nsp:
                    continue
                kept.append(txt)
            return " ".join(kept).strip()
        except Exception as e:
            print(f"[STT] 트랜스크립션 실패: {type(e).__name__}: {e}")
            return ""


def make_stt():
    """사이클 #27 — STT 백엔드 라우팅. cfg.stt_backend 와 OPENAI_API_KEY 유무에
    따라 OpenAIWhisperSTT 또는 WhisperSTT 를 반환. OpenAI 초기화 실패 시
    안전하게 faster-whisper 로 폴백한다.
    """
    backend = (cfg.stt_backend or "auto").lower()
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    if backend == "openai" or (backend == "auto" and has_openai):
        try:
            return OpenAIWhisperSTT()
        except Exception as e:
            print(f"      OpenAI STT 초기화 실패 → faster-whisper 폴백: {e}")
    return WhisperSTT()


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

    # 알려진 안전 폴백 — Edge-TTS 가 항상 제공하는 기본 한국어 음성.
    # 사용자가 잘못된 voice 를 cfg 에 넣었거나 카탈로그가 stale 한 경우에 사용.
    _FALLBACK_VOICE = "ko-KR-InJoonNeural"
    _FALLBACK_RATE = "+5%"
    _FALLBACK_PITCH = "-5Hz"

    async def _synthesize_async(self, text: str) -> str:
        """Edge-TTS 합성. 설정 음성으로 실패하면 기본 음성으로 1회 폴백.

        시도별로 *별도 temp 파일* 을 사용 — 부분 쓰기로 corrupt 된 파일을 다음
        시도가 같은 경로에 덮어쓰며 Edge-TTS 의 append/truncate 동작 차이로
        잡음 mp3 가 만들어지는 회귀 차단.
        """
        import edge_tts

        async def _try(voice: str, rate: str, pitch: str) -> Optional[str]:
            """성공 시 합성된 파일 경로, 실패 시 None. 부분 파일은 항상 정리."""
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                attempt_path = f.name
            try:
                communicate = edge_tts.Communicate(
                    text, voice=voice, rate=rate, pitch=pitch,
                )
                await communicate.save(attempt_path)
                if os.path.getsize(attempt_path) > 0:
                    return attempt_path
            except Exception as exc:
                print(f"[TTS] 합성 실패 (voice={voice}): "
                      f"{type(exc).__name__}: {exc}")
            # 실패 — 부분 파일 정리 + None 반환
            try:
                os.unlink(attempt_path)
            except OSError:
                pass
            return None

        # 1차: 사용자 설정 음성
        primary_path = await _try(cfg.tts_voice, cfg.tts_rate, cfg.tts_pitch)
        if primary_path is not None:
            return primary_path

        # 2차: 기본 폴백 음성 — 카탈로그가 stale 해도 항상 작동
        if cfg.tts_voice != EdgeTTS._FALLBACK_VOICE:
            fallback_path = await _try(
                EdgeTTS._FALLBACK_VOICE,
                EdgeTTS._FALLBACK_RATE,
                EdgeTTS._FALLBACK_PITCH,
            )
            if fallback_path is not None:
                print(f"[TTS] '{cfg.tts_voice}' 합성 실패 → 기본 음성으로 폴백")
                return fallback_path

        # 둘 다 실패 — 빈 임시 파일을 반환 (synthesize_bytes_verified 가
        # audio verify 단계에서 빈 파일을 차단하고 retry 로 처리).
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            return f.name

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
        from .tts_verifier import verify_tts_candidate

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

        warnings = list(verdict.get("warnings", []))
        audio = b""
        last_audio_reason = "synth_empty"

        # 합성 후 ffprobe 로 출력 검증. 실패 시 _AUDIO_VERIFY_ATTEMPTS 회까지 재시도 —
        # Edge-TTS 가 가끔 빈 버퍼/무음 mp3 를 돌려주는 회귀를 사용자에게 노출하기 전에 차단.
        for attempt in range(1, _AUDIO_VERIFY_ATTEMPTS + 1):
            path = self._synthesize(sanitized)
            try:
                with open(path, "rb") as f:
                    audio = f.read()
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass

            audio_ok, audio_reason = _verify_audio_bytes(audio)
            if audio_ok:
                if audio_reason == "ffprobe_missing" and "ffprobe_missing" not in warnings:
                    warnings.append("ffprobe_missing")
                return {
                    "audio": audio,
                    "ok": True,
                    "reason": "ok",
                    "warnings": warnings,
                    "length": len(sanitized),
                    "regenerated": regenerated,
                }

            last_audio_reason = audio_reason
            print(f"[TTS-AudioVerify] 시도 {attempt}/{_AUDIO_VERIFY_ATTEMPTS} 실패: {audio_reason}")

        return {
            "audio": b"",
            "ok": False,
            "reason": f"synth_retry_exhausted:{last_audio_reason}",
            "warnings": warnings,
            "length": len(sanitized),
            "regenerated": regenerated,
        }
