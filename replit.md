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

## Features

- **Dual Mode**: Desktop (pygame) or Web (FastAPI + WebSocket)
- **Agentic Tools**: web_search, get_weather, get_time, remember/recall, set_timer, see (vision)
- **Voice I/O**: Browser microphone → Whisper STT → Claude → Edge-TTS → browser audio
- **Camera**: Browser webcam → JPEG frames → Claude Vision analysis
- **Emotion Orb**: Canvas animation reflecting assistant's emotional state — selectable visual styles (ORBITAL / PULSE / REACTOR / NEURAL), all preserving the 7 emotion palettes. Choice persists in `localStorage('orbStyle')` and applies to both orbs in compare mode.
- **Auth**: Local username/password with session tokens
- **Friendly Error Surface**: When an LLM backend fails (credit exhausted, auth failure, rate limit, network error), `_friendly_error()` in `brain.py` converts raw provider exceptions into Korean guidance messages that name the exact alternative-backend buttons to press. Raw stack traces, request IDs, and provider payloads are kept server-side only. `think_stream` rolls back the orphan user history entry on any failure so the next call doesn't hit consecutive-user errors.
