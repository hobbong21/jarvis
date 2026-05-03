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
from pathlib import Path
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

CREATE TABLE IF NOT EXISTS commands (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT    NOT NULL,
    conv_id       INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    kind          TEXT    NOT NULL DEFAULT 'text',
    command_text  TEXT    NOT NULL DEFAULT '',
    image_path    TEXT,
    audio_path    TEXT,
    video_path    TEXT,
    response_text TEXT,
    status        TEXT    NOT NULL DEFAULT 'pending',
    meta_json     TEXT,
    created_at    REAL    NOT NULL,
    completed_at  REAL
);

-- 사이클 #16: 사비스가 학습한 멀티모달 지식 저장공간.
-- facts (key/value 단순 사실) 와 다른 점:
--   * topic + content (자유 서술), tags(JSON 배열) 로 검색 가능한 지식 카드
--   * source 출처 추적 (user|conversation|tool|web|inferred)
--   * 이미지/음성/영상 첨부 가능 — commands 와 같은 파일-경로 패턴 (BLOB 회피)
--   * confidence (0.0~1.0) — 자동 학습 결과의 신뢰도
-- context_block() 가 이 테이블을 LLM 프롬프트에 자동 주입하므로 사비스가
-- 매 답변마다 활용한다.
CREATE TABLE IF NOT EXISTS knowledge (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT    NOT NULL,
    conv_id       INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    topic         TEXT    NOT NULL DEFAULT '',
    content       TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT 'user',
    confidence    REAL    NOT NULL DEFAULT 1.0,
    image_path    TEXT,
    audio_path    TEXT,
    video_path    TEXT,
    tags_json     TEXT,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL
);

-- 사이클 #22 (HARN-12): 사용자 피드백 (👍/👎 + 코멘트). command 단위로 1건.
-- rating: -1 (👎) / +1 (👍) / 0 (취소). UNIQUE(command_id) 로 토글 의미.
CREATE TABLE IF NOT EXISTS command_feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id    INTEGER NOT NULL REFERENCES commands(id) ON DELETE CASCADE,
    user_id       TEXT    NOT NULL,
    rating        INTEGER NOT NULL DEFAULT 0,
    comment       TEXT,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    UNIQUE(command_id)
);

