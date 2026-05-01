# S.A.R.V.I.S — Personal AI Assistant

A multimodal AI assistant inspired by the 4-stage agent pattern (Task Planning → Model Selection → Task Execution → Response Generation). Features face recognition, voice interaction, and tool-augmented intelligence.

## Architecture

- **Backend**: FastAPI + WebSockets (`server.py`) on port 5000
- **Frontend**: HTML5 Canvas / Vanilla JS / CSS in the `web/` directory
- **AI Brain**: Claude (Anthropic) or Ollama as LLM backend (`brain.py`)
- **STT**: Faster-Whisper for speech-to-text
- **TTS**: Edge-TTS (Microsoft) for text-to-speech
- **Vision**: OpenCV + optional face_recognition for webcam analysis

## Key Files

- `server.py` — FastAPI web server (entry point for web mode)
- `brain.py` — LLM controller (Claude tool_use loop + Ollama simple chat)
- `tools.py` — Tool definitions and executor (web search, weather, timer, memory, vision)
- `audio_io.py` — Speech recording, Whisper STT, Edge-TTS
- `vision.py` — VisionSystem (desktop) + WebVision (web, browser-pushed frames)
- `config.py` — Centralized configuration via environment variables
- `auth.py` — User authentication with PBKDF2 hashing
- `emotion.py` — 7 assistant emotional states
- `web/` — Frontend assets (HTML, CSS, JS, orb animation)

## Environment Variables

- `ANTHROPIC_API_KEY` — Required for Claude backend (set as a secret)
- `OPENAI_API_KEY` — Required for OpenAI backend and `compare` mode (set as a secret)
- `SARVIS_BACKEND` — `"openai"` (default), `"claude"`, `"ollama"`, or `"compare"`
- `OLLAMA_HOST` — Ollama server URL (default `http://localhost:11434`). Use a tunnel URL (e.g. cloudflared) when SARVIS runs on Replit but Ollama runs on a local machine.
- `OLLAMA_MODEL` — Ollama model tag (default `qwen2.5:7b`). Examples: `llama3.2:3b`, `qwen2.5:14b`, `gemma2:9b`.
- `PORCUPINE_ACCESS_KEY` — Optional, for desktop wake-word detection

## Running

The workflow runs `python server.py` on port 5000. The app auto-loads the Whisper model on startup.

## Development Methodology — Harness

SARVIS uses **[Harness](harness/README.md)** (a Claude Code Team-Architecture Factory plugin, Apache-2.0) as its **meta development system**. Harness is *not* a runtime feature — it is the architectural rule book that decides how SARVIS evolves.

**Target composition** (per Harness Phase 2): `Supervisor[Pipeline(Fan-out → Expert-Pool → Generate-Verify)]` plus `Hierarchical Delegation` for development.

