"""사비스 도구 시스템 — LLM이 호출하는 전문 모델/기능들

Microsoft SARVIS의 4단계 패턴을 Claude tool_use로 구현:
  Task Planning → Model Selection → Task Execution → Response Generation
"""
import base64
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# cv2 는 vision 모듈의 lazy 로더를 재사용 (배포 cold start 60초 제한 회피).
# 모듈 import 시 cv2 를 즉시 로드하면 uvicorn 이 포트 열기 전에 헬스체크 실패.
from .vision import _ensure_cv2

def _get_cv2():
    """cv2 모듈 객체를 lazy 로 반환 (없으면 None)."""
    if _ensure_cv2():
        import cv2 as _cv2
        return _cv2
    return None

from .config import cfg


# ============================================================
# Anthropic Tool Use 형식의 도구 스펙
# ============================================================
TOOL_DEFINITIONS = [
    {
        "name": "see",
        "description": (
            "Take a snapshot from the camera and describe what's visible. "
            "Use this when the user asks about their physical surroundings, "
            "what they're holding, their appearance, or anything visual."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Specific question about the scene to focus the analysis (in Korean)",
                }
            },
            "required": ["question"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Quick fact check via web search snippets (top 6). Use for short, "
            "lookup-style questions where titles + snippets are enough. "
            "Time-sensitive queries automatically get today's date appended. "
            "For deep answers needing article body, prefer 'web_answer'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_answer",
        "description": (
            "Search + fetch top pages and return relevant excerpts so you can give "
            "a grounded answer. Use whenever the user asks a factual / 'what is' / "
            "'how does' / 'who is' / 'latest news on' question that goes beyond a "
            "one-line snippet. Slower than web_search but returns real article text "
            "with sources."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language question to research",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_weather",
        "description": "Get current weather for a location (free Open-Meteo API).",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name (e.g., 'Seoul', 'Tokyo')",
                }
            },
            "required": ["location"],
        },
    },
    {
        "name": "get_time",
        "description": "Get the current date and time.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "remember",
        "description": (
            "Store information in long-term memory. Use when the user asks you to "
            "remember something, or when you discover important user info."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short identifier"},
                "value": {"type": "string", "description": "Information to store"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "recall",
        "description": "Search long-term memory for information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "set_timer",
        "description": "Set a timer that announces when expired.",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "description": "Duration in seconds"},
                "label": {"type": "string", "description": "What the timer is for"},
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "identify_person",
        "description": (
            "Identify who the person on the camera is by comparing their face "
            "against the registered people in S.A.R.V.I.S's memory. "
            "Use when the user asks 'who is this', 'who am I', 'do you recognize me', "
            "'내가 누구야', '이 사람 누구야', '나 알아?', or whenever knowing the "
            "person's identity helps personalize the response. Returns the person's "
            "name from the registry, or '모름' if no match."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "observe_action",
        "description": (
            "Analyze the user's recent action/behavior visible on camera. "
            "Use when the user asks 'what am I doing', 'how do I look right now', "
            "or when behavior monitoring is enabled and you need to describe an activity. "
            "Returns a description of the person's current pose, gesture, or activity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "Aspect to focus on: 'pose', 'gesture', 'activity', or a Korean phrase",
                }
            },
            "required": [],
        },
    },
]


