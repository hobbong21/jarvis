"""server.py 엔드투엔드 테스트 — REST + WebSocket 라우트.

사이클 #8 T001 (Task #8): server.py 의 라인 커버리지를 14.7% → ≥50% 로 상향.

전략:
- ``fastapi.testclient.TestClient`` 로 실제 ASGI 앱을 인-프로세스로 호출.
- import 시점에 무거운 STT/TTS/FaceRegistry 가 로드되므로
  ``SARVIS_SKIP_CV2_PRELOAD=1`` 와 임시 데이터 디렉토리(SARVIS_MEMORY_DB,
  SARVIS_FACES_DIR) 를 ``setUpModule`` 에서 미리 설정한다.
- import 후에는 모듈 전역(server.STT, server.TTS, server.FACE_REGISTRY,
  server.parallel_analyze, server.telemetry.LOG_PATH) 을 가벼운 fake 로
  교체하여 외부 의존성(Whisper/Edge-TTS/Anthropic/네트워크) 없이 실행.
- WebSocket /ws 는 환영 인사 task 가 TTS 호출을 한 번 발화하므로 fake TTS
  가 즉시 빈 audio 를 돌려주도록 한다.

커버 대상:
- GET /                       → index.html 캐시 우회 렌더
- GET /api/health             → 상태 dict
- GET /api/harness/telemetry  → token 인증, summary
- POST /api/harness/evolve    → token + Brain mock + 실행
- GET /api/harness/actions    → 카탈로그 + 권장값
- POST /api/harness/actions/apply  + /revert  + /audit
- POST /api/harness/evolve/export  (dry_run 경로)
- WebSocket /ws               → ping/pong, list_faces, register_face,
                                  delete_face, reset, observe, models_list,
                                  switch_backend, switch_model, text_input,
                                  malformed JSON 무시
- WebSocket /api/harness/ws   → 인증 실패 + 인증 성공 후 summary 송신
- 에러 경로                    → 토큰 미설정 시 비-루프백 차단(403),
                                  잘못된 토큰(401), 잘못된 body(400/404)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Generator, List, Tuple
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 무거운 import 전 환경 격리
os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")
_TMP_DIR = tempfile.mkdtemp(prefix="sarvis_test_server_")
os.environ.setdefault("SARVIS_MEMORY_DB", str(Path(_TMP_DIR) / "memory.db"))
os.environ.setdefault("SARVIS_FACES_DIR", str(Path(_TMP_DIR) / "faces"))
os.environ.setdefault("SARVIS_USERS_FILE", str(Path(_TMP_DIR) / "users.json"))
os.environ.setdefault("SARVIS_TOOL_MEMORY", str(Path(_TMP_DIR) / "tool_memory.json"))
# evolve/telemetry 인증 게이트 통과용 — 토큰 미설정 시 TestClient 는 비-loopback 으로 분류됨
os.environ["HARNESS_TELEMETRY_TOKEN"] = "test-token"
# 혹시 모를 GitHub 자격 leak 방지 (export dry_run 만 호출하므로 사용 안 함)
os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("GH_TOKEN", None)

# server.py 가 import 되는 순간 _load_stt() 가 백그라운드 스레드에서 진짜
# Whisper 모델 다운로드/로드를 시도한다. CI 변동성 + 메모리 사용량을 줄이고
# 결과적으로 server.STT 가 None 으로 유지되도록 audio_io.WhisperSTT 를 미리
# 가짜 클래스로 치환. (실제 STT 사용 분기는 fake/None 양쪽에서 모두 테스트
# 됨 — test_ws_audio_with_stt_not_ready_emits_error.)
from sarvis import audio_io  # noqa: E402


class _StubWhisperSTT:  # pragma: no cover — import-time only
    def __init__(self, *_a, **_kw):
        raise RuntimeError("WhisperSTT load disabled in tests")


audio_io.WhisperSTT = _StubWhisperSTT  # type: ignore[attr-defined]

from sarvis import server  # noqa: E402
from sarvis import telemetry  # noqa: E402
from sarvis import harness_actions  # noqa: E402
from sarvis.emotion import Emotion  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "test-token"


# ============================================================
# Fakes — 외부 의존성 차단
# ============================================================
class _FakeTTS:
    """EdgeTTS 자리 — synthesize_bytes_verified 만 사용된다."""

    def synthesize_bytes_verified(self, text: str, regen):  # noqa: D401, ARG002
        return {"audio": b"", "ok": True, "reason": "ok", "regenerated": False}


class _FakeBrain:
    """Brain 자리 — UserSession 이 호출하는 메서드만 구현."""

    def __init__(self, *_a, **_kw):
        self.tools = None
        self.anthropic_client = None
        self.openai_client = None
        self.zhipuai_client = None
        self.gemini_client = None
        self.history: List[dict] = []

    def get_client(self):
        return None

    def think_stream_with_fallback(
        self, prompt: str, ctx: str, on_fallback=None,
    ) -> Generator[Tuple[Any, Any, Any], None, None]:
        # (chunk, emotion=None, body=None) 진행 청크 1개 + (None, Emotion, body) 종결 1개
        yield ("안녕", None, None)
        yield (None, Emotion.NEUTRAL, "안녕하세요. 응답 더미입니다.")

    def think(self, text: str, ctx: str = ""):
        return Emotion.NEUTRAL, "음성 응답 더미"

    def compare_stream(self, prompt: str, ctx: str):
        yield ("claude", "더미", None, None)
        yield ("claude", None, Emotion.NEUTRAL, "claude 더미")
        yield ("openai", None, Emotion.NEUTRAL, "openai 더미")

    def switch_backend(self, target: str):
        return None

    def switch_model(self, backend: str, model: str):
        if not backend or not model:
            raise ValueError("invalid backend/model")
        return None

    def reset_history(self):
        self.history = []

    def regenerate_safe_tts(self, original: str, reason: str) -> str:
        return original


class _FakeFaceRegistry:
    def __init__(self):
        self._people: List[str] = []

    def list_people(self):
        return list(self._people)

    def register(self, name: str, jpeg_bytes: bytes) -> str:
        self._people.append(name)
        return name

    def delete(self, name: str) -> bool:
        if name in self._people:
            self._people.remove(name)
            return True
        return False


async def _fake_parallel_analyze(text: str, session=None) -> Dict[str, Any]:
    return {"intent": "chat", "ms": 0.1, "facts": [], "emotion": "neutral"}


# ============================================================
# 모듈 단위 셋업/티어다운
# ============================================================
_orig_refs: Dict[str, Any] = {}


def setUpModule() -> None:
    # Telemetry log path 를 임시 파일로 redirect — data/ 오염 방지.
    _orig_refs["LOG_PATH"] = telemetry.LOG_PATH
    telemetry.LOG_PATH = Path(_TMP_DIR) / "harness_telemetry.jsonl"

    _orig_refs["AUDIT_PATH"] = harness_actions.AUDIT_PATH
    harness_actions.AUDIT_PATH = Path(_TMP_DIR) / "harness_actions.jsonl"

    # 외부 의존성 fake 로 교체.
    _orig_refs["TTS"] = server.TTS
    _orig_refs["FACE_REGISTRY"] = server.FACE_REGISTRY
    _orig_refs["parallel_analyze"] = server.parallel_analyze
    _orig_refs["Brain"] = server.Brain

    server.TTS = _FakeTTS()
    server.FACE_REGISTRY = _FakeFaceRegistry()
    server.parallel_analyze = _fake_parallel_analyze
    server.Brain = _FakeBrain  # UserSession 이 self.brain = Brain() 호출


def tearDownModule() -> None:
    telemetry.LOG_PATH = _orig_refs.get("LOG_PATH", telemetry.LOG_PATH)
    harness_actions.AUDIT_PATH = _orig_refs.get("AUDIT_PATH", harness_actions.AUDIT_PATH)
    server.TTS = _orig_refs.get("TTS", server.TTS)
    server.FACE_REGISTRY = _orig_refs.get("FACE_REGISTRY", server.FACE_REGISTRY)
    server.parallel_analyze = _orig_refs.get("parallel_analyze", server.parallel_analyze)
    server.Brain = _orig_refs.get("Brain", server.Brain)


# ============================================================
# REST 엔드포인트 테스트
# ============================================================
class IndexEndpointTests(unittest.TestCase):
    def test_index_renders_with_cache_busting(self):
        with TestClient(server.app) as client:
            r = client.get("/")
        self.assertEqual(r.status_code, 200)
        # web/index.html 본문 + 정적 자산에 ?v= 캐시 버스터가 붙어야 한다
        self.assertIn("text/html", r.headers.get("content-type", ""))
        body = r.text
        # 셋 중 적어도 하나는 캐시 버스터가 포함됨 (정적 파일 mtime 기반)
        self.assertTrue(
            ("style.css?v=" in body)
            or ("orb.js?v=" in body)
            or ("app.js?v=" in body),
            "정적 자산 URL 에 ?v= 캐시 버스터가 주입돼야 함",
        )
        # dev 환경에서는 캐시 비활성 헤더가 응답에 붙음
        cache_ctl = r.headers.get("cache-control", "")
        self.assertIn("no-store", cache_ctl)


class HealthEndpointTests(unittest.TestCase):
    def test_health_ok(self):
        with TestClient(server.app) as client:
            r = client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIn("backend", data)
        self.assertIn("stt_ready", data)
        self.assertIn("connections", data)


class HarnessTelemetryEndpointTests(unittest.TestCase):
    def test_requires_token(self):
        with TestClient(server.app) as client:
            r = client.get("/api/harness/telemetry")
        self.assertEqual(r.status_code, 401)

    def test_wrong_token(self):
        with TestClient(server.app) as client:
            r = client.get("/api/harness/telemetry?token=wrong")
        self.assertEqual(r.status_code, 401)

    def test_summary_via_query_token(self):
        with TestClient(server.app) as client:
            r = client.get(f"/api/harness/telemetry?token={TOKEN}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("total", body)

    def test_summary_via_bearer_header(self):
        with TestClient(server.app) as client:
            r = client.get(
                "/api/harness/telemetry",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
        self.assertEqual(r.status_code, 200)


class HarnessEvolveEndpointTests(unittest.TestCase):
    def test_evolve_no_token_blocked(self):
        with TestClient(server.app) as client:
            r = client.post("/api/harness/evolve")
        self.assertEqual(r.status_code, 401)

    def test_evolve_runs_with_mocked_proposer(self):
        # propose_next_cycle 을 mock 해서 LLM 미호출 + 즉시 결과 반환.
        fake_result = {
            "ok": False, "reason": "insufficient_data: total=0 < min_turns=10",
            "cycle": None, "path": None, "markdown": None,
            "total": 0, "summary": {"total": 0},
        }
        with patch("sarvis.harness_evolve.propose_next_cycle", return_value=fake_result) as m:
            with TestClient(server.app) as client:
                r = client.post(f"/api/harness/evolve?token={TOKEN}&min_turns=999")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["reason"], fake_result["reason"])
        self.assertFalse(body["ok"])
        # min_turns 가 상향만 허용 → 999 그대로 propose 에 전달돼야 함
        self.assertTrue(m.called)
        called_min = m.call_args[0][2] if len(m.call_args[0]) >= 3 else m.call_args.kwargs.get("min_turns")
        self.assertEqual(called_min, 999)


class HarnessActionsEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        # 카탈로그/cfg 격리 — apply 누적이 다른 테스트로 새지 않게.
        harness_actions.reset_catalog_for_tests()

    def tearDown(self) -> None:
        harness_actions.reset_catalog_for_tests()

    def test_list_actions(self):
        with TestClient(server.app) as client:
            r = client.get(f"/api/harness/actions?token={TOKEN}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("actions", body)
        self.assertIn("recommendations", body)
        self.assertIsInstance(body["actions"], list)
        self.assertGreater(len(body["actions"]), 0)

    def test_apply_then_revert(self):
        actions = harness_actions.list_actions()
        target = actions[0]
        name = target["name"]
        # 안전 범위 안 값으로 적용
        lo, hi = target["bounds"]
        new_value = (lo + hi) / 2

        with TestClient(server.app) as client:
            r_apply = client.post(
                f"/api/harness/actions/apply?token={TOKEN}",
                json={"name": name, "value": new_value, "source": "test"},
            )
            self.assertEqual(r_apply.status_code, 200, r_apply.text)
            self.assertTrue(r_apply.json()["ok"])

            # revert 1회 — 직전 값으로 복원
            r_rev = client.post(
                f"/api/harness/actions/revert?token={TOKEN}",
                json={"name": name},
            )
            self.assertEqual(r_rev.status_code, 200)
            self.assertTrue(r_rev.json()["ok"])

            # revert 두 번째 — 적용 이력 없으므로 ok=False
            r_rev2 = client.post(
                f"/api/harness/actions/revert?token={TOKEN}",
                json={"name": name},
            )
            self.assertEqual(r_rev2.status_code, 200)
            self.assertFalse(r_rev2.json()["ok"])

            # 감사 로그
            r_audit = client.get(f"/api/harness/actions/audit?token={TOKEN}&limit=10")
            self.assertEqual(r_audit.status_code, 200)
            self.assertIn("audit", r_audit.json())

    def test_apply_missing_name(self):
        with TestClient(server.app) as client:
            r = client.post(
                f"/api/harness/actions/apply?token={TOKEN}",
                json={"value": 1.0},
            )
        self.assertEqual(r.status_code, 400)

    def test_apply_unknown_name(self):
        with TestClient(server.app) as client:
            r = client.post(
                f"/api/harness/actions/apply?token={TOKEN}",
                json={"name": "no_such_action", "value": 1},
            )
        self.assertEqual(r.status_code, 404)

    def test_apply_malformed_json(self):
        # body 가 JSON 이 아닐 때 — 서버는 빈 dict 로 처리 후 400 (missing 'name').
        with TestClient(server.app) as client:
            r = client.post(
                f"/api/harness/actions/apply?token={TOKEN}",
                content=b"not-json",
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(r.status_code, 400)

    def test_revert_missing_name(self):
        with TestClient(server.app) as client:
            r = client.post(
                f"/api/harness/actions/revert?token={TOKEN}",
                json={},
            )
        self.assertEqual(r.status_code, 400)


class HarnessEvolveExportEndpointTests(unittest.TestCase):
    def test_export_missing_path(self):
        with TestClient(server.app) as client:
            r = client.post(
                f"/api/harness/evolve/export?token={TOKEN}",
                json={},
            )
        self.assertEqual(r.status_code, 400)

    def test_export_invalid_path_returns_failure(self):
        # path traversal/존재하지 않는 path → harness_evolve 가 invalid_proposal_path 반환
        with TestClient(server.app) as client:
            r = client.post(
                f"/api/harness/evolve/export?token={TOKEN}",
                json={"path": "/etc/passwd", "dry_run": True},
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "invalid_proposal_path")


# ============================================================
# WebSocket /ws — 메인 대화 채널
# ============================================================
def _drain_until(ws, predicate, max_msgs: int = 40):
    """원하는 메시지가 올 때까지 수신 (최대 max_msgs). 못 받으면 None."""
    seen = []
    for _ in range(max_msgs):
        try:
            data = ws.receive()
        except Exception:
            break
        if data.get("type") == "websocket.disconnect":
            break
        if "text" in data and data["text"]:
            try:
                obj = json.loads(data["text"])
            except Exception:
                continue
            seen.append(obj)
            if predicate(obj):
                return obj, seen
        elif "bytes" in data and data["bytes"] is not None:
            seen.append({"_bytes": len(data["bytes"])})
    return None, seen


class MainWebSocketTests(unittest.TestCase):
    def test_ws_ready_and_ping(self):
        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ws:
                # 첫 메시지는 ready
                ready = ws.receive_json()
                self.assertEqual(ready["type"], "ready")
                self.assertIn("backend", ready)

                ws.send_text(json.dumps({"type": "ping"}))
                pong, _ = _drain_until(ws, lambda o: o.get("type") == "pong")
                self.assertIsNotNone(pong, "pong 미수신")

    def test_ws_list_faces_and_register_delete(self):
        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # ready

                ws.send_text(json.dumps({"type": "list_faces"}))
                lst, _ = _drain_until(ws, lambda o: o.get("type") == "face_list")
                self.assertIsNotNone(lst)
                self.assertEqual(lst["faces"], [])

                # 등록 — vision.crop_largest_face_jpeg() 가 None 반환하므로 실패 응답
                ws.send_text(json.dumps({"type": "register_face", "name": "alice"}))
                reg, _ = _drain_until(
                    ws, lambda o: o.get("type") == "face_register_result"
                )
                self.assertIsNotNone(reg)
                # 카메라 프레임이 없어 ok=False
                self.assertFalse(reg["ok"])

                # 빈 이름 → 안내 메시지
                ws.send_text(json.dumps({"type": "register_face", "name": ""}))
                reg2, _ = _drain_until(
                    ws, lambda o: o.get("type") == "face_register_result"
                )
                self.assertIsNotNone(reg2)
                self.assertFalse(reg2["ok"])

                # 삭제 — 존재하지 않으면 ok=False
                ws.send_text(json.dumps({"type": "delete_face", "name": "ghost"}))
                dele, _ = _drain_until(
                    ws, lambda o: o.get("type") == "face_delete_result"
                )
                self.assertIsNotNone(dele)
                self.assertFalse(dele["ok"])

    def test_ws_reset_observe_models_and_switch(self):
        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # ready

                ws.send_text(json.dumps({"type": "reset"}))
                ack, _ = _drain_until(ws, lambda o: o.get("type") == "reset_ack")
                self.assertIsNotNone(ack)

                # observe on — Claude 백엔드 아니면 error 메시지가 나올 수 있음
                ws.send_text(json.dumps({"type": "observe", "on": False}))
                ob, _ = _drain_until(ws, lambda o: o.get("type") == "observe_state")
                self.assertIsNotNone(ob)
                self.assertFalse(ob["on"])

                ws.send_text(json.dumps({"type": "models_list"}))
                ml, _ = _drain_until(ws, lambda o: o.get("type") == "models_list")
                self.assertIsNotNone(ml)
                self.assertIn("catalog", ml)

                # switch_backend → backend_changed 발화
                ws.send_text(json.dumps({"type": "switch_backend", "backend": "openai"}))
                ch, _ = _drain_until(ws, lambda o: o.get("type") == "backend_changed")
                self.assertIsNotNone(ch)
                self.assertEqual(ch["backend"], "openai")

                # switch_model 빈 인자 → ValueError → error 메시지 발화
                ws.send_text(json.dumps({"type": "switch_model", "backend": "", "model": ""}))
                err, _ = _drain_until(ws, lambda o: o.get("type") == "error")
                self.assertIsNotNone(err)

    def test_ws_text_input_streams_response(self):
        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # ready

                ws.send_text(json.dumps({"type": "text_input", "text": "안녕"}))
                # stream_end 가 도착해야 응답 완료
                end, history = _drain_until(
                    ws, lambda o: o.get("type") == "stream_end", max_msgs=80,
                )
                self.assertIsNotNone(end, f"stream_end 미수신. history={history!r}")
                self.assertIn("text", end)

    def test_ws_malformed_text_is_ignored(self):
        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # ready
                ws.send_text("not-a-json")
                # 그냥 무시되는지만 확인 — 이어서 ping 보내고 응답 받기
                ws.send_text(json.dumps({"type": "ping"}))
                pong, _ = _drain_until(ws, lambda o: o.get("type") == "pong")
                self.assertIsNotNone(pong)

    def test_ws_audio_with_stt_not_ready_emits_error(self):
        # STT 가 None 인 상태에서 오디오 바이너리 전송 → 에러 메시지 발화
        prev_stt = server.STT
        server.STT = None
        try:
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws") as ws:
                    ws.receive_json()  # ready
                    # 0x02 = audio 매직 + 임의 페이로드
                    ws.send_bytes(b"\x02fakeaudio")
                    err, _ = _drain_until(
                        ws, lambda o: o.get("type") == "error", max_msgs=30,
                    )
                    self.assertIsNotNone(err)
        finally:
            server.STT = prev_stt


# ============================================================
# WebSocket /ws — 음성 입력 (handle_audio) 오케스트레이션
# ============================================================
# Task #14: 사이클 #8 의 STT-not-ready 에러 분기에 더해, STT 가 정상일 때의
# handle_audio 본문 (server.py 1062-1133) 70+ 라인을 회귀 검사한다.
#
# 검증하는 분기:
# - 정상 경로: STT → Brain → TTS → 클라이언트가 user msg + assistant msg + audio bytes 수신
# - 빈 transcription: STT 가 "" 또는 1자만 돌려주면 Brain/TTS 가 호출되지 않고 idle 로 복귀
# - TTS 차단: synthesize_bytes_verified 가 ok=False / audio=b"" → tts_blocked 이벤트 발화
# - Brain 예외: think() 가 raise → user msg 후 error 이벤트 + 임시 파일 정리
class _RecordingSTT:
    """`server.STT` 자리 — transcribe(path) 호출을 기록하고 미리 정한 텍스트 반환."""

    def __init__(self, text: str = "안녕"):
        self.text = text
        self.calls: List[str] = []

    def transcribe(self, path: str) -> str:
        self.calls.append(path)
        return self.text


class _ScriptedTTS:
    """`server.TTS` 자리 — synthesize_bytes_verified 가 미리 정한 dict 반환."""

    def __init__(
        self,
        audio: bytes = b"FAKEMP3",
        ok: bool = True,
        reason: str = "ok",
        regenerated: bool = False,
    ):
        self.audio = audio
        self.ok = ok
        self.reason = reason
        self.regenerated = regenerated
        self.calls: List[str] = []

    def synthesize_bytes_verified(self, text, regen):  # noqa: ARG002
        self.calls.append(text)
        return {
            "audio": self.audio,
            "ok": self.ok,
            "reason": self.reason,
            "regenerated": self.regenerated,
        }


def _drain_audio_turn(ws, max_msgs: int = 80):
    """오디오 turn 종료 신호(state=idle 발화)까지 모든 msg/bytes 를 모은다.

    handle_audio 의 finally 가 항상 'state=idle' 를 emit 하므로 이를 종료
    표지로 사용한다. (환영 인사가 보낼 수 있는 idle 도 같은 이벤트지만, 우리는
    audio 를 보내기 전에 await receive_json()=ready 만 한 직후 곧바로 audio
    를 send 하므로 환영 인사는 0.5s sleep 중에 _preempt_welcome() 가 취소함.)
    """
    seen: List[Dict[str, Any]] = []
    handler_started = False
    for _ in range(max_msgs):
        try:
            data = ws.receive()
        except Exception:
            break
        if data.get("type") == "websocket.disconnect":
            break
        if data.get("text"):
            try:
                obj = json.loads(data["text"])
            except Exception:
                continue
            seen.append(obj)
            # handle_audio 진입 표지: state=listening 또는 thinking
            if obj.get("type") == "state" and obj.get("state") in {"listening", "thinking"}:
                handler_started = True
            if (
                handler_started
                and obj.get("type") == "state"
                and obj.get("state") == "idle"
            ):
                return seen
        elif data.get("bytes") is not None:
            seen.append({"_bytes": len(data["bytes"])})
    return seen


class HandleAudioTests(unittest.TestCase):
    """server.handle_audio 본문 회귀 검사 (Task #14)."""

    def setUp(self) -> None:
        # 매 테스트마다 STT/TTS 를 깨끗한 fake 로 갈아끼우고 종료 시 복원.
        self._prev_stt = server.STT
        self._prev_tts = server.TTS
        # 텔레메트리 결과 검사를 위해 LOG_PATH 를 테스트 단위 임시 파일로 분리.
        self._prev_log_path = telemetry.LOG_PATH
        self._tmp_log = Path(_TMP_DIR) / f"audio_turn_{id(self)}.jsonl"
        if self._tmp_log.exists():
            self._tmp_log.unlink()
        telemetry.LOG_PATH = self._tmp_log

    def tearDown(self) -> None:
        server.STT = self._prev_stt
        server.TTS = self._prev_tts
        telemetry.LOG_PATH = self._prev_log_path

    def _read_telemetry(self) -> List[Dict[str, Any]]:
        if not self._tmp_log.exists():
            return []
        rows: List[Dict[str, Any]] = []
        with open(self._tmp_log, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        return rows

    # --------------- 정상 경로 ---------------
    def test_audio_happy_path_emits_user_assistant_and_audio_bytes(self):
        stt = _RecordingSTT(text="안녕")
        tts = _ScriptedTTS(audio=b"FAKEMP3", ok=True, reason="ok")
        server.STT = stt
        server.TTS = tts

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # ready
                ws.send_bytes(b"\x02" + b"webm")
                seen = _drain_audio_turn(ws)

        # STT 가 webm 임시 파일 경로로 호출됐고 그 파일은 finally 에서 삭제됐는지.
        self.assertEqual(len(stt.calls), 1, f"STT.transcribe 1회 호출 기대, seen={seen!r}")
        self.assertTrue(stt.calls[0].endswith(".webm"))
        self.assertFalse(
            Path(stt.calls[0]).exists(),
            "handle_audio finally 가 임시 .webm 을 정리해야 함",
        )

        # 사용자 메시지 → 어시스턴트 메시지 → 오디오 바이트 순서 확인.
        user_msgs = [o for o in seen if o.get("type") == "message" and o.get("role") == "user"]
        asst_msgs = [o for o in seen if o.get("type") == "message" and o.get("role") == "assistant"]
        audio_chunks = [o for o in seen if "_bytes" in o]
        self.assertEqual(len(user_msgs), 1, f"user message 1건 기대. seen={seen!r}")
        self.assertEqual(user_msgs[0]["text"], "안녕")
        self.assertEqual(len(asst_msgs), 1, f"assistant message 1건 기대. seen={seen!r}")
        self.assertEqual(asst_msgs[0]["text"], "음성 응답 더미")
        self.assertGreaterEqual(len(audio_chunks), 1, "TTS audio bytes 가 emit_bytes 로 와야 함")
        self.assertEqual(audio_chunks[0]["_bytes"], len(b"FAKEMP3"))

        # 순서 검증: user msg index < assistant msg index < audio bytes index.
        def _idx(pred):
            for i, o in enumerate(seen):
                if pred(o):
                    return i
            return -1

        i_user = _idx(lambda o: o.get("type") == "message" and o.get("role") == "user")
        i_asst = _idx(lambda o: o.get("type") == "message" and o.get("role") == "assistant")
        i_audio = _idx(lambda o: "_bytes" in o)
        self.assertLess(i_user, i_asst, f"user 가 assistant 보다 먼저 와야 함. seen={seen!r}")
        self.assertLess(i_asst, i_audio, f"assistant 가 audio bytes 보다 먼저 와야 함. seen={seen!r}")

        # speaking 상태 진입 (TTS 단계 도달) 검증.
        self.assertTrue(
            any(o.get("type") == "state" and o.get("state") == "speaking" for o in seen),
            f"speaking state 미발화. seen={seen!r}",
        )

        # 텔레메트리: input_channel='audio' + tts_ok=True + reply_len>0.
        rows = self._read_telemetry()
        audio_rows = [r for r in rows if r.get("input_channel") == "audio"]
        self.assertEqual(len(audio_rows), 1, f"audio turn 1건 텔레메트리 기대. rows={rows!r}")
        row = audio_rows[0]
        self.assertTrue(row.get("tts_ok"))
        self.assertEqual(row.get("tts_reason"), "ok")
        self.assertGreater(row.get("reply_len", 0), 0)
        self.assertGreater(row.get("stt_text_len", 0), 0)
        self.assertNotIn("error", row)
        self.assertNotIn("empty_transcription", row)

    # --------------- 빈 / 너무 짧은 transcription ---------------
    def _assert_short_transcription_is_skipped(self, stt_text: str) -> None:
        """STT 결과가 '' 혹은 1글자 (len(text)<2) 일 때 Brain/TTS 가 스킵돼야 한다는 공통 검증."""
        stt = _RecordingSTT(text=stt_text)
        tts = _ScriptedTTS()
        server.STT = stt
        server.TTS = tts

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # ready
                ws.send_bytes(b"\x02" + b"webm")
                seen = _drain_audio_turn(ws)

        # message / tts_blocked / error 가 한 건도 안 와야 한다.
        self.assertFalse(
            any(o.get("type") == "message" for o in seen),
            f"짧은 transcription({stt_text!r}) → user/assistant message 발화 금지. seen={seen!r}",
        )
        self.assertFalse(any(o.get("type") == "tts_blocked" for o in seen))
        self.assertFalse(any(o.get("type") == "error" for o in seen))
        # TTS 는 호출되지 않았어야 함.
        self.assertEqual(len(tts.calls), 0, "TTS 가 호출되면 안 됨")
        # state=thinking 까지는 도달했지만 speaking 은 못 가야 함.
        self.assertTrue(any(o.get("type") == "state" and o.get("state") == "thinking" for o in seen))
        self.assertFalse(any(o.get("type") == "state" and o.get("state") == "speaking" for o in seen))

        # 텔레메트리에 empty_transcription=True 기록.
        rows = self._read_telemetry()
        audio_rows = [r for r in rows if r.get("input_channel") == "audio"]
        self.assertEqual(len(audio_rows), 1)
        self.assertTrue(audio_rows[0].get("empty_transcription"))

    def test_audio_empty_transcription_skips_brain_and_tts(self):
        # 완전히 빈 문자열 — `not text` 분기.
        self._assert_short_transcription_is_skipped("")

    def test_audio_one_char_transcription_skips_brain_and_tts(self):
        # 1글자 — `len(text) < 2` 분기. Whisper 가 노이즈를 한 음절로 환각하는
        # 흔한 케이스를 별도 회귀 검사로 고정.
        self._assert_short_transcription_is_skipped("가")


    # --------------- TTS 차단 ---------------
    def test_audio_tts_blocked_emits_tts_blocked_message(self):
        stt = _RecordingSTT(text="안녕")
        tts = _ScriptedTTS(audio=b"", ok=False, reason="blocklist:테스트")
        server.STT = stt
        server.TTS = tts

        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # ready
                ws.send_bytes(b"\x02" + b"webm")
                seen = _drain_audio_turn(ws)

        # user msg + assistant msg 는 와야 한다 (TTS 만 차단).
        self.assertTrue(
            any(o.get("type") == "message" and o.get("role") == "user" for o in seen),
            f"user message 누락. seen={seen!r}",
        )
        self.assertTrue(
            any(o.get("type") == "message" and o.get("role") == "assistant" for o in seen),
            f"assistant message 누락. seen={seen!r}",
        )
        blocked = [o for o in seen if o.get("type") == "tts_blocked"]
        self.assertEqual(len(blocked), 1, f"tts_blocked 1건 기대. seen={seen!r}")
        self.assertEqual(blocked[0]["reason"], "blocklist:테스트")
        self.assertIn("message", blocked[0])
        # 오디오 바이트는 와선 안 된다.
        self.assertFalse(any("_bytes" in o for o in seen), "audio bytes 가 와선 안 됨")

        # 순서 검증: user message → assistant message → tts_blocked.
        i_user = next((i for i, o in enumerate(seen) if o.get("type") == "message" and o.get("role") == "user"), -1)
        i_asst = next((i for i, o in enumerate(seen) if o.get("type") == "message" and o.get("role") == "assistant"), -1)
        i_blocked = next((i for i, o in enumerate(seen) if o.get("type") == "tts_blocked"), -1)
        self.assertGreaterEqual(i_user, 0)
        self.assertLess(i_user, i_asst, "user 가 assistant 보다 먼저 와야 함")
        self.assertLess(i_asst, i_blocked, "assistant 가 tts_blocked 보다 먼저 와야 함")


        # 텔레메트리: tts_ok=False / tts_reason=blocklist:....
        rows = self._read_telemetry()
        audio_rows = [r for r in rows if r.get("input_channel") == "audio"]
        self.assertEqual(len(audio_rows), 1)
        self.assertFalse(audio_rows[0].get("tts_ok"))
        self.assertEqual(audio_rows[0].get("tts_reason"), "blocklist:테스트")

    # --------------- Brain 예외 ---------------
    def test_audio_brain_exception_emits_error_and_cleans_up_temp_file(self):
        stt = _RecordingSTT(text="안녕")
        tts = _ScriptedTTS()
        server.STT = stt
        server.TTS = tts

        # _FakeBrain.think 를 한시적으로 예외 raise 로 패치.
        def _boom(self_brain, text, ctx=""):
            raise RuntimeError("brain blew up")

        with patch.object(_FakeBrain, "think", _boom):
            with TestClient(server.app) as client:
                with client.websocket_connect("/ws") as ws:
                    ws.receive_json()  # ready
                    ws.send_bytes(b"\x02" + b"webm")
                    seen = _drain_audio_turn(ws)

        # user msg 는 오고, 그 후 error 메시지가 와야 한다 (assistant 는 X).
        self.assertTrue(
            any(o.get("type") == "message" and o.get("role") == "user" for o in seen),
            f"user message 누락. seen={seen!r}",
        )
        self.assertFalse(
            any(o.get("type") == "message" and o.get("role") == "assistant" for o in seen),
            "Brain 예외 시 assistant message 가 와선 안 됨",
        )
        errs = [o for o in seen if o.get("type") == "error"]
        self.assertEqual(len(errs), 1, f"error 1건 기대. seen={seen!r}")
        self.assertIn("message", errs[0])
        # TTS 는 호출되지 않았어야 함.
        self.assertEqual(len(tts.calls), 0)
        # 임시 파일도 finally 에서 정리됐는지.
        self.assertFalse(Path(stt.calls[0]).exists())

        # 텔레메트리: error=RuntimeError 라벨이 남아야 한다.
        rows = self._read_telemetry()
        audio_rows = [r for r in rows if r.get("input_channel") == "audio"]
        self.assertEqual(len(audio_rows), 1)
        self.assertEqual(audio_rows[0].get("error"), "RuntimeError")


# ============================================================
# WebSocket /api/harness/ws — 텔레메트리 푸시
# ============================================================
class HarnessWebSocketTests(unittest.TestCase):
    def test_ws_rejects_without_token(self):
        # 토큰이 설정돼 있는 상태에서 token 미제공 → close(4401)
        from starlette.websockets import WebSocketDisconnect as _WSDisc
        with TestClient(server.app) as client:
            with self.assertRaises(_WSDisc):
                with client.websocket_connect("/api/harness/ws") as ws:
                    ws.receive_text()

    def test_ws_summary_pushed_with_token(self):
        with TestClient(server.app) as client:
            with client.websocket_connect(
                f"/api/harness/ws?token={TOKEN}"
            ) as ws:
                msg = ws.receive_json()
                self.assertEqual(msg["type"], "summary")
                self.assertIn("summary", msg)
                self.assertIn("total", msg["summary"])


# ============================================================
# WebSocket /ws — compare 모드 (Claude + OpenAI 동시 스트리밍)
# ============================================================
# Task #13: respond_compare(server.py 636-766) 회귀 가드.
# - 정상: 양쪽 백엔드 모두 chunk + end 발화 → compare_done 까지 메시지 순서 검증.
# - 부분 실패: 한쪽만 응답 (다른 쪽 침묵) — compare_done 은 그대로 발화돼야 한다.
# - 전체 실패: compare_stream 자체가 예외 → server.run_stream 의 except 분기가
#   ("system", None, CONCERNED, _friendly_error(...)) 를 큐에 넣어 user 에게는
#   compare_end(source="system") + compare_done 로 안내된다.
class _ScriptedCompareBrain(_FakeBrain):
    """compare_stream 만 클래스-레벨 스크립트로 동작하도록 한 fake.
    - script: respond_compare 가 큐에서 읽을 (source, chunk, emo, body) 튜플 목록.
    - raise_exc: 설정 시 generator 가 첫 next() 에서 해당 예외를 raise.
    """

    script: List[Tuple[Any, ...]] = []
    raise_exc: Any = None  # type: ignore[assignment]

    def compare_stream(self, prompt: str, ctx: str):
        if _ScriptedCompareBrain.raise_exc is not None:
            raise _ScriptedCompareBrain.raise_exc
        for item in _ScriptedCompareBrain.script:
            yield item


class CompareModeWebSocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from sarvis.config import cfg
        cls._orig_backend = cfg.llm_backend  # type: ignore[attr-defined]
        cls._orig_brain = server.Brain        # type: ignore[attr-defined]
        cfg.llm_backend = "compare"
        server.Brain = _ScriptedCompareBrain

    @classmethod
    def tearDownClass(cls) -> None:
        from sarvis.config import cfg
        cfg.llm_backend = cls._orig_backend   # type: ignore[attr-defined]
        server.Brain = cls._orig_brain        # type: ignore[attr-defined]

    def setUp(self) -> None:
        _ScriptedCompareBrain.script = []
        _ScriptedCompareBrain.raise_exc = None

    def _run_compare(self, prompt: str = "안녕"):
        # 매 케이스마다 telemetry log 를 비우고 시작 — compare turn 한 줄만 남도록.
        try:
            if telemetry.LOG_PATH.exists():
                telemetry.LOG_PATH.unlink()
        except Exception:
            pass
        self._last_prompt = prompt
        with TestClient(server.app) as client:
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()  # ready
                ws.send_text(json.dumps({"type": "text_input", "text": prompt}))
                done, history = _drain_until(
                    ws,
                    lambda o: o.get("type") in ("compare_done", "error"),
                    max_msgs=120,
                )
        return done, history

    def _last_telemetry_entry(self) -> Dict[str, Any]:
        # respond_compare 의 finally 절에서 log_turn 이 동기 호출되므로
        # WebSocket 종료 시점에는 jsonl 마지막 줄에 compare turn 이 적혀 있다.
        self.assertTrue(
            telemetry.LOG_PATH.exists(),
            f"telemetry 파일이 없음: {telemetry.LOG_PATH}",
        )
        with open(telemetry.LOG_PATH, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertTrue(lines, "telemetry 파일에 turn 이 기록되지 않음")
        return json.loads(lines[-1])

    def test_compare_normal_both_backends_respond(self):
        """양쪽 백엔드가 chunk + end 를 발화 — compare_start → chunk(둘) →
        compare_end(둘) → compare_done 순서가 보장돼야 한다."""
        _ScriptedCompareBrain.script = [
            ("claude", "안녕", None, None),
            ("openai", "Hello", None, None),
            ("claude", " 친구", None, None),
            ("openai", " friend", None, None),
            ("claude", None, Emotion.NEUTRAL, "안녕 친구"),
            ("openai", None, Emotion.HAPPY, "Hello friend"),
        ]
        done, history = self._run_compare()
        self.assertIsNotNone(done, f"compare_done 미수신. history={history!r}")
        self.assertEqual(done["type"], "compare_done")

        types = [m.get("type") for m in history]
        self.assertIn("compare_start", types)

        # 순서: compare_start 가 chunk/end 보다 먼저, compare_done 이 가장 마지막.
        idx_start = types.index("compare_start")
        idx_done = types.index("compare_done")
        self.assertLess(idx_start, idx_done)
        for t in ("compare_chunk", "compare_end"):
            for i, mt in enumerate(types):
                if mt == t:
                    self.assertGreater(i, idx_start)
                    self.assertLess(i, idx_done)

        # 양쪽 source 가 모두 chunk + end 발화돼야 함.
        sources_chunk = {m.get("source") for m in history
                         if m.get("type") == "compare_chunk"}
        sources_end = {m.get("source") for m in history
                       if m.get("type") == "compare_end"}
        self.assertEqual(sources_chunk, {"claude", "openai"})
        self.assertEqual(sources_end, {"claude", "openai"})

        # compare_end body / emotion 검증.
        ends = {m["source"]: m for m in history if m.get("type") == "compare_end"}
        self.assertEqual(ends["claude"]["text"], "안녕 친구")
        self.assertEqual(ends["claude"]["emotion"], "neutral")
        self.assertEqual(ends["openai"]["text"], "Hello friend")
        self.assertEqual(ends["openai"]["emotion"], "happy")

        # compare_start 의 sources 필드도 두 백엔드를 안내해야 함.
        start = next(m for m in history if m.get("type") == "compare_start")
        self.assertEqual(set(start.get("sources", [])), {"claude", "openai"})

        # 텔레메트리 — backend=compare, compare_sources 누적, reply_len 합산.
        # Task #19: prompt_len/tts_reason/fallback_chain 도 회귀 가드 (요약/진화 입력 보존).
        entry = self._last_telemetry_entry()
        self.assertEqual(entry["backend"], "compare")
        self.assertEqual(set(entry["compare_sources"]), {"claude", "openai"})
        # reply_len = "안녕 친구" + "Hello friend" 두 본문 길이 합.
        self.assertEqual(entry["reply_len"], len("안녕 친구") + len("Hello friend"))
        self.assertEqual(entry["tts_reason"], "compare_no_tts")
        self.assertEqual(entry["fallback_chain"], ["compare:claude+openai"])
        self.assertEqual(entry["prompt_len"], len(self._last_prompt))
        # 정상 경로에서는 error 필드가 없거나 None — turn_meta 의 setdefault 가
        # 호출되지 않았어야 한다 (run_stream 가 정상 종료).
        self.assertNotIn("error", entry)

    def test_compare_only_one_backend_responds(self):
        """한쪽(claude) 만 chunk + end 를 발화하고 다른쪽(openai) 은 침묵.
        그래도 compare_done 까지 도달해야 하며, openai 측 메시지는 없어야 한다.
        """
        _ScriptedCompareBrain.script = [
            ("claude", "단독", None, None),
            ("claude", None, Emotion.NEUTRAL, "단독 응답"),
            # openai 측 종결 이벤트 없음 — compare_stream generator 가 그대로 종료.
        ]
        done, history = self._run_compare()
        self.assertIsNotNone(done, f"compare_done 미수신. history={history!r}")
        self.assertEqual(done["type"], "compare_done")

        sources_chunk = {m.get("source") for m in history
                         if m.get("type") == "compare_chunk"}
        sources_end = [m.get("source") for m in history
                       if m.get("type") == "compare_end"]
        self.assertEqual(sources_chunk, {"claude"})
        self.assertEqual(sources_end, ["claude"])

        end = next(m for m in history if m.get("type") == "compare_end")
        self.assertEqual(end["text"], "단독 응답")

        # 텔레메트리 — compare_sources 에 응답한 백엔드만 누적, reply_len 도 그쪽 길이.
        # Task #19: 단일 응답이어도 compare 모드의 고정 메타(tts_reason/fallback_chain)
        # 와 prompt_len 은 동일하게 보존돼야 한다.
        entry = self._last_telemetry_entry()
        self.assertEqual(entry["backend"], "compare")
        self.assertEqual(entry["compare_sources"], ["claude"])
        self.assertEqual(entry["reply_len"], len("단독 응답"))
        self.assertEqual(entry["tts_reason"], "compare_no_tts")
        self.assertEqual(entry["fallback_chain"], ["compare:claude+openai"])
        self.assertEqual(entry["prompt_len"], len(self._last_prompt))
        self.assertNotIn("error", entry)

    def test_compare_both_backends_raise(self):
        """compare_stream 자체가 예외 — server 의 run_stream 가 system 소스로
        친절 안내를 발화하고, 그래도 compare_done 까지 도달해야 한다 (사용자가
        '응답 없음' 으로 멈춰버리는 회귀를 막는다).
        """
        _ScriptedCompareBrain.raise_exc = RuntimeError("both backends down")
        done, history = self._run_compare()
        self.assertIsNotNone(done, f"compare_done 미수신. history={history!r}")
        self.assertEqual(done["type"], "compare_done")

        ends = [m for m in history if m.get("type") == "compare_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["source"], "system")
        self.assertEqual(ends[0]["emotion"], "concerned")
        self.assertTrue(ends[0]["text"], "system 안내 본문이 비어 있음")

        # compare_start → compare_end(system) → compare_done 순서.
        types = [m.get("type") for m in history]
        idx_start = types.index("compare_start")
        idx_end = types.index("compare_end")
        idx_done = types.index("compare_done")
        self.assertLess(idx_start, idx_end)
        self.assertLess(idx_end, idx_done)

        # 텔레메트리 — system 소스가 누적되고 reply_len 은 안내 본문 길이.
        # Task #19: 예외가 run_stream 에서 흡수되더라도
        #   - error 필드에 백엔드 예외 타입(RuntimeError) 이 기록되고
        #   - compare_sources 에는 'system' 이 포함되며
        #   - 고정 메타(tts_reason/fallback_chain/prompt_len) 는 그대로 보존돼야
        # /api/harness/telemetry 요약과 진화 입력이 깨지지 않는다.
        entry = self._last_telemetry_entry()
        self.assertEqual(entry["backend"], "compare")
        self.assertEqual(entry["compare_sources"], ["system"])
        self.assertIn("system", entry["compare_sources"])
        self.assertEqual(entry["reply_len"], len(ends[0]["text"]))
        self.assertEqual(entry["tts_reason"], "compare_no_tts")
        self.assertEqual(entry["fallback_chain"], ["compare:claude+openai"])
        self.assertEqual(entry["prompt_len"], len(self._last_prompt))
        self.assertEqual(entry.get("error"), "RuntimeError")

    # ----------------------------------------------------------------------
    # Task #18 — server.respond_compare 가 두 백엔드 응답을 모두
    # session.memory.add_message(emotion="<emo>|<source>") 로 영구 기록하는지
    # 검증한다. 사이클 #13 의 기존 케이스는 WebSocket 시퀀스만 보장하므로
    # 메모리 저장이 회귀해도 침묵으로 통과되는 갭이 있었음.
    # ----------------------------------------------------------------------
    def test_compare_persists_assistant_messages_per_source(self):
        """compare 모드 정상/부분 응답 시 session.memory 에 어떤 메시지가
        기록되는지 검증.

        - 정상 시나리오: user prompt 1건 + assistant 2건
          (emotion="neutral|claude", "happy|openai") = 총 3건.
        - 한쪽만 응답: user prompt 1건 + 응답한 backend assistant 1건 = 총 2건.
        """
        from config import cfg
        mem = server.get_memory()
        user_id = cfg.memory_user_id

        # 0) baseline — 이전 케이스들이 같은 DB 싱글톤에 메시지를 남겨두었을
        # 수 있으므로 현재 최대 message id 를 기준점으로 잡고 그 이후만 본다.
        before = mem.get_recent_messages(user_id, limit=500)
        before_max_id = max((m["id"] for m in before), default=0)

        # 1) 정상 — 양쪽 백엔드가 모두 종결.
        _ScriptedCompareBrain.script = [
            ("claude", "안녕", None, None),
            ("openai", "Hello", None, None),
            ("claude", None, Emotion.NEUTRAL, "안녕 친구"),
            ("openai", None, Emotion.HAPPY, "Hello friend"),
        ]
        done, _hist = self._run_compare(prompt="compare-mem-both")
        self.assertIsNotNone(done, "compare_done 미수신 (정상 시나리오)")

        after_both = mem.get_recent_messages(user_id, limit=500)
        new_both = [m for m in after_both if m["id"] > before_max_id]
        roles_both = [(m["role"], m["content"], m["emotion"]) for m in new_both]
        self.assertEqual(
            len(new_both), 3,
            f"정상 시나리오는 user 1 + assistant 2 = 3건이어야 함. 실제: {roles_both!r}",
        )

        # user 발화가 먼저 기록되고 (role=user, emotion=None), 그 뒤에 두
        # assistant 응답이 source 접미와 함께 기록돼야 한다.
        user_rows = [m for m in new_both if m["role"] == "user"]
        asst_rows = [m for m in new_both if m["role"] == "assistant"]
        self.assertEqual(len(user_rows), 1, f"user 메시지 1건 기대. 실제 new={roles_both!r}")
        self.assertEqual(user_rows[0]["content"], "compare-mem-both")
        self.assertEqual(len(asst_rows), 2, f"assistant 메시지 2건 기대. 실제 new={roles_both!r}")

        by_emotion = {m["emotion"]: m["content"] for m in asst_rows}
        self.assertIn(
            "neutral|claude", by_emotion,
            f"emotion='neutral|claude' 누락. 실제: {by_emotion!r}",
        )
        self.assertIn(
            "happy|openai", by_emotion,
            f"emotion='happy|openai' 누락. 실제: {by_emotion!r}",
        )
        self.assertEqual(by_emotion["neutral|claude"], "안녕 친구")
        self.assertEqual(by_emotion["happy|openai"], "Hello friend")

        # 2) 한쪽(claude) 만 응답하는 시나리오 — 두 번째 turn.
        mid_max_id = max(m["id"] for m in after_both)
        _ScriptedCompareBrain.script = [
            ("claude", "단독", None, None),
            ("claude", None, Emotion.NEUTRAL, "단독 응답"),
            # openai 측 종결 이벤트 없음 — finals 에 들어가지 않으므로 메모리에도 안 남는다.
        ]
        done2, _hist2 = self._run_compare(prompt="compare-mem-only-claude")
        self.assertIsNotNone(done2, "compare_done 미수신 (한쪽만 응답)")

        after_one = mem.get_recent_messages(user_id, limit=500)
        new_one = [m for m in after_one if m["id"] > mid_max_id]
        roles_one = [(m["role"], m["content"], m["emotion"]) for m in new_one]
        self.assertEqual(
            len(new_one), 2,
            f"한쪽만 응답은 user 1 + assistant 1 = 2건이어야 함. 실제: {roles_one!r}",
        )

        user_rows_one = [m for m in new_one if m["role"] == "user"]
        asst_rows_one = [m for m in new_one if m["role"] == "assistant"]
        self.assertEqual(len(user_rows_one), 1)
        self.assertEqual(user_rows_one[0]["content"], "compare-mem-only-claude")
        self.assertEqual(len(asst_rows_one), 1)
        self.assertEqual(asst_rows_one[0]["content"], "단독 응답")
        self.assertEqual(
            asst_rows_one[0]["emotion"], "neutral|claude",
            "응답한 백엔드 source 접미('|claude') 가 emotion 에 보존돼야 함",
        )
        # openai 가 침묵했으므로 새 메시지 중 '|openai' 접미는 없어야 한다.
        self.assertFalse(
            any((m["emotion"] or "").endswith("|openai") for m in new_one),
            f"openai 가 응답하지 않았는데 메모리에 '|openai' 메시지가 기록됨: {roles_one!r}",
        )


if __name__ == "__main__":
    unittest.main()
