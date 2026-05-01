"""
SARVIS 장기 메모리 (기획서 v2.0 — SQLite 토대).

이 모듈은 사비스가 대화/사실/관찰을 영구 저장하고 다음 응답에 컨텍스트로
주입하기 위한 **로컬 SQLite** 저장소입니다. 클라우드 의존성 없음.

스키마 (memory.db):
  - conversations    (id, user_id, started_at, ended_at, summary)
  - messages         (id, conv_id, role, content, timestamp, emotion)
  - facts            (id, user_id, key, value, source_msg_id, created_at, updated_at)
  - observations     (id, user_id, type, data_json, frame_path, timestamp)
  - routines         (id, user_id, pattern_desc, frequency, confidence, last_seen)
  - timers_events    (id, user_id, label, scheduled_at, recurring, completed)

ChromaDB / sentence-transformers (의미 검색) 는 v2.0 2단계에서 추가되며,
본 모듈의 공개 API 는 그 때도 변하지 않도록 search_messages() 를
키워드 (LIKE) 검색으로 시작 — 임베딩이 들어와도 동일 시그니처 사용.

Thread-safety: 매 호출마다 새 sqlite3.Connection 을 열고 닫는다 (간단/안전).
PRAGMA journal_mode=WAL 로 다중 reader + 1 writer 동시성 확보.

PII / 안전:
  - facts 의 value 는 사용자가 명시한 사실만 담는다 (시스템 프롬프트가 아님).
  - forget(query) 는 facts/messages 모두에서 hard delete.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

DB_PATH = os.environ.get("SARVIS_MEMORY_DB", "data/memory.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    started_at  REAL    NOT NULL,
    ended_at    REAL,
    summary     TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL CHECK(role IN ('user','assistant','system','tool')),
    content     TEXT    NOT NULL,
    timestamp   REAL    NOT NULL,
    emotion     TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT    NOT NULL,
    key           TEXT    NOT NULL,
    value         TEXT    NOT NULL,
    source_msg_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    UNIQUE(user_id, key)
);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    data_json   TEXT    NOT NULL,
    frame_path  TEXT,
    timestamp   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS routines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT    NOT NULL,
    pattern_desc TEXT    NOT NULL,
    frequency    INTEGER NOT NULL DEFAULT 1,
    confidence   REAL    NOT NULL DEFAULT 0.0,
    last_seen    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS timers_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT    NOT NULL,
    label        TEXT    NOT NULL,
    scheduled_at REAL    NOT NULL,
    recurring    INTEGER NOT NULL DEFAULT 0,
    completed    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_content ON messages(content);
CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_user_ts ON observations(user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_conv_user_started ON conversations(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_timers_user_sched ON timers_events(user_id, scheduled_at);
"""


# 스키마 초기화는 프로세스 당 한 번. (멀티프로세스 fork 환경에서도 안전 — IF NOT EXISTS)
_init_lock = threading.Lock()
_initialized_paths: set = set()


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    # WAL: 동시성 + 충돌 감소. foreign_keys: ON DELETE CASCADE 동작 보장.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(path: str) -> None:
    if path in _initialized_paths:
        return
    with _init_lock:
        if path in _initialized_paths:
            return
        # 사이클 #9 정비: data/ 등 하위 경로 사용 시 부모 디렉토리 자동 생성.
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        conn = _connect(path)
        try:
            conn.executescript(_SCHEMA_SQL)
        finally:
            conn.close()
        _initialized_paths.add(path)


@contextmanager
def _conn_ctx(path: Optional[str] = None):
    p = path or DB_PATH
    _ensure_schema(p)
    conn = _connect(p)
    try:
        yield conn
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────────
# SemanticIndex (v2.0 stage 2) — 옵셔널 의미 검색.
#
# chromadb + sentence-transformers (ko-sroberta-multitask, 384차원) 가
# 설치되어 있고 SARVIS_SEMANTIC=1 일 때만 동작. 그 외에는 모든 메서드가
# no-op / False 반환 → Memory.search_messages 가 LIKE 폴백 사용.
#
# 모델 다운로드는 무거우므로 (~400MB), 기본은 비활성화.
# 활성화 방법:
#   pip install chromadb sentence-transformers
#   export SARVIS_SEMANTIC=1
# ────────────────────────────────────────────────────────────────────
_semantic_warned = False


