"""사비스 도구 시스템 — LLM이 호출하는 전문 모델/기능들

Microsoft SARVIS의 4단계 패턴을 Claude tool_use로 구현:
  Task Planning → Model Selection → Task Execution → Response Generation
"""
import base64
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# cv2 는 vision 모듈의 lazy 로더를 재사용 (배포 cold start 60초 제한 회피).
# 모듈 import 시 cv2 를 즉시 로드하면 uvicorn 이 포트 열기 전에 헬스체크 실패.
from vision import _ensure_cv2

def _get_cv2():
    """cv2 모듈 객체를 lazy 로 반환 (없으면 None)."""
    if _ensure_cv2():
        import cv2 as _cv2
        return _cv2
    return None

from config import cfg


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
            "Search the web for current information. Use when the user asks about "
            "recent news, current facts, or anything beyond your knowledge."
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

        self.memory_path = Path("memory.json")
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

    def _t_web_search(self, query: str) -> str:
        try:
            from duckduckgo_search import DDGS
            results = list(DDGS().text(query, max_results=4, region="kr-kr"))
        except Exception as e:
            return f"검색 실패: {e}"

        if not results:
            return f"'{query}' 검색 결과 없음"
        lines = []
        for r in results[:4]:
            title = r.get("title", "").strip()
            body = r.get("body", "").strip()
            if title and body:
                lines.append(f"- {title}: {body}")
        return "\n".join(lines) if lines else "검색 결과 없음"

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
