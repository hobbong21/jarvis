"""server.py 의 단위 테스트 가능한 헬퍼 — _harness_auth_check / _harness_ws_auth_ok.

architect 사이클 #7 follow-up:
  - 토큰 미설정 시 loopback 만 허용 (개발 모드 안전성)
  - 토큰 설정 시 query 또는 Bearer 헤더에서 일치해야 함
  - 잘못된 토큰은 401, 잘못된 출처는 403

server.py 자체는 import 시 STT/TTS/FaceRegistry 를 초기화하므로 SARVIS_SKIP_CV2_PRELOAD
환경변수로 무거운 cv2 prefetch 만 건너뛰고, 나머지는 자연스럽게 로드한다.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")

from sarvis import server  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _mock_request(host: str = "127.0.0.1", auth_header: str = ""):
    req = MagicMock()
    req.client = MagicMock(host=host)
    req.headers = {"authorization": auth_header} if auth_header else {}
    # MagicMock dict-like: .get
    headers = req.headers
    req.headers = MagicMock()
    req.headers.get = lambda k, default="": headers.get(k, default)
    return req


def _mock_ws(host: str = "127.0.0.1", auth_header: str = ""):
    ws = MagicMock()
    ws.client = MagicMock(host=host)
    headers = {"authorization": auth_header} if auth_header else {}
    ws.headers = MagicMock()
    ws.headers.get = lambda k, default="": headers.get(k, default)
    return ws


class HarnessAuthCheckTests(unittest.TestCase):
    """_harness_auth_check (HTTP request)."""

    def test_loopback_allowed_when_no_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_TELEMETRY_TOKEN", None)
            for host in ("127.0.0.1", "::1", "localhost"):
                req = _mock_request(host=host)
                # 통과해야 함 (예외 없음)
                server._harness_auth_check(req, None)

    def test_non_loopback_blocked_when_no_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_TELEMETRY_TOKEN", None)
            req = _mock_request(host="10.0.0.1")
            with self.assertRaises(HTTPException) as ctx:
                server._harness_auth_check(req, None)
            self.assertEqual(ctx.exception.status_code, 403)

    def test_token_via_query_param(self):
        with patch.dict(os.environ, {"HARNESS_TELEMETRY_TOKEN": "secret123"}):
            req = _mock_request(host="10.0.0.1")
            server._harness_auth_check(req, "secret123")  # OK

    def test_token_via_bearer_header(self):
        with patch.dict(os.environ, {"HARNESS_TELEMETRY_TOKEN": "secret123"}):
            req = _mock_request(host="10.0.0.1", auth_header="Bearer secret123")
            server._harness_auth_check(req, None)  # OK

    def test_wrong_token_rejected(self):
        with patch.dict(os.environ, {"HARNESS_TELEMETRY_TOKEN": "secret123"}):
            req = _mock_request(host="127.0.0.1")
            with self.assertRaises(HTTPException) as ctx:
                server._harness_auth_check(req, "wrong")
            self.assertEqual(ctx.exception.status_code, 401)

    def test_missing_token_rejected_when_required(self):
        with patch.dict(os.environ, {"HARNESS_TELEMETRY_TOKEN": "secret123"}):
            req = _mock_request(host="127.0.0.1")
            with self.assertRaises(HTTPException) as ctx:
                server._harness_auth_check(req, None)
            self.assertEqual(ctx.exception.status_code, 401)


class HarnessWsAuthOkTests(unittest.TestCase):
    """_harness_ws_auth_ok (WebSocket)."""

    def test_loopback_allowed_when_no_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_TELEMETRY_TOKEN", None)
            for host in ("127.0.0.1", "::1", "localhost"):
                ws = _mock_ws(host=host)
                self.assertTrue(server._harness_ws_auth_ok(ws, None))

    def test_non_loopback_blocked_when_no_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARNESS_TELEMETRY_TOKEN", None)
            ws = _mock_ws(host="10.0.0.1")
            self.assertFalse(server._harness_ws_auth_ok(ws, None))

    def test_token_via_query(self):
        with patch.dict(os.environ, {"HARNESS_TELEMETRY_TOKEN": "secret123"}):
            ws = _mock_ws(host="10.0.0.1")
            self.assertTrue(server._harness_ws_auth_ok(ws, "secret123"))

    def test_token_via_bearer(self):
        with patch.dict(os.environ, {"HARNESS_TELEMETRY_TOKEN": "secret123"}):
            ws = _mock_ws(host="10.0.0.1", auth_header="Bearer secret123")
            self.assertTrue(server._harness_ws_auth_ok(ws, None))

    def test_wrong_token_rejected(self):
        with patch.dict(os.environ, {"HARNESS_TELEMETRY_TOKEN": "secret123"}):
            ws = _mock_ws(host="10.0.0.1")
            self.assertFalse(server._harness_ws_auth_ok(ws, "wrong"))

    def test_empty_token_rejected_when_required(self):
        with patch.dict(os.environ, {"HARNESS_TELEMETRY_TOKEN": "secret123"}):
            ws = _mock_ws(host="127.0.0.1")
            self.assertFalse(server._harness_ws_auth_ok(ws, ""))


class HealthEndpointTests(unittest.TestCase):
    """async health 엔드포인트 — dict 반환 형태 검증."""

    def test_health_returns_expected_keys(self):
        import asyncio
        result = asyncio.run(server.health())
        self.assertIn("ok", result)
        self.assertTrue(result["ok"])
        self.assertIn("backend", result)
        self.assertIn("stt_ready", result)
        self.assertIn("connections", result)
        self.assertIsInstance(result["connections"], int)


if __name__ == "__main__":
    unittest.main()