def _semantic_enabled() -> bool:
    return os.getenv("SARVIS_SEMANTIC", "").strip() in ("1", "true", "True", "yes")


class SemanticIndex:
    """옵셔널 ChromaDB 인덱스. 의존성/환경변수 부재 시 자동으로 비활성."""

    EMBED_MODEL = os.getenv("SARVIS_EMBED_MODEL", "jhgan/ko-sroberta-multitask")

    def __init__(self, persist_dir: Optional[str] = None) -> None:
        self.persist_dir = persist_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "chromadb"
        )
        self._client = None
        self._collection = None
        self._encoder = None
        # `available=True` 는 "의존성/환경변수 OK" 를 의미. 인코더/컬렉션은
        # 첫 호출 시 lazy 로드 — 부팅 시점에 ~400MB 모델 다운로드로 서버를
        # 차단하지 않기 위함 (architect P0).
        self.available = False
        if not _semantic_enabled():
            return
        try:
            import chromadb  # type: ignore
            from sentence_transformers import SentenceTransformer  # type: ignore
            self._SentenceTransformer = SentenceTransformer
            self._chromadb = chromadb
            self.available = True
        except ImportError as e:
            global _semantic_warned
            if not _semantic_warned:
                print(f"[memory.SemanticIndex] 의미 검색 비활성화 (의존성 부재): {e}")
                _semantic_warned = True

    def _ensure_loaded(self) -> bool:
        """인코더/컬렉션 lazy 초기화. 첫 호출에서만 무거운 작업 발생."""
        if self._encoder is not None and self._collection is not None:
            return True
        try:
            if self._encoder is None:
                self._encoder = self._SentenceTransformer(self.EMBED_MODEL)
            if self._collection is None:
                self._client = self._chromadb.PersistentClient(path=self.persist_dir)
                self._collection = self._client.get_or_create_collection(
                    name="sarvis_messages",
                    metadata={"hnsw:space": "cosine"},
                )
            return True
        except Exception as e:
            print(f"[memory.SemanticIndex] lazy 로드 실패 — LIKE 폴백: {e}")
            self.available = False
            return False

    def index_message(self, msg_id: int, user_id: str, content: str) -> bool:
        """메시지 1건 인덱싱. 의미 검색 비활성 시 False."""
        if not self.available or not content:
            return False
        if not self._ensure_loaded():
            return False
        try:
            emb = self._encoder.encode([content], normalize_embeddings=True).tolist()
            self._collection.upsert(
                ids=[f"m{msg_id}"],
                embeddings=emb,
                metadatas=[{"user_id": user_id, "msg_id": int(msg_id)}],
                documents=[content],
            )
            return True
        except Exception as e:
            print(f"[memory.SemanticIndex] index_message 실패: {e}")
            return False

    def search(self, user_id: str, query: str, k: int = 10) -> List[int]:
        """의미적으로 유사한 message id 리스트. 비활성 시 빈 리스트."""
        if not self.available or not query:
            return []
        if not self._ensure_loaded():
            return []
        try:
            emb = self._encoder.encode([query], normalize_embeddings=True).tolist()
            res = self._collection.query(
                query_embeddings=emb,
                n_results=max(1, min(int(k), 50)),
                where={"user_id": user_id},
            )
            metas = (res.get("metadatas") or [[]])[0]
            return [int(m["msg_id"]) for m in metas if "msg_id" in m]
        except Exception as e:
            print(f"[memory.SemanticIndex] search 실패: {e}")
            return []


_semantic_singleton: Optional[SemanticIndex] = None


def get_semantic_index() -> SemanticIndex:
    """프로세스 단일 SemanticIndex (lazy)."""
    global _semantic_singleton
    if _semantic_singleton is None:
        _semantic_singleton = SemanticIndex()
    return _semantic_singleton


# ────────────────────────────────────────────────────────────────────
# Memory: 인스턴스 객체. 테스트에서 별도 path 로 분리 가능.
# ────────────────────────────────────────────────────────────────────
class _NullSemanticIndex:
    """SemanticIndex 와 동일 시그니처를 가진 no-op. 테스트 격리용."""
    available = False
    def index_message(self, msg_id, user_id, content): return False
    def search(self, user_id, query, k=10): return []


