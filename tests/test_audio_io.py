"""audio_io.py 단위 테스트 — EdgeTTS 의 verify+regen 게이트 + speak/synth 파이프라인.

architect 사이클 #7 follow-up:
  - synthesize_bytes_verified 의 ok / blocklist / regen 성공 / regen 실패 / regen 예외 분기
  - synthesize_bytes 가 verified 결과의 audio 만 노출하는지
  - speak() 가 빈 입력은 즉시 반환하는지

task #10 추가 (음성 파이프라인 안전 테스트):
  - speak() 의 pygame 재생 루프와 임시파일 정리 (모든 하드웨어 mock)
  - _ensure_pygame 의 init / 이미-init / ImportError 폴백
  - _synthesize / _synthesize_async 가 edge_tts.Communicate 를 통해
    임시 mp3 경로를 반환하는지 (실제 네트워크 없이)
  - SpeechRecorder.record 의 침묵 종료 / 최대 시간 종료 / sounddevice 미설치
  - WhisperSTT 의 init (cpu/cuda/auto) 와 transcribe 의 정상/예외 분기
  - WakeWordListener 의 access_key 누락 / .ppn 누락 / 정상 초기화 +
    start/pause/resume/stop/_loop 분기

EdgeTTS 의 _synthesize 는 외부 네트워크/edge_tts 패키지에 의존하므로
임시 파일을 만들어 반환하는 식으로 mock 한다.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")

from sarvis.audio_io import EdgeTTS, SpeechRecorder, WhisperSTT, WakeWordListener  # noqa: E402
from sarvis.config import cfg  # noqa: E402

# numpy 는 SpeechRecorder.record() 가 lazy 임포트한다. 테스트가 patch.dict(sys.modules,...)
# 로 sounddevice 만 끼워넣을 때, 그 patch 가 시작되기 전에 numpy 가 sys.modules 에 없으면
# 종료 시점에 numpy 가 snapshot 에서 누락돼 sys.modules 에서 제거된다 → 다음 호출에서
# numpy 의 C 확장이 "cannot load module more than once per process" 로 폭주한다.
# 모듈 임포트 시점에 한 번 미리 import 해두면 항상 snapshot 에 포함돼 안전하다.
import numpy as _np_warmup  # noqa: F401, E402


def _fake_synthesize_factory(payload: bytes = b"FAKE_MP3"):
    """_synthesize 호출 시 payload 가 든 임시 파일 경로를 반환하는 mock."""
    def _impl(self, text):
        f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        f.write(payload)
        f.close()
        return f.name
    return _impl


class SynthesizeBytesVerifiedTests(unittest.TestCase):
    def setUp(self):
        self.tts = EdgeTTS()

    def test_blocked_text_returns_empty_audio(self):
        # 빈 텍스트 → verifier 가 empty 로 차단
        result = self.tts.synthesize_bytes_verified("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["audio"], b"")
        self.assertEqual(result["reason"], "empty")
        self.assertFalse(result["regenerated"])

    def test_blocklist_with_no_regen_callback(self):
        with patch("sarvis.tts_verifier._blocklist_cache", ["secret"]):
            result = self.tts.synthesize_bytes_verified("이건 secret 키")
        self.assertFalse(result["ok"])
        self.assertEqual(result["audio"], b"")
        self.assertTrue(result["reason"].startswith("blocklist:"))

    def test_ok_path_returns_audio(self):
        with patch("sarvis.tts_verifier._blocklist_cache", []), \
             patch.object(EdgeTTS, "_synthesize", _fake_synthesize_factory(b"AUDIO")):
            result = self.tts.synthesize_bytes_verified("안녕하세요.")
        self.assertTrue(result["ok"])
        self.assertEqual(result["audio"], b"AUDIO")
        self.assertEqual(result["reason"], "ok")
        self.assertFalse(result["regenerated"])
        self.assertGreater(result["length"], 0)

    def test_regen_success(self):
        # 원본은 차단, 콜백이 안전 텍스트 반환 → 합성 성공
        def regen(orig, reason):
            return "안전한 한국어 응답."

        with patch("sarvis.tts_verifier._blocklist_cache", ["bad"]), \
             patch.object(EdgeTTS, "_synthesize", _fake_synthesize_factory(b"REGEN")):
            result = self.tts.synthesize_bytes_verified("이건 bad 단어 포함",
                                                       regen_callback=regen)
        self.assertTrue(result["ok"])
        self.assertEqual(result["audio"], b"REGEN")
        self.assertTrue(result["regenerated"])

    def test_regen_callback_returns_blocked_text(self):
        # 콜백이 또 차단되는 텍스트를 주면 최종 실패 (regen_failed:...)
        def regen(orig, reason):
            return "여전히 bad 단어 포함"

        with patch("sarvis.tts_verifier._blocklist_cache", ["bad"]):
            result = self.tts.synthesize_bytes_verified("원본 bad", regen_callback=regen)
        self.assertFalse(result["ok"])
        self.assertEqual(result["audio"], b"")
        self.assertTrue(result["reason"].startswith("regen_failed:"))
        self.assertTrue(result["regenerated"])

    def test_regen_callback_raises_does_not_propagate(self):
        def regen(orig, reason):
            raise RuntimeError("콜백 폭발")

        with patch("sarvis.tts_verifier._blocklist_cache", ["bad"]):
            result = self.tts.synthesize_bytes_verified("bad", regen_callback=regen)
        # 예외는 격리 — 원래 차단 사유가 그대로 노출
        self.assertFalse(result["ok"])
        self.assertEqual(result["audio"], b"")
        self.assertTrue(result["reason"].startswith("blocklist:"))

    def test_regen_callback_returns_empty(self):
        def regen(orig, reason):
            return ""

        with patch("sarvis.tts_verifier._blocklist_cache", ["bad"]):
            result = self.tts.synthesize_bytes_verified("bad here", regen_callback=regen)
        self.assertFalse(result["ok"])
        self.assertEqual(result["audio"], b"")

    def test_synthesize_bytes_returns_audio_only(self):
        with patch("sarvis.tts_verifier._blocklist_cache", []), \
             patch.object(EdgeTTS, "_synthesize", _fake_synthesize_factory(b"X")):
            audio = self.tts.synthesize_bytes("안녕")
        self.assertEqual(audio, b"X")

    def test_synthesize_bytes_blocked_returns_empty(self):
        audio = self.tts.synthesize_bytes("")
        self.assertEqual(audio, b"")

    def test_long_text_truncated_warning_in_result(self):
        long = "이 문장은 충분히 깁니다. " * 200
        with patch("sarvis.tts_verifier._blocklist_cache", []), \
             patch.object(EdgeTTS, "_synthesize", _fake_synthesize_factory(b"L")):
            result = self.tts.synthesize_bytes_verified(long)
        self.assertTrue(result["ok"])
        self.assertTrue(any(w.startswith("truncated:") for w in result["warnings"]))


class SpeakTests(unittest.TestCase):
    def test_blank_text_no_pygame_init(self):
        tts = EdgeTTS()
        # 빈 텍스트면 _ensure_pygame 도 호출되지 않아야 — 즉시 return
        with patch.object(EdgeTTS, "_ensure_pygame") as ensure:
            tts.speak("")
            tts.speak("   ")
            ensure.assert_not_called()

    def test_no_pygame_silent_return(self):
        tts = EdgeTTS()
        # pygame 이 None 이면 _synthesize 호출 없이 return
        tts._pygame_checked = True
        tts._pygame = None
        with patch.object(EdgeTTS, "_synthesize") as synth:
            tts.speak("hi")
            synth.assert_not_called()

    def _make_fake_pygame(self, busy_ticks: int = 2):
        """pygame.mixer.music + time.Clock 을 흉내내는 객체 그래프 생성.

        get_busy() 는 busy_ticks 회 True 를 반환한 뒤 False 로 떨어져
        speak() 의 재생 루프가 종료되도록 한다.
        """
        fake = MagicMock(name="pygame")
        fake.mixer.get_init.return_value = False
        # busy 카운터 — list 로 클로저 변수
        remaining = [busy_ticks]

        def _busy():
            if remaining[0] > 0:
                remaining[0] -= 1
                return True
            return False

        fake.mixer.music.get_busy.side_effect = _busy
        # time.Clock().tick() 은 호출만 카운트되면 충분
        clock = MagicMock(name="Clock")
        fake.time.Clock.return_value = clock
        return fake, clock

    def test_speak_full_pipeline_plays_and_unlinks(self):
        """speak() 가 _synthesize → load → play → busy 루프 → unlink 까지 모두 수행."""
        tts = EdgeTTS()
        fake_pygame, clock = self._make_fake_pygame(busy_ticks=3)
        tts._pygame_checked = True
        tts._pygame = fake_pygame

        # 어떤 경로가 만들어졌는지 추적 — load() 의 첫 인자가 _synthesize 결과
        with patch.object(EdgeTTS, "_synthesize",
                          _fake_synthesize_factory(b"PLAY")):
            tts.speak("hello")

        fake_pygame.mixer.music.load.assert_called_once()
        fake_pygame.mixer.music.play.assert_called_once()
        # busy_ticks=3 이므로 tick 도 정확히 3회
        self.assertEqual(clock.tick.call_count, 3)
        # 임시 파일이 정리됐는지 (speak 가 finally 블록에서 unlink)
        synth_path = fake_pygame.mixer.music.load.call_args.args[0]
        self.assertTrue(synth_path.endswith(".mp3"))
        self.assertFalse(os.path.exists(synth_path))

    def test_speak_unlink_failure_swallowed(self):
        """unlink 가 실패해도 speak() 는 예외를 전파하지 않는다."""
        # tempfile 의 첫 호출 시 _get_default_tempdir 이 os.unlink 를 한 번 호출하므로
        # 미리 한 번 임시 파일을 만들어 캐시를 깨운 뒤 patch 한다.
        tempfile.NamedTemporaryFile(delete=True).close()

        tts = EdgeTTS()
        fake_pygame, _ = self._make_fake_pygame(busy_ticks=1)
        tts._pygame_checked = True
        tts._pygame = fake_pygame

        with patch.object(EdgeTTS, "_synthesize",
                          _fake_synthesize_factory(b"X")), \
             patch("sarvis.audio_io.os.unlink", side_effect=OSError("nope")):
            tts.speak("bye")  # 예외 없이 정상 종료해야 함

    def test_speak_play_exception_still_unlinks(self):
        """재생 도중 예외가 발생해도 finally 블록에서 임시파일을 unlink 한다."""
        tts = EdgeTTS()
        fake_pygame, _ = self._make_fake_pygame(busy_ticks=1)
        fake_pygame.mixer.music.play.side_effect = RuntimeError("audio dead")
        tts._pygame_checked = True
        tts._pygame = fake_pygame

        with patch.object(EdgeTTS, "_synthesize",
                          _fake_synthesize_factory(b"BAD")):
            with self.assertRaises(RuntimeError):
                tts.speak("kaboom")

        # load 는 호출됐고, 그 경로의 파일은 finally 에서 unlink 됐어야 함
        synth_path = fake_pygame.mixer.music.load.call_args.args[0]
        self.assertFalse(os.path.exists(synth_path))


class EnsurePygameTests(unittest.TestCase):
    """_ensure_pygame 의 import / 이미 init / ImportError 분기 커버."""

    def _install_fake_pygame(self, get_init_value: bool):
        """sys.modules 에 가짜 pygame 을 주입하고 (모듈, init 호출 캡처) 반환."""
        fake = types.ModuleType("pygame")
        fake.mixer = MagicMock()
        fake.mixer.get_init.return_value = get_init_value
        sys.modules["pygame"] = fake
        return fake

    def tearDown(self):
        sys.modules.pop("pygame", None)

    def test_ensure_pygame_initializes_mixer(self):
        fake = self._install_fake_pygame(get_init_value=False)
        tts = EdgeTTS()
        tts._ensure_pygame()
        self.assertIs(tts._pygame, fake)
        fake.mixer.init.assert_called_once_with(frequency=24000)

    def test_ensure_pygame_skips_init_when_already_initialized(self):
        fake = self._install_fake_pygame(get_init_value=True)
        tts = EdgeTTS()
        tts._ensure_pygame()
        self.assertIs(tts._pygame, fake)
        fake.mixer.init.assert_not_called()

    def test_ensure_pygame_idempotent(self):
        """두 번 호출돼도 import 는 한 번만 (체크 플래그)."""
        fake = self._install_fake_pygame(get_init_value=True)
        tts = EdgeTTS()
        tts._ensure_pygame()
        # 두 번째 호출에서는 fake.mixer 어떤 메서드도 추가 호출되면 안 됨
        fake.mixer.get_init.reset_mock()
        tts._ensure_pygame()
        fake.mixer.get_init.assert_not_called()

    def test_ensure_pygame_import_failure_sets_none(self):
        # pygame 임포트가 실패하도록 sys.modules 에 None 을 박는다.
        sys.modules["pygame"] = None
        try:
            tts = EdgeTTS()
            tts._ensure_pygame()
            self.assertIsNone(tts._pygame)
            self.assertTrue(tts._pygame_checked)
        finally:
            sys.modules.pop("pygame", None)


class SynthesizeWrapperTests(unittest.TestCase):
    """_synthesize / _synthesize_async 가 edge_tts.Communicate 를 통과하는지."""

    def test_synthesize_runs_async_and_returns_path(self):
        captured = {}

        class FakeCommunicate:
            def __init__(self, text, voice=None, rate=None, pitch=None):
                captured["text"] = text
                captured["voice"] = voice
                captured["rate"] = rate
                captured["pitch"] = pitch

            async def save(self, path):
                captured["path"] = path
                with open(path, "wb") as f:
                    f.write(b"MP3DATA")

        fake_edge = types.ModuleType("edge_tts")
        fake_edge.Communicate = FakeCommunicate

        with patch.dict(sys.modules, {"edge_tts": fake_edge}):
            tts = EdgeTTS()
            path = tts._synthesize("hello world")
        try:
            self.assertEqual(path, captured["path"])
            self.assertTrue(os.path.exists(path))
            self.assertTrue(path.endswith(".mp3"))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"MP3DATA")
            # cfg 의 voice/rate/pitch 가 그대로 전달돼야 함
            self.assertEqual(captured["voice"], cfg.tts_voice)
            self.assertEqual(captured["rate"], cfg.tts_rate)
            self.assertEqual(captured["pitch"], cfg.tts_pitch)
            self.assertEqual(captured["text"], "hello world")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_synthesize_bytes_verified_unlink_oserror_swallowed(self):
        """ok 경로에서 임시파일 unlink 가 OSError 라도 결과는 정상."""
        tts = EdgeTTS()
        with patch("sarvis.tts_verifier._blocklist_cache", []), \
             patch.object(EdgeTTS, "_synthesize",
                          _fake_synthesize_factory(b"AUD")), \
             patch("sarvis.audio_io.os.unlink", side_effect=OSError("locked")):
            result = tts.synthesize_bytes_verified("정상 텍스트입니다.")
        self.assertTrue(result["ok"])
        self.assertEqual(result["audio"], b"AUD")


# ============================================================
# SpeechRecorder
# ============================================================
class _FakeStream:
    """sounddevice.InputStream 컨텍스트 매니저 흉내."""

    def __init__(self, chunk_volumes):
        # chunk_volumes: list[float] — 각 read() 가 반환할 RMS 음량
        # numpy 가 sqrt(mean(data**2)) 로 계산하므로 데이터에 vol 을 그대로 채워둔다.
        self._volumes = list(chunk_volumes)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, chunk_size):
        import numpy as np
        if not self._volumes:
            data = np.zeros((chunk_size, 1), dtype="float32")
        else:
            v = self._volumes.pop(0)
            data = np.full((chunk_size, 1), v, dtype="float32")
        return data, False


class SpeechRecorderTests(unittest.TestCase):
    def setUp(self):
        # sounddevice 미설치 환경 — 가짜 모듈을 sys.modules 에 주입
        self.fake_sd = types.ModuleType("sounddevice")
        self.streams = []

        def _factory(samplerate=None, channels=None, dtype=None):
            s = _FakeStream(self._chunk_volumes)
            self.streams.append(s)
            return s

        self.fake_sd.InputStream = _factory

    def _patched(self):
        return patch.dict(sys.modules, {"sounddevice": self.fake_sd})

    def test_silence_terminates_recording(self):
        # 처음 5 청크는 충분히 큰 음량 (말하는 중), 그 다음은 모두 침묵
        loud = max(cfg.silence_threshold * 10, 0.2)
        # silence_duration / chunk_dur(0.1) = silence_limit 청크 동안 침묵하면 종료
        silence_limit = int(cfg.silence_duration / 0.1)
        self._chunk_volumes = [loud] * 5 + [0.0] * (silence_limit + 5)

        with self._patched():
            audio = SpeechRecorder().record()
        # 5(말함) + silence_limit(침묵) 청크 후 break
        expected_chunks = 5 + silence_limit
        chunk_size = int(SpeechRecorder.SAMPLE_RATE * 0.1)
        self.assertEqual(audio.shape[0], expected_chunks * chunk_size)
        self.assertEqual(audio.dtype.name, "float32")

    def test_max_recording_terminates(self):
        # 음량이 항상 임계치 이하 → speaking==False 라서 silence break 가 발동하지 않고
        # max_chunks 까지 채우고 종료해야 한다.
        max_chunks = int(cfg.max_recording / 0.1)
        self._chunk_volumes = [0.0] * (max_chunks + 50)
        with self._patched():
            audio = SpeechRecorder().record()
        chunk_size = int(SpeechRecorder.SAMPLE_RATE * 0.1)
        self.assertEqual(audio.shape[0], max_chunks * chunk_size)

    def test_missing_sounddevice_raises_runtimeerror(self):
        # sys.modules 에 None 을 박으면 import 가 실패 → RuntimeError 변환
        with patch.dict(sys.modules, {"sounddevice": None}):
            with self.assertRaises(RuntimeError) as cm:
                SpeechRecorder().record()
        self.assertIn("sounddevice", str(cm.exception))


# ============================================================
# WhisperSTT
# ============================================================
class _FakeSegment:
    def __init__(self, text):
        self.text = text


class _FakeWhisperModel:
    last_init_kwargs = None

    def __init__(self, model_name, device=None, compute_type=None):
        type(self).last_init_kwargs = {
            "model": model_name, "device": device, "compute_type": compute_type,
        }
        self._raise = False

    def transcribe(self, audio, **kwargs):
        if self._raise:
            raise RuntimeError("decoder boom")
        return iter([_FakeSegment("안녕"), _FakeSegment("하세요")]), object()


def _install_fake_faster_whisper():
    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = _FakeWhisperModel
    return fake


class WhisperSTTTests(unittest.TestCase):
    def test_init_explicit_cpu(self):
        fake_fw = _install_fake_faster_whisper()
        with patch.dict(sys.modules, {"faster_whisper": fake_fw}), \
             patch.object(cfg, "whisper_device", "cpu"):
            stt = WhisperSTT()
        self.assertEqual(_FakeWhisperModel.last_init_kwargs["device"], "cpu")
        self.assertEqual(_FakeWhisperModel.last_init_kwargs["compute_type"], "int8")
        self.assertIsInstance(stt.model, _FakeWhisperModel)

    def test_init_auto_falls_back_to_cpu_without_torch(self):
        fake_fw = _install_fake_faster_whisper()
        with patch.dict(sys.modules, {"faster_whisper": fake_fw, "torch": None}), \
             patch.object(cfg, "whisper_device", "auto"):
            WhisperSTT()
        self.assertEqual(_FakeWhisperModel.last_init_kwargs["device"], "cpu")

    def test_init_auto_uses_cuda_when_torch_available(self):
        fake_fw = _install_fake_faster_whisper()
        fake_torch = types.ModuleType("torch")
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: True)
        with patch.dict(sys.modules, {"faster_whisper": fake_fw, "torch": fake_torch}), \
             patch.object(cfg, "whisper_device", "auto"):
            WhisperSTT()
        self.assertEqual(_FakeWhisperModel.last_init_kwargs["device"], "cuda")
        self.assertEqual(_FakeWhisperModel.last_init_kwargs["compute_type"], "float16")

    def test_transcribe_joins_segments(self):
        fake_fw = _install_fake_faster_whisper()
        with patch.dict(sys.modules, {"faster_whisper": fake_fw}), \
             patch.object(cfg, "whisper_device", "cpu"):
            stt = WhisperSTT()
        text = stt.transcribe("dummy.wav")
        self.assertEqual(text, "안녕 하세요")

    def test_transcribe_swallows_exception(self):
        fake_fw = _install_fake_faster_whisper()
        with patch.dict(sys.modules, {"faster_whisper": fake_fw}), \
             patch.object(cfg, "whisper_device", "cpu"):
            stt = WhisperSTT()
        stt.model._raise = True
        # 예외는 빈 문자열로 격리
        self.assertEqual(stt.transcribe("dummy.wav"), "")


# ============================================================
# WakeWordListener
# ============================================================
class _FakePorcupine:
    def __init__(self, frame_length=512):
        self.frame_length = frame_length
        self._results = []
        self.deleted = False

    def process(self, pcm):
        if self._results:
            return self._results.pop(0)
        return -1

    def delete(self):
        self.deleted = True


class _FakeRecorder:
    def __init__(self, frame_length=512):
        self.frame_length = frame_length
        self.started = False
        self.stopped = False
        self.deleted = False
        self.read_calls = 0
        self.read_should_raise = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def delete(self):
        self.deleted = True

    def read(self):
        self.read_calls += 1
        if self.read_should_raise:
            raise RuntimeError("mic failure")
        return [0] * self.frame_length


def _install_fake_porcupine_modules(create_returns=None, frame_length=512):
    """sys.modules 에 가짜 pvporcupine + pvrecorder 모듈을 주입한다."""
    porcupine = create_returns or _FakePorcupine(frame_length=frame_length)

    fake_pv = types.ModuleType("pvporcupine")
    fake_pv.create = MagicMock(return_value=porcupine)

    fake_rec_mod = types.ModuleType("pvrecorder")
    recorder_holder = {}

    def _PvRecorder(frame_length=512):
        rec = _FakeRecorder(frame_length=frame_length)
        recorder_holder["rec"] = rec
        return rec

    fake_rec_mod.PvRecorder = _PvRecorder
    return fake_pv, fake_rec_mod, porcupine, recorder_holder


class WakeWordListenerTests(unittest.TestCase):
    def test_missing_access_key_raises(self):
        # __init__ 이 pvporcupine 을 lazy 임포트하므로 빈 access_key 검사 전에
        # ImportError 가 나지 않도록 가짜 모듈을 주입한다.
        fake_pv, fake_rec, _, _ = _install_fake_porcupine_modules()
        with patch.dict(sys.modules, {"pvporcupine": fake_pv, "pvrecorder": fake_rec}), \
             patch.object(cfg, "porcupine_access_key", ""):
            with self.assertRaises(ValueError):
                WakeWordListener(on_wake=lambda: None)

    def test_missing_keyword_file_raises(self):
        fake_pv, fake_rec, _, _ = _install_fake_porcupine_modules()
        with patch.dict(sys.modules, {"pvporcupine": fake_pv, "pvrecorder": fake_rec}), \
             patch.object(cfg, "porcupine_access_key", "key"), \
             patch.object(cfg, "wake_keyword_path", ""), \
             patch("os.path.exists", return_value=False):
            with self.assertRaises(FileNotFoundError):
                WakeWordListener(on_wake=lambda: None)

    def test_init_with_explicit_keyword_path(self):
        fake_pv, fake_rec, porcupine, holder = _install_fake_porcupine_modules()
        # 임시 .ppn 파일을 만들어 cfg.wake_keyword_path 로 지정
        with tempfile.NamedTemporaryFile(suffix=".ppn", delete=False) as f:
            ppn_path = f.name
        try:
            with patch.dict(sys.modules, {"pvporcupine": fake_pv, "pvrecorder": fake_rec}), \
                 patch.object(cfg, "porcupine_access_key", "key"), \
                 patch.object(cfg, "wake_keyword_path", ppn_path):
                listener = WakeWordListener(on_wake=lambda: None)
            self.assertIs(listener.porcupine, porcupine)
            self.assertIs(listener.recorder, holder["rec"])
            fake_pv.create.assert_called_once()
            kwargs = fake_pv.create.call_args.kwargs
            self.assertEqual(kwargs["access_key"], "key")
            self.assertEqual(kwargs["keyword_paths"], [ppn_path])
        finally:
            os.unlink(ppn_path)

    def test_init_autoscans_default_keyword_paths(self):
        fake_pv, fake_rec, porcupine, _ = _install_fake_porcupine_modules()
        # 첫 default 경로가 존재하는 것처럼 흉내
        real_exists = os.path.exists

        def _exists(p):
            if p.endswith("sarvis.ppn"):
                return True
            return real_exists(p)

        with patch.dict(sys.modules, {"pvporcupine": fake_pv, "pvrecorder": fake_rec}), \
             patch.object(cfg, "porcupine_access_key", "key"), \
             patch.object(cfg, "wake_keyword_path", ""), \
             patch("os.path.exists", side_effect=_exists):
            listener = WakeWordListener(on_wake=lambda: None)
        self.assertIs(listener.porcupine, porcupine)

    def _build_listener(self, porcupine):
        fake_pv, fake_rec, _, holder = _install_fake_porcupine_modules(
            create_returns=porcupine
        )
        with tempfile.NamedTemporaryFile(suffix=".ppn", delete=False) as f:
            ppn_path = f.name
        with patch.dict(sys.modules, {"pvporcupine": fake_pv, "pvrecorder": fake_rec}), \
             patch.object(cfg, "porcupine_access_key", "key"), \
             patch.object(cfg, "wake_keyword_path", ppn_path):
            listener = WakeWordListener(on_wake=lambda: None)
        os.unlink(ppn_path)
        return listener, holder["rec"]

    def test_start_pause_resume_stop_full_lifecycle(self):
        porcupine = _FakePorcupine(frame_length=8)
        # 첫 process 호출에서 wake 트리거
        porcupine._results = [0]

        wake_event = threading.Event()
        listener, recorder = self._build_listener(porcupine)
        listener.on_wake = wake_event.set

        listener.start()
        # wake 콜백이 잡힐 때까지 잠시 대기
        self.assertTrue(wake_event.wait(timeout=2.0))
        self.assertTrue(recorder.started)

        # pause → recorder.stop 호출
        listener.pause()
        self.assertTrue(listener._paused)
        # paused 상태에서 _loop 가 sleep 으로 빠지는지 잠깐 대기
        time.sleep(0.15)

        # resume → recorder 재개
        recorder.stopped = False
        listener.resume()
        self.assertFalse(listener._paused)

        listener.stop()
        # _loop 가 종료되도록 잠시 대기
        if listener._thread:
            listener._thread.join(timeout=2.0)
        self.assertTrue(porcupine.deleted)

    def test_loop_handles_recorder_read_exception(self):
        porcupine = _FakePorcupine(frame_length=8)
        listener, recorder = self._build_listener(porcupine)
        recorder.read_should_raise = True

        listener.start()
        # read 가 몇 번은 호출되도록 대기 (각 예외는 print 만 하고 계속)
        for _ in range(50):
            if recorder.read_calls > 2:
                break
            time.sleep(0.02)
        listener.stop()
        if listener._thread:
            listener._thread.join(timeout=2.0)
        self.assertGreater(recorder.read_calls, 0)

    def test_pause_resume_swallow_recorder_errors(self):
        porcupine = _FakePorcupine(frame_length=8)
        listener, recorder = self._build_listener(porcupine)

        def _raise():
            raise RuntimeError("device gone")

        recorder.stop = _raise  # pause 가 try/except 로 감싸 무시해야 함
        listener.pause()
        self.assertTrue(listener._paused)

        recorder.start = _raise  # resume 도 마찬가지
        listener.resume()
        self.assertFalse(listener._paused)

    def test_stop_swallows_cleanup_errors(self):
        porcupine = _FakePorcupine(frame_length=8)
        listener, recorder = self._build_listener(porcupine)

        def _raise():
            raise RuntimeError("nope")

        recorder.stop = _raise
        recorder.delete = _raise
        porcupine.delete = _raise
        # 예외 없이 _running 만 False 로 떨어져야 한다
        listener.stop()
        self.assertFalse(listener._running)


if __name__ == "__main__":
    unittest.main()
