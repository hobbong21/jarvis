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


class WebSearchEnhancedTests(unittest.TestCase):
    """강화된 웹 검색 — 캐시, 도메인 다양화, 뉴스 엔드포인트, 키워드 추출, 병렬 fetch."""

    def setUp(self):
        # 테스트 간 캐시 격리
        ToolExecutor._cache.clear()
        self.exec = _make_executor(tempfile.mkdtemp())

    def tearDown(self):
        ToolExecutor._cache.clear()
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    # --- 캐시 ---
    def test_cache_returns_same_result_within_ttl(self):
        ToolExecutor._cache_put("k1", "hello")
        self.assertEqual(ToolExecutor._cache_get("k1"), "hello")

    def test_cache_expires_after_ttl(self):
        ToolExecutor._cache["k2"] = (time.time() - ToolExecutor._CACHE_TTL_S - 1, "stale")
        self.assertIsNone(ToolExecutor._cache_get("k2"))

    def test_cache_evicts_oldest_when_full(self):
        original = ToolExecutor._CACHE_MAX
        try:
            ToolExecutor._CACHE_MAX = 2
            ToolExecutor._cache_put("a", "A")
            time.sleep(0.01)
            ToolExecutor._cache_put("b", "B")
            time.sleep(0.01)
            ToolExecutor._cache_put("c", "C")  # 오래된 a 가 밀려야 함
            self.assertIsNone(ToolExecutor._cache_get("a"))
            self.assertEqual(ToolExecutor._cache_get("b"), "B")
            self.assertEqual(ToolExecutor._cache_get("c"), "C")
        finally:
            ToolExecutor._CACHE_MAX = original

    # --- 도메인 다양화 ---
    def test_dedupe_by_domain_keeps_one_per_host(self):
        results = [
            {"href": "https://news.naver.com/a", "title": "A", "body": ""},
            {"href": "https://news.naver.com/b", "title": "B", "body": ""},
            {"href": "https://www.example.com/c", "title": "C", "body": ""},
            {"href": "https://example.com/d", "title": "D", "body": ""},
        ]
        out = ToolExecutor._dedupe_by_domain(results, max_per_domain=1, max_total=10)
        # naver.com 1개 + example.com 1개 가 primary, 나머지는 overflow
        primary_hosts = [ToolExecutor._domain_of(r["href"]) for r in out[:2]]
        self.assertEqual(set(primary_hosts), {"news.naver.com", "example.com"})

    def test_domain_of_strips_www(self):
        self.assertEqual(ToolExecutor._domain_of("https://www.example.com/x"), "example.com")
        self.assertEqual(ToolExecutor._domain_of("http://sub.example.com/x"), "sub.example.com")
        self.assertEqual(ToolExecutor._domain_of("notaurl"), "")

    # --- 뉴스 의도 / 시간 민감 ---
    def test_news_intent_detected(self):
        self.assertTrue(ToolExecutor._is_news_intent("오늘 주요 뉴스"))
        self.assertTrue(ToolExecutor._is_news_intent("breaking news today"))
        self.assertFalse(ToolExecutor._is_news_intent("파이썬 리스트 사용법"))

    # --- 키워드 추출 (한글 조사 제거) ---
    def test_keywords_strip_korean_particles(self):
        kws = ToolExecutor._query_keywords("삼성전자가 발표한 신제품은 무엇인가")
        # "삼성전자가" → "삼성전자", "신제품은" → "신제품"
        self.assertIn("삼성전자", kws)
        self.assertIn("신제품", kws)
        self.assertIn("발표한", kws)

    def test_keywords_drop_stopwords(self):
        kws = ToolExecutor._query_keywords("오늘 뭐야 알려줘")
        # 모두 stopword → []
        self.assertEqual(kws, [])

    def test_keywords_dedupe_case_insensitive(self):
        kws = ToolExecutor._query_keywords("Python python PYTHON")
        # 대소문자 무시 dedupe
        self.assertEqual(len(kws), 1)

    def test_strip_ko_particle_safe_short_token(self):
        # 길이 3 미만이면 그대로 (잘못된 절단 방지)
        self.assertEqual(ToolExecutor._strip_ko_particle("나"), "나")
        self.assertEqual(ToolExecutor._strip_ko_particle("너는"), "너는")

    # --- window ranking (토큰 다양성) ---
    def test_window_prefers_diverse_token_coverage(self):
        # 텍스트: 앞쪽엔 "사과"만 5번, 뒤쪽엔 "사과"+"바나나"+"포도" 1번씩
        text = (
            "사과 사과 사과 사과 사과 " + ("x " * 200) +
            "사과 바나나 포도"
        )
        out = ToolExecutor._extract_relevant_window(
            text, "사과 바나나 포도", window=80, max_windows=1
        )
        # diverse 한 뒷부분 윈도우가 선택되어야 함
        self.assertIn("바나나", out)
        self.assertIn("포도", out)

    def test_window_falls_back_to_head_when_no_match(self):
        text = "전혀 다른 내용입니다"
        out = ToolExecutor._extract_relevant_window(text, "존재하지않는키워드", window=10)
        # 0건 매칭 → 본문 앞부분 반환
        self.assertTrue(out.startswith("전혀"))

    # --- _t_web_search 통합 (캐시 적중 확인) ---
    def test_web_search_uses_cache(self):
        fake_ddgs = MagicMock()
        fake_ddgs.text.return_value = iter([
            {"title": "T1", "body": "B1", "href": "https://a.com/1"},
        ])
        fake_module = MagicMock(DDGS=MagicMock(return_value=fake_ddgs))
        with patch.dict(sys.modules, {"duckduckgo_search": fake_module}):
            out1 = self.exec._t_web_search("재귀호출테스트")
            out2 = self.exec._t_web_search("재귀호출테스트")
        self.assertEqual(out1, out2)
        # 두 번째 호출은 캐시 적중 → DDGS().text 는 1번만 호출
        self.assertEqual(fake_ddgs.text.call_count, 1)

    def test_web_search_diversifies_domains(self):
        fake_ddgs = MagicMock()
        fake_ddgs.text.return_value = iter([
            {"title": "A1", "body": "X", "href": "https://news.naver.com/1"},
            {"title": "A2", "body": "Y", "href": "https://news.naver.com/2"},
            {"title": "B1", "body": "Z", "href": "https://other.com/1"},
        ])
        fake_module = MagicMock(DDGS=MagicMock(return_value=fake_ddgs))
        with patch.dict(sys.modules, {"duckduckgo_search": fake_module}):
            out = self.exec._t_web_search("다양화 테스트")
        # other.com 이 등장 (도메인 다양화 효과로 상위에 살아남음)
        self.assertIn("other.com", out)

    def test_web_search_news_intent_calls_news_endpoint(self):
        fake_ddgs = MagicMock()
        fake_ddgs.text.return_value = iter([])
        fake_ddgs.news.return_value = iter([
            {"title": "속보!", "body": "내용", "url": "https://news.example.com/1"},
        ])
        fake_module = MagicMock(DDGS=MagicMock(return_value=fake_ddgs))
        with patch.dict(sys.modules, {"duckduckgo_search": fake_module}):
            out = self.exec._t_web_search("오늘 뉴스 알려줘")
        # 뉴스 엔드포인트가 호출됐어야 함
        self.assertGreaterEqual(fake_ddgs.news.call_count, 1)
        self.assertIn("속보", out)

    # --- _t_web_answer 통합 ---
    def test_web_answer_parallel_fetch_combines_excerpts(self):
        fake_ddgs = MagicMock()
        fake_ddgs.text.return_value = iter([
            {"title": "Title1", "body": "snippet1", "href": "https://a.com/1"},
            {"title": "Title2", "body": "snippet2", "href": "https://b.com/2"},
        ])
        fake_module = MagicMock(DDGS=MagicMock(return_value=fake_ddgs))

        def fake_fetch(url, max_chars=8000, timeout=5.0, max_redirects=3):
            if "a.com" in url:
                return "사과에 대한 자세한 본문 내용입니다 사과 정보 풍부"
            if "b.com" in url:
                return "바나나에 대한 본문 텍스트입니다 바나나 영양"
            return ""

        with patch.dict(sys.modules, {"duckduckgo_search": fake_module}), \
             patch.object(ToolExecutor, "_fetch_clean_text", staticmethod(fake_fetch)):
            out = self.exec._t_web_answer("사과 바나나")
        self.assertIn("Title1", out)
        self.assertIn("Title2", out)
        # 두 출처 모두 본문 발췌가 포함되어야 함
        self.assertIn("사과", out)
        self.assertIn("바나나", out)

    def test_cache_key_is_case_insensitive(self):
        """Apple 과 apple 은 같은 검색 — 첫 호출 결과가 둘째 호출에서 캐시 적중."""
        fake_ddgs = MagicMock()
        fake_ddgs.text.return_value = iter([
            {"title": "Apple Inc", "body": "tech", "href": "https://apple.com"},
        ])
        fake_module = MagicMock(DDGS=MagicMock(return_value=fake_ddgs))
        with patch.dict(sys.modules, {"duckduckgo_search": fake_module}):
            r1 = self.exec._t_web_search("Apple")
            r2 = self.exec._t_web_search("apple")
            r3 = self.exec._t_web_search("APPLE")
        self.assertEqual(r1, r2)
        self.assertEqual(r2, r3)
        # DDGS().text 는 1번만 호출 (나머지는 캐시 적중)
        self.assertEqual(fake_ddgs.text.call_count, 1)

    def test_dedupe_by_domain_fills_max_total_when_all_same_domain(self):
        """모든 결과가 같은 도메인이어도 max_total 까지 결과를 채워 빈 응답 회피."""
        results = [
            {"href": f"https://example.com/{i}", "title": f"T{i}", "body": "x"}
            for i in range(5)
        ]
        out = ToolExecutor._dedupe_by_domain(
            results, max_per_domain=1, max_total=4,
        )
        # 첫 1개는 primary, 나머지 3개는 overflow 에서 채움 → 총 4개 보장
        self.assertEqual(len(out), 4)

    def test_web_answer_falls_back_to_snippets_when_fetch_fails(self):
        fake_ddgs = MagicMock()
        fake_ddgs.text.return_value = iter([
            {"title": "OnlySnippet", "body": "유일한 스니펫", "href": "https://x.com/1"},
        ])
        fake_module = MagicMock(DDGS=MagicMock(return_value=fake_ddgs))
        with patch.dict(sys.modules, {"duckduckgo_search": fake_module}), \
             patch.object(ToolExecutor, "_fetch_clean_text", staticmethod(lambda *a, **k: "")):
            out = self.exec._t_web_answer("테스트")
        self.assertIn("스니펫", out)


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


