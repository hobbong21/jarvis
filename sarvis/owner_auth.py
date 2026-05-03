"""주인(owner) 인증 — 얼굴 + 음성 패스프레이즈.

사이클 #18 — 서비스 시작 시 주인 인증을 강제하는 로그인 시스템.

설계 원칙:
1. **등록(enroll) 1회**: 처음 부팅에서 주인이 본인 얼굴(카메라 캡처) + 음성
   패스프레이즈를 등록한다. `data/owner.json` 에 저장.
2. **등록 후 매 연결 인증 강제**: WebSocket 연결마다 (a) 얼굴 매치 (b) 음성
   패스프레이즈 매치 두 단계 모두 통과해야 메인 기능 사용 가능.
3. **미등록 상태 = 게이트 비활성화**: owner.json 이 없으면 인증 게이트가
   동작하지 않는다 → 기존 테스트 회귀 0, 첫 부팅 사용자가 막히지 않음.
   대신 클라이언트가 "주인 등록" UI 를 띄움.
4. **얼굴 인증의 점진적 개선**:
   - face_recognition 라이브러리가 있으면: 등록 시 얼굴 인코딩(128차원)을
     저장 → 로그인 프레임의 인코딩과 코사인/유클리드 거리 비교.
   - 없으면(웹 환경 폴백): 얼굴 박스 감지만 확인 (보안 약함, 동작은 함).
5. **음성 인증**: 사용자가 등록 시 정한 한국어 패스프레이즈를 발화하면
   STT 로 텍스트화 → 정규화(NFC, 공백/구두점 제거, 소문자) 후 difflib
   SequenceMatcher 로 0.8 이상 유사도면 통과. (1차 구현 — 사이클 #19에서
   화자 임베딩(resemblyzer)으로 업그레이드 예정.)

API:
    OwnerAuth(path="data/owner.json")
        .is_enrolled() -> bool
        .enroll(face_name: str, voice_passphrase: str,
                face_encoding: list[float] | None = None) -> None
        .reset() -> None
        .verify_voice(text: str) -> bool
        .verify_face_encoding(encoding: list[float]) -> bool
        .info() -> dict (face_name, hint — 패스프레이즈 자체는 노출 안 함)

상태(라이프사이클)는 OwnerAuth 인스턴스가 아니라 호출 측(서버 WS 핸들러)이
가진다. OwnerAuth 는 stateless 검증자 + 영구 저장소만 담당.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional


# 음성 패스프레이즈 매칭 임계값 (0.0 ~ 1.0). 보수적으로 0.78 — STT 가 한 두
# 글자 빠뜨려도 통과시키되 완전 다른 문장은 거부.
VOICE_MATCH_THRESHOLD = 0.78

# 얼굴 거리 임계값 (face_recognition 의 face_distance 결과). cfg.face_match_tolerance
# 와 비슷한 값. 0.6 이하면 같은 사람으로 본다.
FACE_DISTANCE_THRESHOLD = 0.55


_PUNCT_RE = re.compile(r"[\s\.,!?\-_/\\\(\)\[\]\{\}<>:;\"'`~@#$%^&*+=|·…！？。、，：；「」『』]+")


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


class OwnerAuth:
    """주인 인증 정보 영구 저장소 + 검증자.

    파일 포맷 `data/owner.json`:
        {
            "enrolled": true,
            "face_name": "민수",
            "voice_passphrase_display": "사비스 안녕 나야",
            "voice_passphrase_norm": "사비스안녕나야",
            "face_encoding": [0.123, ...]  // 128 floats, 없으면 null
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

    @property
    def has_face_encoding(self) -> bool:
        with self._lock:
            enc = self._data.get("face_encoding")
            return isinstance(enc, list) and len(enc) >= 64

    def info(self) -> Dict[str, Any]:
        """클라이언트에 안전하게 노출 가능한 메타. 패스프레이즈 평문은 노출 안 함."""
        with self._lock:
            display = str(self._data.get("voice_passphrase_display") or "")
            # 힌트: 길이만 노출, 평문은 안 보냄. 사용자가 잊었을 때 reset 안내.
            hint_len = len(display) if display else 0
            return {
                "enrolled": bool(self._data.get("enrolled")),
                "face_name": str(self._data.get("face_name") or ""),
                "voice_passphrase_len": hint_len,
                "has_face_encoding": self.has_face_encoding,
                "created_at": float(self._data.get("created_at") or 0.0),
            }

    # ---------- 등록/리셋 ----------
    def enroll(
        self,
        face_name: str,
        voice_passphrase: str,
        face_encoding: Optional[List[float]] = None,
    ) -> None:
        """주인 등록. 기존 등록이 있으면 덮어씀(reset 효과)."""
        face_name = (face_name or "").strip()
        voice_passphrase = (voice_passphrase or "").strip()
        if not face_name:
            raise ValueError("주인 이름이 비어있습니다.")
        if len(normalize_voice(voice_passphrase)) < 4:
            raise ValueError(
                "음성 패스프레이즈가 너무 짧습니다 (정규화 후 4자 이상 필요).",
            )
        norm = normalize_voice(voice_passphrase)
        enc: Optional[List[float]] = None
        if face_encoding is not None:
            try:
                enc = [float(x) for x in face_encoding]
            except Exception:
                enc = None
        with self._lock:
            self._data = {
                "enrolled": True,
                "face_name": face_name,
                "voice_passphrase_display": voice_passphrase,
                "voice_passphrase_norm": norm,
                "face_encoding": enc,
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
    def verify_voice(self, spoken_text: str) -> bool:
        """발화 텍스트가 등록 패스프레이즈와 충분히 유사하면 True."""
        with self._lock:
            target = str(self._data.get("voice_passphrase_norm") or "")
        if not target:
            return False
        spoken_norm = normalize_voice(spoken_text)
        if not spoken_norm:
            return False
        if spoken_norm == target:
            return True
        sim = SequenceMatcher(None, spoken_norm, target).ratio()
        return sim >= VOICE_MATCH_THRESHOLD

    def verify_face_encoding(self, encoding: List[float]) -> bool:
        """얼굴 인코딩(128차원) 거리가 임계값 이하면 True."""
        if not encoding:
            return False
        with self._lock:
            stored = self._data.get("face_encoding")
        if not stored:
            return False
        d = face_distance(stored, encoding)
        return d <= FACE_DISTANCE_THRESHOLD

    def voice_similarity_to(self, spoken_text: str) -> float:
        """디버그/UI 피드백용 — 등록 패스프레이즈 대비 유사도."""
        with self._lock:
            target_disp = str(self._data.get("voice_passphrase_display") or "")
        if not target_disp:
            return 0.0
        return voice_similarity(spoken_text, target_disp)
