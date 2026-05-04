"""주인(owner) 인증 — 얼굴 + 음성 패스프레이즈 + 라이브니스 + 챌린지.

사이클 #18: 얼굴(1각도, 128차원 인코딩) + 음성 패스프레이즈(STT 유사도) 2단계 인증.
사이클 #20 (F-01 보강): 다음 항목 추가
  * **5각도 얼굴 캡처** (정면/좌/우/위/아래) — 등록 시 5개 인코딩을 모두 저장,
    검증 시 min(distance) ≤ threshold 로 통과. 머리 각도가 약간 달라도 인식
    되며 단일 사진(인쇄물) 위조에 강건.
  * **눈 깜빡임 라이브니스** — 본 모듈은 stateless 검증자만 담당 (`verify_blink`),
    실제 EAR 시계열 누적/판정 로직은 server 의 auth_state 에서 관리.
  * **챌린지 문장 풀** — 로그인 시 무작위 한국어 문장 1개를 발급, 사용자가
    패스프레이즈 또는 챌린지 둘 중 하나를 말하면 음성 인증 통과 (녹음 재생
    공격 차단). 챌린지는 1회용 — 검증 성공/실패 후 폐기.

설계 원칙 (변경 없음):
1. 등록(enroll) 1회 → `data/owner.json` 저장.
2. 등록 후 매 연결 인증 강제. 미등록이면 게이트 비활성 (첫 부팅 사용자 보호).
3. 얼굴 인코딩이 없는 환경(face_recognition 미설치)에서는 박스 감지 폴백.
4. `OwnerAuth` 인스턴스는 stateless — 라이브니스/챌린지 발급 상태는 호출자(서버 세션)가 보관.

JSON 파일 포맷 (호환):
    구버전: {"face_encoding": [128 floats], ...}
    신버전: {"face_encodings": [[128 floats] x N], ...}
    두 형식 모두 verify_face_encoding 이 자동 처리.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# 음성 패스프레이즈 / 챌린지 매칭 임계값.
VOICE_MATCH_THRESHOLD = 0.78

# 얼굴 거리 임계값 (face_recognition.face_distance 척도).
FACE_DISTANCE_THRESHOLD = 0.55

# 등록 시 권장 캡처 각도 — 클라이언트 UI 안내용. (서버는 5개 인코딩만 받음.)
ENROLL_FACE_ANGLES: Tuple[str, ...] = ("front", "left", "right", "up", "down")
ENROLL_FACE_LABELS_KO: Dict[str, str] = {
    "front": "정면",
    "left": "왼쪽",
    "right": "오른쪽",
    "up": "위",
    "down": "아래",
}
# 최소/권장 캡처 개수.
MIN_ENROLL_ENCODINGS = 1   # 1장만 있어도 동작 (구버전 호환)
RECOMMENDED_ENROLL_ENCODINGS = 5

# 챌린지 문장 풀 — 로그인마다 무작위 1개 발급. 길이/발음을 다양화하여
# 사전 녹음 공격을 어렵게 한다. 한국어 자연스러움 + 외우기 쉬움 우선.
VOICE_CHALLENGE_POOL: Tuple[str, ...] = (
    "오늘 하늘이 참 맑네요",
    "사비스 잘 지냈어요",
    "지금 시간이 어떻게 되나요",
    "내일 일정 알려 주세요",
    "오랜만에 인사드려요",
    "음성 인증 진행할게요",
    "조용한 곳에서 말하고 있어요",
    "주인 확인 부탁드립니다",
    "사비스 오늘도 부탁해요",
    "이 문장을 그대로 따라 말합니다",
)

# 라이브니스(눈 깜빡임) 판정 파라미터. 서버 auth_state 에서 사용.
EAR_OPEN_THRESHOLD = 0.24      # 이 값 이상이면 눈 뜬 상태로 본다.
EAR_CLOSE_THRESHOLD = 0.18     # 이 값 이하면 눈 감음.
BLINK_WINDOW_SECONDS = 6.0     # 깜빡임 검출 윈도우 (초).
BLINK_MIN_FRAMES = 4           # 윈도우 내 최소 EAR 측정 프레임.

# 주기적 재인증 (사이클 #29). 마지막 인증 통과 후 일정 시간이 지나면
# face_ok/voice_ok 를 리셋해 사용자에게 다시 인증을 요구한다. 재인증 트리거 후
# REAUTH_GRACE_SECONDS 안에 통과하지 못하면 자동 로그아웃.
REAUTH_INTERVAL_SECONDS = 3600.0   # 1시간.
REAUTH_GRACE_SECONDS = 60.0        # 자리비움 허용 시간.


_PUNCT_RE = re.compile(
    r"[\s\.,!?\-_/\\\(\)\[\]\{\}<>:;\"'`~@#$%^&*+=|·…！？。、，：；「」『』]+"
)


def normalize_voice(text: str) -> str:
    """음성 매칭용 정규화 — NFC, 소문자, 공백/구두점 제거."""
    if not isinstance(text, str):
        return ""
    t = unicodedata.normalize("NFC", text).strip().lower()
    t = _PUNCT_RE.sub("", t)
    return t


def voice_similarity(a: str, b: str) -> float:
    """두 문자열의 정규화 후 유사도 (0.0 ~ 1.0)."""
    na = normalize_voice(a)
    nb = normalize_voice(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def face_distance(a: List[float], b: List[float]) -> float:
    """두 얼굴 인코딩의 유클리드 거리. face_recognition 라이브러리와 같은 척도."""
    if not a or not b or len(a) != len(b):
        return float("inf")
    s = 0.0
    for x, y in zip(a, b):
        d = float(x) - float(y)
        s += d * d
    return math.sqrt(s)


def random_challenge() -> str:
    """챌린지 문장 풀에서 무작위 1개 반환. secrets 사용 → 예측 불가."""
    return secrets.choice(VOICE_CHALLENGE_POOL)


class OwnerAuth:
    """주인 인증 정보 영구 저장소 + 검증자 (stateless 검증).

    파일 포맷 `data/owner.json` (사이클 #20 신버전):
        {
            "enrolled": true,
            "face_name": "민수",
            "voice_passphrase_display": "사비스 안녕 나야",
            "voice_passphrase_norm": "사비스안녕나야",
            "face_encodings": [[128 floats] x N],   // 신: 다중 각도
            "face_encoding": [128 floats],          // 구버전 호환 (단일)
            "face_angles": ["front","left","right","up","down"],  // 메타
            "schema_version": 2,
            "created_at": 1714712345.0
        }
    """

    def __init__(self, path: str = "data/owner.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        self._load()

    # ---------- I/O ----------
    def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except Exception:
            # 손상된 파일은 미등록으로 간주 (덮어쓰기 시도하지 않음 — 백업 유지).
            self._data = {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # ---------- 상태 ----------
    def is_enrolled(self) -> bool:
        with self._lock:
            return bool(self._data.get("enrolled"))

    @property
    def face_name(self) -> str:
        with self._lock:
            return str(self._data.get("face_name") or "")

    def _stored_encodings(self) -> List[List[float]]:
        """저장된 얼굴 인코딩 목록 (신/구 포맷 통합). lock 내에서 호출."""
        encs: List[List[float]] = []
        multi = self._data.get("face_encodings")
        if isinstance(multi, list):
            for e in multi:
                if isinstance(e, list) and len(e) >= 64:
                    encs.append([float(x) for x in e])
        single = self._data.get("face_encoding")
        if not encs and isinstance(single, list) and len(single) >= 64:
            encs.append([float(x) for x in single])
        return encs

    @property
    def has_face_encoding(self) -> bool:
        with self._lock:
            return len(self._stored_encodings()) > 0

    @property
    def face_encoding_count(self) -> int:
        with self._lock:
            return len(self._stored_encodings())

    def info(self) -> Dict[str, Any]:
        """클라이언트에 안전하게 노출 가능한 메타. 패스프레이즈 평문은 노출 안 함."""
        with self._lock:
            display = str(self._data.get("voice_passphrase_display") or "")
            hint_len = len(display) if display else 0
            return {
                "enrolled": bool(self._data.get("enrolled")),
                "face_name": str(self._data.get("face_name") or ""),
                "voice_passphrase_len": hint_len,
                "has_face_encoding": len(self._stored_encodings()) > 0,
                "face_encoding_count": len(self._stored_encodings()),
                "face_angles": list(self._data.get("face_angles") or []),
                "schema_version": int(self._data.get("schema_version") or 1),
                "created_at": float(self._data.get("created_at") or 0.0),
            }

    # ---------- 등록/리셋 ----------
    def enroll(
        self,
        face_name: str,
        voice_passphrase: str,
        face_encoding: Optional[List[float]] = None,
        face_encodings: Optional[List[List[float]]] = None,
        face_angles: Optional[List[str]] = None,
    ) -> None:
        """주인 등록. 기존 등록이 있으면 덮어씀(reset 효과).

        Args:
            face_name: 표시 이름.
            voice_passphrase: 음성 패스프레이즈 (정규화 후 4자 이상).
            face_encoding: 단일 인코딩 (구버전 호환 / 폴백 — face_encodings 가
                있으면 무시되거나 합쳐짐).
            face_encodings: 5각도 등 다중 인코딩 (사이클 #20 신규). 우선 사용.
            face_angles: 각 인코딩의 각도 라벨 (예: ["front","left",...]) — 메타.
        """
        face_name = (face_name or "").strip()
        voice_passphrase = (voice_passphrase or "").strip()
        if not face_name:
            raise ValueError("주인 이름이 비어있습니다.")
        if len(normalize_voice(voice_passphrase)) < 4:
            raise ValueError(
                "음성 패스프레이즈가 너무 짧습니다 (정규화 후 4자 이상 필요).",
            )
        norm = normalize_voice(voice_passphrase)

        encs: List[List[float]] = []
        if face_encodings:
            for e in face_encodings:
                if not e:
                    continue
                try:
                    encs.append([float(x) for x in e])
                except Exception:
                    continue
        if not encs and face_encoding is not None:
            try:
                encs.append([float(x) for x in face_encoding])
            except Exception:
                pass

        angles: List[str] = []
        if face_angles:
            angles = [str(a) for a in face_angles[: len(encs)]]

        with self._lock:
            self._data = {
                "enrolled": True,
                "schema_version": 2,
                "face_name": face_name,
                "voice_passphrase_display": voice_passphrase,
                "voice_passphrase_norm": norm,
                "face_encodings": encs,
                # 구버전 호환: 첫 인코딩만 단일 필드에도 미러.
                "face_encoding": encs[0] if encs else None,
                "face_angles": angles,
                "created_at": time.time(),
            }
            self._save()

    def reset(self) -> None:
        """주인 등록 해제 (재등록 가능 상태로)."""
        with self._lock:
            self._data = {}
            try:
                if self.path.exists():
                    self.path.unlink()
            except Exception:
                pass

    # ---------- 검증 ----------
    def verify_voice(
        self,
        spoken_text: str,
        challenge_text: Optional[str] = None,
    ) -> Tuple[bool, float, str]:
        """발화 텍스트가 패스프레이즈 또는 챌린지와 충분히 유사하면 통과.

        Returns:
            (ok, similarity, matched_against)
                matched_against: 'passphrase' | 'challenge' | ''
        """
        with self._lock:
            target_norm = str(self._data.get("voice_passphrase_norm") or "")
            target_disp = str(self._data.get("voice_passphrase_display") or "")
        if not target_norm:
            return (False, 0.0, "")
        spoken_norm = normalize_voice(spoken_text)
        if not spoken_norm:
            return (False, 0.0, "")

        # 1) 패스프레이즈 매칭.
        if spoken_norm == target_norm:
            return (True, 1.0, "passphrase")
        sim_pass = SequenceMatcher(None, spoken_norm, target_norm).ratio()

        # 2) 챌린지 매칭 (있으면).
        sim_chal = 0.0
        if challenge_text:
            chal_norm = normalize_voice(challenge_text)
            if chal_norm:
                if spoken_norm == chal_norm:
                    return (True, 1.0, "challenge")
                sim_chal = SequenceMatcher(None, spoken_norm, chal_norm).ratio()

        # 둘 중 더 높은 쪽으로 판정.
        if sim_chal > sim_pass:
            ok = sim_chal >= VOICE_MATCH_THRESHOLD
            return (ok, sim_chal, "challenge" if ok else "")
        ok = sim_pass >= VOICE_MATCH_THRESHOLD
        return (ok, sim_pass, "passphrase" if ok else "")

    def verify_face_encoding(self, encoding: List[float]) -> bool:
        """주어진 인코딩이 저장된 인코딩들 중 하나라도 임계값 이내이면 True.

        다중 각도 등록 시 min(distance) ≤ THRESHOLD 로 통과 — 머리 각도가
        약간 달라도 인식됨.
        """
        if not encoding:
            return False
        with self._lock:
            stored_list = self._stored_encodings()
        if not stored_list:
            return False
        best = min(face_distance(s, encoding) for s in stored_list)
        return best <= FACE_DISTANCE_THRESHOLD

    def face_distance_min(self, encoding: List[float]) -> float:
        """디버그/UI 피드백용 — 저장된 인코딩들과의 최소 거리."""
        if not encoding:
            return float("inf")
        with self._lock:
            stored_list = self._stored_encodings()
        if not stored_list:
            return float("inf")
        return min(face_distance(s, encoding) for s in stored_list)

    def voice_similarity_to(
        self,
        spoken_text: str,
        challenge_text: Optional[str] = None,
    ) -> float:
        """디버그/UI 피드백용 — 패스프레이즈/챌린지 중 더 높은 유사도."""
        with self._lock:
            target_disp = str(self._data.get("voice_passphrase_display") or "")
        if not target_disp:
            return 0.0
        sim_pass = voice_similarity(spoken_text, target_disp)
        if challenge_text:
            sim_chal = voice_similarity(spoken_text, challenge_text)
            return max(sim_pass, sim_chal)
        return sim_pass


# ============================================================
# 라이브니스 (눈 깜빡임) — stateless 헬퍼
# ============================================================
def detect_blink_in_window(
    ear_samples: List[Tuple[float, float]],
    window_seconds: float = BLINK_WINDOW_SECONDS,
) -> Tuple[bool, Dict[str, float]]:
    """EAR 시계열에서 깜빡임 발생 여부 판정.

    Args:
        ear_samples: [(timestamp, ear_value), ...] 시간 오름차순 가정.
        window_seconds: 윈도우 길이 (초). 가장 최근 N초만 본다.

    Returns:
        (blinked, stats)
            blinked: 윈도우 내 (open → close → open) 패턴 1회 이상 감지되면 True.
            stats: {min, max, count, span} 디버그용.

    판정 알고리즘:
        - 윈도우 내 EAR 시퀀스를 순회.
        - "뜬 → 감음 → 뜬" 상태 전이가 1회 이상 발생하면 깜빡임으로 본다.
        - EAR_OPEN_THRESHOLD 이상이면 OPEN, EAR_CLOSE_THRESHOLD 이하면 CLOSE.
        - 그 사이 값은 직전 상태 유지 (히스테리시스).
    """
    if not ear_samples:
        return (False, {"min": 0.0, "max": 0.0, "count": 0, "span": 0.0})
    now = ear_samples[-1][0]
    window = [(t, e) for (t, e) in ear_samples if now - t <= window_seconds]
    if len(window) < BLINK_MIN_FRAMES:
        ears = [e for _t, e in window]
        return (
            False,
            {
                "min": min(ears) if ears else 0.0,
                "max": max(ears) if ears else 0.0,
                "count": len(window),
                "span": (window[-1][0] - window[0][0]) if window else 0.0,
            },
        )

    state = "unknown"   # "open" | "close" | "unknown"
    transitions: List[str] = []
    for _t, ear in window:
        if ear >= EAR_OPEN_THRESHOLD:
            new_state = "open"
        elif ear <= EAR_CLOSE_THRESHOLD:
            new_state = "close"
        else:
            new_state = state  # 히스테리시스
        if new_state != state and new_state in ("open", "close"):
            transitions.append(new_state)
            state = new_state

    # open → close → open 패턴 검출.
    blinked = False
    for i in range(len(transitions) - 2):
        if (
            transitions[i] == "open"
            and transitions[i + 1] == "close"
            and transitions[i + 2] == "open"
        ):
            blinked = True
            break

    ears = [e for _t, e in window]
    return (
        blinked,
        {
            "min": min(ears),
            "max": max(ears),
            "count": len(window),
            "span": window[-1][0] - window[0][0],
        },
    )


# ============================================================
# 주기적 재인증 — stateless 시간 헬퍼 (사이클 #29)
# ============================================================
def is_reauth_due(last_authed_at: float, now: float) -> bool:
    """마지막 인증 통과 시각 기준으로 재인증이 필요한지 판정.

    `last_authed_at` 이 0(미인증) 이면 False — 첫 인증 자체는 별도 게이트가 처리.
    그 외엔 (now - last_authed_at) 이 REAUTH_INTERVAL_SECONDS 이상이면 True.
    """
    if last_authed_at <= 0.0:
        return False
    return (now - last_authed_at) >= REAUTH_INTERVAL_SECONDS


def is_grace_expired(reauth_pending_since: float, now: float) -> bool:
    """재인증 트리거 시각 기준으로 grace 윈도우가 만료됐는지 판정.

    `reauth_pending_since` 이 0(트리거 안 됨) 이면 False.
    """
    if reauth_pending_since <= 0.0:
        return False
    return (now - reauth_pending_since) >= REAUTH_GRACE_SECONDS