class Memory:
    """SARVIS 장기 메모리 게이트웨이. SQLite 단일 파일 백엔드."""

    def __init__(
        self,
        path: Optional[str] = None,
        semantic_index: Optional["SemanticIndex"] = None,
    ) -> None:
        self.path = path or DB_PATH
        _ensure_schema(self.path)
        # 사이클 #7 (v2.0 stage 2) — 의미 검색 인덱스.
        # 격리: 사용자 지정 path 가 있으면 (테스트 등) 자동으로 NullIndex 주입.
        # 명시적 semantic_index 인자가 우선. 기본 path 일 때만 전역 싱글톤 공유.
        if semantic_index is not None:
            self._semantic = semantic_index
        elif path and path != DB_PATH:
            # 사용자 지정 DB → 운영 chromadb 오염 방지.
            self._semantic = _NullSemanticIndex()
        else:
            self._semantic = get_semantic_index()

    # ── 대화 lifecycle ─────────────────────────────────────────────
    def start_conversation(self, user_id: str) -> int:
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO conversations(user_id, started_at) VALUES (?, ?)",
                (user_id, time.time()),
            )
            return int(cur.lastrowid)

    def end_conversation(self, conv_id: int, summary: Optional[str] = None) -> None:
        with _conn_ctx(self.path) as conn:
            conn.execute(
                "UPDATE conversations SET ended_at=?, summary=COALESCE(?, summary) WHERE id=?",
                (time.time(), summary, conv_id),
            )

    def get_or_start_conversation(self, user_id: str, idle_window_sec: float = 1800.0) -> int:
        """가장 최근 활성 대화가 idle_window_sec 안에 있으면 재사용, 아니면 새로 시작."""
        now = time.time()
        with _conn_ctx(self.path) as conn:
            row = conn.execute(
                """
                SELECT c.id, MAX(COALESCE(m.timestamp, c.started_at)) AS last_activity
                FROM conversations c
                LEFT JOIN messages m ON m.conv_id = c.id
                WHERE c.user_id=? AND c.ended_at IS NULL
                GROUP BY c.id
                ORDER BY c.id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row and (now - float(row["last_activity"])) <= idle_window_sec:
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO conversations(user_id, started_at) VALUES (?, ?)",
                (user_id, now),
            )
            return int(cur.lastrowid)

    # ── 메시지 ────────────────────────────────────────────────────
    def add_message(
        self,
        conv_id: int,
        role: str,
        content: str,
        emotion: Optional[str] = None,
    ) -> int:
        if role not in ("user", "assistant", "system", "tool"):
            raise ValueError(f"invalid role: {role}")
        if not isinstance(content, str):
            content = str(content)
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO messages(conv_id, role, content, timestamp, emotion) VALUES (?, ?, ?, ?, ?)",
                (conv_id, role, content, time.time(), emotion),
            )
            msg_id = int(cur.lastrowid)
            # 사이클 #7: 사용자 메시지만 의미 인덱싱 (assistant 응답은 제외 — recall 의 query 가
            # 일반적으로 사용자 발화에서 출발하므로 인덱스 크기/의미 모두 user 우선이 맞음).
            user_id = None
            if role == "user":
                row = conn.execute(
                    "SELECT user_id FROM conversations WHERE id=?", (conv_id,)
                ).fetchone()
                user_id = row["user_id"] if row else None
        if role == "user" and user_id and self._semantic.available:
            try:
                self._semantic.index_message(msg_id, user_id, content)
            except Exception as e:
                # 인덱싱 실패는 SQLite 트랜잭션에 영향 없도록 격리.
                print(f"[Memory.add_message] semantic index 실패: {e}")
        return msg_id

    def get_recent_messages(self, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """가장 최근 N 개 메시지를 시간순(오래된 → 최신)으로 반환."""
        limit = max(1, min(int(limit), 500))
        with _conn_ctx(self.path) as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.conv_id, m.role, m.content, m.timestamp, m.emotion
                FROM messages m
                JOIN conversations c ON c.id = m.conv_id
                WHERE c.user_id=?
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def search_messages(
        self,
        user_id: str,
        query: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """메시지 검색.

        사이클 #7 (v2.0 stage 2):
        - SemanticIndex 활성 시 의미 검색 (cosine top-k) → SQLite 에서 메타데이터 join.
        - 비활성/실패 시 키워드(LIKE) 폴백.

        반환 순서: 의미 검색 활성일 때는 유사도 순 (top-1 먼저).
                   LIKE 폴백일 때는 최신 순.
        """
        q = (query or "").strip()
        if not q:
            return []
        limit = max(1, min(int(limit), 50))

        # 1차: 의미 검색
        if self._semantic.available:
            try:
                ids = self._semantic.search(user_id, q, k=limit)
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    with _conn_ctx(self.path) as conn:
                        rows = conn.execute(
                            f"""
                            SELECT m.id, m.conv_id, m.role, m.content, m.timestamp, m.emotion
                            FROM messages m
                            JOIN conversations c ON c.id = m.conv_id
                            WHERE c.user_id=? AND m.id IN ({placeholders})
                            """,
                            (user_id, *ids),
                        ).fetchall()
                    by_id = {int(r["id"]): dict(r) for r in rows}
                    return [by_id[i] for i in ids if i in by_id]
            except Exception as e:
                print(f"[Memory.search_messages] semantic 실패 — LIKE 폴백: {e}")

        # 2차: LIKE 폴백
        like = f"%{q}%"
        with _conn_ctx(self.path) as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.conv_id, m.role, m.content, m.timestamp, m.emotion
                FROM messages m
                JOIN conversations c ON c.id = m.conv_id
                WHERE c.user_id=? AND m.content LIKE ? ESCAPE '\\'
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (user_id, like, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _format_role_label(row: Dict[str, Any]) -> str:
        """recall 출력용 role 라벨. emotion 에 '|source' 가 인코딩돼 있으면 노출.

        compare 모드는 add_message(emotion="<emo>|<source>") 로 저장하므로
        recall 시 (assistant|claude) / (assistant|openai) 처럼 출처를 보존한다.
        """
        role = row.get("role") or "?"
        emo = row.get("emotion") or ""
        if "|" in emo:
            try:
                _emo, src = emo.rsplit("|", 1)
                if src:
                    return f"{role}|{src}"
            except ValueError:
                pass
        return role

    # ── 사실 (facts) ──────────────────────────────────────────────
    def upsert_fact(
        self,
        user_id: str,
        key: str,
        value: str,
        source_msg_id: Optional[int] = None,
    ) -> int:
        if not key or not isinstance(key, str):
            raise ValueError("fact key required")
        if not isinstance(value, str):
            value = str(value)
        now = time.time()
        with _conn_ctx(self.path) as conn:
            row = conn.execute(
                "SELECT id FROM facts WHERE user_id=? AND key=?",
                (user_id, key),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE facts SET value=?, source_msg_id=COALESCE(?, source_msg_id), updated_at=? WHERE id=?",
                    (value, source_msg_id, now, int(row["id"])),
                )
                return int(row["id"])
            cur = conn.execute(
                """
                INSERT INTO facts(user_id, key, value, source_msg_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, key, value, source_msg_id, now, now),
            )
            return int(cur.lastrowid)

    def get_facts(self, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with _conn_ctx(self.path) as conn:
            rows = conn.execute(
                "SELECT id, key, value, source_msg_id, created_at, updated_at FROM facts WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_fact(self, user_id: str, key: str) -> bool:
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                "DELETE FROM facts WHERE user_id=? AND key=?",
                (user_id, key),
            )
            return cur.rowcount > 0

    # ── 관찰 (observations) ───────────────────────────────────────
    def add_observation(
        self,
        user_id: str,
        type_: str,
        data: Dict[str, Any],
        frame_path: Optional[str] = None,
    ) -> int:
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO observations(user_id, type, data_json, frame_path, timestamp) VALUES (?, ?, ?, ?, ?)",
                (user_id, type_, json.dumps(data, ensure_ascii=False), frame_path, time.time()),
            )
            return int(cur.lastrowid)

    def get_observations(
        self,
        user_id: str,
        type_: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with _conn_ctx(self.path) as conn:
            if type_:
                rows = conn.execute(
                    "SELECT id, type, data_json, frame_path, timestamp FROM observations WHERE user_id=? AND type=? ORDER BY id DESC LIMIT ?",
                    (user_id, type_, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, type, data_json, frame_path, timestamp FROM observations WHERE user_id=? ORDER BY id DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d.pop("data_json") or "{}")
            except json.JSONDecodeError:
                d["data"] = {}
                d.pop("data_json", None)
            out.append(d)
        return out

    # ── 타이머 / 이벤트 ────────────────────────────────────────────
    def add_timer_event(
        self,
        user_id: str,
        label: str,
        scheduled_at: float,
        recurring: bool = False,
    ) -> int:
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO timers_events(user_id, label, scheduled_at, recurring) VALUES (?, ?, ?, ?)",
                (user_id, label, float(scheduled_at), 1 if recurring else 0),
            )
            return int(cur.lastrowid)

    def upcoming_timers(self, user_id: str, within_sec: float = 86400.0) -> List[Dict[str, Any]]:
        now = time.time()
        with _conn_ctx(self.path) as conn:
            rows = conn.execute(
                "SELECT id, label, scheduled_at, recurring, completed FROM timers_events "
                "WHERE user_id=? AND completed=0 AND scheduled_at >= ? AND scheduled_at <= ? "
                "ORDER BY scheduled_at ASC",
                (user_id, now, now + within_sec),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 망각 (forget) ──────────────────────────────────────────────
    def forget(self, user_id: str, query: str) -> Dict[str, int]:
        """query 문자열을 포함하는 facts/messages 를 hard delete.

        반환: {"facts": N, "messages": M}
        """
        q = (query or "").strip()
        if not q:
            return {"facts": 0, "messages": 0}
        like = f"%{q}%"
        with _conn_ctx(self.path) as conn:
            cur1 = conn.execute(
                "DELETE FROM facts WHERE user_id=? AND (key LIKE ? OR value LIKE ?)",
                (user_id, like, like),
            )
            cur2 = conn.execute(
                """
                DELETE FROM messages
                WHERE id IN (
                    SELECT m.id FROM messages m
                    JOIN conversations c ON c.id=m.conv_id
                    WHERE c.user_id=? AND m.content LIKE ?
                )
                """,
                (user_id, like),
            )
            return {"facts": cur1.rowcount, "messages": cur2.rowcount}

    # ── 컨텍스트 블록 (system prompt 주입용) ──────────────────────
    def context_block(
        self,
        user_id: str,
        query: Optional[str] = None,
        max_facts: int = 8,
        max_recalls: int = 4,
    ) -> str:
        """LLM system prompt 에 [기억:...] 블록으로 주입할 한국어 컨텍스트 문자열.

        - max_facts 개의 최근 facts (key: value)
        - 새 발화 query 와 키워드 매칭되는 과거 메시지 max_recalls 개
        - 비어 있으면 빈 문자열 반환 (= 주입 안 함)
        """
        parts: List[str] = []
        facts = self.get_facts(user_id, limit=max_facts)
        if facts:
            parts.append("저장된 사실:")
            for f in facts:
                parts.append(f"- {f['key']}: {f['value']}")
        if query:
            recalls = self.search_messages(user_id, query, limit=max_recalls)
            if recalls:
                parts.append("\n관련 과거 발언:")
                for m in recalls:
                    snippet = (m["content"] or "").strip().replace("\n", " ")
                    if len(snippet) > 120:
                        snippet = snippet[:120] + "…"
                    label = self._format_role_label(m)
                    parts.append(f"- ({label}) {snippet}")
        if not parts:
            return ""
        return "[기억]\n" + "\n".join(parts)


# ── 자동 사실 추출 (한국어 자기소개 패턴) ──────────────────────────
# 사용자가 발화 중 "내 이름은 X" / "내가 좋아하는 건 X" 같은 자기소개 패턴을
# 흘리면 facts 에 자동 upsert 한다. 정규식만 사용 (LLM 호출 0회 — 결정적/저비용).
# 너무 적극적이면 잘못된 사실을 박제할 위험이 있어 가장 흔한 한국어 자기소개 패턴만
# 보수적으로 다룬다.
_FACT_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # 이름 — "내 이름은 X" / "X라고 해" / "저는 X입니다" (X 는 한글 2~10자)
    ("name",       re.compile(r"(?:내|제|나의|저의)\s*이름은\s+(.{1,30})(?:이에요|예요|입니다|이야|야|이다|임|이라고|라고)?\s*[.!?·,]?\s*$")),
    ("name",       re.compile(r"(?:^|\s)(?:나는|저는)\s+(.{1,30})\s*(?:이?라고)\s*해(?:요|s)?\s*[.!?·,]?\s*$")),
    ("name",       re.compile(r"(?:^|\s)(?:나는|저는)\s+([가-힣]{2,10})\s*(?:이에요|예요|입니다|이야|야)\s*[.!?·,]?\s*$")),
    ("nickname",   re.compile(r"(?:나를|저를|내|제)\s+(.{1,20})\s*(?:라고|이라고)\s*불러")),
    ("job",        re.compile(r"(?:나는|저는|내|제)\s*직업은?\s+(.{1,40})(?:이에요|예요|입니다|이야|야|이다)?\s*[.!?·,]?\s*$")),
    ("job",        re.compile(r"(?:나는|저는)\s+(.{1,40})(?:으로|로)\s*일(?:하고\s*있|해|합니다|해요)")),
    ("location",   re.compile(r"(?:나는|저는|내|제가)\s+(.{1,30})에\s*살아?(?:요|아요|고\s*있어요|고\s*있습니다)?\s*[.!?·,]?\s*$")),
    ("birthday",   re.compile(r"(?:내|제)\s*생일은\s+(.{1,30})(?:이에요|예요|입니다|이야|야)?\s*[.!?·,]?\s*$")),
    # favorite/hobby — greedy (lazy + '이?야' 가 '떡볶이야' 를 '떡볶'+'이야' 로 잘못 자르는 문제 해결)
    ("favorite",   re.compile(r"(?:내가|제가)\s*(?:좋아하는|제일\s*좋아하는)\s+(?:건|것은|건요|것은요)?\s*(.{1,40})(?:예요|이에요|입니다|이야|야)\s*[.!?·,]?\s*$")),
    ("hobby",      re.compile(r"(?:내|제)\s*취미는\s+(.{1,40})(?:예요|이에요|입니다|이야|야)\s*[.!?·,]?\s*$")),
    ("language",   re.compile(r"(?:나는|저는)\s+(.{1,20})(?:어|언어)?\s*(?:를|을)\s*(?:한다|쓴다|써요|합니다|해요)\s*[.!?·,]?\s*$")),
]

# 너무 짧거나 LLM 호출어/잡담일 가능성이 높은 값은 거르자.
_FACT_VALUE_BANLIST = {
    "그", "이", "저", "뭐", "뭐야", "응", "음", "어", "아", "예", "네",
    "사비스", "사비스야", "사비스에게", "사비스가",
}

# 추출된 값 끝에 흔히 붙는 한국어 종결어미/조사. 긴 것부터 제거 (탐욕).
_TRAILING_PARTICLES = (
    "이라고", "라고", "이에요", "이예요", "예요", "에요", "입니다",
    "이야", "이다", "이어요", "이고", "이라", "이임", "임", "야", "라", "다",
)


def _strip_trailing_particles(s: str) -> str:
    s = s.strip(" .,!?·\"'`~")
    changed = True
    while changed:
        changed = False
        for p in _TRAILING_PARTICLES:
            if len(s) > len(p) + 1 and s.endswith(p):
                s = s[: -len(p)].rstrip()
                changed = True
                break
    return s


def extract_user_facts(text: str) -> List[Tuple[str, str]]:
    """사용자 발화에서 자기소개성 사실을 (key, value) 리스트로 추출.

    LLM 호출 없이 정규식으로만 동작 — 결정적이고 빠르며 비용 0. 동일 key 가
    여러 번 매칭되면 가장 마지막(가장 길고 구체적인) 값을 채택한다.
    """
    if not text or not isinstance(text, str):
        return []
    s = text.strip()
    if len(s) < 4 or len(s) > 400:
        return []
    found: Dict[str, str] = {}
    for key, pat in _FACT_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        val = _strip_trailing_particles((m.group(1) or "").strip())
        if not val or len(val) < 1 or len(val) > 60:
            continue
        if val in _FACT_VALUE_BANLIST:
            continue
        # 같은 key 가 이미 있으면 더 긴 값(=더 구체적) 우선.
        if key not in found or len(val) > len(found[key]):
            found[key] = val
    return list(found.items())


# 모듈 레벨 싱글톤 (server.py / brain.py 가 import 만으로 사용 가능).
_default: Optional[Memory] = None
_default_lock = threading.Lock()


def get_memory() -> Memory:
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = Memory()
    return _default


def reset_default_for_tests(path: Optional[str] = None) -> Memory:
    """테스트 헬퍼: 기본 싱글톤을 새 path 로 교체."""
    global _default
    with _default_lock:
        _default = Memory(path)
    return _default
