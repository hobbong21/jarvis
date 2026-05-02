"""tools.py 단위 테스트 — ToolExecutor 의 각 _t_* 도구.

architect 사이클 #7 follow-up:
  - 외부 I/O (LLM, HTTP, 카메라) 는 mock 해 결정적으로 검증
  - get_time / remember / recall / set_timer 는 순수 로직
  - get_weather 는 urllib.request.urlopen 만 mock
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SARVIS_SKIP_CV2_PRELOAD", "1")

from sarvis.tools import _WEATHER_CODES, ToolExecutor  # noqa: E402


def _make_executor(tmp_dir, **kwargs):
    """ToolExecutor 를 격리된 메모리 경로로 생성."""
    os.environ["SARVIS_TOOL_MEMORY"] = str(Path(tmp_dir) / "memory.json")
    vision = kwargs.pop("vision", MagicMock())
    client = kwargs.pop("client", MagicMock())
    return ToolExecutor(vision_system=vision, anthropic_client=client, **kwargs)


class ExecuteDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.exec = _make_executor(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    def test_unknown_tool_returns_error_string(self):
        result = self.exec.execute("nonexistent_tool", {})
        self.assertIn("Unknown tool", result)

    def test_argument_error_caught(self):
        # _t_remember 는 key, value 를 요구
        result = self.exec.execute("remember", {"wrong_arg": "x"})
        self.assertIn("Argument error", result)

    def test_tool_exception_caught(self):
        # _t_get_weather 가 임의 예외를 던지도록 패치
        with patch.object(self.exec, "_t_get_weather", side_effect=RuntimeError("boom")):
            result = self.exec.execute("get_weather", {"location": "Seoul"})
            self.assertIn("failed", result.lower())
            self.assertIn("boom", result)

    def test_on_event_called_start_and_end(self):
        events = []
        self.exec.on_event = lambda name, status: events.append((name, status))
        self.exec.execute("get_time", {})
        self.assertIn(("get_time", "start"), events)
        self.assertIn(("get_time", "end"), events)

    def test_on_event_end_called_even_on_failure(self):
        events = []
        self.exec.on_event = lambda name, status: events.append((name, status))
        with patch.object(self.exec, "_t_get_time", side_effect=RuntimeError("x")):
            self.exec.execute("get_time", {})
        self.assertIn(("get_time", "end"), events)

    def test_definitions_returns_list(self):
        defs = self.exec.definitions()
        self.assertIsInstance(defs, list)
        names = {d["name"] for d in defs}
        for n in ("see", "get_weather", "get_time", "remember", "recall", "set_timer"):
            self.assertIn(n, names)


class GetTimeTests(unittest.TestCase):
    def test_format_contains_year_month_day(self):
        e = _make_executor(tempfile.mkdtemp())
        out = e._t_get_time()
        self.assertIn("년", out)
        self.assertIn("월", out)
        self.assertIn("일", out)
        self.assertIn("시", out)
        self.assertIn("분", out)
        # 요일 한국어 단어 중 하나가 포함되어야
        self.assertTrue(any(w in out for w in ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")))


class RememberRecallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.exec = _make_executor(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    def test_remember_stores_value_with_timestamp(self):
        out = self.exec._t_remember("favorite_color", "blue")
        self.assertIn("favorite_color", out)
        self.assertIn("blue", out)
        self.assertIn("favorite_color", self.exec.memory)
        self.assertEqual(self.exec.memory["favorite_color"]["value"], "blue")
        self.assertIsInstance(self.exec.memory["favorite_color"]["ts"], float)

    def test_remember_persists_to_disk(self):
        self.exec._t_remember("k", "v")
        path = Path(os.environ["SARVIS_TOOL_MEMORY"])
        self.assertTrue(path.exists())
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["k"]["value"], "v")

    def test_remember_creates_parent_dir(self):
        # SARVIS_TOOL_MEMORY 가 깊은 경로여도 자동 생성
        os.environ["SARVIS_TOOL_MEMORY"] = str(Path(self._tmp.name) / "deep" / "x" / "memory.json")
        e = ToolExecutor(vision_system=MagicMock(), anthropic_client=MagicMock())
        e._t_remember("k", "v")
        self.assertTrue(Path(os.environ["SARVIS_TOOL_MEMORY"]).exists())

    def test_recall_finds_by_key_substring(self):
        self.exec._t_remember("favorite_color", "blue")
        out = self.exec._t_recall("color")
        self.assertIn("favorite_color", out)
        self.assertIn("blue", out)

    def test_recall_finds_by_value_substring(self):
        self.exec._t_remember("favorite_color", "azure")
        out = self.exec._t_recall("AZURE")  # case-insensitive
        self.assertIn("azure", out)

    def test_recall_no_match(self):
        out = self.exec._t_recall("anything")
        self.assertIn("관련된 기억 없음", out)

    def test_recall_caps_at_five(self):
        for i in range(10):
            self.exec._t_remember(f"k_match_{i}", "common_value")
        out = self.exec._t_recall("match")
        # 최대 5개만 노출
        self.assertEqual(len(out.split("\n")), 5)

    def test_load_memory_handles_corrupt_file(self):
        path = Path(self._tmp.name) / "corrupt.json"
        path.write_text("{not json", encoding="utf-8")
        os.environ["SARVIS_TOOL_MEMORY"] = str(path)
        e = ToolExecutor(vision_system=MagicMock(), anthropic_client=MagicMock())
        self.assertEqual(e.memory, {})


class SetTimerTests(unittest.TestCase):
    def setUp(self):
        self.exec = _make_executor(tempfile.mkdtemp())

    def tearDown(self):
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    def test_invalid_zero_or_negative(self):
        for s in (0, -1, -100):
            self.assertIn("1초 이상", self.exec._t_set_timer(s))

    def test_human_format_seconds(self):
        out = self.exec._t_set_timer(30, label="물 끓이기")
        self.assertIn("30초", out)
        self.assertIn("물 끓이기", out)

    def test_human_format_minutes(self):
        out = self.exec._t_set_timer(120, label="x")
        self.assertIn("2분", out)

    def test_human_format_minutes_seconds(self):
        out = self.exec._t_set_timer(125, label="x")
        self.assertIn("2분", out)
        self.assertIn("5초", out)

    def test_callback_fires(self):
        triggered = threading.Event()
        called_with = []

        def cb(label):
            called_with.append(label)
            triggered.set()

        self.exec.on_timer = cb
        self.exec._t_set_timer(1, label="짧은")
        # 1초 + 약간 여유
        self.assertTrue(triggered.wait(timeout=3.0))
        self.assertEqual(called_with, ["짧은"])


class GetWeatherTests(unittest.TestCase):
    def setUp(self):
        self.exec = _make_executor(tempfile.mkdtemp())

    def tearDown(self):
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    def _mock_urlopen(self, payloads):
        """payloads 시퀀스를 순서대로 반환하는 urlopen 컨텍스트 매니저 mock."""
        bodies = [json.dumps(p).encode("utf-8") for p in payloads]

        def opener(url, timeout=None):
            body = bodies.pop(0)
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=body)))
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        return opener

    def test_geocoding_no_results(self):
        opener = self._mock_urlopen([{"results": []}])
        with patch("urllib.request.urlopen", side_effect=opener):
            out = self.exec._t_get_weather("Atlantis")
        self.assertIn("위치 정보를 찾을 수 없습니다", out)

    def test_success_path_includes_temp_and_humidity(self):
        geo = {"results": [{"latitude": 37.5, "longitude": 127.0,
                            "name": "서울", "country": "대한민국"}]}
        weather = {"current": {"temperature_2m": 21.3, "weather_code": 1,
                               "wind_speed_10m": 2.1, "relative_humidity_2m": 55}}
        opener = self._mock_urlopen([geo, weather])
        with patch("urllib.request.urlopen", side_effect=opener):
            out = self.exec._t_get_weather("Seoul")
        self.assertIn("서울", out)
        self.assertIn("21.3", out)
        self.assertIn("55", out)
        self.assertIn(_WEATHER_CODES[1], out)

    def test_unknown_weather_code_falls_back(self):
        geo = {"results": [{"latitude": 0.0, "longitude": 0.0, "name": "X", "country": ""}]}
        weather = {"current": {"temperature_2m": 0, "weather_code": 9999,
                               "wind_speed_10m": 0, "relative_humidity_2m": 0}}
        opener = self._mock_urlopen([geo, weather])
        with patch("urllib.request.urlopen", side_effect=opener):
            out = self.exec._t_get_weather("X")
        self.assertIn("코드 9999", out)

    def test_network_error_returns_friendly(self):
        with patch("urllib.request.urlopen", side_effect=OSError("DNS")):
            out = self.exec._t_get_weather("Seoul")
        self.assertIn("날씨 조회 실패", out)


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        self.exec = _make_executor(tempfile.mkdtemp())

    def tearDown(self):
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    def test_results_formatted(self):
        fake_ddgs_cls = MagicMock()
        fake_ddgs = MagicMock()
        fake_ddgs.text.return_value = iter([
            {"title": "Hello", "body": "world"},
            {"title": "Foo", "body": "bar"},
        ])
        fake_ddgs_cls.return_value = fake_ddgs
        fake_module = MagicMock(DDGS=fake_ddgs_cls)
        with patch.dict(sys.modules, {"duckduckgo_search": fake_module}):
            out = self.exec._t_web_search("query")
        self.assertIn("Hello", out)
        self.assertIn("world", out)
        self.assertIn("Foo", out)

    def test_empty_results(self):
        fake_ddgs = MagicMock()
        fake_ddgs.text.return_value = iter([])
        fake_module = MagicMock(DDGS=MagicMock(return_value=fake_ddgs))
        with patch.dict(sys.modules, {"duckduckgo_search": fake_module}):
            out = self.exec._t_web_search("nothing")
        self.assertIn("검색 결과 없음", out)

    def test_import_error_returns_friendly(self):
        # duckduckgo_search 가 사용 불가일 때
        with patch.dict(sys.modules, {"duckduckgo_search": None}):
            # 실제로 None 으로 두면 import 가 ModuleNotFoundError 를 일으킴
            out = self.exec._t_web_search("q")
        self.assertIn("검색 실패", out)


class SeeAndIdentifyTests(unittest.TestCase):
    """카메라 도구 — cv2 가 없거나 vision 이 frame 을 못 줄 때의 폴백."""

    def setUp(self):
        self.exec = _make_executor(tempfile.mkdtemp())

    def tearDown(self):
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    def test_see_no_frame(self):
        self.exec.vision.read.return_value = None
        out = self.exec._t_see("뭐 보여?")
        self.assertIn("프레임", out)

    def test_observe_action_no_frame(self):
        self.exec.vision.read.return_value = None
        out = self.exec._t_observe_action()
        self.assertIn("사람이 보이지 않거나", out)

    def test_identify_person_no_registry(self):
        self.exec.face_registry = None
        out = self.exec._t_identify_person()
        self.assertIn("등록", out)

    def test_identify_person_empty_registry(self):
        registry = MagicMock()
        registry.get_references.return_value = []
        self.exec.face_registry = registry
        out = self.exec._t_identify_person()
        self.assertIn("등록된 얼굴이 없습니다", out)


if __name__ == "__main__":
    unittest.main()
