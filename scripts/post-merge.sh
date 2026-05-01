#!/usr/bin/env bash
# 사이클 #9 — task 머지 후 자동 실행되는 setup 스크립트.
# stdin 은 닫혀있으므로 모든 명령은 비대화형이어야 함.
set -euo pipefail

echo "[post-merge] starting at $(date -Iseconds)"

# requirements.txt 가 바뀌었을 때만 pip install (속도 + 멱등성).
# 머지 직전 커밋이 requirements.txt 를 건드렸는지 가볍게 확인.
if git --no-optional-locks diff --name-only HEAD~1 HEAD 2>/dev/null | grep -qx 'requirements.txt'; then
    echo "[post-merge] requirements.txt changed — pip install"
    python -m pip install --quiet --no-input -r requirements.txt
else
    echo "[post-merge] requirements.txt unchanged — skip pip install"
fi

# DB 마이그레이션 없음 (SQLite 자동 생성). 빌드 단계 없음 (Python + 정적 프론트엔드).
# data/ 디렉토리는 telemetry/audit 가 lazy-create 하므로 별도 작업 불필요.

echo "[post-merge] done at $(date -Iseconds)"
