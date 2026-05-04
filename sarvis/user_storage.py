"""사용자 개인 저장공간 — 5GB 한도, 파일/대화 아카이브, AI 접근 토글.

사이클 #30. owner_auth 로 인증된 사용자(face_name) 별로 분리된 디렉토리를 운영.

디렉토리 구조::

    data/users/<safe_face_name>/
        files/                    # 사용자 업로드 + 미디어 + AI 산출물
            <file_id>_<safe_original_name>
        conversations/            # 대화 마크다운
            <file_id>_<safe_title>.md
        metadata.json             # {file_id: {name, size, uploaded_at, ai_access, kind}}

핵심 설계:
  · 파일 저장 시 5GB(`cfg.user_storage_limit_bytes`) 합산 한도 검사 → 초과 시
    `QuotaExceeded` 예외. 자동 삭제 등 destructive 동작은 하지 않음 (사용자가 결정).
  · `ai_access` 토글은 모든 파일에 부여되며 기본값 True (사용자 결정 Q4=C). 사용자가
    특정 파일을 AI 도구에서 숨기고 싶을 때 False 로 토글.
  · 경로 traversal 방지 — file_id 는 영숫자만, 원본 이름은 `_safe_name` 으로 정규화.
  · 동시 접근 — 단일 프로세스 가정. 메서드별 threading.Lock 으로 직렬화 (간단 케이스).
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class QuotaExceeded(Exception):
    """5GB 한도 초과 시 발생. 호출자에게 사용자 친화적 메시지 전달용."""


_NAME_SAFE = re.compile(r"[^0-9A-Za-z가-힣_\-\.]+")


def _safe_name(name: str, fallback: str = "file") -> str:
    """파일/디렉토리 안전 문자열 — 한글/영숫자/_/-/. 만 허용. 길이 80 제한."""
    if not isinstance(name, str):
        return fallback
    s = _NAME_SAFE.sub("_", name.strip())
    s = s.strip("._-")
    return (s[:80] or fallback)


def _new_file_id() -> str:
    """짧은 파일 식별자 — 12자 hex (충돌 확률 극히 낮음)."""
    return secrets.token_hex(6)


# 허용 kind — 외부 호출자가 임의 문자열 보내는 걸 방지하는 화이트리스트.
ALLOWED_KINDS = ("upload", "conversation", "media", "ai_artifact")


class UserStorage:
    """단일 사용자(face_name) 의 개인 저장공간.

    `face_name` 은 owner_auth 의 표시 이름. 디스크상에는 `_safe_name` 으로 정규화된
    디렉토리에 저장된다. 빈 이름은 거부.
    """

    def __init__(
        self,
        face_name: str,
        root: str = "data/users",
        limit_bytes: int = 5 * 1024 ** 3,
    ):
        if not face_name or not face_name.strip():
            raise ValueError("face_name 이 비어있습니다.")
        if limit_bytes <= 0:
            raise ValueError("limit_bytes 는 양수여야 합니다.")

        self.face_name = face_name.strip()
        self.limit_bytes = int(limit_bytes)
        self._lock = threading.Lock()

        safe = _safe_name(self.face_name, fallback="user")
        self.user_dir = Path(root) / safe
        self.files_dir = self.user_dir / "files"
        self.conv_dir = self.user_dir / "conversations"
        self.meta_path = self.user_dir / "metadata.json"

        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.conv_dir.mkdir(parents=True, exist_ok=True)

        self._meta: Dict[str, Dict[str, Any]] = self._load_meta()

    # ---------- 메타 I/O ----------
    def _load_meta(self) -> Dict[str, Dict[str, Any]]:
        if not self.meta_path.exists():
            return {}
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        files = data.get("files")
        return files if isinstance(files, dict) else {}

    def _save_meta(self) -> None:
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"files": self._meta}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.meta_path)

    # ---------- 용량 ----------
    @property
    def used_bytes(self) -> int:
        with self._lock:
            return sum(int(m.get("size", 0)) for m in self._meta.values())

    @property
    def free_bytes(self) -> int:
        return max(0, self.limit_bytes - self.used_bytes)

    def _disk_path(self, entry: Dict[str, Any]) -> Path:
        """메타 항목의 실제 디스크 경로. 외부 참조(register_external)는 ``external_path`` 사용."""
        ext = entry.get("external_path")
        if ext:
            return Path(ext)
        sub = self.conv_dir if entry.get("kind") == "conversation" else self.files_dir
        return sub / entry["disk_name"]

    # ---------- 저장 ----------
    def save_file(
        self,
        original_name: str,
        data: bytes,
        kind: str = "upload",
        ai_access: bool = True,
    ) -> str:
        """바이너리/텍스트 파일 저장. 성공 시 file_id 반환. 한도 초과 시 QuotaExceeded.

        Args:
            original_name: 사용자에게 표시할 원본 파일명.
            data: 파일 바이트.
            kind: ALLOWED_KINDS 중 하나.
            ai_access: AI 도구가 이 파일을 보고/읽을 수 있는지. 기본 True.
        """
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"허용되지 않은 kind: {kind!r}")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data 는 bytes 여야 합니다.")
        size = len(data)
        if size <= 0:
            raise ValueError("빈 파일은 저장할 수 없습니다.")

        with self._lock:
            used = sum(int(m.get("size", 0)) for m in self._meta.values())
            if used + size > self.limit_bytes:
                raise QuotaExceeded(
                    f"저장공간 한도({self.limit_bytes / 1024 ** 3:.1f}GB) 초과 — "
                    f"{(used + size - self.limit_bytes) / 1024 ** 2:.1f}MB 만큼 부족합니다."
                )

            file_id = _new_file_id()
            safe_orig = _safe_name(original_name, fallback="file")
            disk_name = f"{file_id}_{safe_orig}"
            sub = self.conv_dir if kind == "conversation" else self.files_dir
            (sub / disk_name).write_bytes(bytes(data))

            self._meta[file_id] = {
                "name": original_name.strip() or safe_orig,
                "disk_name": disk_name,
                "size": size,
                "uploaded_at": time.time(),
                "ai_access": bool(ai_access),
                "kind": kind,
            }
            self._save_meta()
        return file_id

    def register_external(
        self,
        original_name: str,
        file_path: str,
        kind: str = "media",
        ai_access: bool = True,
    ) -> str:
        """외부에 이미 저장된 파일을 메타에만 등록. 데이터 복사 없음.

        사이클 #32 — 녹화/녹음/사진처럼 다른 경로(`data/recordings/...`) 에 이미 쓰여진
        파일을 사용자 저장공간 카탈로그에 노출하기 위한 경로. ``size`` 는 디스크에서
        직접 읽어 합산된다. 5GB 한도 검사도 포함되며 한도 초과 시 ``QuotaExceeded``.

        ``delete_file`` 은 외부 참조 파일의 실제 디스크 파일을 건드리지 않는다 — 메타만
        제거. 호출자가 디스크 파일 수명을 별도로 책임진다.
        """
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"허용되지 않은 kind: {kind!r}")
        p = Path(file_path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"파일 없음: {file_path}")
        size = p.stat().st_size
        if size <= 0:
            raise ValueError("빈 파일은 등록할 수 없습니다.")

        with self._lock:
            used = sum(int(m.get("size", 0)) for m in self._meta.values())
            if used + size > self.limit_bytes:
                raise QuotaExceeded(
                    f"저장공간 한도({self.limit_bytes / 1024 ** 3:.1f}GB) 초과 — "
                    f"{(used + size - self.limit_bytes) / 1024 ** 2:.1f}MB 만큼 부족합니다."
                )
            file_id = _new_file_id()
            self._meta[file_id] = {
                "name": (original_name or p.name).strip(),
                "disk_name": "",
                "external_path": str(p.resolve()),
                "size": size,
                "uploaded_at": time.time(),
                "ai_access": bool(ai_access),
                "kind": kind,
            }
            self._save_meta()
        return file_id

    def save_conversation(
        self,
        markdown: str,
        title: Optional[str] = None,
        ai_access: bool = True,
    ) -> str:
        """대화 마크다운 저장. title 없으면 timestamp 기반 자동 생성. file_id 반환."""
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("빈 대화는 저장할 수 없습니다.")
        ts = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
        name = (title.strip() if title and title.strip() else f"conversation_{ts}") + ".md"
        return self.save_file(
            original_name=name,
            data=markdown.encode("utf-8"),
            kind="conversation",
            ai_access=ai_access,
        )

    # ---------- 조회 ----------
    def list_files(
        self,
        kind: Optional[str] = None,
        ai_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """파일 메타 목록 — 업로드 시각 내림차순. kind 필터 + ai_access 필터."""
        with self._lock:
            items = []
            for fid, m in self._meta.items():
                if kind is not None and m.get("kind") != kind:
                    continue
                if ai_only and not m.get("ai_access"):
                    continue
                items.append({
                    "file_id": fid,
                    "name": m.get("name", ""),
                    "size": int(m.get("size", 0)),
                    "uploaded_at": float(m.get("uploaded_at", 0.0)),
                    "ai_access": bool(m.get("ai_access", False)),
                    "kind": m.get("kind", "upload"),
                })
        items.sort(key=lambda x: x["uploaded_at"], reverse=True)
        return items

    def get_metadata(self, file_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            m = self._meta.get(file_id)
            if not m:
                return None
            return {
                "file_id": file_id,
                "name": m.get("name", ""),
                "size": int(m.get("size", 0)),
                "uploaded_at": float(m.get("uploaded_at", 0.0)),
                "ai_access": bool(m.get("ai_access", False)),
                "kind": m.get("kind", "upload"),
            }

    def read_file(self, file_id: str, ai_call: bool = False) -> bytes:
        """파일 데이터 반환.

        ai_call=True 면 AI 도구의 호출 — ai_access=False 인 파일은 PermissionError.
        ai_call=False 는 사용자가 다운로드 등 직접 액션 — 토글 무시.
        """
        with self._lock:
            entry = self._meta.get(file_id)
            if not entry:
                raise FileNotFoundError(f"파일 없음: {file_id}")
            if ai_call and not entry.get("ai_access"):
                raise PermissionError(f"AI 접근이 비활성화된 파일입니다: {entry.get('name')}")
            path = self._disk_path(entry)
        if not path.exists():
            raise FileNotFoundError(f"디스크에 파일이 없습니다: {path}")
        return path.read_bytes()

    # ---------- 변경 ----------
    def set_ai_access(self, file_id: str, allow: bool) -> bool:
        """AI 접근 토글. 메타 없으면 False, 변경 시 True."""
        with self._lock:
            entry = self._meta.get(file_id)
            if not entry:
                return False
            entry["ai_access"] = bool(allow)
            self._save_meta()
        return True

    def rename(self, file_id: str, new_name: str) -> bool:
        """표시 이름만 변경 (디스크 파일은 그대로). 메타 없으면 False."""
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("새 이름이 비어있습니다.")
        with self._lock:
            entry = self._meta.get(file_id)
            if not entry:
                return False
            entry["name"] = new_name
            self._save_meta()
        return True

    def delete_file(self, file_id: str) -> bool:
        """파일 삭제. 메타 항상 제거. 디스크 파일은 register_external 참조면 보존."""
        with self._lock:
            entry = self._meta.pop(file_id, None)
            if not entry:
                return False
            is_external = bool(entry.get("external_path"))
            if not is_external:
                path = self._disk_path(entry)
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
            self._save_meta()
        return True

    # ---------- 검색 ----------
    def search_files(
        self,
        query: str,
        ai_only: bool = False,
        max_results: int = 20,
    ) -> List[Dict[str, Any]]:
        """파일명/저장된 텍스트 본문에서 query(소문자 비교) 매칭. max_results 개 반환.

        대용량 바이너리는 텍스트 검색 안 함 (`utf-8` 디코드 실패하면 본문 스킵).
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        out: List[Dict[str, Any]] = []
        for meta in self.list_files(ai_only=ai_only):
            if len(out) >= max_results:
                break
            if q in meta["name"].lower():
                out.append(meta)
                continue
            try:
                data = self.read_file(meta["file_id"], ai_call=False)
                text = data.decode("utf-8", errors="ignore").lower()
            except Exception:
                continue
            if q in text:
                out.append(meta)
        return out
