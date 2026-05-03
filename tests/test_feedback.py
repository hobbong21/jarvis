"""사이클 #22 (HARN-12 + HARN-05) — feedback + my_sarvis_summary 단위 테스트."""
from __future__ import annotations

import os
import time

import pytest

from sarvis.memory import Memory


@pytest.fixture
def mem(tmp_path) -> Memory:
    db = tmp_path / "memory.db"
    return Memory(path=str(db))


def _cmd(mem: Memory, user="owner", text="hi", kind="text", status="done"):
    return mem.log_command(user_id=user, command_text=text, kind=kind, status=status)


# ── set_feedback / get_feedback ──────────────────────────────────
def test_set_feedback_basic_up(mem):
    cid = _cmd(mem)
    fb = mem.set_feedback(cid, "owner", 1)
    assert fb["rating"] == 1 and fb["command_id"] == cid
    got = mem.get_feedback(cid)
    assert got["rating"] == 1


def test_set_feedback_with_comment(mem):
    cid = _cmd(mem)
    mem.set_feedback(cid, "owner", -1, "톤이 너무 딱딱해")
    got = mem.get_feedback(cid)
    assert got["rating"] == -1
    assert got["comment"] == "톤이 너무 딱딱해"


def test_set_feedback_upsert_overwrites(mem):
    cid = _cmd(mem)
    mem.set_feedback(cid, "owner", -1, "별로")
    mem.set_feedback(cid, "owner", 1, None)
    got = mem.get_feedback(cid)
    assert got["rating"] == 1
    assert got["comment"] is None


def test_set_feedback_zero_cancel(mem):
    cid = _cmd(mem)
    mem.set_feedback(cid, "owner", 1)
    fb = mem.set_feedback(cid, "owner", 0)
    assert fb["rating"] == 0


def test_set_feedback_invalid_rating(mem):
    cid = _cmd(mem)
    for bad in (2, -2, 99, "x"):
        with pytest.raises(ValueError):
            mem.set_feedback(cid, "owner", bad)


def test_set_feedback_unknown_command(mem):
    with pytest.raises(ValueError):
        mem.set_feedback(99999, "owner", 1)


def test_set_feedback_truncates_long_comment(mem):
    cid = _cmd(mem)
    long = "가" * 5000
    mem.set_feedback(cid, "owner", -1, long)
    got = mem.get_feedback(cid)
    assert len(got["comment"]) == 1000


def test_get_feedback_none_for_unrated(mem):
    cid = _cmd(mem)
    assert mem.get_feedback(cid) is None


def test_feedback_cascade_on_command_delete(mem):
    cid = _cmd(mem)
    mem.set_feedback(cid, "owner", 1)
    mem.delete_command(cid)
    assert mem.get_feedback(cid) is None


# ── my_sarvis_summary ────────────────────────────────────────────
def test_my_sarvis_empty_state(mem):
    s = mem.my_sarvis_summary("owner")
    assert s["command_count"] == 0
    assert s["error_count"] == 0
    assert s["feedback"]["rated"] == 0
    assert s["feedback"]["satisfaction_pct"] is None
    assert s["top_kinds"] == []
    assert s["recent_negative"] == []
    assert s["window_days"] == 7.0
    assert s["storage_mb"] >= 0.0


def test_my_sarvis_counts_commands_within_window(mem):
    for _ in range(3):
        _cmd(mem)
    _cmd(mem, kind="voice")
    _cmd(mem, kind="image")
    s = mem.my_sarvis_summary("owner")
    assert s["command_count"] == 5


def test_my_sarvis_top_kinds_sorted(mem):
    for _ in range(4):
        _cmd(mem, kind="text")
    for _ in range(2):
        _cmd(mem, kind="voice")
    _cmd(mem, kind="image")
    s = mem.my_sarvis_summary("owner")
    kinds = [k["kind"] for k in s["top_kinds"]]
    assert kinds[0] == "text"
    assert "voice" in kinds and "image" in kinds


def test_my_sarvis_satisfaction_pct(mem):
    ids = [_cmd(mem) for _ in range(5)]
    mem.set_feedback(ids[0], "owner", 1)
    mem.set_feedback(ids[1], "owner", 1)
    mem.set_feedback(ids[2], "owner", 1)
    mem.set_feedback(ids[3], "owner", -1)
    # ids[4] rated=0 → counted as neither
    mem.set_feedback(ids[4], "owner", 0)
    s = mem.my_sarvis_summary("owner")
    fb = s["feedback"]
    assert fb["up"] == 3
    assert fb["down"] == 1
    assert fb["rated"] == 4
    assert abs(fb["satisfaction_pct"] - 75.0) < 0.01


def test_my_sarvis_error_count(mem):
    _cmd(mem, status="done")
    _cmd(mem, status="error")
    _cmd(mem, status="error")
    s = mem.my_sarvis_summary("owner")
    assert s["command_count"] == 3
    assert s["error_count"] == 2


def test_my_sarvis_recent_negative_includes_comment(mem):
    cid = _cmd(mem, text="이상한 응답")
    mem.set_feedback(cid, "owner", -1, "더 친절해줘")
    s = mem.my_sarvis_summary("owner")
    assert len(s["recent_negative"]) == 1
    rec = s["recent_negative"][0]
    assert rec["cmd_id"] == cid
    assert rec["comment"] == "더 친절해줘"
    assert rec["rating"] == -1


def test_my_sarvis_excludes_other_users(mem):
    _cmd(mem, user="owner")
    _cmd(mem, user="guest")
    _cmd(mem, user="guest")
    s = mem.my_sarvis_summary("owner")
    assert s["command_count"] == 1


def test_my_sarvis_excludes_old_commands(mem):
    cid = _cmd(mem)
    # Backdate via direct SQL to simulate old row outside window.
    import sqlite3
    with sqlite3.connect(mem.path) as conn:
        conn.execute(
            "UPDATE commands SET created_at=? WHERE id=?",
            (time.time() - 30 * 86400.0, cid),
        )
        conn.commit()
    s = mem.my_sarvis_summary("owner", window_sec=7 * 86400.0)
    assert s["command_count"] == 0


def test_my_sarvis_window_days_clamped_to_minimum(mem):
    # 0초 입력은 60초로 clamp 되어 결과 안전 (key 존재).
    s = mem.my_sarvis_summary("owner", window_sec=0.0)
    assert "window_days" in s
    assert s["window_days"] >= 0.0


def test_my_sarvis_recent_negative_limits_to_5(mem):
    for i in range(8):
        cid = _cmd(mem, text=f"명령 {i}")
        mem.set_feedback(cid, "owner", -1, f"코멘트 {i}")
    s = mem.my_sarvis_summary("owner")
    assert len(s["recent_negative"]) == 5
