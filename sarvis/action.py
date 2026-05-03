"""행동인식 — MediaPipe Pose + YOLO 객체검출 하이브리드.

세 가지 신호를 단일 워커 스레드에서 처리:
  1) wake_gesture     — 손 들기 sustained → 호출어와 동일 효과
  2) fall_detected    — 어깨 급강하 + 수평 자세 sustained → 알림
  3) activity_changed — YOLO 객체 + Pose 자세 룰 → "요리/식사/공부/운동/..." 라벨

설계 원칙:
  - mediapipe / ultralytics 미설치 시 graceful fallback (조용히 비활성)
  - 카메라 read 충돌 방지: 메인 루프가 frame.submit() 으로 넘김 (워커가 스스로 read X)
  - ~10fps 다운샘플링 (60fps 메인 렌더 영향 최소화)
  - 모든 임계치/쿨다운은 cfg 로 노출
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional

from .config import cfg


# ============================================================
# Lazy import — 의존성 누락이 import 실패로 번지지 않도록
# ============================================================
_mp_pose = None         # type: ignore
_yolo_model = None      # type: ignore
_loaded = {"pose": False, "yolo": False}


def _load_pose():
    if _loaded["pose"]:
        return _mp_pose
    _loaded["pose"] = True
    try:
        import mediapipe as mp  # noqa: F401
        globals()["_mp_pose"] = mp.solutions.pose
        return globals()["_mp_pose"]
    except Exception as e:
        print(f"[Action] MediaPipe Pose 미설치 — 손들기/넘어짐 비활성: {e}")
        return None


def _load_yolo():
    if _loaded["yolo"]:
        return _yolo_model
    _loaded["yolo"] = True
    try:
        from ultralytics import YOLO
        model = YOLO(cfg.yolo_model)
        globals()["_yolo_model"] = model
        return model
    except Exception as e:
        print(f"[Action] YOLO 미사용 — 활동분류 비활성: {e}")
        return None


# ============================================================
# 이벤트 데이터
# ============================================================
@dataclass
class ActionEvent:
    kind: str           # "wake_gesture" | "fall_detected" | "activity_changed"
    payload: str
    confidence: float
    ts: float


# ============================================================
# Recognizer — 프레임 1장 → 이벤트 리스트
# ============================================================
class ActionRecognizer:
    # MediaPipe Pose landmark indices
    NOSE = 0
    L_SHOULDER, R_SHOULDER = 11, 12
    L_WRIST, R_WRIST = 15, 16
    L_HIP, R_HIP = 23, 24
    L_KNEE, R_KNEE = 25, 26

    def __init__(self, on_event: Callable[[ActionEvent], None]):
        self.on_event = on_event

        pose_mod = _load_pose() if cfg.gesture_wake_enabled or cfg.fall_detect_enabled else None
        if pose_mod is not None:
            self._pose_proc = pose_mod.Pose(
                static_image_mode=False,
                model_complexity=0,                # lite 모델, 실시간 우선
                enable_segmentation=False,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )
        else:
            self._pose_proc = None

        self._yolo = _load_yolo() if cfg.activity_recognize_enabled else None

        # 손들기 상태머신
        self._hand_up_frames = 0
        self._last_wake_ts = 0.0

        # 넘어짐 상태머신
        self._sh_y_history: Deque[float] = deque(maxlen=15)
        self._horizontal_frames = 0
        self._last_fall_ts = 0.0

        # 활동 상태
        self._last_activity_ts = 0.0
        self._current_activity = ""
        self._current_activity_detail = ""

    # -------- 상태 조회 --------
    def get_current_activity(self) -> str:
        return self._current_activity

    def get_current_activity_detail(self) -> str:
        return self._current_activity_detail

    # -------- 메인 진입점 --------
    def process(self, frame) -> List[ActionEvent]:
        """프레임 1장을 처리하고 이벤트들을 반환. 콜백도 함께 발화."""
        if frame is None:
            return []
        events: List[ActionEvent] = []
        landmarks = self._extract_pose(frame)

        if landmarks is not None:
            if cfg.gesture_wake_enabled:
                ev = self._check_wake_gesture(landmarks)
                if ev:
                    events.append(ev)
            if cfg.fall_detect_enabled:
                ev = self._check_fall(landmarks)
                if ev:
                    events.append(ev)

        if cfg.activity_recognize_enabled:
            now = time.time()
            if now - self._last_activity_ts >= cfg.activity_interval_s:
                self._last_activity_ts = now
                ev = self._classify_activity(frame, landmarks)
                if ev:
                    events.append(ev)

        for ev in events:
            try:
                self.on_event(ev)
            except Exception as e:
                print(f"[Action] 이벤트 콜백 오류 ({ev.kind}): {e}")
        return events

    # -------- Pose 추출 --------
    def _extract_pose(self, frame):
        if self._pose_proc is None:
            return None
        try:
            import cv2
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            results = self._pose_proc.process(rgb)
            if not results.pose_landmarks:
                return None
            return results.pose_landmarks.landmark
        except Exception as e:
            print(f"[Action] Pose 추출 실패: {e}")
            return None

    # -------- 손 들기 (호출어 대체) --------
    def _check_wake_gesture(self, landmarks) -> Optional[ActionEvent]:
        """손목이 코보다 위 + N프레임 sustained → wake_gesture."""
        now = time.time()
        if now - self._last_wake_ts < cfg.gesture_wake_cooldown_s:
            return None

        nose = landmarks[self.NOSE]
        if nose.visibility < 0.5:
            self._hand_up_frames = 0
            return None

        l_w = landmarks[self.L_WRIST]
        r_w = landmarks[self.R_WRIST]
        margin = 0.05  # 코보다 5% 더 위(정규좌표) 여야 인정
        hand_above = (
            (l_w.visibility > 0.5 and l_w.y < nose.y - margin)
            or (r_w.visibility > 0.5 and r_w.y < nose.y - margin)
        )

        if hand_above:
            self._hand_up_frames += 1
            if self._hand_up_frames >= cfg.gesture_wake_sustain_frames:
                self._hand_up_frames = 0
                self._last_wake_ts = now
                return ActionEvent(
                    kind="wake_gesture",
                    payload="hand_raised",
                    confidence=0.9,
                    ts=now,
                )
        else:
            self._hand_up_frames = 0
        return None

    # -------- 넘어짐 감지 --------
    def _check_fall(self, landmarks) -> Optional[ActionEvent]:
        """급강하 + 수평 자세 sustained → fall_detected."""
        now = time.time()
        if now - self._last_fall_ts < cfg.fall_cooldown_s:
            return None

        l_sh = landmarks[self.L_SHOULDER]
        r_sh = landmarks[self.R_SHOULDER]
        l_hp = landmarks[self.L_HIP]
        r_hp = landmarks[self.R_HIP]
        if min(l_sh.visibility, r_sh.visibility, l_hp.visibility, r_hp.visibility) < 0.4:
            return None

        sh_y = (l_sh.y + r_sh.y) / 2.0
        hip_y = (l_hp.y + r_hp.y) / 2.0
        sh_x = (l_sh.x + r_sh.x) / 2.0
        hip_x = (l_hp.x + r_hp.x) / 2.0

        self._sh_y_history.append(sh_y)

        # 수평 자세: 좌우 거리 > 상하 거리 × 1.2 (몸통이 옆으로 누움)
        dy = abs(sh_y - hip_y)
        dx = abs(sh_x - hip_x)
        is_horizontal = dx > dy * 1.2

        if is_horizontal:
            self._horizontal_frames += 1
        else:
            self._horizontal_frames = 0

        # 1초 내 어깨가 임계치만큼 떨어졌는지
        rapid_drop = False
        if len(self._sh_y_history) == self._sh_y_history.maxlen:
            window = list(self._sh_y_history)
            min_y_recent = min(window[: len(window) // 3])
            if window[-1] - min_y_recent > cfg.fall_velocity_threshold:
                rapid_drop = True

        if rapid_drop and self._horizontal_frames >= cfg.fall_horizontal_frames:
            self._last_fall_ts = now
            self._horizontal_frames = 0
            self._sh_y_history.clear()
            return ActionEvent(
                kind="fall_detected",
                payload="fall",
                confidence=0.7,
                ts=now,
            )
        return None

    # -------- 활동 분류 (YOLO + Pose 룰 + 선택적 VLM) --------
    def _classify_activity(self, frame, landmarks) -> Optional[ActionEvent]:
        if self._yolo is None and landmarks is None:
            return None

        objs: set = set()
        if self._yolo is not None:
            try:
                results = self._yolo.predict(frame, verbose=False, conf=0.4, imgsz=480)
                names = self._yolo.names
                for r in results or []:
                    if r.boxes is None:
                        continue
                    for cls in r.boxes.cls.tolist():
                        objs.add(names[int(cls)])
            except Exception as e:
                print(f"[Action] YOLO 추론 실패: {e}")

        pose_state = self._pose_state(landmarks) if landmarks is not None else "unknown"
        label = self._infer_activity(objs, pose_state)
        if not label:
            return None

        detail = self._format_detail(label, objs, pose_state)
        if label == self._current_activity and detail == self._current_activity_detail:
            return None  # 변화 없음 → 이벤트 미발화 (스팸 방지)

        self._current_activity = label
        self._current_activity_detail = detail
        return ActionEvent(
            kind="activity_changed",
            payload=label,
            confidence=0.6,
            ts=time.time(),
        )

    def _pose_state(self, landmarks) -> str:
        """서기 / 앉기 / 누워있기 간단 분류."""
        sh_y = (landmarks[self.L_SHOULDER].y + landmarks[self.R_SHOULDER].y) / 2.0
        hip_y = (landmarks[self.L_HIP].y + landmarks[self.R_HIP].y) / 2.0
        try:
            knee_y = (landmarks[self.L_KNEE].y + landmarks[self.R_KNEE].y) / 2.0
            knee_visible = (
                landmarks[self.L_KNEE].visibility > 0.4
                or landmarks[self.R_KNEE].visibility > 0.4
            )
        except Exception:
            knee_y = 1.0
            knee_visible = False

        if abs(sh_y - hip_y) < 0.05:
            return "lying"
        if knee_visible and hip_y - sh_y > 0.18 and knee_y - hip_y > 0.12:
            return "standing"
        if hip_y - sh_y > 0.10:
            return "sitting"
        return "unknown"

    @staticmethod
    def _infer_activity(objs: set, pose: str) -> str:
        """COCO 객체 + 자세 → 한국어 활동 라벨. 없으면 빈 문자열."""
        kitchen = {"knife", "bowl", "fork", "spoon", "oven", "microwave", "refrigerator", "wine glass"}
        eating = {"fork", "spoon", "bowl", "cup", "dining table", "pizza", "sandwich", "donut", "cake", "banana", "apple", "orange"}
        study  = {"book", "laptop", "keyboard", "mouse"}
        screen = {"tv", "cell phone"}
        sport  = {"sports ball", "skateboard", "tennis racket", "baseball bat", "frisbee", "skis", "snowboard"}
        guitar = {"guitar"}  # COCO 에는 없지만 향후 커스텀 모델 대비

        if objs & kitchen and pose in ("standing", "unknown"):
            return "요리"
        if objs & eating and pose == "sitting":
            return "식사"
        if objs & study and pose == "sitting":
            return "공부/업무"
        if objs & sport:
            return "운동"
        if objs & guitar:
            return "악기 연주"
        if objs & screen and pose == "sitting":
            return "화면 시청"
        if pose == "standing":
            return "서있음"
        if pose == "sitting":
            return "앉아있음"
        if pose == "lying":
            return "누워있음"
        return ""

    @staticmethod
    def _format_detail(label: str, objs: set, pose: str) -> str:
        items = sorted(o for o in objs if o not in {"person"})
        head = ", ".join(items[:5]) if items else "감지된 물체 없음"
        return f"{label} (자세={pose}, 주변={head})"


# ============================================================
# Worker thread — 메인 루프가 frame을 submit, 워커가 ~10fps 로 처리
# ============================================================
class ActionLoop:
    def __init__(self, recognizer: ActionRecognizer):
        self.recognizer = recognizer
        self._latest_frame = None
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return (
            self.recognizer._pose_proc is not None
            or self.recognizer._yolo is not None
        )

    def start(self):
        if not self.enabled:
            print("[ActionLoop] 의존성 없음 — 비활성")
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("[ActionLoop] 시작 — 행동인식 활성")

    def stop(self):
        self._running = False
        self._wakeup.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def submit(self, frame):
        """메인 루프에서 호출. 최신 프레임 1장만 보관 (drop policy)."""
        if not self._running:
            return
        with self._lock:
            self._latest_frame = frame
        self._wakeup.set()

    def _loop(self):
        period = 1.0 / max(1, cfg.action_target_fps)
        while self._running:
            self._wakeup.wait(timeout=0.5)
            self._wakeup.clear()
            if not self._running:
                break
            with self._lock:
                frame = self._latest_frame
                self._latest_frame = None
            if frame is None:
                continue
            t0 = time.time()
            try:
                self.recognizer.process(frame)
            except Exception as e:
                print(f"[ActionLoop] 처리 오류: {e}")
            elapsed = time.time() - t0
            if elapsed < period:
                time.sleep(period - elapsed)
