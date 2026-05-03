"""비전 시스템 — 카메라 캡처 + 얼굴 인식. 렌더링은 ui.py에서 처리.

VisionSystem  : 데스크톱(pygame) 모드 — 로컬 cv2 카메라
WebVision     : 웹 모드 — 브라우저가 보낸 프레임을 보관, 같은 인터페이스 제공
FaceRegistry  : 웹 등록용 — 사람 이름 ↔ 얼굴 JPEG 저장 (Claude Vision 식별)
"""
from __future__ import annotations

import base64
import math
import os
import pickle
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

cv2 = None  # type: ignore
np = None   # type: ignore
HAS_CV2: Optional[bool] = None  # None=미시도, True=사용가능, False=실패
_cv2_lock = threading.Lock()

def _ensure_cv2() -> bool:
    """cv2/numpy 를 호출 시점에 지연 로드 (배포 cold start 60초 제한 회피).

    cv2(opencv-python) 는 ~80MB 이고 import 만으로 5~15초 소요되어,
    모듈 로드 시점에 import 하면 uvicorn 이 포트를 열기 전에
    Replit 의 60초 헬스체크가 타임아웃됨.
    동시 호출 안전: double-check + Lock 으로 상태 전이 원자화.
    """
    global cv2, np, HAS_CV2
    if HAS_CV2 is not None:
        return bool(HAS_CV2)
    with _cv2_lock:
        if HAS_CV2 is not None:
            return bool(HAS_CV2)
        try:
            import cv2 as _cv2
            import numpy as _np
            cv2 = _cv2
            np = _np
            HAS_CV2 = True
        except Exception as e:
            print(f"[vision] cv2 로드 실패: {e}")
            HAS_CV2 = False
    return bool(HAS_CV2)


def _bg_preload_cv2():
    """백그라운드에서 미리 cv2 로드 (첫 사용자 액션이 도착하기 전 준비)."""
    _ensure_cv2()


# 환경변수 SARVIS_SKIP_CV2_PRELOAD=1 이면 스레드를 띄우지 않는다.
# 테스트/CI 환경에서 `import vision` 만으로 무거운 cv2 가 로드되는 것을 방지.
# (qa-engineer 사이클 #6 P1)
if os.environ.get("SARVIS_SKIP_CV2_PRELOAD", "").strip() not in ("1", "true", "True"):
    threading.Thread(target=_bg_preload_cv2, daemon=True, name="cv2-loader").start()

from .config import cfg

# face_recognition 은 임포트 시간이 매우 길어 서버 시작을 차단할 수 있으므로
# 실제 사용 시점에 지연 로드(lazy import)한다.
HAS_FACE_REC: Optional[bool] = None  # None=미확인, True=사용가능, False=미설치
_face_recognition_mod = None

def _get_face_recognition():
    """face_recognition 모듈을 처음 호출 시에만 import."""
    global HAS_FACE_REC, _face_recognition_mod
    if HAS_FACE_REC is None:
        try:
            import face_recognition as _fr
            _face_recognition_mod = _fr
            HAS_FACE_REC = True
        except ImportError:
            HAS_FACE_REC = False
            print("[Vision] face_recognition 미설치. pip install face_recognition")
    return _face_recognition_mod

# cv2 상태 로그는 백그라운드 로더에서 실패 시에만 출력 (_ensure_cv2 내부)


def is_face_landmarks_supported() -> bool:
    """face_recognition.face_landmarks 사용 가능 여부 — 라이브니스 capability probe.

    사이클 #20 — 세션 시작 시 1회 호출해 `blink_required` 를 결정. EAR 추출이
    한두 번 실패한다고 영구 우회되는 위험을 차단하기 위함. face_recognition
    모듈이 import 되고 cv2 가 로드돼 있으면 True.
    """
    return _get_face_recognition() is not None and _ensure_cv2()