-- 사이클 #23 (HA Stage S1 Read-Only) — Harness Agent 자율 진화 계층.
-- 보조 기획서 §11. 모든 ha_* 테이블은 코드 레벨에서 read-only/append-only 가드.
CREATE TABLE IF NOT EXISTS ha_messages (
    msg_id         TEXT    PRIMARY KEY,
    schema_version TEXT    NOT NULL DEFAULT '1.0',
    from_agent     TEXT    NOT NULL,
    to_agent       TEXT    NOT NULL,
    payload_json   TEXT    NOT NULL,
    signature      TEXT    NOT NULL,
    created_at     REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS ha_issues (
    issue_id        TEXT    PRIMARY KEY,
    category        TEXT    NOT NULL,
    severity        TEXT    NOT NULL,
    evidence_json   TEXT    NOT NULL DEFAULT '[]',
    signal          TEXT,
    narrative       TEXT,
    confidence      REAL    NOT NULL DEFAULT 0.5,
    status          TEXT    NOT NULL DEFAULT 'open',
    created_at      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS ha_kill_switch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    activated_by    TEXT    NOT NULL,
    activated_at    REAL    NOT NULL,
    deactivated_at  REAL,
    reason          TEXT
);

CREATE TABLE IF NOT EXISTS ha_optout (
    user_id        TEXT    PRIMARY KEY,
    opted_out_at   REAL    NOT NULL
);

-- 사이클 #24 (HA Stage S2) — Diagnostician 진단 결과.
CREATE TABLE IF NOT EXISTS ha_diagnoses (
    diagnosis_id        TEXT    PRIMARY KEY,
    issue_id            TEXT    NOT NULL REFERENCES ha_issues(issue_id) ON DELETE CASCADE,
    hypotheses_json     TEXT    NOT NULL DEFAULT '[]',
    root_cause          TEXT,
    confidence          REAL    NOT NULL DEFAULT 0.5,
    recommended_action  TEXT,
    five_whys_json      TEXT    NOT NULL DEFAULT '[]',
    method              TEXT    NOT NULL DEFAULT 'heuristic',
    created_at          REAL    NOT NULL
);

-- 사이클 #24 (architect P0 보완) — append-only DB 강제 트리거.
-- ha_messages / ha_diagnoses / ha_kill_switch_log 는 코드 규약뿐 아니라
-- DB 레벨에서도 UPDATE/DELETE 를 차단해 감사 무결성을 보장한다.
CREATE TRIGGER IF NOT EXISTS trg_ha_messages_no_update
BEFORE UPDATE ON ha_messages
BEGIN SELECT RAISE(ABORT, 'ha_messages is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ha_messages_no_delete
BEFORE DELETE ON ha_messages
BEGIN SELECT RAISE(ABORT, 'ha_messages is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ha_diagnoses_no_update
BEFORE UPDATE ON ha_diagnoses
BEGIN SELECT RAISE(ABORT, 'ha_diagnoses is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ha_diagnoses_no_delete
BEFORE DELETE ON ha_diagnoses
BEGIN SELECT RAISE(ABORT, 'ha_diagnoses is append-only'); END;
-- (참고) ha_kill_switch_log 는 open/close 페어 (deactivated_at UPDATE) 가
-- 설계상 필요해 append-only 트리거 대상에서 제외. 대신 일단 기록된 행은
-- 코드 경로에서만 갱신되며, DELETE 는 평소 일어나지 않는다.
CREATE INDEX IF NOT EXISTS idx_ha_diagnoses_issue ON ha_diagnoses(issue_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ha_issues_status ON ha_issues(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ha_issues_created ON ha_issues(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ha_messages_created ON ha_messages(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_content ON messages(content);
CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_user_ts ON observations(user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_conv_user_started ON conversations(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_timers_user_sched ON timers_events(user_id, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_commands_user_created ON commands(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_user_updated ON knowledge(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_topic ON knowledge(user_id, topic);
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


def _migrate_add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str,
) -> None:
    """기존 DB 에 누락된 컬럼을 idempotent 하게 추가.

    SQLite 는 `ALTER TABLE ADD COLUMN` 에 IF NOT EXISTS 를 지원하지 않으므로
    PRAGMA table_info 로 현재 컬럼 목록을 본 뒤 분기.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    cols = {r["name"] if isinstance(r, sqlite3.Row) else r[1] for r in rows}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
            # 사이클 #15: commands 테이블 audio_path/video_path 마이그레이션.
            # 신규 DB 는 _SCHEMA_SQL 가 처음부터 컬럼 포함 → no-op,
            # 기존 DB (사이클 #14 만 적용된 상태) 는 여기서 채워진다.
            _migrate_add_column_if_missing(conn, "commands", "audio_path", "TEXT")
            _migrate_add_column_if_missing(conn, "commands", "video_path", "TEXT")
            # 사이클 #22 (HARN-12): 레거시 DB 에 command_feedback 테이블 보장.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS command_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id INTEGER NOT NULL REFERENCES commands(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL,
                    rating INTEGER NOT NULL DEFAULT 0,
                    comment TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(command_id)
                )
            """)
            # 사이클 #23 (HA Stage S1): 레거시 DB 에 ha_* 테이블 보장.
            for ddl in (
                """CREATE TABLE IF NOT EXISTS ha_messages (
                    msg_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL DEFAULT '1.0',
                    from_agent TEXT NOT NULL,
                    to_agent TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    created_at REAL NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS ha_issues (
                    issue_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    signal TEXT,
                    narrative TEXT,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at REAL NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS ha_kill_switch_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activated_by TEXT NOT NULL,
                    activated_at REAL NOT NULL,
                    deactivated_at REAL,
                    reason TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS ha_optout (
                    user_id TEXT PRIMARY KEY,
                    opted_out_at REAL NOT NULL
                )""",
                "CREATE INDEX IF NOT EXISTS idx_ha_issues_created ON ha_issues(created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_ha_messages_created ON ha_messages(created_at DESC)",
                # 사이클 #24
                """CREATE TABLE IF NOT EXISTS ha_diagnoses (
                    diagnosis_id TEXT PRIMARY KEY,
                    issue_id TEXT NOT NULL REFERENCES ha_issues(issue_id) ON DELETE CASCADE,
                    hypotheses_json TEXT NOT NULL DEFAULT '[]',
                    root_cause TEXT,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    recommended_action TEXT,
                    five_whys_json TEXT NOT NULL DEFAULT '[]',
                    method TEXT NOT NULL DEFAULT 'heuristic',
                    created_at REAL NOT NULL
                )""",
                "CREATE INDEX IF NOT EXISTS idx_ha_diagnoses_issue ON ha_diagnoses(issue_id, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_ha_issues_status ON ha_issues(status, created_at DESC)",
                # append-only 강제 트리거 (architect P0 보완)
                "CREATE TRIGGER IF NOT EXISTS trg_ha_messages_no_update "
                "BEFORE UPDATE ON ha_messages BEGIN "
                "SELECT RAISE(ABORT, 'ha_messages is append-only'); END",
                "CREATE TRIGGER IF NOT EXISTS trg_ha_messages_no_delete "
                "BEFORE DELETE ON ha_messages BEGIN "
                "SELECT RAISE(ABORT, 'ha_messages is append-only'); END",
                "CREATE TRIGGER IF NOT EXISTS trg_ha_diagnoses_no_update "
                "BEFORE UPDATE ON ha_diagnoses BEGIN "
                "SELECT RAISE(ABORT, 'ha_diagnoses is append-only'); END",
                "CREATE TRIGGER IF NOT EXISTS trg_ha_diagnoses_no_delete "
                "BEFORE DELETE ON ha_diagnoses BEGIN "
                "SELECT RAISE(ABORT, 'ha_diagnoses is append-only'); END",
            ):
                conn.execute(ddl)
            # 사이클 #16: knowledge 테이블이 없는 레거시 DB 도 자동 생성.
            # _SCHEMA_SQL 의 CREATE TABLE IF NOT EXISTS 가 이미 처리하지만,
            # 인덱스도 함께 ensure 한다 (no-op for fresh DBs).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_user_updated "
                "ON knowledge(user_id, updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_topic "
                "ON knowledge(user_id, topic)"
            )
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
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chromadb"
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

    # ── 명령 로그 (멀티모달: 텍스트 + 이미지) ──────────────────────
    # 사용자가 사비스에게 시킨 일과 그에 따른 이미지를 영구 저장한다.
    # 이미지 바이트는 별도 파일(예: data/commands/<id>.jpg)로 저장하고
    # 경로만 image_path 컬럼에 기록 — SQLite BLOB 으로 큰 이진 데이터를
    # 들고 다니지 않기 위함.
    def log_command(
        self,
        user_id: str,
        command_text: str,
        kind: str = "text",
        image_path: Optional[str] = None,
        conv_id: Optional[int] = None,
        status: str = "pending",
        meta: Optional[Dict[str, Any]] = None,
        audio_path: Optional[str] = None,
        video_path: Optional[str] = None,
    ) -> int:
        if kind not in ("text", "voice", "image", "audio", "video", "multimodal"):
            raise ValueError(f"invalid kind: {kind}")
        if status not in ("pending", "done", "error"):
            raise ValueError(f"invalid status: {status}")
        if not isinstance(command_text, str):
            command_text = str(command_text)
        meta_blob = json.dumps(meta, ensure_ascii=False) if meta else None
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                """
                INSERT INTO commands(user_id, conv_id, kind, command_text,
                                     image_path, audio_path, video_path,
                                     status, meta_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, conv_id, kind, command_text, image_path, audio_path,
                 video_path, status, meta_blob, time.time()),
            )
            return int(cur.lastrowid)

    def update_command(
        self,
        cmd_id: int,
        response_text: Optional[str] = None,
        status: Optional[str] = None,
        image_path: Optional[str] = None,
        audio_path: Optional[str] = None,
        video_path: Optional[str] = None,
    ) -> bool:
        if status is not None and status not in ("pending", "done", "error"):
            raise ValueError(f"invalid status: {status}")
        sets: List[str] = []
        args: List[Any] = []
        if response_text is not None:
            sets.append("response_text=?"); args.append(response_text)
        if status is not None:
            sets.append("status=?"); args.append(status)
            if status in ("done", "error"):
                sets.append("completed_at=?"); args.append(time.time())
        if image_path is not None:
            sets.append("image_path=?"); args.append(image_path)
        if audio_path is not None:
            sets.append("audio_path=?"); args.append(audio_path)
        if video_path is not None:
            sets.append("video_path=?"); args.append(video_path)
        if not sets:
            return False
        args.append(int(cmd_id))
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                f"UPDATE commands SET {', '.join(sets)} WHERE id=?",
                args,
            )
            return cur.rowcount > 0

    def get_command(self, cmd_id: int) -> Optional[Dict[str, Any]]:
        with _conn_ctx(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM commands WHERE id=?", (int(cmd_id),)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("meta_json"):
            try:
                d["meta"] = json.loads(d["meta_json"])
            except json.JSONDecodeError:
                d["meta"] = {}
        else:
            d["meta"] = {}
        d.pop("meta_json", None)
        return d

    def recent_commands(
        self,
        user_id: str,
        limit: int = 50,
        kind: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with _conn_ctx(self.path) as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM commands WHERE user_id=? AND kind=? "
                    "ORDER BY id DESC LIMIT ?",
                    (user_id, kind, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM commands WHERE user_id=? "
                    "ORDER BY id DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if d.get("meta_json"):
                try:
                    d["meta"] = json.loads(d["meta_json"])
                except json.JSONDecodeError:
                    d["meta"] = {}
            else:
                d["meta"] = {}
            d.pop("meta_json", None)
            out.append(d)
        return out

    def delete_command(self, cmd_id: int) -> bool:
        """행 삭제 + image/audio/video 파일도 best-effort 정리."""
        with _conn_ctx(self.path) as conn:
            row = conn.execute(
                "SELECT image_path, audio_path, video_path FROM commands WHERE id=?",
                (int(cmd_id),),
            ).fetchone()
            if not row:
                return False
            paths = [row["image_path"], row["audio_path"], row["video_path"]]
            conn.execute("DELETE FROM commands WHERE id=?", (int(cmd_id),))
        for p in paths:
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return True

    # ── 사이클 #22 (HARN-12 + HARN-05): 사용자 피드백 + My Sarvis 요약 ──
    def set_feedback(
        self,
        command_id: int,
        user_id: str,
        rating: int,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """command 1건에 대한 👍(+1)/👎(-1)/취소(0) + 코멘트 기록 (upsert).

        같은 command_id 에 다시 호출 시 rating/comment 갱신.
        rating: -1 | 0 | +1 외 값은 ValueError.
        """
        try:
            r = int(rating)
        except (TypeError, ValueError):
            raise ValueError(f"invalid rating: {rating!r}")
        if r not in (-1, 0, 1):
            raise ValueError(f"rating must be -1|0|+1, got {r}")
        if comment is not None and not isinstance(comment, str):
            comment = str(comment)
        if comment is not None:
            comment = comment.strip()[:1000] or None
        now = time.time()
        with _conn_ctx(self.path) as conn:
            row = conn.execute(
                "SELECT id FROM commands WHERE id=?", (int(command_id),),
            ).fetchone()
            if not row:
                raise ValueError(f"command not found: {command_id}")
            cur = conn.execute(
                """
                INSERT INTO command_feedback(command_id, user_id, rating,
                                             comment, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(command_id) DO UPDATE SET
                    rating=excluded.rating,
                    comment=excluded.comment,
                    updated_at=excluded.updated_at
                """,
                (int(command_id), user_id, r, comment, now, now),
            )
            fb_row = conn.execute(
                "SELECT * FROM command_feedback WHERE command_id=?",
                (int(command_id),),
            ).fetchone()
        return dict(fb_row) if fb_row else {}

    def get_feedback(self, command_id: int) -> Optional[Dict[str, Any]]:
        with _conn_ctx(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM command_feedback WHERE command_id=?",
                (int(command_id),),
            ).fetchone()
        return dict(row) if row else None

    def my_sarvis_summary(
        self, user_id: str, window_sec: float = 7 * 86400.0,
    ) -> Dict[str, Any]:
        """사용자용 'My Sarvis' 패널용 집계 (기획서 17.5.2).

        - 최근 window 내 명령 수, 만족도(👍/👎 비율), Top5 종류, 저장 용량.
        """
        cutoff = time.time() - max(60.0, float(window_sec))
        with _conn_ctx(self.path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM commands "
                "WHERE user_id=? AND created_at>=?",
                (user_id, cutoff),
            ).fetchone()["n"]
            kind_rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM commands "
                "WHERE user_id=? AND created_at>=? "
                "GROUP BY kind ORDER BY n DESC LIMIT 5",
                (user_id, cutoff),
            ).fetchall()
            fb_rows = conn.execute(
                "SELECT f.rating AS r FROM command_feedback f "
                "JOIN commands c ON c.id=f.command_id "
                "WHERE c.user_id=? AND f.updated_at>=?",
                (user_id, cutoff),
            ).fetchall()
            err_n = conn.execute(
                "SELECT COUNT(*) AS n FROM commands "
                "WHERE user_id=? AND created_at>=? AND status='error'",
                (user_id, cutoff),
            ).fetchone()["n"]
            recent_neg = conn.execute(
                """
                SELECT c.id AS cmd_id, c.command_text, c.kind, c.created_at,
                       f.rating, f.comment, f.updated_at AS fb_at
                FROM command_feedback f
                JOIN commands c ON c.id=f.command_id
                WHERE c.user_id=? AND f.rating<0 AND f.updated_at>=?
                ORDER BY f.updated_at DESC LIMIT 5
                """,
                (user_id, cutoff),
            ).fetchall()
        up = sum(1 for r in fb_rows if r["r"] > 0)
        dn = sum(1 for r in fb_rows if r["r"] < 0)
        rated = up + dn
        sat_pct = (100.0 * up / rated) if rated else None
        # 저장 용량 (best-effort): data/ + chromadb/ 디스크 사용량
        storage_mb = 0.0
        try:
            root = Path(self.path).resolve().parent
            for p in root.rglob("*"):
                if p.is_file():
                    try:
                        storage_mb += p.stat().st_size
                    except OSError:
                        pass
            storage_mb = round(storage_mb / (1024 * 1024), 2)
        except Exception:
            storage_mb = 0.0
        return {
            "window_days": round(window_sec / 86400.0, 1),
            "command_count": int(total),
            "error_count": int(err_n),
            "feedback": {
                "up": up, "down": dn, "rated": rated,
                "satisfaction_pct": sat_pct,
            },
            "top_kinds": [{"kind": r["kind"], "n": r["n"]} for r in kind_rows],
            "recent_negative": [dict(r) for r in recent_neg],
            "storage_mb": storage_mb,
        }

    # ── 사이클 #23 (HA Stage S1) — Harness Agent 데이터 게이트 ───────
    # 모든 메서드는 HA 모듈의 read/write scope 가드와 별개로 코드 레벨
    # 검증을 다시 수행 (다층 방어). ha_messages 는 INSERT 만 허용.
    def ha_message_append(
        self,
        msg_id: str,
        from_agent: str,
        to_agent: str,
        payload: Dict[str, Any],
        signature: str,
        schema_version: str = "1.0",
    ) -> None:
        """HA 에이전트 간 메시지를 append-only 로 기록."""
        if not msg_id or not isinstance(msg_id, str):
            raise ValueError("msg_id 필요")
        if from_agent == to_agent:
            raise ValueError("from_agent == to_agent 금지")
        with _conn_ctx(self.path) as conn:
            try:
                conn.execute(
                    "INSERT INTO ha_messages(msg_id, schema_version, from_agent, "
                    "to_agent, payload_json, signature, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (msg_id, schema_version, from_agent, to_agent,
                     json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     signature, time.time()),
                )
            except sqlite3.IntegrityError as ex:
                raise ValueError(f"msg_id 중복: {msg_id}") from ex

    def ha_messages_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with _conn_ctx(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM ha_messages ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("payload_json", "null"))
            except json.JSONDecodeError:
                d["payload"] = None
            out.append(d)
        return out

    def ha_issue_insert(
        self,
        issue_id: str,
        category: str,
        severity: str,
        evidence: List[Any],
        signal: Optional[str],
        narrative: str,
        confidence: float,
    ) -> None:
        if severity not in ("critical", "high", "medium", "low", "info"):
            raise ValueError(f"invalid severity: {severity}")
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            raise ValueError("invalid confidence")
        if not (0.0 <= conf <= 1.0):
            raise ValueError(f"confidence out of range: {conf}")
        with _conn_ctx(self.path) as conn:
            try:
                conn.execute(
                    "INSERT INTO ha_issues(issue_id, category, severity, "
                    "evidence_json, signal, narrative, confidence, status, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                    (issue_id, category, severity,
                     json.dumps(evidence, ensure_ascii=False),
                     signal, narrative, conf, time.time()),
                )
            except sqlite3.IntegrityError as ex:
                raise ValueError(f"issue_id 중복: {issue_id}") from ex

    def ha_issues_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with _conn_ctx(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM ha_issues ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["evidence"] = json.loads(d.pop("evidence_json", "[]"))
            except json.JSONDecodeError:
                d["evidence"] = []
            out.append(d)
        return out

    def ha_optout_set(self, user_id: str, on: bool) -> bool:
        if not user_id:
            raise ValueError("user_id 필요")
        with _conn_ctx(self.path) as conn:
            if on:
                conn.execute(
                    "INSERT OR REPLACE INTO ha_optout(user_id, opted_out_at) "
                    "VALUES (?, ?)",
                    (user_id, time.time()),
                )
                return True
            else:
                conn.execute("DELETE FROM ha_optout WHERE user_id=?", (user_id,))
                return False

    def ha_is_opted_out(self, user_id: str) -> bool:
        with _conn_ctx(self.path) as conn:
            r = conn.execute(
                "SELECT 1 FROM ha_optout WHERE user_id=?", (user_id,),
            ).fetchone()
        return r is not None

    def ha_kill_switch_log_open(self, activated_by: str, reason: str) -> int:
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO ha_kill_switch_log(activated_by, activated_at, reason) "
                "VALUES (?, ?, ?)",
                (activated_by, time.time(), reason),
            )
            return int(cur.lastrowid)

    def ha_kill_switch_log_close(self, deactivated_by: str = "owner") -> bool:
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                "UPDATE ha_kill_switch_log SET deactivated_at=? "
                "WHERE deactivated_at IS NULL",
                (time.time(),),
            )
            return cur.rowcount > 0

    def ha_issues_open(self, limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with _conn_ctx(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM ha_issues WHERE status='open' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["evidence"] = json.loads(d.pop("evidence_json", "[]"))
            except json.JSONDecodeError:
                d["evidence"] = []
            out.append(d)
        return out

    def ha_issue_set_status(self, issue_id: str, status: str) -> bool:
        if status not in ("open", "diagnosed", "in_progress",
                          "resolved", "rejected"):
            raise ValueError(f"invalid status: {status}")
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                "UPDATE ha_issues SET status=? WHERE issue_id=?",
                (status, issue_id),
            )
            return cur.rowcount > 0

    def ha_diagnosis_insert(
        self,
        diagnosis_id: str,
        issue_id: str,
        hypotheses: List[Dict[str, Any]],
        root_cause: Optional[str],
        confidence: float,
        recommended_action: Optional[str],
        five_whys: Optional[List[str]] = None,
        method: str = "heuristic",
    ) -> None:
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            raise ValueError("invalid confidence")
        if not (0.0 <= conf <= 1.0):
            raise ValueError(f"confidence out of range: {conf}")
        whys = list(five_whys or [])
        with _conn_ctx(self.path) as conn:
            try:
                conn.execute(
                    "INSERT INTO ha_diagnoses(diagnosis_id, issue_id, "
                    "hypotheses_json, root_cause, confidence, "
                    "recommended_action, five_whys_json, method, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (diagnosis_id, issue_id,
                     json.dumps(hypotheses, ensure_ascii=False),
                     root_cause, conf, recommended_action,
                     json.dumps(whys, ensure_ascii=False),
                     method, time.time()),
                )
            except sqlite3.IntegrityError as ex:
                raise ValueError(f"diagnosis_id 중복: {diagnosis_id}") from ex

    def ha_diagnoses_for_issue(
        self, issue_id: str, limit: int = 5,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        with _conn_ctx(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM ha_diagnoses WHERE issue_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (issue_id, limit),
            ).fetchall()
        return [self._row_to_diagnosis(r) for r in rows]

    def ha_diagnoses_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with _conn_ctx(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM ha_diagnoses ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_diagnosis(r) for r in rows]

    @staticmethod
    def _row_to_diagnosis(r) -> Dict[str, Any]:
        d = dict(r)
        try:
            d["hypotheses"] = json.loads(d.pop("hypotheses_json", "[]"))
        except json.JSONDecodeError:
            d["hypotheses"] = []
        try:
            d["five_whys"] = json.loads(d.pop("five_whys_json", "[]"))
        except (json.JSONDecodeError, KeyError):
            d["five_whys"] = []
        return d

    def ha_observer_input(
        self, window_sec: float = 24 * 3600.0, exclude_optout: bool = True,
    ) -> Dict[str, Any]:
        """Observer 입력 — PII 마스킹은 호출자 책임. 옵트아웃 사용자 제외."""
        cutoff = time.time() - max(60.0, float(window_sec))
        with _conn_ctx(self.path) as conn:
            join_clause = (
                "LEFT JOIN ha_optout o ON o.user_id=c.user_id"
                if exclude_optout else ""
            )
            where_optout = "AND o.user_id IS NULL" if exclude_optout else ""
            rows = conn.execute(
                f"""
                SELECT c.id, c.user_id, c.kind, c.command_text, c.response_text,
                       c.status, c.meta_json, c.created_at, c.completed_at,
                       f.rating, f.comment
                FROM commands c
                {join_clause}
                LEFT JOIN command_feedback f ON f.command_id = c.id
                WHERE c.created_at >= ? {where_optout}
                ORDER BY c.created_at DESC LIMIT 5000
                """,
                (cutoff,),
            ).fetchall()
            # 베이스라인 (28일 vs 7일 만족도) — 옵트아웃 제외 일관 적용
            cutoff_28 = time.time() - 28 * 86400.0
            cutoff_7 = time.time() - 7 * 86400.0
            base = conn.execute(
                f"""
                SELECT
                  AVG(CASE WHEN c.created_at >= ? THEN f.rating END) AS sat_7d,
                  AVG(CASE WHEN c.created_at >= ? THEN f.rating END) AS sat_28d,
                  COUNT(CASE WHEN c.created_at >= ? THEN 1 END) AS n_7d,
                  COUNT(CASE WHEN c.created_at >= ? THEN 1 END) AS n_28d
                FROM commands c
                {join_clause}
                LEFT JOIN command_feedback f ON f.command_id = c.id
                WHERE c.created_at >= ? {where_optout}
                """,
                (cutoff_7, cutoff_28, cutoff_7, cutoff_28, cutoff_28),
            ).fetchone()
        traces = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d.pop("meta_json", "null") or "null")
            except json.JSONDecodeError:
                d["meta"] = None
            traces.append(d)
        return {
            "window_sec": window_sec,
            "traces": traces,
            "baseline": {
                "sat_7d": base["sat_7d"], "sat_28d": base["sat_28d"],
                "n_7d": int(base["n_7d"] or 0),
                "n_28d": int(base["n_28d"] or 0),
            },
        }

    # ── 멀티모달 학습 지식 (knowledge) ──────────────────────────────
    # 사비스가 시간이 지나며 쌓는 "알게 된 것" 들을 영구 저장하는 자유 서술
    # 카드. facts 가 key=value 형태의 단순 사실이라면 knowledge 는 topic +
    # 풍부한 content + 첨부 미디어(이미지/음성/영상) + tags + confidence 로
    # 구성된다. context_block() 가 자동으로 LLM 프롬프트에 끌어다 주입.
    _KNOWLEDGE_SOURCES = ("user", "conversation", "tool", "web", "inferred")

    def add_knowledge(
        self,
        user_id: str,
        content: str,
        topic: str = "",
        source: str = "user",
        confidence: float = 1.0,
        image_path: Optional[str] = None,
        audio_path: Optional[str] = None,
        video_path: Optional[str] = None,
        tags: Optional[List[str]] = None,
        conv_id: Optional[int] = None,
    ) -> int:
        if source not in self._KNOWLEDGE_SOURCES:
            raise ValueError(f"invalid source: {source}")
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            raise ValueError(f"invalid confidence: {confidence!r}")
        if not (0.0 <= conf <= 1.0):
            raise ValueError(f"confidence out of range: {conf}")
        if not isinstance(content, str):
            content = str(content)
        if not isinstance(topic, str):
            topic = str(topic)
        tags_blob = json.dumps(tags, ensure_ascii=False) if tags else None
        now = time.time()
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                """
                INSERT INTO knowledge(user_id, conv_id, topic, content, source,
                                      confidence, image_path, audio_path,
                                      video_path, tags_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, conv_id, topic, content, source, conf,
                 image_path, audio_path, video_path, tags_blob, now, now),
            )
            return int(cur.lastrowid)

    def update_knowledge(
        self,
        kid: int,
        content: Optional[str] = None,
        topic: Optional[str] = None,
        confidence: Optional[float] = None,
        image_path: Optional[str] = None,
        audio_path: Optional[str] = None,
        video_path: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> bool:
        sets: List[str] = []
        args: List[Any] = []
        if content is not None:
            sets.append("content=?"); args.append(str(content))
        if topic is not None:
            sets.append("topic=?"); args.append(str(topic))
        if confidence is not None:
            try:
                conf = float(confidence)
            except (TypeError, ValueError):
                raise ValueError(f"invalid confidence: {confidence!r}")
            if not (0.0 <= conf <= 1.0):
                raise ValueError(f"confidence out of range: {conf}")
            sets.append("confidence=?"); args.append(conf)
        if image_path is not None:
            sets.append("image_path=?"); args.append(image_path)
        if audio_path is not None:
            sets.append("audio_path=?"); args.append(audio_path)
        if video_path is not None:
            sets.append("video_path=?"); args.append(video_path)
        if tags is not None:
            sets.append("tags_json=?"); args.append(
                json.dumps(tags, ensure_ascii=False) if tags else None
            )
        if not sets:
            return False
        sets.append("updated_at=?"); args.append(time.time())
        args.append(int(kid))
        with _conn_ctx(self.path) as conn:
            cur = conn.execute(
                f"UPDATE knowledge SET {', '.join(sets)} WHERE id=?", args,
            )
            return cur.rowcount > 0

    @staticmethod
    def _knowledge_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        if d.get("tags_json"):
            try:
                d["tags"] = json.loads(d["tags_json"])
            except json.JSONDecodeError:
                d["tags"] = []
        else:
            d["tags"] = []
        d.pop("tags_json", None)
        return d

    def get_knowledge(self, kid: int) -> Optional[Dict[str, Any]]:
        with _conn_ctx(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM knowledge WHERE id=?", (int(kid),)
            ).fetchone()
        return self._knowledge_row_to_dict(row) if row else None

    def recent_knowledge(
        self,
        user_id: str,
        limit: int = 20,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with _conn_ctx(self.path) as conn:
            if source:
                rows = conn.execute(
                    "SELECT * FROM knowledge WHERE user_id=? AND source=? "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (user_id, source, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM knowledge WHERE user_id=? "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
        return [self._knowledge_row_to_dict(r) for r in rows]

    def search_knowledge(
        self,
        user_id: str,
        query: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """topic / content / tags_json 에 대한 단순 LIKE 검색.

        한국어/영어 혼용 발화도 부분 문자열로 매칭된다. 임베딩이나 FTS 를
        쓰지 않아 비용이 0 이고 결정적. 미래에 FTS5 로 업그레이드 가능.
        """
        q = (query or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        limit = max(1, min(int(limit), 200))
        with _conn_ctx(self.path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM knowledge
                WHERE user_id=?
                  AND (topic LIKE ? OR content LIKE ? OR IFNULL(tags_json,'') LIKE ?)
                ORDER BY confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (user_id, like, like, like, limit),
            ).fetchall()
        return [self._knowledge_row_to_dict(r) for r in rows]

    def delete_knowledge(self, kid: int) -> bool:
        """행 삭제 + 첨부 미디어 파일 best-effort 정리."""
        with _conn_ctx(self.path) as conn:
            row = conn.execute(
                "SELECT image_path, audio_path, video_path FROM knowledge WHERE id=?",
                (int(kid),),
            ).fetchone()
            if not row:
                return False
            paths = [row["image_path"], row["audio_path"], row["video_path"]]
            conn.execute("DELETE FROM knowledge WHERE id=?", (int(kid),))
        for p in paths:
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return True

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
        max_knowledge: int = 4,
    ) -> str:
        """LLM system prompt 에 [기억:...] 블록으로 주입할 한국어 컨텍스트 문자열.

        - max_facts 개의 최근 facts (key: value)
        - 새 발화 query 와 키워드 매칭되는 과거 메시지 max_recalls 개
        - 사이클 #16: max_knowledge 개의 학습 지식 카드 (query 가 있으면
          search_knowledge, 없으면 recent_knowledge). 첨부 미디어가 있으면
          [이미지]/[음성]/[영상] 마커로 표시 — LLM 이 미디어 존재를 인지하도록.
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
        if max_knowledge > 0:
            try:
                if query:
                    kn = self.search_knowledge(user_id, query, limit=max_knowledge)
                else:
                    kn = []
                if not kn:
                    kn = self.recent_knowledge(user_id, limit=max_knowledge)
            except sqlite3.Error:
                kn = []
            if kn:
                parts.append("\n학습한 지식:")
                for k in kn:
                    topic = (k.get("topic") or "").strip()
                    content = (k.get("content") or "").strip().replace("\n", " ")
                    if len(content) > 160:
                        content = content[:160] + "…"
                    media: List[str] = []
                    if k.get("image_path"): media.append("이미지")
                    if k.get("audio_path"): media.append("음성")
                    if k.get("video_path"): media.append("영상")
                    media_tag = f" [{'/'.join(media)} 첨부]" if media else ""
                    head = f"{topic}: " if topic else ""
                    parts.append(f"- {head}{content}{media_tag}")
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
