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
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

DB_PATH = os.environ.get("SARVIS_MEMORY_DB", "memory.db")

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
# Memory: 인스턴스 객체. 테스트에서 별도 path 로 분리 가능.
# ────────────────────────────────────────────────────────────────────
class Memory:
    """SARVIS 장기 메모리 게이트웨이. SQLite 단일 파일 백엔드."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or DB_PATH
        _ensure_schema(self.path)

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
            return int(cur.lastrowid)

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
        """키워드(LIKE) 기반 검색. v2.0 2단계에서 임베딩 검색으로 업그레이드 예정."""
        q = (query or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        limit = max(1, min(int(limit), 50))
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