def compute_eye_aspect_ratio_from_jpeg(jpeg_bytes: bytes) -> Optional[float]:
    """JPEG 에서 가장 큰 얼굴의 양 눈 EAR(평균) 반환.

    사이클 #20 — 라이브니스(눈 깜빡임) 검출용. EAR(Eye Aspect Ratio) 는
    Soukupová & Čech (2016) 의 정의를 따른다:
        EAR = (||p2-p6|| + ||p3-p5||) / (2 * ||p1-p4||)
    눈을 뜨면 ~0.30, 감으면 ~0.10 수준. 시계열로 모아 깜빡임 패턴(open→close→open)
    을 검출 — `owner_auth.detect_blink_in_window` 참고.

    face_recognition.face_landmarks(model="large") 가 반환하는 left_eye/right_eye
    각각 6점을 사용. 미설치/검출 실패 시 None 반환 → 호출자가 폴백 처리.
    """
    fr = _get_face_recognition()
    if fr is None or not _ensure_cv2():
        return None
    try:
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return None
        small = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        landmarks_list = fr.face_landmarks(rgb, model="large")
        if not landmarks_list:
            return None

        def _eye_span(lm: Dict) -> float:
            pts = (lm.get("left_eye") or []) + (lm.get("right_eye") or [])
            if not pts:
                return 0.0
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return (max(xs) - min(xs)) * (max(ys) - min(ys))

        lm = max(landmarks_list, key=_eye_span)

        def _ear(eye_pts: List[Tuple[int, int]]) -> float:
            if len(eye_pts) < 6:
                return 0.0
            p1, p2, p3, p4, p5, p6 = eye_pts[:6]
            def _d(a, b):
                return math.hypot(a[0] - b[0], a[1] - b[1])
            num = _d(p2, p6) + _d(p3, p5)
            den = 2.0 * _d(p1, p4)
            return num / den if den > 0 else 0.0

        ears: List[float] = []
        le = lm.get("left_eye") or []
        re_ = lm.get("right_eye") or []
        if len(le) >= 6:
            ears.append(_ear(le))
        if len(re_) >= 6:
            ears.append(_ear(re_))
        if not ears:
            return None
        return float(sum(ears) / len(ears))
    except Exception:
        return None


def compute_face_encoding_from_jpeg(jpeg_bytes: bytes) -> Optional[List[float]]:
    """JPEG 바이트에서 가장 큰 얼굴의 128차원 인코딩 추출.

    사이클 #18 — 주인 인증(OwnerAuth) 등록/로그인용. face_recognition
    또는 cv2 가 없으면 None — 호출자(server)는 폴백 모드로 전환.
    실패 시 None (예외는 흡수). 비교적 무거우므로 `asyncio.to_thread` 권장.
    """
    fr = _get_face_recognition()
    if fr is None or not _ensure_cv2():
        return None
    try:
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return None
        # 0.5 배 다운샘플 — face_recognition 의 hog 모델이 ~3x 빠름.
        small = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        locs = fr.face_locations(rgb, model="hog")
        if not locs:
            return None
        encs = fr.face_encodings(rgb, locs)
        if not encs:
            return None
        # 가장 큰 얼굴 (등록자 본인일 확률 ↑) 선택.
        def _area(loc):
            t, r, b, l = loc
            return max(0, r - l) * max(0, b - t)
        idx = max(range(len(locs)), key=lambda i: _area(locs[i]))
        return [float(x) for x in encs[idx]]
    except Exception:
        return None


# ============================================================
# 얼굴 메모리 — 등록된 사람들의 인코딩 저장/조회
# ============================================================
class FaceMemory:
    def __init__(self):
        self.dir = Path(cfg.faces_dir)
        # 사이클 #9 정비: data/faces 같은 다단계 경로 지원.
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "faces.pkl"
        self.encodings: List = []
        self.names: List[str] = []
        self._load()

    def _load(self):
        if self.path.exists():
            with open(self.path, "rb") as f:
                data = pickle.load(f)
            self.encodings = data.get("encodings", [])
            self.names = data.get("names", [])
            registered = ", ".join(self.names) if self.names else "없음"
            print(f"      등록된 얼굴 {len(self.names)}명: {registered}")

    def save(self):
        with open(self.path, "wb") as f:
            pickle.dump({"encodings": self.encodings, "names": self.names}, f)

    def add(self, name: str, encoding):
        self.encodings.append(encoding)
        self.names.append(name)
        self.save()

    def identify(self, face_encoding) -> Optional[str]:
        fr = _get_face_recognition()
        if not self.encodings or not _ensure_cv2() or fr is None:
            return None
        distances = fr.face_distance(self.encodings, face_encoding)
        best_idx = int(np.argmin(distances))
        if distances[best_idx] < cfg.face_match_tolerance:
            return self.names[best_idx]
        return None