# ============================================================
# 사이클 #30 — 사용자 저장공간 도구 (_t_storage_*)
# ============================================================
class StorageToolsTests(unittest.TestCase):
    """list_files / read_file / search_files — UserStorage 와 통합."""

    def setUp(self):
        from sarvis.user_storage import UserStorage
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = UserStorage("u", root=self._tmp.name, limit_bytes=10 ** 6)
        self.exec = _make_executor(self._tmp.name, user_storage=self.storage)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    # ---------- 인증 가드 ----------
    def test_no_storage_attached_returns_friendly_message(self):
        ex = _make_executor(self._tmp.name)  # user_storage 없음
        for name in ("storage_list_files", "storage_read_file", "storage_search_files"):
            args = {"file_id": "x"} if name == "storage_read_file" else {"query": "x"} if name == "storage_search_files" else {}
            out = ex.execute(name, args)
            self.assertIn("활성화되지 않았습니다", out)

    def test_set_user_storage_setter_works(self):
        ex = _make_executor(self._tmp.name)
        self.assertIsNone(ex.user_storage)
        ex.set_user_storage(self.storage)
        self.assertIs(ex.user_storage, self.storage)

    # ---------- list ----------
    def test_list_empty_storage(self):
        out = self.exec.execute("storage_list_files", {})
        self.assertIn("없습니다", out)

    def test_list_shows_only_ai_allowed(self):
        on = self.storage.save_file("public.md", b"hi", ai_access=True)
        off = self.storage.save_file("secret.md", b"hi", ai_access=False)
        out = self.exec.execute("storage_list_files", {})
        self.assertIn("public.md", out)
        self.assertNotIn("secret.md", out)
        self.assertIn(on, out)
        self.assertNotIn(off, out)

    def test_list_kind_filter(self):
        self.storage.save_file("a.md", b"x", kind="upload")
        self.storage.save_file("b.md", b"x", kind="conversation")
        out = self.exec.execute("storage_list_files", {"kind": "conversation"})
        self.assertIn("b.md", out)
        self.assertNotIn("a.md", out)

    # ---------- read ----------
    def test_read_empty_id_helps(self):
        out = self.exec.execute("storage_read_file", {"file_id": ""})
        self.assertIn("storage_list_files", out)

    def test_read_unknown_id(self):
        out = self.exec.execute("storage_read_file", {"file_id": "nosuch"})
        self.assertIn("찾을 수 없습니다", out)

    def test_read_text_file_returns_body(self):
        fid = self.storage.save_file("note.md", "회의록 내용".encode("utf-8"))
        out = self.exec.execute("storage_read_file", {"file_id": fid})
        self.assertIn("note.md", out)
        self.assertIn("회의록 내용", out)

    def test_read_blocked_when_ai_access_off(self):
        fid = self.storage.save_file("private.md", b"shh", ai_access=False)
        out = self.exec.execute("storage_read_file", {"file_id": fid})
        self.assertIn("AI 접근을 차단", out)

    def test_read_too_large_returns_metadata(self):
        big = b"x" * (300 * 1024)  # 300KB > 256KB max_inline
        fid = self.storage.save_file("big.bin", big)
        out = self.exec.execute("storage_read_file", {"file_id": fid})
        self.assertIn("너무 커서", out)
        self.assertIn("big.bin", out)

    def test_read_binary_returns_meta(self):
        # 0x80 같은 단독 바이트는 UTF-8 디코드 실패.
        fid = self.storage.save_file("img.bin", b"\x80\x81\x82\x83")
        out = self.exec.execute("storage_read_file", {"file_id": fid})
        self.assertIn("바이너리", out)

    # ---------- search ----------
    def test_search_empty_query(self):
        out = self.exec.execute("storage_search_files", {"query": ""})
        self.assertIn("비어있습니다", out)

    def test_search_no_match(self):
        self.storage.save_file("a.md", b"hello world")
        out = self.exec.execute("storage_search_files", {"query": "xyzzy"})
        self.assertIn("일치하는 파일이 없습니다", out)

    def test_search_finds_in_filename_and_body(self):
        self.storage.save_file("project.md", b"unrelated body")
        self.storage.save_file("notes.md", "프로젝트 회의 내용".encode("utf-8"))
        out = self.exec.execute("storage_search_files", {"query": "project"})
        self.assertIn("project.md", out)
        out2 = self.exec.execute("storage_search_files", {"query": "프로젝트"})
        self.assertIn("notes.md", out2)

    def test_search_excludes_ai_off_files(self):
        self.storage.save_file("hidden.md", b"keyword", ai_access=False)
        out = self.exec.execute("storage_search_files", {"query": "keyword"})
        self.assertIn("일치하는 파일이 없습니다", out)

    # ---------- save_conversation (Phase 3d) ----------
    def test_save_conversation_no_storage(self):
        ex = _make_executor(self._tmp.name)  # storage 없음
        out = ex.execute("save_conversation", {"content": "# 회의\n내용"})
        self.assertIn("활성화되지 않았습니다", out)

    def test_save_conversation_empty_body(self):
        out = self.exec.execute("save_conversation", {"content": "   "})
        self.assertIn("비어있습니다", out)

    def test_save_conversation_creates_file(self):
        out = self.exec.execute(
            "save_conversation",
            {"content": "# 오늘 회의\n- 결론: 진행", "title": "2026-05-04 회의"},
        )
        self.assertIn("저장했습니다", out)
        self.assertIn("file_id=", out)
        files = self.storage.list_files(kind="conversation")
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0]["name"].endswith(".md"))

    def test_save_conversation_quota_exceeded(self):
        from sarvis.user_storage import UserStorage
        small = UserStorage("u2", root=self._tmp.name, limit_bytes=10)
        ex = _make_executor(self._tmp.name, user_storage=small)
        big_body = "x" * 50
        out = ex.execute("save_conversation", {"content": big_body})
        self.assertIn("공간 부족", out)

    def test_save_conversation_respects_ai_access(self):
        out = self.exec.execute(
            "save_conversation",
            {"content": "# 비밀 메모\n조용히", "ai_access": False},
        )
        self.assertIn("저장했습니다", out)
        files = self.storage.list_files()
        self.assertFalse(files[0]["ai_access"])


