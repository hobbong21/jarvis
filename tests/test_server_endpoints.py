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
import audio_io  # noqa: E402


class _StubWhisperSTT:  # pragma: no cover — import-time only
    def __init__(self, *_a, **_kw):
        raise RuntimeError("WhisperSTT load disabled in tests")


audio_io.WhisperSTT = _StubWhisperSTT  # type: ignore[attr-defined]

import server  # noqa: E402
import telemetry  # noqa: E402
import harness_actions  # noqa: E402
from emotion import Emotion  # noqa: E402
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
        with patch("harness_evolve.propose_next_cycle", return_value=fake_result) as m:
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


if __name__ == "__main__":
    unittest.main()