# ============================================================
# 도구 실행기
# ============================================================
class ToolExecutor:
    def __init__(
        self,
        vision_system,
        anthropic_client,
        on_event: Optional[Callable[[str, str], None]] = None,
        on_timer: Optional[Callable[[str], None]] = None,
        face_registry=None,
    ):
        self.vision = vision_system
        self.client = anthropic_client  # Claude Vision 호출용
        self.on_event = on_event       # callback(tool_name, status: "start"|"end")
        self.on_timer = on_timer       # callback(label) — 타이머 만료 시 호출
        self.face_registry = face_registry  # FaceRegistry (선택)

        # 사이클 #9 정비: 도구의 영속 메모리도 data/ 아래로 통일.
        self.memory_path = Path(os.environ.get("SARVIS_TOOL_MEMORY", "data/memory.json"))
        self.memory: dict = self._load_memory()

    def definitions(self) -> List[dict]:
        return TOOL_DEFINITIONS

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        """LLM이 결정한 도구 실행"""
        if self.on_event:
            self.on_event(name, "start")
        try:
            method = getattr(self, f"_t_{name}", None)
            if method is None:
                return f"Unknown tool: {name}"
            result = method(**args)
        except TypeError as e:
            result = f"Argument error: {e}"
        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
        finally:
            if self.on_event:
                self.on_event(name, "end")
        return result

    # -------- Tools --------

    def _t_see(self, question: str) -> str:
        """카메라 프레임 → Claude Vision"""
        frame = self.vision.read()
        if frame is None:
            return "카메라 프레임을 가져올 수 없습니다."

        # JPEG 압축 (속도/대역폭)
        cv2 = _get_cv2()
        if cv2 is None:
            return "카메라 기능을 사용할 수 없습니다 (cv2 미설치)."
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return "이미지 인코딩 실패"
        b64 = base64.standard_b64encode(buf.tobytes()).decode("utf-8")

        try:
            msg = self.client.messages.create(
                model=cfg.vision_model,  # 비전은 Haiku로 빠르게
                max_tokens=300,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    f"사용자의 카메라에서 찍힌 장면이야. "
                                    f"다음 질문에 한국어로 간결히 답해줘 (1-2문장):\n{question}"
                                ),
                            },
                        ],
                    }
                ],
            )
            return msg.content[0].text.strip()
        except Exception as e:
            return f"비전 분석 실패: {e}"

    # ---- 웹 검색 헬퍼들 (사이클 #29) ------------------------------------
    # 시간 민감 질의 패턴 — 매칭되면 오늘 날짜를 query 에 자동 부착해 검색 신선도↑
    _TIME_SENSITIVE_PATTERNS = (
        "오늘", "지금", "현재", "최근", "요즘", "이번 주", "이번주", "올해",
        "어제", "내일", "방금", "실시간", "라이브",
        "today", "now", "current", "latest", "recent", "this week", "this year",
    )

    @staticmethod
    def _date_hint(query: str) -> str:
        """시간 민감 질의면 'YYYY-MM-DD' 부착, 아니면 원본 그대로.

        검색 엔진은 보통 최근 키워드보다 명시적 날짜를 우선시하므로,
        '오늘 환율', 'latest news on X' 같은 질의에 신선도 향상 효과.
        """
        q = (query or "").strip()
        if not q:
            return q
        ql = q.lower()
        if not any(p in ql for p in ToolExecutor._TIME_SENSITIVE_PATTERNS):
            return q
        today = datetime.now().strftime("%Y-%m-%d")
        if today in q:
            return q
        return f"{q} {today}"

    @staticmethod
    def _strip_time_qualifier(query: str) -> str:
        """검색 결과 0건 시 재시도용 — 시간 한정자/날짜를 제거한 더 일반적 질의."""
        import re
        q = query
        for p in ToolExecutor._TIME_SENSITIVE_PATTERNS:
            q = q.replace(p, " ")
        # YYYY-MM-DD / YYYY 제거
        q = re.sub(r"\b20\d{2}(-\d{2}-\d{2})?\b", " ", q)
        q = re.sub(r"\s+", " ", q).strip()
        return q or query

    @staticmethod
    def _is_safe_url(url: str) -> bool:
        """SSRF 방어 — http/https 만 허용 + 호스트의 모든 해석 IP 가 공인망인지 검증.

        차단: 사설/loopback/link-local/multicast/reserved/unspecified + 클라우드
        metadata IP(169.254.169.254 / fd00:ec2::254). DNS 가 여러 레코드를
        리턴하면 "하나라도" 내부 IP 면 거부 (rebinding 부분 방어).
        """
        try:
            import ipaddress
            import socket
            from urllib.parse import urlparse
            p = urlparse(url)
            if p.scheme not in ("http", "https"):
                return False
            host = p.hostname
            if not host:
                return False
            infos = socket.getaddrinfo(host, None)
            if not infos:
                return False
            for fam, _stype, _proto, _cn, sa in infos:
                ip_str = sa[0]
                # IPv6 zone id 제거
                if "%" in ip_str:
                    ip_str = ip_str.split("%", 1)[0]
                try:
                    ip = ipaddress.ip_address(ip_str)
                except Exception:
                    return False
                if (ip.is_private or ip.is_loopback or ip.is_link_local
                        or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                    return False
                if ip_str in ("169.254.169.254", "fd00:ec2::254"):
                    return False
            return True
        except Exception:
            return False

    @staticmethod
    def _fetch_clean_text(url: str, max_chars: int = 6000, timeout: float = 5.0,
                         max_redirects: int = 3) -> str:
        """URL 의 본문 텍스트를 추출. script/style/nav/footer 제거 후 잘라서 반환.

        SSRF 방어: 매 리다이렉트 hop 마다 _is_safe_url 재검증 + 자동 follow 차단.
        에러는 빈 문자열로 리턴 (호출자가 다음 URL 로 폴백).
        """
        try:
            import urllib.request
            import urllib.error

            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None  # 자동 리다이렉트 차단 — 수동 처리

            opener = urllib.request.build_opener(_NoRedirect())
            current = url
            raw: bytes = b""
            ctype: str = ""
            for _ in range(max_redirects + 1):
                if not ToolExecutor._is_safe_url(current):
                    return ""
                req = urllib.request.Request(
                    current,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 SARVIS/1.0"
                        ),
                        "Accept-Language": "ko,en;q=0.7",
                    },
                )
                try:
                    r = opener.open(req, timeout=timeout)
                except urllib.error.HTTPError as he:
                    # 3xx — 자동 follow 가 막혀서 raise 됨. 수동으로 다음 hop 검증.
                    if 300 <= he.code < 400:
                        loc = he.headers.get("Location") if he.headers else None
                        if not loc:
                            return ""
                        # 상대 URL 절대화
                        from urllib.parse import urljoin
                        current = urljoin(current, loc)
                        continue
                    return ""
                with r:
                    ctype = r.headers.get("Content-Type", "") or ""
                    if "html" not in ctype.lower():
                        return ""
                    raw = r.read(800_000)
                break
            else:
                return ""
            if not raw:
                return ""
            try:
                from bs4 import BeautifulSoup
            except Exception:
                # bs4 없으면 매우 단순 strip
                import re
                text = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", errors="replace"))
                return re.sub(r"\s+", " ", text).strip()[:max_chars]
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form"]):
                tag.decompose()
            # main/article 우선, 없으면 body 전체
            root = soup.find("article") or soup.find("main") or soup.body or soup
            text = root.get_text(separator=" ", strip=True) if root else ""
            import re
            text = re.sub(r"\s+", " ", text).strip()
            return text[:max_chars]
        except Exception:
            return ""

    @staticmethod
    def _extract_relevant_window(text: str, query: str, window: int = 500, max_windows: int = 3) -> str:
        """본문에서 query 키워드 주변 텍스트 윈도우 N개를 추출해 합침.

        키워드 매칭 0건이면 본문 앞부분 1개 윈도우 반환.
        """
        if not text:
            return ""
        # 한글/영문 토큰 분해 (단순)
        import re
        tokens = [t for t in re.split(r"[\s\.,!?·、，。！？\(\)\[\]\"']+", query) if len(t) >= 2]
        # 너무 일반적인 stopword 제거
        STOP = {"오늘", "지금", "현재", "최근", "요즘", "올해", "어제", "내일",
                "today", "now", "current", "latest", "what", "who", "how", "when",
                "이거", "그거", "저거", "이건", "그건", "이것", "그것"}
        tokens = [t for t in tokens if t.lower() not in STOP][:6]
        if not tokens:
            return text[:window]

        text_l = text.lower()
        hits: List[int] = []
        for tok in tokens:
            start = 0
            tl = tok.lower()
            while True:
                idx = text_l.find(tl, start)
                if idx < 0:
                    break
                hits.append(idx)
                start = idx + len(tl)
                if len(hits) >= 30:
                    break
        if not hits:
            return text[:window]
        hits.sort()
        # 인접한 hit 들을 합쳐 window 그룹화
        windows: List[tuple] = []  # (lo, hi)
        cur_lo = max(0, hits[0] - window // 2)
        cur_hi = min(len(text), hits[0] + window // 2)
        for h in hits[1:]:
            lo = max(0, h - window // 2)
            hi = min(len(text), h + window // 2)
            if lo <= cur_hi:  # 겹치거나 인접
                cur_hi = max(cur_hi, hi)
            else:
                windows.append((cur_lo, cur_hi))
                cur_lo, cur_hi = lo, hi
        windows.append((cur_lo, cur_hi))
        windows = windows[:max_windows]
        chunks = [text[lo:hi].strip() for (lo, hi) in windows]
        return " … ".join(chunks)

    def _t_web_search(self, query: str) -> str:
        """빠른 스니펫 검색 (상위 6개). 시간 민감 질의는 날짜 부착 + 빈 결과 시 재시도."""
        try:
            from duckduckgo_search import DDGS
        except Exception as e:
            return f"검색 실패: {e}"

        q1 = self._date_hint(query)
        results: list = []
        try:
            results = list(DDGS().text(q1, max_results=6, region="kr-kr"))
        except Exception as e:
            return f"검색 실패: {e}"

        # 1차 빈 결과 → 시간 한정자 제거 + region wt-wt 로 재시도
        if not results:
            q2 = self._strip_time_qualifier(query)
            if q2 and q2 != q1:
                try:
                    results = list(DDGS().text(q2, max_results=6, region="wt-wt"))
                except Exception:
                    results = []

        if not results:
            return f"'{query}' 검색 결과 없음"
        lines = []
        for r in results[:6]:
            title = (r.get("title") or "").strip()
            body = (r.get("body") or "").strip()
            href = (r.get("href") or "").strip()
            if title and body:
                src = f" ({href})" if href else ""
                lines.append(f"- {title}{src}: {body}")
        return "\n".join(lines) if lines else f"'{query}' 검색 결과 없음"

    def _t_web_answer(self, query: str) -> str:
        """검색 → 상위 3개 페이지 본문 fetch → 질문 키워드 주변 발췌 → 합쳐서 반환.

        brain 이 받아서 종합 답변하기 좋은 형태:
          [출처1] 제목 (URL)
          ...본문 발췌...
          [출처2] ...
        총 ~6KB 이내로 정렬.
        """
        try:
            from duckduckgo_search import DDGS
        except Exception as e:
            return f"검색 실패: {e}"

        q1 = self._date_hint(query)
        try:
            results = list(DDGS().text(q1, max_results=8, region="kr-kr"))
        except Exception as e:
            return f"검색 실패: {e}"
        if not results:
            q2 = self._strip_time_qualifier(query)
            if q2 and q2 != q1:
                try:
                    results = list(DDGS().text(q2, max_results=8, region="wt-wt"))
                except Exception:
                    results = []
        if not results:
            return f"'{query}' 검색 결과 없음"

        # 상위 페이지 fetch — 최대 3개 성공할 때까지
        sections: List[str] = []
        used = 0
        for r in results:
            if used >= 3:
                break
            href = (r.get("href") or "").strip()
            title = (r.get("title") or "").strip() or "(제목 없음)"
            snippet = (r.get("body") or "").strip()
            if not href:
                continue
            body = self._fetch_clean_text(href, max_chars=8000)
            excerpt = self._extract_relevant_window(body, query, window=500, max_windows=2) if body else ""
            if not excerpt:
                # 페이지 본문 못 받으면 스니펫이라도 사용
                if not snippet:
                    continue
                excerpt = snippet
            sections.append(f"[출처{used + 1}] {title}\nURL: {href}\n{excerpt}")
            used += 1

        if not sections:
            # 최후 폴백 — 스니펫만 합쳐서 반환
            lines = []
            for r in results[:5]:
                t = (r.get("title") or "").strip()
                b = (r.get("body") or "").strip()
                h = (r.get("href") or "").strip()
                if t and b:
                    lines.append(f"- {t} ({h}): {b}")
            return "\n".join(lines) if lines else f"'{query}' 결과 없음"

        joined = "\n\n".join(sections)
        # 안전 길이 제한
        if len(joined) > 7000:
            joined = joined[:7000] + " …"
        return joined

    def _t_get_weather(self, location: str) -> str:
        import urllib.parse
        import urllib.request

        try:
            # 1) 지오코딩
            q = urllib.parse.quote(location)
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=ko"
            with urllib.request.urlopen(geo_url, timeout=5) as r:
                geo = json.loads(r.read())
            if not geo.get("results"):
                return f"'{location}' 위치 정보를 찾을 수 없습니다."
            place = geo["results"][0]
            lat, lon = place["latitude"], place["longitude"]
            name = place.get("name", location)
            country = place.get("country", "")

            # 2) 날씨
            w_url = (
                f"https://api.open-meteo.com/v1/forecast?"
                f"latitude={lat}&longitude={lon}"
                f"&current=temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m"
                f"&timezone=auto"
            )
            with urllib.request.urlopen(w_url, timeout=5) as r:
                w = json.loads(r.read())
            cur = w["current"]
            code = cur["weather_code"]
            desc = _WEATHER_CODES.get(code, f"코드 {code}")

            return (
                f"{name}{(' (' + country + ')') if country else ''} 현재 {desc}, "
                f"기온 {cur['temperature_2m']}°C, "
                f"습도 {cur['relative_humidity_2m']}%, "
                f"풍속 {cur['wind_speed_10m']}m/s"
            )
        except Exception as e:
            return f"날씨 조회 실패: {e}"

    def _t_get_time(self) -> str:
        weekdays = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
        n = datetime.now()
        return f"{n.year}년 {n.month}월 {n.day}일 {weekdays[n.weekday()]} {n.hour}시 {n.minute}분"

    def _t_remember(self, key: str, value: str) -> str:
        self.memory[key] = {"value": value, "ts": time.time()}
        self._save_memory()
        return f"기억함: '{key}' = '{value}'"

    def _t_recall(self, query: str) -> str:
        q = query.lower()
        matches = [
            (k, v["value"])
            for k, v in self.memory.items()
            if q in k.lower() or q in v["value"].lower()
        ]
        if not matches:
            return f"'{query}'와 관련된 기억 없음"
        return "\n".join(f"{k}: {v}" for k, v in matches[:5])

    def _t_observe_action(self, focus: str = "activity") -> str:
        """카메라에서 사람의 행동/자세/제스처를 인식 (Claude Vision)."""
        frame = self.vision.read()
        if frame is None:
            return "카메라에 사람이 보이지 않거나 프레임을 가져올 수 없습니다."

        cv2 = _get_cv2()
        if cv2 is None:
            return "카메라 기능을 사용할 수 없습니다 (cv2 미설치)."
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return "이미지 인코딩 실패"
        b64 = base64.standard_b64encode(buf.tobytes()).decode("utf-8")

        try:
            msg = self.client.messages.create(
                model=cfg.vision_model,
                max_tokens=200,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    "이 이미지는 사용자의 카메라 화면이야. "
                                    f"사람의 {focus}(행동/자세/제스처)을 한국어로 1-2문장으로 묘사해. "
                                    "사람이 명확히 보이지 않으면 '사람이 보이지 않음'이라고만 답해. "
                                    "객관적 사실만, 추측은 하지 마."
                                ),
                            },
                        ],
                    }
                ],
            )
            return msg.content[0].text.strip()
        except Exception as e:
            return f"행동 인식 실패: {e}"

    def _t_identify_person(self) -> str:
        """현재 카메라 프레임의 얼굴을 등록된 사람들과 비교해 식별 (Claude Vision)."""
        if self.face_registry is None:
            return "얼굴 등록 시스템이 활성화되지 않았습니다."

        refs = self.face_registry.get_references()
        if not refs:
            return "등록된 얼굴이 없습니다. 먼저 사용자의 얼굴을 등록해야 합니다."

        # 현재 프레임에서 가장 큰 얼굴 잘라내기
        crop_jpeg = None
        if hasattr(self.vision, "crop_largest_face_jpeg"):
            crop_jpeg = self.vision.crop_largest_face_jpeg()
        if crop_jpeg is None:
            # 폴백: 전체 프레임
            frame = self.vision.read()
            cv2 = _get_cv2()
            if frame is None or cv2 is None:
                return "카메라에 사람이 보이지 않거나 프레임을 가져올 수 없습니다."
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                return "이미지 인코딩 실패"
            crop_jpeg = buf.tobytes()

        current_b64 = base64.standard_b64encode(crop_jpeg).decode("utf-8")

        # 메시지 구성: 등록된 사진 N장 + 현재 사진 1장 + 지시문
        content: List[dict] = []
        names_listed = []
        for idx, (name, b64) in enumerate(refs, 1):
            names_listed.append(f"{idx}. {name}")
            content.append({"type": "text", "text": f"등록된 사람 {idx}: {name}"})
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            })
        content.append({"type": "text", "text": "현재 카메라에 찍힌 사람:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": current_b64},
        })
        roster = "\n".join(names_listed)
        content.append({
            "type": "text",
            "text": (
                "위에 등록된 사람들의 얼굴 사진과 현재 카메라 사진을 비교해. "
                f"현재 사진 속 인물이 다음 중 누구인지 정확히 식별해:\n{roster}\n\n"
                "응답 형식: 일치하는 사람의 이름만 정확히 한 단어로. "
                "확신이 없거나 일치하는 사람이 없으면 '모름'이라고만 답해. "
                "추가 설명 금지, 이름 또는 '모름'만."
            ),
        })

        try:
            msg = self.client.messages.create(
                model=cfg.vision_model,
                max_tokens=30,
                messages=[{"role": "user", "content": content}],
            )
            answer = msg.content[0].text.strip()
            # 정리: 따옴표/마침표 제거
            answer = answer.strip(" .,'\"\n")
            if not answer or answer == "모름":
                return "현재 카메라의 사람은 등록된 사람과 일치하지 않습니다."

            # 등록된 이름 중 하나와 매칭되는지 확인 (보호장치)
            registered_names = [r[0] for r in refs]
            for n in registered_names:
                if n in answer or answer in n:
                    return f"식별됨: {n}"
            return f"가장 유사한 후보: {answer} (확실하지 않음)"
        except Exception as e:
            return f"얼굴 식별 실패: {e}"

    def _t_set_timer(self, seconds: int, label: str = "타이머") -> str:
        if seconds <= 0:
            return "타이머는 1초 이상이어야 합니다."

        def trigger():
            time.sleep(seconds)
            print(f"\n⏰ 타이머 만료: {label}")
            if self.on_timer:
                self.on_timer(label)

        threading.Thread(target=trigger, daemon=True).start()
        # 사람이 읽기 좋은 형식
        if seconds >= 60:
            mins, secs = divmod(seconds, 60)
            human = f"{mins}분 {secs}초" if secs else f"{mins}분"
        else:
            human = f"{seconds}초"
        return f"{human} 타이머 '{label}' 설정됨"

    # -------- 메모리 입출력 --------
    def _load_memory(self) -> dict:
        if self.memory_path.exists():
            try:
                return json.loads(self.memory_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_memory(self):
        # 사이클 #9 정비: data/ 등 하위 경로면 부모 디렉토리 자동 생성.
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.memory_path.write_text(
            json.dumps(self.memory, indent=2, ensure_ascii=False), encoding="utf-8"
        )


# WMO weather codes → 한국어
_WEATHER_CODES = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림",
    45: "안개", 48: "서리 안개",
    51: "이슬비 약함", 53: "이슬비", 55: "강한 이슬비",
    61: "비 약함", 63: "비", 65: "강한 비",
    71: "눈 약함", 73: "눈", 75: "강한 눈",
    77: "싸락눈", 80: "소나기", 81: "강한 소나기", 82: "매우 강한 소나기",
    85: "눈 소나기", 86: "강한 눈 소나기",
    95: "뇌우", 96: "뇌우+우박", 99: "강한 뇌우+우박",
}