# ============================================================
# 비전 시스템 — 카메라 캡처 + 주기적 얼굴 인식
# ============================================================
class VisionSystem:
    def __init__(self):
        if not _ensure_cv2():
            raise RuntimeError("cv2 미설치. 카메라 사용 불가.")
        self.cap = cv2.VideoCapture(cfg.camera_index)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.camera_width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.camera_height)
        if not self.cap.isOpened():
            raise RuntimeError(f"카메라를 열 수 없습니다 (index={cfg.camera_index})")

        self.face_memory = FaceMemory() if _get_face_recognition() else None
        self.current_user: Optional[str] = None
        self.face_boxes: List[Tuple[int, int, int, int]] = []
        self._last_face_check = 0.0

    def read(self):
        if not _ensure_cv2():
            return None
        ok, frame = self.cap.read()
        if not ok:
            return None
        return cv2.flip(frame, 1)

    def update_face_recognition(self, frame):
        fr = _get_face_recognition()
        if not fr or not _ensure_cv2():
            return
        now = time.time()
        if now - self._last_face_check < cfg.face_check_interval:
            return
        self._last_face_check = now

        small = cv2.resize(frame, (0, 0), fx=0.25, fy=0.25)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)

        locations = fr.face_locations(rgb, model="hog")
        if not locations:
            self.face_boxes = []
            self.current_user = None
            return

        encodings = fr.face_encodings(rgb, locations)
        if not encodings:
            return

        self.current_user = self.face_memory.identify(encodings[0])
        self.face_boxes = [
            (t * 4, r * 4, b * 4, l * 4) for (t, r, b, l) in locations
        ]

    def release(self):
        if _ensure_cv2() and self.cap:
            self.cap.release()


# ============================================================
# WebVision — 브라우저에서 보내준 프레임을 보관하는 어댑터
# tools.py 의 _t_see / _t_observe_action 은 vision.read() 만 호출하므로
# VisionSystem 과 동일 인터페이스를 제공한다.
# ============================================================
class WebVision:
    """프론트엔드가 보낸 최신 프레임을 메모리에 보관."""

    # 클래스 수준 Haar cascade 공유 (한 번만 로딩)
    _cascade = None

    @classmethod
    def _get_cascade(cls):
        if not _ensure_cv2():
            return None
        if cls._cascade is None:
            path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            cls._cascade = cv2.CascadeClassifier(path)
        return cls._cascade

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._frame_ts: float = 0.0
        self._frame_w: int = 0
        self._frame_h: int = 0
        self.current_user: Optional[str] = None
        self.face_boxes: List[Tuple[int, int, int, int]] = []
        self._last_face_check: float = 0.0

    def push_jpeg(self, data: bytes) -> bool:
        """브라우저가 WebSocket으로 보낸 JPEG 바이트를 디코드해 저장."""
        if not _ensure_cv2():
            return False
        arr = np.frombuffer(data, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return False
        h, w = frame.shape[:2]
        with self._lock:
            self._frame = frame
            self._frame_ts = time.time()
            self._frame_w = w
            self._frame_h = h

        now = time.time()
        if now - self._last_face_check >= 1.0:
            self._last_face_check = now
            self._detect_faces(frame, w, h)
            return True
        return False

    def _detect_faces(self, frame, fw: int, fh: int):
        """OpenCV Haar cascade 얼굴 감지."""
        if not _ensure_cv2():
            return
        try:
            cascade = self._get_cascade()
            if cascade is None:
                return
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            detections = cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30)
            )
            if len(detections) == 0:
                self.face_boxes = []
            else:
                boxes = []
                for (x, y, w, h) in detections:
                    boxes.append((int(y), int(x + w), int(y + h), int(x)))
                self.face_boxes = boxes
        except Exception:
            self.face_boxes = []

    def get_frame_size(self) -> Tuple[int, int]:
        with self._lock:
            return self._frame_w, self._frame_h

    def read(self):
        """가장 최근 프레임 반환. 너무 오래된 건 무효 처리."""
        with self._lock:
            if self._frame is None:
                return None
            if time.time() - self._frame_ts > 5.0:
                return None
            return self._frame.copy()

    def release(self):
        with self._lock:
            self._frame = None
        self.face_boxes = []

    def update_face_recognition(self, frame):
        """no-op — push_jpeg 내부에서 감지 처리."""
        return

    def crop_largest_face_jpeg(
        self,
        padding: float = 0.25,
        quality: int = 85,
        require_face: bool = False,
    ) -> Optional[bytes]:
        """가장 큰 감지된 얼굴을 잘라 JPEG 바이트로 반환.

        require_face=True 면 얼굴이 명확히 감지될 때만 반환 (등록용).
        require_face=False 면 얼굴이 없을 때 전체 프레임 반환 (식별용 폴백).
        """
        if not _ensure_cv2():
            return None
        with self._lock:
            frame = None if self._frame is None else self._frame.copy()
        if frame is None:
            return None

        h, w = frame.shape[:2]

        # 신선한 감지 — 캐시된 face_boxes 가 오래됐을 수 있으므로 즉시 재감지
        boxes: List[Tuple[int, int, int, int]] = []
        cascade = self._get_cascade()
        if cascade is not None:
            try:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                gray = cv2.equalizeHist(gray)
                detections = cascade.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
                )
                for (x, y, bw, bh) in detections:
                    boxes.append((int(y), int(x + bw), int(y + bh), int(x)))
            except Exception:
                boxes = []

        if not boxes:
            if require_face:
                return None
            crop = frame
        else:
            def area(b):
                t, r, bo, l = b
                return max(0, r - l) * max(0, bo - t)
            t, r, bo, l = max(boxes, key=area)
            bw = r - l
            bh = bo - t
            pad_x = int(bw * padding)
            pad_y = int(bh * padding)
            l2 = max(0, l - pad_x)
            t2 = max(0, t - pad_y)
            r2 = min(w, r + pad_x)
            b2 = min(h, bo + pad_y)
            crop = frame[t2:b2, l2:r2] if (r2 > l2 and b2 > t2) else frame

        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return None
        return buf.tobytes()


