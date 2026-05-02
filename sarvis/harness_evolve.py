"""/harness:evolve — 텔레메트리 누적 데이터를 기반으로 차세대 Harness 초안 자동 제안.

흐름:
  1. telemetry.summarize() + telemetry.recent(N) 수집
  2. 사이클 번호 계산 (harness/sarvis/proposals/cycle-*.md 카운트 + 1)
  3. LLM (Anthropic 우선, 없으면 OpenAI) 에 분석 프롬프트 전송
  4. 결과 markdown 을 harness/sarvis/proposals/cycle-{n}.md 에 저장
  5. {ok, cycle, path, markdown, reason} 반환

PII 안전: 메타데이터(intent/backend/지연 등)만 LLM 에 전달, 사용자 발화 본문 미포함.

사이클 #5 T003: export_proposal_to_github() — proposal 을 GitHub Issue 로 export.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from . import telemetry
from .config import cfg

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROPOSALS_DIR = _PROJECT_ROOT / "harness" / "sarvis" / "proposals"
MIN_TURNS = 10  # 최소 누적 턴 — 데모용 낮음. 운영은 100+ 권장.

# 사이클 #5: GitHub Issue body 최대 길이 (GitHub 한도 65536, 안전 마진).
GH_BODY_MAX = 60000


def _next_cycle_number() -> int:
    """기존 cycle-{n}.md 중 최대 n + 1. 없으면 3 (현재 작업 중인 사이클 #3 다음)."""
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    nums = []
    for p in PROPOSALS_DIR.glob("cycle-*.md"):
        m = re.match(r"cycle-(\d+)\.md", p.name)
        if m:
            try:
                nums.append(int(m.group(1)))
            except ValueError:
                continue
    return (max(nums) + 1) if nums else 4


def _build_prompt(summary: Dict, recent: list) -> str:
    """LLM 입력 프롬프트. 메타데이터만 직렬화, 본문 없음."""
    summary_json = json.dumps(summary, ensure_ascii=False, indent=2)
    # recent 의 각 항목에서 *_len 류 메타만 보존 (이미 sanitize 된 상태이지만 한 번 더 필터)
    safe_recent = []
    for r in recent[-30:]:  # 최대 30개
        safe_recent.append({
            k: v for k, v in r.items()
            if k in {
                "ts", "backend", "fallback_used", "fallback_chain", "intent",
                "emotion", "fanout_ms", "llm_ms", "tts_ms", "tts_ok", "tts_reason",
                "tts_regenerated", "prompt_len", "reply_len", "compare_sources",
            }
        })

    return (
        "당신은 SARVIS Harness 의 차세대 진화 초안 작성자입니다.\n"
        "아래는 SARVIS 음성 AI 시스템의 운영 텔레메트리 데이터입니다.\n"
        "사용자 발화 본문은 포함되지 않으며, 라우팅/지연/품질 메타데이터만 있습니다.\n\n"
        "## 집계 통계\n```json\n" + summary_json + "\n```\n\n"
        "## 최근 턴 메타 (최대 30개)\n```json\n"
        + json.dumps(safe_recent, ensure_ascii=False, indent=2) + "\n```\n\n"
        "## 작성 지침\n"
        "다음 형식의 한국어 markdown 으로 차세대 Harness 사이클 제안서를 작성하세요:\n\n"
        "1. **현황 요약** — 백엔드 분포, 폴백률, TTS 실패율, intent 패턴 중 의미 있는 신호 3가지\n"
        "2. **식별된 문제점** — 데이터에서 보이는 약점/병목 2~4개 (구체적 수치 인용)\n"
        "3. **차세대 사이클 제안 (4개 항목)** — 각 항목마다:\n"
        "   - 패턴명 (Microsoft SARVIS 6 패턴 중 또는 신규)\n"
        "   - 영향받는 모듈 (server.py / brain.py / 신규 모듈명)\n"
        "   - 구체적 acceptance criteria 1~2개\n"
        "4. **회귀 위험** — 새 변경이 기존 동작에 줄 수 있는 영향 1~2개\n"
        "5. **검증 계획** — smoke 테스트 / curl 체크리스트 3~5개\n\n"
        "데이터가 부족한 영역(예: total < 20)은 솔직히 명시하세요."
    )


def propose_next_cycle(
    anthropic_client=None,
    openai_client=None,
    min_turns: int = MIN_TURNS,
) -> Dict:
    """차세대 Harness 사이클 초안 생성.

    반환: {ok: bool, reason: str, cycle: int|None, path: str|None, markdown: str|None,
           total: int, summary: dict}
    """
    summary = telemetry.summarize()
    total = summary.get("total", 0)

    if total < min_turns:
        return {
            "ok": False,
            "reason": f"insufficient_data: total={total} < min_turns={min_turns}",
            "cycle": None, "path": None, "markdown": None,
            "total": total, "summary": summary,
        }

    if anthropic_client is None and openai_client is None:
        return {
            "ok": False,
            "reason": "no_llm_backend_available",
            "cycle": None, "path": None, "markdown": None,
            "total": total, "summary": summary,
        }

    recent = telemetry.recent(50)
    prompt = _build_prompt(summary, recent)

    markdown: Optional[str] = None
    used_backend = None
    error_msg: Optional[str] = None

    if anthropic_client is not None:
        try:
            msg = anthropic_client.messages.create(
                model=cfg.claude_model,
                max_tokens=2500,
                messages=[{"role": "user", "content": prompt}],
            )
            parts = []
            for b in msg.content:
                if hasattr(b, "text"):
                    parts.append(b.text)
            markdown = "".join(parts).strip()
            used_backend = "claude"
        except Exception as e:
            error_msg = f"claude_failed: {type(e).__name__}: {e}"

    if not markdown and openai_client is not None:
        try:
            resp = openai_client.chat.completions.create(
                model=cfg.openai_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2500,
            )
            markdown = (resp.choices[0].message.content or "").strip()
            used_backend = "openai"
        except Exception as e:
            error_msg = (error_msg + " | " if error_msg else "") + (
                f"openai_failed: {type(e).__name__}: {e}"
            )

    if not markdown:
        return {
            "ok": False,
            "reason": error_msg or "llm_returned_empty",
            "cycle": None, "path": None, "markdown": None,
            "total": total, "summary": summary,
        }

    cycle = _next_cycle_number()
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROPOSALS_DIR / f"cycle-{cycle}.md"
    header = (
        f"# Harness 사이클 #{cycle} 제안서 (자동 생성)\n\n"
        f"- 생성 시각: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"- 생성 백엔드: {used_backend}\n"
        f"- 입력 텔레메트리: total={total} 턴 (최근 50개 + 전체 집계)\n\n"
        "---\n\n"
    )
    out_path.write_text(header + markdown + "\n", encoding="utf-8")

    return {
        "ok": True,
        "reason": "ok",
        "cycle": cycle,
        "path": str(out_path.relative_to(_PROJECT_ROOT)),
        "markdown": markdown,
        "total": total,
        "summary": summary,
        "used_backend": used_backend,
    }


# ════════════════════════════════════════════════════════════════════════
# 사이클 #5 T003: GitHub Issue Export
# ════════════════════════════════════════════════════════════════════════

GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


def _resolve_repo(repo: Optional[str]) -> Optional[str]:
    """우선순위: 인자 > HARNESS_GITHUB_REPO > GITHUB_REPO 환경변수."""
    candidate = repo or os.environ.get("HARNESS_GITHUB_REPO") or os.environ.get("GITHUB_REPO")
    if not candidate:
        return None
    candidate = candidate.strip()
    if not GITHUB_REPO_RE.match(candidate):
        return None
    return candidate


def _read_proposal(path: str) -> Optional[Dict]:
    """Proposal 경로 검증 (PROPOSALS_DIR 안에 있어야 path traversal 방지) + 읽기."""
    p = Path(path)
    if not p.is_absolute():
        p = (_PROJECT_ROOT / p).resolve()
    else:
        p = p.resolve()
    base = PROPOSALS_DIR.resolve()
    try:
        p.relative_to(base)
    except ValueError:
        return None  # PROPOSALS_DIR 밖 → 거부
    if not p.is_file() or p.suffix != ".md":
        return None
    body = p.read_text(encoding="utf-8")
    title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else p.stem
    return {"path": str(p), "title": title, "body": body}


def export_proposal_to_github(
    proposal_path: str,
    repo: Optional[str] = None,
    token: Optional[str] = None,
    labels: Optional[list] = None,
    dry_run: bool = False,
) -> Dict:
    """Proposal markdown 을 GitHub Issue 로 게시.

    Args:
        proposal_path: PROPOSALS_DIR 안의 .md 경로 (상대/절대 모두 허용, traversal 차단).
        repo: "owner/name" 형식. 미지정 시 환경변수 fallback.
        token: GitHub Personal Access Token. 미지정 시 환경변수 GITHUB_TOKEN/GH_TOKEN.
        labels: Issue labels (선택).
        dry_run: True 면 API 호출 없이 payload 만 반환 (테스트용).

    Returns:
        {ok, reason, issue_url, issue_number, repo, title, dry_run}
    """
    proposal = _read_proposal(proposal_path)
    if not proposal:
        return {
            "ok": False, "reason": "invalid_proposal_path",
            "issue_url": None, "issue_number": None,
            "repo": None, "title": None, "dry_run": dry_run,
        }

    resolved_repo = _resolve_repo(repo)
    if not resolved_repo:
        return {
            "ok": False, "reason": "missing_or_invalid_repo (set HARNESS_GITHUB_REPO env or pass repo='owner/name')",
            "issue_url": None, "issue_number": None,
            "repo": None, "title": proposal["title"], "dry_run": dry_run,
        }

    body = proposal["body"]
    if len(body) > GH_BODY_MAX:
        body = body[:GH_BODY_MAX] + "\n\n*(truncated by harness_evolve)*\n"

    payload = {
        "title": proposal["title"],
        "body": body,
        "labels": list(labels) if labels else ["harness", "auto-proposal"],
    }

    if dry_run:
        return {
            "ok": True, "reason": "dry_run",
            "issue_url": None, "issue_number": None,
            "repo": resolved_repo, "title": proposal["title"], "dry_run": True,
            "payload_size": len(body),
        }

    gh_token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not gh_token:
        return {
            "ok": False, "reason": "missing_github_token (set GITHUB_TOKEN env or pass token=...)",
            "issue_url": None, "issue_number": None,
            "repo": resolved_repo, "title": proposal["title"], "dry_run": False,
        }

    url = f"https://api.github.com/repos/{resolved_repo}/issues"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sarvis-harness-evolve/1.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            obj = json.loads(raw.decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            err_body = ""
        return {
            "ok": False,
            "reason": f"github_api_error: HTTP {e.code} — {err_body}",
            "issue_url": None, "issue_number": None,
            "repo": resolved_repo, "title": proposal["title"], "dry_run": False,
        }
    except urllib.error.URLError as e:
        return {
            "ok": False,
            "reason": f"github_network_error: {e!r}",
            "issue_url": None, "issue_number": None,
            "repo": resolved_repo, "title": proposal["title"], "dry_run": False,
        }

    # 사이클 #5 architect 권고: issue_url 스킴 allowlist 검증 (https://github.com/ 만).
    raw_url = obj.get("html_url") or ""
    issue_url = raw_url if isinstance(raw_url, str) and raw_url.startswith("https://github.com/") else None
    return {
        "ok": True, "reason": "ok",
        "issue_url": issue_url,
        "issue_number": obj.get("number"),
        "repo": resolved_repo, "title": proposal["title"], "dry_run": False,
    }
