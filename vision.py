"""비전 시스템 — 카메라 캡처 + 얼굴 인식. 렌더링은 ui.py에서 처리.

VisionSystem  : 데스크톱(pygame) 모드 — 로컬 cv2 카메라
WebVision     : 웹 모드 — 브라우저가 보낸 프레임을 보관, 같은 인터페이스 제공
"""
import pickle
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from config import cfg

try:
    import face_recognition
    HAS_FACE_REC = True
except ImportError:
    HAS_FACE_REC = False
    print("[Vision] face_recognition 미설치. pip install face_recognition")


# ============================================================
# 얼굴 메모리 — 등록된 사람들의 인코딩 저장/조회
# ============================================================
class FaceMemory:
    def __init__(self):
        self.dir = Path(cfg.faces_dir)
        self.dir.mkdir(exist_ok=True)
        self.path = self.dir / "faces.pkl"
        self.encodings: List[np.ndarray] = []
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

    def add(self, name: str, encoding: np.ndarray):
        self.encodings.append(encoding)
        self.names.append(name)
        self.save()

    def identify(self, face_encoding: np.ndarray) -> Optional[str]:
        if not self.encodings:
            return None
        distances = face_recognition.face_distance(self.encodings, face_encoding)
        best_idx = int(np.argmin(distances))
        if distances[best_idx] < cfg.face_match_tolerance:
            return self.names[best_idx]
        return None


# ============================================================
# 비전 시스템 — 카메라 캡처 + 주기적 얼굴 인식
# ============================================================
class VisionSystem:
    def __init__(self):
        self.cap = cv2.VideoCapture(cfg.camera_index)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.camera_width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.camera_height)
        if not self.cap.isOpened():
            raise RuntimeError(f"카메라를 열 수 없습니다 (index={cfg.camera_index})")

        self.face_memory = FaceMemory() if HAS_FACE_REC else None
        self.current_user: Optional[str] = None
        self.face_boxes: List[Tuple[int, int, int, int]] = []  # (top, right, bottom, left)
        self._last_face_check = 0.0

    def read(self) -> Optional[np.ndarray]:
        ok, frame = self.cap.read()
        if not ok:
            return None
        # 좌우 반전 (거울 효과)
        return cv2.flip(frame, 1)

    def update_face_recognition(self, frame: np.ndarray):
        """주기적으로만 얼굴 인식 실행 (매 프레임 X — CPU 절약)"""
        if not HAS_FACE_REC:
            return
        now = time.time()
        if now - self._last_face_check < cfg.face_check_interval:
            return
        self._last_face_check = now

        # 1/4 크기로 처리해서 속도 ↑
        small = cv2.resize(frame, (0, 0), fx=0.25, fy=0.25)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)

        locations = face_recognition.face_locations(rgb, model="hog")
        if not locations:
            self.face_boxes = []
            self.current_user = None
            return

        encodings = face_recognition.face_encodings(rgb, locations)
        if not encodings:
            return

        # 첫 번째 얼굴 식별
        self.current_user = self.face_memory.identify(encodings[0])
        # 좌표를 원래 크기로 복원
        self.face_boxes = [
            (t * 4, r * 4, b * 4, l * 4) for (t, r, b, l) in locations
        ]

    def release(self):
        self.cap.release()


# ============================================================
# WebVision — 브라우저에서 보내준 프레임을 보관하는 어댑터
# tools.py 의 _t_see / _t_observe_action 은 vision.read() 만 호출하므로
# VisionSystem 과 동일 인터페이스를 제공한다.
# ============================================================
class WebVision:
    """프론트엔드가 보낸 최신 프레임을 메모리에 보관."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._frame_ts: float = 0.0
        # 데스크톱 비전과 동일 필드 (UI/도구가 참조)
        self.current_user: Optional[str] = None
        self.face_boxes: List[Tuple[int, int, int, int]] = []

    def push_jpeg(self, data: bytes):
        """브라우저가 WebSocket으로 보낸 JPEG 바이트를 디코드해 저장."""
        arr = np.frombuffer(data, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return
        with self._lock:
            self._frame = frame
            self._frame_ts = time.time()

    def read(self) -> Optional[np.ndarray]:
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

    def update_face_recognition(self, frame: np.ndarray):
        """웹 모드에선 얼굴 인식을 사용하지 않음 (no-op)."""
        return
