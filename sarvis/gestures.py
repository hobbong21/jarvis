"""제스처/포즈 인식 — 사이클 #28 (시나리오 A: 손 제스처 명령 + 손 들어 호출).

MediaPipe Hands + Pose 를 룰 기반으로 분류해 사용자의 손 모양과 자세를 감지.
WebVision 이 push_jpeg 마다 GestureDetector.push_frame 을 호출하면, 안정화 윈도우
(0.5s) + 쿨다운(2s) 을 거쳐 GestureEvent 를 콜백으로 발사한다.

지원 제스처:
  - "raised_hand" : 한쪽 손목이 같은 쪽 어깨보다 위 + 0.8s 유지 (호출용)
  - "thumbs_up"   : 엄지만 펴짐 (확인)
  - "open_palm"   : 5손가락 모두 펴짐 (정지/barge-in)
  - "fist"        : 모두 접힘 (취소)
  - "peace"       : 검지+중지만 (보조)

설계 원칙:
  - 무거운 import (mediapipe) 는 첫 push_frame 에서만 수행 → cold start 차단 안 함.
  - 모든 예외는 내부에서 삼키고 print — vision push_jpeg 흐름을 절대 막지 않는다.
  - cv2 는 vision.py 가 이미 로드한 인스턴스를 그대로 사용 (지연 import).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

mp = None  # type: ignore
HAS_MP: Optional[bool] = None
_mp_lock = threading.Lock()


def _ensure_mp() -> bool:
    """mediapipe 를 호출 시점에 지연 로드. cold start 보호."""
    global mp, HAS_MP
    if HAS_MP is not None:
        return bool(HAS_MP)
    with _mp_lock:
        if HAS_MP is not None:
            return bool(HAS_MP)
        try:
            import mediapipe as _mp
            mp = _mp
            HAS_MP = True
            print("[gestures] mediapipe 로드 OK")
        except Exception as e:
            print(f"[gestures] mediapipe 로드 실패: {e}")
            HAS_MP = False
    return bool(HAS_MP)


@dataclass
class GestureEvent:
    name: str           # "raised_hand" | "thumbs_up" | "open_palm" | "fist" | "peace"
    confidence: float
    ts: float


# ---- 손 모양 룰 분류 ----------------------------------------------------
# MediaPipe Hands 21 landmarks. tip / pip 인덱스.
_FINGER_TIPS: Tuple[int, ...] = (4, 8, 12, 16, 20)  # thumb, index, middle, ring, pinky
_FINGER_PIPS: Tuple[int, ...] = (3, 6, 10, 14, 18)


def _classify_hand(landmarks) -> Optional[str]:
    """21개 hand landmark 를 받아 손 모양 라벨 또는 None.

    - 엄지(tip=4)는 손바닥 방향이 화면에 따라 다르므로 손목(0) 기준 x 거리 비교.
    - 나머지 4개 손가락은 세로 손 가정에서 tip.y < pip.y 이면 펴짐.
    """
    try:
        extended: List[bool] = []
        for tip, pip in zip(_FINGER_TIPS, _FINGER_PIPS):
            if tip == 4:
                d_tip = abs(landmarks[tip].x - landmarks[0].x)
                d_pip = abs(landmarks[pip].x - landmarks[0].x)
                extended.append(d_tip > d_pip)
            else:
                extended.append(landmarks[tip].y < landmarks[pip].y)
        n = sum(extended)
        # 정확한 패턴 매칭 우선
        if extended == [True, False, False, False, False]:
            return "thumbs_up"
        if extended == [False, True, True, False, False]:
            return "peace"
        if n == 0:
            return "fist"
        if n >= 4:
            return "open_palm"
    except Exception:
        return None
    return None


class GestureDetector:
    """MediaPipe Hands + Pose 통합 + debounce/쿨다운.

    호출자(WebVision):
        det = GestureDetector(on_event=lambda ev: ...)
        det.push_frame(bgr_frame)   # 매 카메라 프레임마다 (또는 throttle)
        det.close()                  # 세션 종료 시
    """

    def __init__(
        self,
        on_event: Callable[[GestureEvent], None],
        cooldown_s: float = 2.0,
        stable_window_s: float = 0.5,
        raised_dwell_s: float = 0.8,
        min_throttle_s: float = 0.12,  # 최대 ~8Hz 처리
    ):
        self._on_event = on_event
        self._hands = None
        self._pose = None
        self._init_lock = threading.Lock()
        self._last_emit_ts: dict = {}                # name -> ts
        self._stable_buf: List[Tuple[float, Optional[str]]] = []
        self._raised_since: float = 0.0
        self._last_processed: float = 0.0
        self._cooldown = float(cooldown_s)
        self._stable_window = float(stable_window_s)
        self._raised_dwell = float(raised_dwell_s)
        self._min_throttle = float(min_throttle_s)

        # ── 사이클 #28 — 추론 워커 스레드 ───────────────────────
        # MediaPipe Hands/Pose process() 는 CPU 무거움(특히 첫 호출 모델 로드).
        # WebSocket 이벤트 루프 차단 방지 위해 별도 daemon 스레드에서 실행.
        # push_frame() 은 "최신 프레임 1장" 만 보관(이전 프레임 drop) → backlog X.
        self._latest_frame = None        # numpy BGR (BGR 그대로, 워커가 RGB 변환)
        self._frame_lock = threading.Lock()
        self._frame_event = threading.Event()
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._run_worker, name="gesture-worker", daemon=True
        )
        self._worker.start()

    def _ensure_models(self) -> bool:
        if self._hands is not None:
            return True
        if not _ensure_mp():
            return False
        with self._init_lock:
            if self._hands is None:
                try:
                    self._hands = mp.solutions.hands.Hands(
                        static_image_mode=False,
                        max_num_hands=1,
                        min_detection_confidence=0.6,
                        min_tracking_confidence=0.5,
                    )
                    self._pose = mp.solutions.pose.Pose(
                        static_image_mode=False,
                        model_complexity=0,
                        min_detection_confidence=0.5,
                        min_tracking_confidence=0.5,
                    )
                    print("[gestures] MediaPipe Hands+Pose 초기화 OK")
                except Exception as e:
                    print(f"[gestures] 모델 초기화 실패: {e}")
                    self._hands = None
                    self._pose = None
                    return False
        return self._hands is not None

    def _maybe_emit(self, name: str, conf: float) -> None:
        now = time.time()
        last = self._last_emit_ts.get(name, 0.0)
        if now - last < self._cooldown:
            return
        self._last_emit_ts[name] = now
        try:
            self._on_event(GestureEvent(name=name, confidence=conf, ts=now))
        except Exception as e:
            print(f"[gestures] callback 예외 (무시): {e}")

    def push_frame(self, frame_bgr) -> None:
        """이벤트 루프에서 호출되는 비차단 진입점.

        최신 프레임만 보관(이전 frame 은 drop)하고 워커를 깨운다. 어떤 예외도
        호출자에게 전파하지 않음.
        """
        if frame_bgr is None:
            return
        try:
            with self._frame_lock:
                self._latest_frame = frame_bgr
            self._frame_event.set()
        except Exception:
            pass

    def _run_worker(self) -> None:
        """별도 스레드에서 MediaPipe 추론 루프. 최신 프레임만 처리, 이전은 drop."""
        while not self._stop.is_set():
            # 새 프레임 도착 대기 (최대 200ms 후 wake — stop flag 체크용)
            self._frame_event.wait(timeout=0.2)
            if self._stop.is_set():
                break
            self._frame_event.clear()
            with self._frame_lock:
                frame = self._latest_frame
                self._latest_frame = None
            if frame is None:
                continue
            now = time.time()
            if now - self._last_processed < self._min_throttle:
                continue
            self._last_processed = now
            try:
                self._process_frame(frame)
            except Exception as e:
                print(f"[gestures] worker 예외 (계속): {type(e).__name__}: {e}")

    def _process_frame(self, frame_bgr) -> None:
        """워커 스레드 내부에서 호출되는 실제 추론 + 분류 로직."""
        if not self._ensure_models():
            return
        try:
            import cv2
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            now = time.time()

            # ── Hands ──────────────────────────────────────
            hand_name: Optional[str] = None
            try:
                hres = self._hands.process(rgb)
                if hres and hres.multi_hand_landmarks:
                    lm = hres.multi_hand_landmarks[0].landmark
                    hand_name = _classify_hand(lm)
            except Exception:
                pass

            # 안정화 — stable_window 동안 동일 라벨이 60% 이상이면 emit
            self._stable_buf = [
                (t, n) for (t, n) in self._stable_buf if now - t < self._stable_window
            ]
            self._stable_buf.append((now, hand_name))
            if hand_name:
                hits = sum(1 for (_, n) in self._stable_buf if n == hand_name)
                if len(self._stable_buf) >= 2 and hits >= max(2, int(len(self._stable_buf) * 0.6)):
                    self._maybe_emit(hand_name, 0.8)

            # ── Pose: 손 들기 ──────────────────────────────
            try:
                pres = self._pose.process(rgb)
            except Exception:
                pres = None
            raised = False
            if pres and pres.pose_landmarks:
                p = pres.pose_landmarks.landmark
                # 11=L_shoulder, 12=R_shoulder, 15=L_wrist, 16=R_wrist
                # MediaPipe y: 화면 위=0, 아래=1. wrist.y < shoulder.y 면 손이 어깨 위.
                try:
                    l_ok = (p[15].visibility > 0.5 and p[11].visibility > 0.5
                            and p[15].y < p[11].y - 0.05)
                    r_ok = (p[16].visibility > 0.5 and p[12].visibility > 0.5
                            and p[16].y < p[12].y - 0.05)
                    raised = bool(l_ok or r_ok)
                except Exception:
                    raised = False

            if raised:
                if self._raised_since == 0.0:
                    self._raised_since = now
                elif now - self._raised_since >= self._raised_dwell:
                    self._maybe_emit("raised_hand", 0.9)
                    # 한 번 발사 후 dwell 리셋 — 같은 동작이 cooldown 내에 재발사되지 않도록
                    self._raised_since = 0.0
            else:
                self._raised_since = 0.0
        except Exception as e:
            # 절대 raise 하지 않음 — vision push_jpeg 흐름 보호
            print(f"[gestures] push_frame 예외 (무시): {type(e).__name__}: {e}")

    def close(self) -> None:
        # 워커 스레드 정리
        try:
            self._stop.set()
            self._frame_event.set()
            if self._worker.is_alive():
                self._worker.join(timeout=1.0)
        except Exception:
            pass
        try:
            if self._hands is not None:
                self._hands.close()
            if self._pose is not None:
                self._pose.close()
        except Exception:
            pass
        self._hands = None
        self._pose = None