# ============================================================
# 사이클 #32 — translate_text (양방향 번역 wrapper)
# ============================================================
class TranslateTextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    def _executor_with_client(self, claude_response_text: str):
        # Claude messages.create 응답을 mock.
        client = MagicMock()
        msg = MagicMock()
        msg.content = [MagicMock(text=claude_response_text)]
        client.messages.create.return_value = msg
        return _make_executor(self._tmp.name, client=client), client

    def test_empty_text_returns_helpful_message(self):
        ex = _make_executor(self._tmp.name)
        out = ex.execute("translate_text", {"text": "  "})
        self.assertIn("비어있습니다", out)

    def test_no_client_returns_message(self):
        ex = _make_executor(self._tmp.name, client=None)
        out = ex.execute("translate_text", {"text": "Hello"})
        self.assertIn("연결되지 않았습니다", out)

    def test_translation_with_src_label(self):
        ex, _client = self._executor_with_client("[SRC: English]\n안녕하세요")
        out = ex.execute("translate_text", {"text": "Hello", "target_lang": "ko"})
        self.assertIn("English", out)
        self.assertIn("Korean", out)
        self.assertIn("안녕하세요", out)

    def test_translation_without_src_label_still_works(self):
        ex, _client = self._executor_with_client("こんにちは")
        out = ex.execute("translate_text", {"text": "안녕", "target_lang": "ja"})
        self.assertIn("Japanese", out)
        self.assertIn("こんにちは", out)

    def test_korean_alias_normalized(self):
        ex, client = self._executor_with_client("[SRC: Korean]\nHello")
        ex.execute("translate_text", {"text": "안녕", "target_lang": "영어"})
        # client.messages.create 가 호출됐고 prompt 안에 English 가 있어야 함.
        call_args = client.messages.create.call_args
        prompt_text = call_args.kwargs["messages"][0]["content"]
        self.assertIn("English", prompt_text)

    def test_default_target_is_korean(self):
        ex, client = self._executor_with_client("[SRC: English]\n안녕")
        ex.execute("translate_text", {"text": "Hi"})
        call_args = client.messages.create.call_args
        prompt_text = call_args.kwargs["messages"][0]["content"]
        self.assertIn("Korean", prompt_text)

    def test_api_failure_caught(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("network down")
        ex = _make_executor(self._tmp.name, client=client)
        out = ex.execute("translate_text", {"text": "Hello"})
        self.assertIn("번역 실패", out)
        self.assertIn("network down", out)


# ============================================================
# 사이클 #32 — Phase 5b: count_objects, read_emotion
# 비전 도구는 Claude API 응답을 mock 하고 _get_vision_b64 를 패치한다.
# ============================================================
class CountObjectsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    def _executor_with_vision(self, claude_text: str = "사람 2명이 보입니다."):
        client = MagicMock()
        msg = MagicMock()
        msg.content = [MagicMock(text=claude_text)]
        client.messages.create.return_value = msg
        ex = _make_executor(self._tmp.name, client=client)
        # 카메라 프레임 mock — 임의 b64 반환.
        ex._get_vision_b64 = lambda: "ZmFrZWltYWdl"
        return ex, client

    def test_empty_target_helpful(self):
        ex, _ = self._executor_with_vision()
        out = ex.execute("count_objects", {"target": ""})
        self.assertIn("비어있습니다", out)

    def test_no_camera_frame(self):
        ex = _make_executor(self._tmp.name)
        ex._get_vision_b64 = lambda: None
        out = ex.execute("count_objects", {"target": "사람"})
        self.assertIn("카메라 프레임", out)

    def test_no_client(self):
        ex = _make_executor(self._tmp.name, client=None)
        ex._get_vision_b64 = lambda: "ZmFrZQ=="
        out = ex.execute("count_objects", {"target": "사람"})
        self.assertIn("연결되지 않았습니다", out)

    def test_returns_claude_text(self):
        ex, _ = self._executor_with_vision("사람 2명이 보입니다.")
        out = ex.execute("count_objects", {"target": "사람"})
        self.assertEqual(out, "사람 2명이 보입니다.")

    def test_target_passed_in_prompt(self):
        ex, client = self._executor_with_vision()
        ex.execute("count_objects", {"target": "의자"})
        call_args = client.messages.create.call_args
        prompt_text = call_args.kwargs["messages"][0]["content"][1]["text"]
        self.assertIn("의자", prompt_text)


class ReadEmotionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    def _executor_with_vision(self, claude_text: str = "차분한 표정. 눈이 살짝 처져 있어."):
        client = MagicMock()
        msg = MagicMock()
        msg.content = [MagicMock(text=claude_text)]
        client.messages.create.return_value = msg
        ex = _make_executor(self._tmp.name, client=client)
        ex._get_vision_b64 = lambda: "ZmFrZQ=="
        return ex, client

    def test_no_frame(self):
        ex = _make_executor(self._tmp.name)
        ex._get_vision_b64 = lambda: None
        out = ex.execute("read_emotion", {})
        self.assertIn("카메라 프레임", out)

    def test_no_client(self):
        ex = _make_executor(self._tmp.name, client=None)
        ex._get_vision_b64 = lambda: "ZmFrZQ=="
        out = ex.execute("read_emotion", {})
        self.assertIn("연결되지 않았습니다", out)

    def test_returns_claude_text(self):
        ex, _ = self._executor_with_vision("차분한 표정.")
        out = ex.execute("read_emotion", {})
        self.assertEqual(out, "차분한 표정.")

    def test_api_failure(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("oops")
        ex = _make_executor(self._tmp.name, client=client)
        ex._get_vision_b64 = lambda: "ZmFrZQ=="
        out = ex.execute("read_emotion", {})
        self.assertIn("표정 분석 실패", out)


# ============================================================
# 사이클 #32 — Phase 5c: check_posture, compare_photos
# ============================================================
class CheckPostureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    def _make(self, claude_text: str = "허리는 곧지만 어깨가 약간 굽었어요."):
        client = MagicMock()
        msg = MagicMock()
        msg.content = [MagicMock(text=claude_text)]
        client.messages.create.return_value = msg
        ex = _make_executor(self._tmp.name, client=client)
        ex._get_vision_b64 = lambda: "ZmFrZQ=="
        return ex, client

    def test_no_frame(self):
        ex = _make_executor(self._tmp.name)
        ex._get_vision_b64 = lambda: None
        out = ex.execute("check_posture", {})
        self.assertIn("카메라 프레임", out)

    def test_no_client(self):
        ex = _make_executor(self._tmp.name, client=None)
        ex._get_vision_b64 = lambda: "ZmFrZQ=="
        out = ex.execute("check_posture", {})
        self.assertIn("연결되지 않았습니다", out)

    def test_context_passed_in_prompt(self):
        ex, client = self._make()
        ex.execute("check_posture", {"context": "desk_work"})
        prompt = client.messages.create.call_args.kwargs["messages"][0]["content"][1]["text"]
        self.assertIn("desk_work", prompt)

    def test_returns_claude_text(self):
        ex, _ = self._make("좋은 자세입니다.")
        out = ex.execute("check_posture", {})
        self.assertEqual(out, "좋은 자세입니다.")


class ComparePhotosTests(unittest.TestCase):
    def setUp(self):
        from sarvis.user_storage import UserStorage
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = UserStorage("u", root=self._tmp.name, limit_bytes=10 ** 6)
        # 두 가짜 이미지 저장 (실제 JPEG 안 만들어도 됨 — Claude API 는 mock).
        self.fid_a = self.storage.save_file("before.jpg", b"\xff\xd8\xff\xe0fake_a", kind="media")
        self.fid_b = self.storage.save_file("after.jpg", b"\xff\xd8\xff\xe0fake_b", kind="media")

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("SARVIS_TOOL_MEMORY", None)

    def _make_executor(self, claude_text: str = "두 사진의 차이는 ..."):
        client = MagicMock()
        msg = MagicMock()
        msg.content = [MagicMock(text=claude_text)]
        client.messages.create.return_value = msg
        ex = _make_executor(self._tmp.name, client=client, user_storage=self.storage)
        return ex, client

    def test_no_storage(self):
        ex = _make_executor(self._tmp.name)
        out = ex.execute("compare_photos", {"file_id_a": "x", "file_id_b": "y"})
        self.assertIn("활성화되지 않았습니다", out)

    def test_empty_ids(self):
        ex, _ = self._make_executor()
        out = ex.execute("compare_photos", {"file_id_a": "", "file_id_b": ""})
        self.assertIn("file_id", out)

    def test_same_id_rejected(self):
        ex, _ = self._make_executor()
        out = ex.execute("compare_photos", {"file_id_a": self.fid_a, "file_id_b": self.fid_a})
        self.assertIn("같은 file_id", out)

    def test_unknown_id(self):
        ex, _ = self._make_executor()
        out = ex.execute("compare_photos", {"file_id_a": "nope", "file_id_b": self.fid_b})
        self.assertIn("찾을 수 없습니다", out)

    def test_ai_blocked(self):
        self.storage.set_ai_access(self.fid_a, False)
        ex, _ = self._make_executor()
        out = ex.execute("compare_photos", {"file_id_a": self.fid_a, "file_id_b": self.fid_b})
        self.assertIn("AI 접근 차단", out)

    def test_returns_claude_text(self):
        ex, _ = self._make_executor("두 사진의 차이: 조명이 더 밝아졌습니다.")
        out = ex.execute("compare_photos", {"file_id_a": self.fid_a, "file_id_b": self.fid_b})
        self.assertEqual(out, "두 사진의 차이: 조명이 더 밝아졌습니다.")

    def test_both_images_passed_to_api(self):
        ex, client = self._make_executor()
        ex.execute("compare_photos", {"file_id_a": self.fid_a, "file_id_b": self.fid_b})
        content = client.messages.create.call_args.kwargs["messages"][0]["content"]
        # 첫 두 항목이 image 여야 함.
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[1]["type"], "image")


if __name__ == "__main__":
    unittest.main()