**Current implementation status** (cycle #5 complete, 2026-05-01):
- ✅ **Supervisor + Pipeline + Hierarchical** — `brain.py`, `.claude/agents/`.
- ✅ **Expert Pool** — `Brain.think_stream_with_fallback()` + `_ollama_healthcheck()`.
- ✅ **Fan-out / Fan-in** — `analysis.parallel_analyze()` (4-way 200ms timeout).
- ✅ **Generate-Verify** — `tts_verifier` + `synthesize_bytes_verified()` + `Brain.regenerate_safe_tts()`.
- ✅ **Telemetry & Feedback Loop** — `log_turn()` to JSONL (no PII). Real-time `WS /api/harness/ws` push + 5s polling fallback. **Cycle #5**: `summarize().latency` exposes `avg/p50/p95/p99/count` for `fanout_ms`/`llm_ms`/`tts_ms`/`total_ms` (nearest-rank percentile, pure Python). `respond_internal` / `respond_compare` now also record `total_ms`. Dashboard shows a new "응답시간 분포" table.
- ✅ **Self-Evolution Proposer + Export** — `propose_next_cycle()` writes `harness/sarvis/proposals/cycle-{n}.md`. **Cycle #5**: `export_proposal_to_github()` + `POST /api/harness/evolve/export` posts the proposal as a GitHub Issue. Path-traversal blocked (`PROPOSALS_DIR` allowlist), `repo` from arg or env (`HARNESS_GITHUB_REPO`/`GITHUB_REPO`), token from env (`GITHUB_TOKEN`/`GH_TOKEN`) **only** — never accepted in body. `issue_url` is verified against the `https://github.com/` scheme allowlist on both server and client. Dashboard adds a "GitHub Issue 로 내보내기" button (with dry-run option) inside the evolve result.
- ✅ **Regression Tests** *(new in cycle #5)* — `tests/test_telemetry.py` (12) + `tests/test_evolve_export.py` (12). Pure stdlib `unittest` (no extra deps). Run via `python -m unittest discover tests`. Covers: summarize keyset equivalence (empty vs non-empty), nearest-rank percentile correctness, PII sanitization (str/list/tuple/dict all → `*_len`), pub-sub callback isolation + idempotent subscribe, GitHub export (traversal block, missing repo/token, body truncation, dry-run, env priority).

Cycle #5 added/changed:
- `telemetry.py` — `_percentile`, `_latency_stats`, `LATENCY_KEYS`, `summarize().latency` (consistent empty/non-empty). Sanitize collections via `len()`. Fixed file-handle leak in `_rotate_if_needed`.
- `server.py` — `total_ms` recorded in `respond_internal` / `respond_compare`. New `POST /api/harness/evolve/export` endpoint.
- `harness_evolve.py` — `export_proposal_to_github()` with traversal-safe `_read_proposal()`, `_resolve_repo()`, `https://github.com/` `issue_url` allowlist, 60KB body cap, urllib + `asyncio.to_thread` (20s timeout).
- `web/harness/dashboard.html` — "응답시간 분포" table; dynamic GitHub export button, dry-run checkbox, repo input, scheme-checked link.
- `tests/__init__.py`, `tests/test_telemetry.py`, `tests/test_evolve_export.py` — 24 unit tests.

Remaining work (cycle #6 candidates) is tracked in `harness/sarvis/validation.md` §10.

Key locations:
- `harness/` — Original Harness plugin assets (READMEs EN/KO/JA, CHANGELOG, landing page source, banner images, plus `harness/sarvis/` SARVIS-specific Phase outputs). **Repo-internal — not publicly served.**
- `web/harness/` — Curated public landing assets only (`index.html`, `privacy.html`, 4 banner PNGs). This is what the `/harness/` route serves; markdown / LICENSE / .gitignore / sarvis/* are deliberately kept out of the public mount.
- `harness/sarvis/{analysis,architecture,validation}.md` — Phase 1/2/6 outputs of applying Harness to SARVIS itself.
- `.claude/skills/harness/SKILL.md` — Harness meta-skill with triggers (`하네스 구성해줘`, `build a harness`, `ハーネスを構成して`).
- `.claude/skills/harness/references/agent-design-patterns.md` — The six team patterns and a decision tree.
- `.claude/skills/tts-verifier/SKILL.md` — Phase 4 generated skill (Generate-Verify gate before TTS).
- `.claude/agents/_orchestrator.md` — Supervisor policy mirroring `brain.py`.
- `.claude/agents/{architect,voice-engineer,vision-engineer,backend-engineer,frontend-engineer,qa-engineer,security-reviewer}.md` — Development team roles with explicit input/output/forbidden rules.

Procedure for new SARVIS features:
1. `architect` agent picks one (or a composition) of the six patterns and updates `harness/sarvis/architecture.md`.
2. Delegate to leaf engineers per the table above.
3. `qa-engineer` 7-item checklist must pass.
4. `security-reviewer` 5-item checklist must pass.
5. Record the change + rationale in `replit.md` (this file).

## Features

- **Dual Mode**: Desktop (pygame) or Web (FastAPI + WebSocket)
- **Agentic Tools**: web_search, get_weather, get_time, remember/recall, set_timer, see (vision)
- **Voice I/O**: Browser microphone → Whisper STT → Claude → Edge-TTS → browser audio
- **Camera**: Browser webcam → JPEG frames → Claude Vision analysis
- **Emotion Orb**: Canvas animation reflecting assistant's emotional state — selectable visual styles (ORBITAL / PULSE / REACTOR / NEURAL), all preserving the 7 emotion palettes. Choice persists in `localStorage('orbStyle')` and applies to both orbs in compare mode.
- **Conversation-First UI** *(2026-05-01)*: The default desktop layout is a centered chat (`.chat-main`) with the dialogue log + integrated mic/text/SEND input bar. The orb panel (`.orb-pane`), vision panel (`.side-pane`: camera + face register), and mode panel (backend picker) are all hidden by default and toggle on via three top-bar buttons (`오브 / 비전 / 모드`). Layout uses CSS grid with `grid-template-columns: 0px 1fr 0px` plus `.show-orb` / `.show-vision` modifier classes (smooth transition). Per-user preference persists in `localStorage('panelState')`. Mobile uses the existing bottom tab bar with the order **대화 (default) / 오브 / 비전**, and `setupPanelToggles()` is guarded with `isMobile()` so desktop panel classes never collide with the mobile tab system. New-message badges target the `chat` tab on mobile.
- **Static Spoken Welcome** *(2026-05-01)*: The first-load greeting (`server.py welcome()`) uses a fixed Korean string — *"안녕하세요, 사비스입니다. 무엇을 도와드릴까요?"* — and synthesizes Edge-TTS directly, bypassing `think_stream_with_fallback`. This eliminates the regression where a transient LLM outage on page-load caused a red "internal server error" toast. The welcome `stream_end` carries `is_welcome=true` so the client can distinguish it from response audio. The browser's autoplay policy is handled by `_unlockAudioOnGesture()` in `web/app.js`: the welcome MP3 buffer is queued in a FIFO (`_pendingTtsQueue`, max 3) and played on the first `pointerdown`/`keydown`/`touchstart`. If that gesture targets an input control (mic / send / text input — including mobile equivalents), the queued welcome is discarded and `_suppressNextWelcomeAudio` blocks any in-flight welcome bytes so the user's own utterance isn't overlapped. On the server side, `_preempt_welcome()` is invoked before each `text_input` and `0x02` audio frame — it cancels the welcome task immediately so user input can never be dropped by the welcome's `busy` lock and never arrives out-of-order. The welcome task is also cancelled in the WS `finally` block (clean disconnect). On client reconnect (`connectWS`), all welcome flags + queue are cleared (TDZ-safe try/catch) so a stale `_suppressNextWelcomeAudio` from a previous session can't drop a new welcome.
- **Auth**: Local username/password with session tokens
- **Friendly Error Surface**: When an LLM backend fails (credit exhausted, auth failure, rate limit, network error), `_friendly_error()` in `brain.py` converts raw provider exceptions into Korean guidance messages that name the exact alternative-backend buttons to press. Raw stack traces, request IDs, and provider payloads are kept server-side only. `think_stream` rolls back the orphan user history entry on any failure so the next call doesn't hit consecutive-user errors.
