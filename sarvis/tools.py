"""사비스 도구 시스템 — LLM이 호출하는 전문 모델/기능들

Microsoft SARVIS의 4단계 패턴을 Claude tool_use로 구현:
  Task Planning → Model Selection → Task Execution → Response Generation
"""
import base64
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
        "name": "read_text",
        "description": (
            "Read and extract text visible on screen/camera. Use when the user asks "
            "'읽어줘', '뭐라고 써있어', '글자 읽어', '텍스트 읽어', 'read this', "
            "'화면 읽어', '여기 뭐라고 써있어', '간판 읽어', '문서 읽어', '메뉴판 읽어'. "
            "Captures the camera frame and extracts all visible text using vision AI."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "What kind of text to focus on (e.g. '간판', '메뉴', '문서', '화면', '라벨'). Leave empty to read all visible text.",
                }
            },
            "required": [],
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
        "name": "start_recording",
        "description": (
            "Start recording video from the user's camera. Use when the user asks "
            "'녹화해', '녹화 시작', 'record', 'start recording', '찍어', '영상 찍어'. "
            "Returns confirmation that recording has started."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Optional label for this recording (e.g. '운동 기록', '요리 과정')",
                }
            },
            "required": [],
        },
    },
    {
        "name": "stop_recording",
        "description": (
            "Stop the current video recording and save the file. Use when the user asks "
            "'녹화 중지', '녹화 끝', '녹화 멈춰', 'stop recording', '그만 찍어'. "
            "Returns the saved file info."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "start_audio_recording",
        "description": (
            "Start recording audio from the user's microphone. Use when the user asks "
            "'녹음해', '녹음 시작', '음성 녹음', '소리 녹음', 'record audio', "
            "'목소리 녹음해', '말 녹음해'. Returns confirmation that audio recording has started."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Optional label for this audio recording (e.g. '회의 녹음', '메모')",
                }
            },
            "required": [],
        },
    },
    {
        "name": "stop_audio_recording",
        "description": (
            "Stop the current audio recording and save the file. Use when the user asks "
            "'녹음 중지', '녹음 끝', '녹음 멈춰', 'stop audio recording', '녹음 그만'. "
            "Returns the saved file info."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "open_url",
        "description": (
            "Open a URL/website in the user's browser. Use when the user asks "
            "'유튜브 열어', '네이버 켜줘', '구글 열어줘', '이 사이트 열어', "
            "'브라우저에서 열어줘', 'open youtube', '검색창 열어'. "
            "Opens in a new browser tab."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL to open (e.g. 'https://youtube.com'). If user gives a site name, convert to URL.",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "send_notification",
        "description": (
            "Send a browser notification to the user. Use when the user asks "
            "'알려줘', '알림 보내줘', '리마인드해줘', 'notify me', "
            "or when a timer/alarm fires and user needs to be notified."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Notification title (short, in Korean)",
                },
                "body": {
                    "type": "string",
                    "description": "Notification body text",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "set_alarm",
        "description": (
            "Set an alarm at a specific time. Use when the user asks "
            "'알람 맞춰줘', '몇 시에 알려줘', '알람 설정', '깨워줘', "
            "'오후 3시에 알려줘', 'set alarm'. Different from set_timer: "
            "set_timer is for duration (e.g. 5 minutes), set_alarm is for "
            "a specific clock time (e.g. 15:00)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hour": {
                    "type": "integer",
                    "description": "Hour in 24h format (0-23)",
                },
                "minute": {
                    "type": "integer",
                    "description": "Minute (0-59)",
                },
                "label": {
                    "type": "string",
                    "description": "What the alarm is for",
                },
            },
            "required": ["hour", "minute"],
        },
    },
    {
        "name": "set_volume",
        "description": (
            "Adjust the assistant's voice/TTS volume. Use when the user asks "
            "'볼륨 올려', '소리 줄여', '볼륨 50', '소리 크게', '소리 작게', "
            "'음량 조절', 'volume up', 'louder', 'quieter'. "
            "Value 0-100 (0=mute, 100=max)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "integer",
                    "description": "Volume level 0-100",
                }
            },
            "required": ["level"],
        },
    },
    {
        "name": "change_setting",
        "description": (
            "Change a Sarvis system setting. Use when the user asks to switch AI model, "
            "change voice, or adjust system configuration. "
            "'모델 바꿔', 'GPT로 바꿔줘', '클로드로 전환', '음성 바꿔줘', "
            "'목소리 변경', '여자 목소리로', '남자 목소리로'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "setting": {
                    "type": "string",
                    "description": "Setting to change: 'backend' (claude/openai/gemini), 'voice' (preset name), 'model' (model name)",
                    "enum": ["backend", "voice", "model"],
                },
                "value": {
                    "type": "string",
                    "description": "New value for the setting",
                },
            },
            "required": ["setting", "value"],
        },
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
        on_recording: Optional[Callable[[str, str], None]] = None,
        on_system_cmd: Optional[Callable[[dict], None]] = None,
    ):
        self.vision = vision_system
        self.client = anthropic_client  # Claude Vision 호출용
        self.on_event = on_event       # callback(tool_name, status: "start"|"end")
        self.on_timer = on_timer       # callback(label) — 타이머 만료 시 호출
        self.face_registry = face_registry  # FaceRegistry (선택)
        self.on_recording = on_recording   # callback(action, label, kind) — "start"|"stop"
        self.on_system_cmd = on_system_cmd  # callback(cmd_dict) — 시스템 제어 명령
        self.is_recording = False
        self._recording_label = ""
        self.is_audio_recording = False
        self._audio_recording_label = ""

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

        b64 = self._frame_to_b64(frame)
        if b64 is None:
            return "카메라 기능을 사용할 수 없습니다 (cv2 미설치)."

        try:
            msg = self.client.messages.create(
                model=cfg.vision_model,
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

    def _t_read_text(self, focus: str = "") -> str:
        """카메라 프레임에서 텍스트 추출 (OCR via Claude Vision)"""
        frame = self.vision.read()
        if frame is None:
            return "카메라 프레임을 가져올 수 없습니다."

        b64 = self._frame_to_b64(frame, quality=92)
        if b64 is None:
            return "카메라 기능을 사용할 수 없습니다 (cv2 미설치)."

        focus_hint = f" 특히 '{focus}' 부분에 집중해서" if focus else ""

        prompt = (
            "이 이미지에 보이는 모든 텍스트를 정확하게 읽어서 그대로 옮겨줘."
            f"{focus_hint}\n\n"
            "규칙:\n"
            "- 보이는 글자를 최대한 정확히, 원문 그대로 추출해.\n"
            "- 한국어, 영어, 숫자, 특수문자 모두 포함.\n"
            "- 텍스트 영역이 여러 개면 위치별로 구분해서 알려줘 "
            "(예: [상단], [중앙], [하단], [왼쪽], [오른쪽]).\n"
            "- 글자가 흐리거나 잘려서 불확실한 부분은 [?]로 표시해.\n"
            "- 텍스트가 전혀 보이지 않으면 '텍스트가 보이지 않습니다'라고만 답해.\n"
            "- 불필요한 설명 없이 추출된 텍스트만 간결하게."
        )

        try:
            msg = self.client.messages.create(
                model=cfg.vision_model,
                max_tokens=1024,
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
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
            return msg.content[0].text.strip()
        except Exception as e:
            return f"텍스트 읽기 실패: {e}"

    def _frame_to_b64(self, frame, quality: int = 85) -> Optional[str]:
        """카메라 프레임을 JPEG base64로 변환."""
        cv2 = _get_cv2()
        if cv2 is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return None
        return base64.standard_b64encode(buf.tobytes()).decode("utf-8")

    # ---- 웹 검색 헬퍼들 (사이클 #29) ------------------------------------
    # 시간 민감 질의 패턴 — 매칭되면 오늘 날짜를 query 에 자동 부착해 검색 신선도↑
    _TIME_SENSITIVE_PATTERNS = (
        "오늘", "지금", "현재", "최근", "요즘", "이번 주", "이번주", "올해",
        "어제", "내일", "방금", "실시간", "라이브", "지난주", "지난 주",
        "이번 달", "이번달", "지난달", "오늘날짜",
        "today", "now", "current", "latest", "recent", "this week", "this year",
        "this month", "last week",
    )

    # 뉴스성 질의 — DDGS.news() 까지 추가로 호출해 신선한 뉴스 결과 합침.
    _NEWS_INTENT_PATTERNS = (
        "뉴스", "속보", "기사", "이슈", "보도", "헤드라인",
        "news", "headline", "breaking",
    )

    # 검색 결과 in-memory TTL 캐시 — 같은 질의 재호출/도구 연속 호출 시
    # DDGS rate-limit 회피 + 응답 속도 개선. (최대 항목/체류시간 작게 유지)
    _CACHE_TTL_S = 600  # 10분
    _CACHE_MAX = 64
    _cache: Dict[str, Tuple[float, str]] = {}
    _cache_lock = threading.Lock()

    @staticmethod
    def _cache_get(key: str) -> Optional[str]:
        with ToolExecutor._cache_lock:
            entry = ToolExecutor._cache.get(key)
            if not entry:
                return None
            ts, val = entry
            if time.time() - ts > ToolExecutor._CACHE_TTL_S:
                ToolExecutor._cache.pop(key, None)
                return None
            return val

    @staticmethod
    def _cache_put(key: str, val: str) -> None:
        with ToolExecutor._cache_lock:
            if len(ToolExecutor._cache) >= ToolExecutor._CACHE_MAX:
                # 가장 오래된 항목 제거 (FIFO)
                try:
                    oldest = min(ToolExecutor._cache.items(), key=lambda kv: kv[1][0])[0]
                    ToolExecutor._cache.pop(oldest, None)
                except ValueError:
                    pass
            ToolExecutor._cache[key] = (time.time(), val)

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
        q = query
        for p in ToolExecutor._TIME_SENSITIVE_PATTERNS:
            q = q.replace(p, " ")
        # YYYY-MM-DD / YYYY 제거
        q = re.sub(r"\b20\d{2}(-\d{2}-\d{2})?\b", " ", q)
        q = re.sub(r"\s+", " ", q).strip()
        return q or query

    @staticmethod
    def _is_news_intent(query: str) -> bool:
        ql = (query or "").lower()
        return any(p in ql for p in ToolExecutor._NEWS_INTENT_PATTERNS)

    @staticmethod
    def _domain_of(url: str) -> str:
        """URL 의 호스트(www. 제거, lowercase) — 중복 출처 dedupe 용."""
        try:
            from urllib.parse import urlparse
            host = (urlparse(url).hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return ""

    @staticmethod
    def _dedupe_by_domain(results: List[dict], max_per_domain: int = 1,
                         max_total: int = 8) -> List[dict]:
        """검색 결과를 도메인별 빈도 제한으로 다양화.

        같은 사이트에서 N개가 잡히면 1개만 살리고 나머지는 뒤쪽으로 밀어
        다양한 출처가 상위에 오게 한다. 단 모든 결과가 동일 도메인일 경우에도
        최소 min(len(results), max_total) 개는 보장 — overflow 를 도메인 무시
        하고 채워 사용자에게 빈 결과를 돌려주지 않는다.
        """
        if not results:
            return results
        seen: Dict[str, int] = {}
        primary: List[dict] = []
        overflow: List[dict] = []
        for r in results:
            href = (r.get("href") or "").strip()
            d = ToolExecutor._domain_of(href)
            if not d:
                primary.append(r)
                continue
            cnt = seen.get(d, 0)
            if cnt < max_per_domain:
                primary.append(r)
                seen[d] = cnt + 1
            else:
                overflow.append(r)
            if len(primary) >= max_total:
                break
        # 부족하면 overflow 로 채움 — 도메인 제한 무시하고 max_total 까지 확보.
        if len(primary) < max_total and overflow:
            need = max_total - len(primary)
            primary.extend(overflow[:need])
        return primary

    @staticmethod
    def _ddgs_search(query: str, *, max_results: int = 8,
                    region: str = "kr-kr",
                    include_news: bool = False) -> List[dict]:
        """DDGS.text() 호출. include_news=True 면 .news() 결과를 앞쪽에 합침.

        반환 형식은 {title, body, href} dict 리스트. 실패는 [] 리턴(폴백 가능).
        """
        try:
            from duckduckgo_search import DDGS
        except Exception:
            return []
        out: List[dict] = []
        ddgs = DDGS()
        if include_news:
            try:
                # news 는 {title, body, url, date, source} 등을 줌 — href 키로 정규화.
                for n in ddgs.news(query, max_results=min(4, max_results), region=region):
                    href = (n.get("url") or n.get("href") or "").strip()
                    if not href:
                        continue
                    title = (n.get("title") or "").strip()
                    body = (n.get("body") or n.get("excerpt") or "").strip()
                    out.append({"title": title, "body": body, "href": href})
            except Exception:
                pass
        try:
            for r in ddgs.text(query, max_results=max_results, region=region):
                out.append(r)
        except Exception:
            # text 실패해도 news 결과는 반환
            pass
        return out

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

    # 한글 조사 — 키워드 매칭 시 어미를 떼어내 회수율 향상.
    # 짧은 명사("나", "너")의 잘못된 절단 방지를 위해 토큰 길이 ≥3 일 때만 적용.
    _KO_PARTICLES = (
        "으로써", "으로서", "으로", "에서", "에게", "께서", "한테", "이라고",
        "라고", "이며", "보다", "처럼", "마저", "조차", "까지", "부터",
        "이나", "이라", "이라도", "이든", "이면",
        "은", "는", "이", "가", "을", "를", "와", "과", "의", "도", "만",
    )

    _STOPWORDS = {
        "오늘", "지금", "현재", "최근", "요즘", "올해", "어제", "내일",
        "오늘날짜", "이번주", "이번달", "지난주", "지난달",
        "today", "now", "current", "latest", "what", "who", "how", "when",
        "where", "why", "is", "are", "the", "a", "an", "of", "in", "on", "at",
        "이거", "그거", "저거", "이건", "그건", "이것", "그것",
        "뭐", "뭐야", "어때", "알려줘", "찾아줘", "검색", "관련",
    }

    @staticmethod
    def _strip_ko_particle(tok: str) -> str:
        """한글 토큰 끝의 조사를 제거. 길이 3미만/영문 단독 토큰은 그대로."""
        if len(tok) < 3:
            return tok
        if not any("가" <= ch <= "힣" for ch in tok):
            return tok
        for p in ToolExecutor._KO_PARTICLES:
            if tok.endswith(p) and len(tok) > len(p) + 1:
                return tok[: -len(p)]
        return tok

    @staticmethod
    def _query_keywords(query: str, max_tokens: int = 6) -> List[str]:
        """질의에서 의미있는 키워드만 추출. 한글 조사 제거 + stopword 제거."""
        raw = [t for t in re.split(r"[\s\.,!?·、，。！？\(\)\[\]\"'’“”~]+", query) if t]
        out: List[str] = []
        seen = set()
        for t in raw:
            tl = t.lower()
            if tl in ToolExecutor._STOPWORDS:
                continue
            stripped = ToolExecutor._strip_ko_particle(t)
            sl = stripped.lower()
            if len(sl) < 2 or sl in seen or sl in ToolExecutor._STOPWORDS:
                continue
            seen.add(sl)
            out.append(stripped)
            if len(out) >= max_tokens:
                break
        return out

    @staticmethod
    def _extract_relevant_window(text: str, query: str, window: int = 500, max_windows: int = 3) -> str:
        """본문에서 query 키워드 주변 텍스트 윈도우 N개를 추출해 합침.

        Ranking: 윈도우 안의 *서로 다른* 키워드 개수가 많을수록 가점 (단순 hit 카운트
        보다 의미적으로 관련된 구간을 우선). 키워드 매칭 0건이면 본문 앞부분 반환.
        """
        if not text:
            return ""
        tokens = ToolExecutor._query_keywords(query)
        if not tokens:
            return text[:window]

        text_l = text.lower()
        # 토큰별 hit 위치 수집.
        token_hits: Dict[str, List[int]] = {}
        all_hits: List[Tuple[int, str]] = []
        for tok in tokens:
            tl = tok.lower()
            positions: List[int] = []
            start = 0
            while True:
                idx = text_l.find(tl, start)
                if idx < 0:
                    break
                positions.append(idx)
                all_hits.append((idx, tl))
                start = idx + len(tl)
                if len(positions) >= 30:
                    break
            token_hits[tl] = positions

        if not all_hits:
            return text[:window]

        # 인접 hit 들을 합쳐 window 그룹화.
        all_hits.sort()
        windows_raw: List[Tuple[int, int, set]] = []  # (lo, hi, distinct_tokens)
        cur_lo = max(0, all_hits[0][0] - window // 2)
        cur_hi = min(len(text), all_hits[0][0] + window // 2)
        cur_set = {all_hits[0][1]}
        for idx, tok in all_hits[1:]:
            lo = max(0, idx - window // 2)
            hi = min(len(text), idx + window // 2)
            if lo <= cur_hi:  # 겹치거나 인접
                cur_hi = max(cur_hi, hi)
                cur_set.add(tok)
            else:
                windows_raw.append((cur_lo, cur_hi, cur_set))
                cur_lo, cur_hi, cur_set = lo, hi, {tok}
        windows_raw.append((cur_lo, cur_hi, cur_set))

        # 점수: distinct 토큰 수 우선 + 길이 보너스(서로 동률일 때).
        windows_raw.sort(key=lambda w: (-len(w[2]), -(w[1] - w[0])))
        picked = windows_raw[:max_windows]
        # 본문 순서로 다시 정렬 (자연스러운 읽기 순)
        picked.sort(key=lambda w: w[0])
        chunks = [text[lo:hi].strip() for (lo, hi, _) in picked]
        return " … ".join(c for c in chunks if c)

    def _t_web_search(self, query: str) -> str:
        """빠른 스니펫 검색 (상위 6개). 시간 민감 질의는 날짜 부착 + 뉴스 의도면
        DDGS.news() 결과 합침 + 도메인 다양화 + TTL 캐시.

        실패/0건 시: stripped query 로 wt-wt region 재시도 → 최후엔 친절 메시지.
        """
        try:
            from duckduckgo_search import DDGS  # noqa: F401
        except Exception as e:
            return f"검색 실패: {e}"

        # 캐시 키는 lowercase 로 정규화 — "Apple" 과 "apple" 은 같은 검색.
        ck = f"search::{(query or '').strip().lower()}"
        cached = self._cache_get(ck)
        if cached:
            return cached

        q1 = self._date_hint(query)
        news = self._is_news_intent(query) or query != q1  # 시간/뉴스 의도면 news 합침
        results = self._ddgs_search(q1, max_results=8, region="kr-kr", include_news=news)

        if not results:
            q2 = self._strip_time_qualifier(query)
            if q2 and q2 != q1:
                results = self._ddgs_search(q2, max_results=8, region="wt-wt", include_news=False)

        if not results:
            return f"'{query}' 검색 결과 없음"

        results = self._dedupe_by_domain(results, max_per_domain=1, max_total=6)

        lines = []
        for r in results[:6]:
            title = (r.get("title") or "").strip()
            body = (r.get("body") or "").strip()
            href = (r.get("href") or "").strip()
            if title and body:
                src = f" ({href})" if href else ""
                lines.append(f"- {title}{src}: {body}")
        out = "\n".join(lines) if lines else f"'{query}' 검색 결과 없음"
        self._cache_put(ck, out)
        return out

    def _t_web_answer(self, query: str) -> str:
        """검색 → 상위 페이지 본문 병렬 fetch → 키워드 주변 발췌 → 합쳐서 반환.

        강화 사항:
          - DDGS.text + DDGS.news (시간/뉴스 질의) 결합
          - 같은 도메인 1개로 dedupe → 출처 다양화
          - 상위 5개 URL 을 ThreadPoolExecutor 로 병렬 fetch (지연 ↓)
          - 본문 발췌는 토큰 다양성 기반 window ranking
          - TTL 캐시(_CACHE_TTL_S) 로 동일 질의 즉시 재반환

        반환 형식:
          [출처1] 제목
          URL: ...
          ...본문 발췌...
          ...
        """
        try:
            from duckduckgo_search import DDGS  # noqa: F401
        except Exception as e:
            return f"검색 실패: {e}"

        ck = f"answer::{(query or '').strip().lower()}"
        cached = self._cache_get(ck)
        if cached:
            return cached

        q1 = self._date_hint(query)
        news = self._is_news_intent(query) or query != q1
        results = self._ddgs_search(q1, max_results=10, region="kr-kr", include_news=news)
        if not results:
            q2 = self._strip_time_qualifier(query)
            if q2 and q2 != q1:
                results = self._ddgs_search(q2, max_results=10, region="wt-wt", include_news=False)
        if not results:
            return f"'{query}' 검색 결과 없음"

        # 도메인 다양화 — 상위 5개를 후보로
        results = self._dedupe_by_domain(results, max_per_domain=1, max_total=5)

        # 후보 URL 목록 추출 (href 없는 결과 제외).
        candidates: List[dict] = []
        for r in results:
            href = (r.get("href") or "").strip()
            if not href:
                continue
            candidates.append(r)
            if len(candidates) >= 5:
                break

        if not candidates:
            return f"'{query}' 검색 결과 없음"

        # 상위 후보 URL 병렬 fetch (max 5 워커, 페이지당 5초 timeout).
        urls = [(i, (c.get("href") or "").strip()) for i, c in enumerate(candidates)]
        bodies: Dict[int, str] = {}
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=min(5, len(urls))) as ex:
                fut_to_idx = {
                    ex.submit(self._fetch_clean_text, url, 8000, 5.0): i
                    for i, url in urls
                }
                try:
                    for fut in as_completed(fut_to_idx, timeout=12):
                        i = fut_to_idx[fut]
                        try:
                            bodies[i] = fut.result() or ""
                        except Exception:
                            bodies[i] = ""
                except Exception:
                    # as_completed timeout 등 — 이미 끝난 future 의 결과는 회수.
                    for fut, i in fut_to_idx.items():
                        if i in bodies:
                            continue
                        if fut.done():
                            try:
                                bodies[i] = fut.result(timeout=0) or ""
                            except Exception:
                                bodies[i] = ""
        except Exception:
            # ThreadPoolExecutor 자체 실패 — 순차 폴백 (이미 받은 결과 보존, 누락 분만 재시도)
            for i, url in urls:
                if i in bodies:
                    continue
                try:
                    bodies[i] = self._fetch_clean_text(url, max_chars=8000)
                except Exception:
                    bodies[i] = ""

        # 발췌 합성 — 본문 성공한 출처 최대 3개 우선.
        sections: List[str] = []
        used = 0
        for i, c in enumerate(candidates):
            if used >= 3:
                break
            href = (c.get("href") or "").strip()
            title = (c.get("title") or "").strip() or "(제목 없음)"
            snippet = (c.get("body") or "").strip()
            body = bodies.get(i, "")
            excerpt = self._extract_relevant_window(body, query, window=500, max_windows=2) if body else ""
            if not excerpt:
                if not snippet:
                    continue
                excerpt = snippet
            sections.append(f"[출처{used + 1}] {title}\nURL: {href}\n{excerpt}")
            used += 1

        if not sections:
            # 최후 폴백 — 스니펫만 합쳐서 반환 (페이지 차단된 환경)
            lines = []
            for r in candidates[:5]:
                t = (r.get("title") or "").strip()
                b = (r.get("body") or "").strip()
                h = (r.get("href") or "").strip()
                if t and b:
                    lines.append(f"- {t} ({h}): {b}")
            out = "\n".join(lines) if lines else f"'{query}' 결과 없음"
            self._cache_put(ck, out)
            return out

        joined = "\n\n".join(sections)
        if len(joined) > 7000:
            joined = joined[:7000] + " …"
        self._cache_put(ck, joined)
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

    # -------- 녹화 / 녹음 --------
    def _t_start_recording(self, label: str = "") -> str:
        if self.is_recording:
            return "이미 영상 녹화 중입니다. 먼저 녹화를 중지해주세요."
        frame = self.vision.read()
        if frame is None:
            return "카메라가 켜져 있지 않습니다. 먼저 카메라를 시작해주세요."
        self.is_recording = True
        self._recording_label = label or ""
        if self.on_recording:
            self.on_recording("start", self._recording_label, "video")
        msg = "영상 녹화를 시작했습니다."
        if label:
            msg += f" (라벨: {label})"
        return msg

    def _t_stop_recording(self) -> str:
        if not self.is_recording:
            return "현재 영상 녹화 중이 아닙니다."
        self.is_recording = False
        label = self._recording_label
        self._recording_label = ""
        if self.on_recording:
            self.on_recording("stop", label, "video")
        return "영상 녹화를 중지했습니다. 파일을 저장하고 있습니다."

    def _t_start_audio_recording(self, label: str = "") -> str:
        if self.is_audio_recording:
            return "이미 음성 녹음 중입니다. 먼저 녹음을 중지해주세요."
        self.is_audio_recording = True
        self._audio_recording_label = label or ""
        if self.on_recording:
            self.on_recording("start", self._audio_recording_label, "audio")
        msg = "음성 녹음을 시작했습니다."
        if label:
            msg += f" (라벨: {label})"
        return msg

    def _t_stop_audio_recording(self) -> str:
        if not self.is_audio_recording:
            return "현재 음성 녹음 중이 아닙니다."
        self.is_audio_recording = False
        label = self._audio_recording_label
        self._audio_recording_label = ""
        if self.on_recording:
            self.on_recording("stop", label, "audio")
        return "음성 녹음을 중지했습니다. 파일을 저장하고 있습니다."

    # -------- 시스템 제어 도구 --------
    def _t_open_url(self, url: str) -> str:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if self.on_system_cmd:
            self.on_system_cmd({"type": "sys_open_url", "url": url})
        return f"브라우저에서 {url} 을 열었습니다."

    def _t_send_notification(self, title: str, body: str = "") -> str:
        if self.on_system_cmd:
            self.on_system_cmd({"type": "sys_notification", "title": title, "body": body})
        return f"알림을 보냈습니다: {title}"

    def _t_set_alarm(self, hour: int, minute: int = 0, label: str = "알람") -> str:
        hour = max(0, min(23, hour))
        minute = max(0, min(59, minute))
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target += timedelta(days=1)
        diff = (target - now).total_seconds()

        def trigger():
            time.sleep(diff)
            print(f"\n⏰ 알람: {label} ({hour:02d}:{minute:02d})")
            if self.on_timer:
                self.on_timer(f"🔔 {label}")
            if self.on_system_cmd:
                self.on_system_cmd({
                    "type": "sys_notification",
                    "title": f"⏰ 알람: {label}",
                    "body": f"{hour:02d}:{minute:02d} 알람이 울렸습니다.",
                })

        threading.Thread(target=trigger, daemon=True).start()
        return f"{hour:02d}:{minute:02d} 알람 '{label}' 설정됨 (약 {int(diff//60)}분 후)"

    def _t_set_volume(self, level: int) -> str:
        level = max(0, min(100, level))
        if self.on_system_cmd:
            self.on_system_cmd({"type": "sys_set_volume", "level": level})
        return f"음량을 {level}%로 설정했습니다."

    def _t_change_setting(self, setting: str, value: str) -> str:
        setting = setting.strip().lower()
        value = value.strip()
        if setting not in ("backend", "voice", "model"):
            return f"알 수 없는 설정: {setting}. backend, voice, model 중 선택해주세요."
        if self.on_system_cmd:
            self.on_system_cmd({"type": "sys_change_setting", "setting": setting, "value": value})
        labels = {"backend": "AI 백엔드", "voice": "음성", "model": "모델"}
        return f"{labels.get(setting, setting)}을(를) '{value}'(으)로 변경 요청했습니다."

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