# ============================================================
# FaceRegistry — 웹 등록 얼굴 사진 저장 (Claude Vision 식별용)
# ============================================================
_NAME_SAFE = re.compile(r"[^0-9A-Za-z\uAC00-\uD7A3_\-]")


def _safe_filename(name: str) -> str:
    """파일명 안전 문자열로 변환 (한글/영숫자/_/- 만 허용)."""
    s = _NAME_SAFE.sub("_", name.strip())
    return s[:40] or "unknown"


class FaceRegistry:
    """등록된 사람 ↔ 얼굴 JPEG 저장소.

    저장: data/faces/{safe_name}.jpg + data/faces/_index.json (원본 표시 이름 매핑)
    """

    def __init__(self, faces_dir: str = "data/faces"):
        self.dir = Path(faces_dir)
        # 사이클 #9 정비: data/faces 같은 다단계 경로 지원.
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "_index.json"
        self._lock = threading.Lock()
        self._index: Dict[str, str] = {}  # safe_name -> display_name
        self._load_index()

    def _load_index(self):
        import json
        if self.index_path.exists():
            try:
                self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:
                self._index = {}

    def _save_index(self):
        import json
        self.index_path.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def register(self, display_name: str, jpeg_bytes: bytes) -> str:
        """이름으로 얼굴 사진 저장. 같은 이름이면 덮어씀."""
        if not display_name or not jpeg_bytes:
            raise ValueError("이름과 사진이 필요합니다.")
        safe = _safe_filename(display_name)
        with self._lock:
            (self.dir / f"{safe}.jpg").write_bytes(jpeg_bytes)
            self._index[safe] = display_name.strip()
            self._save_index()
        return display_name.strip()

    def delete(self, display_name: str) -> bool:
        safe = _safe_filename(display_name)
        with self._lock:
            path = self.dir / f"{safe}.jpg"
            existed = path.exists()
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            if safe in self._index:
                del self._index[safe]
                self._save_index()
        return existed

    def list_people(self) -> List[str]:
        with self._lock:
            return [
                self._index.get(p.stem, p.stem)
                for p in sorted(self.dir.glob("*.jpg"))
            ]

    def get_references(self) -> List[Tuple[str, str]]:
        """[(display_name, base64_jpeg), ...] — Claude Vision 호출용."""
        out: List[Tuple[str, str]] = []
        with self._lock:
            for p in sorted(self.dir.glob("*.jpg")):
                try:
                    b64 = base64.standard_b64encode(p.read_bytes()).decode("utf-8")
                    out.append((self._index.get(p.stem, p.stem), b64))
                except Exception:
                    continue
        return out

    def is_empty(self) -> bool:
        with self._lock:
            return not any(self.dir.glob("*.jpg"))
