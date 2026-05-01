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

**Current implementation status** (cycle #2 complete, 2026-05-01):
- ✅ **Supervisor + Pipeline + Hierarchical** — `brain.py`, `.claude/agents/`.
- ✅ **Expert Pool** — `Brain.think_stream_with_fallback()` automatically retries the next available backend on failure and emits a `backend_fallback` WS event so the user is informed transparently. Manual switch buttons remain available as the last resort.
- ✅ **Fan-out / Fan-in** — `analysis.parallel_analyze()` runs intent / emotion-hint / face-context / memory-hint concurrently via `asyncio.gather` (200ms per-task timeout). Result is merged into the LLM context before `think_stream`.
- ✅ **Generate-Verify** — `tts_verifier.verify_tts_candidate()` checks length / Korean ratio / blocklist / control chars + auto-sanitizes. Called by `audio_io.synthesize_bytes_verified()`. Blocked candidates trigger a `tts_blocked` WS event (text still rendered).
- ✅ **Telemetry & Feedback Loop** — `telemetry.log_turn()` writes per-turn metadata (backend, fallback chain, latencies, intent, TTS result) to `data/harness_telemetry.jsonl`. `GET /api/harness/telemetry` returns aggregate stats. PII (utterance bodies) is never persisted — only lengths.

Cycle #2 added these modules:
- `tts_verifier.py` + `data/tts_blocklist.json`
- `analysis.py`
- `telemetry.py`
- New API: `GET /api/harness/telemetry`
- New WS events: `backend_fallback`, `tts_blocked`

Remaining work (cycle #3 candidates) is tracked in `harness/sarvis/validation.md` §7.

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
- **Auth**: Local username/password with session tokens
- **Friendly Error Surface**: When an LLM backend fails (credit exhausted, auth failure, rate limit, network error), `_friendly_error()` in `brain.py` converts raw provider exceptions into Korean guidance messages that name the exact alternative-backend buttons to press. Raw stack traces, request IDs, and provider payloads are kept server-side only. `think_stream` rolls back the orphan user history entry on any failure so the next call doesn't hit consecutive-user errors.
