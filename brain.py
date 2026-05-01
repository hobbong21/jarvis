"""두뇌 — Claude tool_use 루프 / Ollama 단순 채팅. 감정 태그 파싱 포함."""
import re
import time
import traceback
from typing import Generator, Iterator, Optional, Tuple

from config import cfg
from emotion import Emotion, parse_emotion

# Ollama 헬스체크 캐시 (TTL 60s) — 사이클 #3 #2: 항상 후보화
_OLLAMA_HEALTH_TTL = 60.0
_ollama_health_cache: dict = {"checked_at": 0.0, "ok": False, "client": None}

_EMOTION_PREFIX_RE = re.compile(r"^\s*\[emotion:\w+\]\s*", re.IGNORECASE)


_ALT_BUTTONS = {
    "claude": "[2·OPENAI] 또는 [3·OLLAMA]",
    "openai": "[1·CLAUDE] 또는 [3·OLLAMA]",
    "ollama": "[1·CLAUDE] 또는 [2·OPENAI]",
}


def _friendly_error(e: Exception, backend: str) -> str:
    """백엔드 예외를 사용자 친화적 한글 메시지로 변환.
    크레딧 부족·인증 실패·네트워크 오류 등 흔한 케이스를 분기하고,
    대안 백엔드 사용을 안내한다. 원문/스택트레이스/request_id 는 노출하지 않는다."""
    msg = str(e).lower()
    label = backend.upper()
    alt = _ALT_BUTTONS.get(backend, "다른 백엔드")

    # 크레딧 / 결제 부족 — 가장 흔한 케이스
    if any(k in msg for k in ("credit balance", "insufficient_quota", "exceeded your current quota",
                              "billing", "plans &amp; billing", "plans & billing", "quota")):
        if backend == "claude":
            return ("⚠ Claude API 크레딧이 부족합니다.\n"
                    f"→ 하단의 {alt} 버튼을 눌러 다른 AI 로 전환하세요.\n"
                    "(console.anthropic.com 의 Plans & Billing 에서 충전 시 즉시 복구됩니다.)")
        if backend == "openai":
            return ("⚠ OpenAI API 크레딧이 부족합니다.\n"
                    f"→ 하단의 {alt} 버튼을 눌러 다른 AI 로 전환하세요.\n"
                    "(platform.openai.com 의 Billing 에서 충전 시 즉시 복구됩니다.)")
        return f"⚠ {label} 크레딧이 부족합니다.\n→ 하단의 {alt} 버튼을 눌러주세요."

    # 인증 실패
    if any(k in msg for k in ("api key", "authentication", "401", "unauthorized", "invalid_api_key")):
        return (f"⚠ {label} API 키가 유효하지 않습니다.\n"
                f"→ 환경변수를 확인하거나 하단의 {alt} 버튼을 눌러주세요.")

    # 모델 미지원 / 권한
    if "403" in msg or "permission" in msg or "model_not_found" in msg:
        return (f"⚠ {label} 모델 접근 권한이 없습니다.\n"
                f"→ 하단의 {alt} 버튼을 눌러주세요.")

    # Rate limit
    if "rate limit" in msg or "429" in msg:
        return (f"⚠ {label} 요청 한도 초과 — 잠시 후 다시 시도하거나\n"
                f"→ 하단의 {alt} 버튼을 눌러주세요.")

    # 연결/네트워크
    if any(k in msg for k in ("connection", "timeout", "timed out", "network", "unreachable")):
        if backend == "ollama":
            return (f"⚠ Ollama 로컬 서버({cfg.ollama_host})에 연결할 수 없습니다.\n"
                    f"→ ollama serve 가 실행 중인지 확인하거나 하단의 {alt} 버튼을 눌러주세요.")
        return (f"⚠ {label} 서버 연결 실패. 잠시 후 다시 시도하거나\n"
                f"→ 하단의 {alt} 버튼을 눌러주세요.")

    # 기타 — 원문/request_id 노출 없이 일반화된 안내만
    return (f"⚠ {label} 통신 오류가 발생했습니다.\n"
            f"→ 하단의 {alt} 버튼을 눌러 다른 AI 로 전환해보세요.")


class Brain:
    """SARVIS의 LLM 컨트롤러.

    Microsoft SARVIS의 4단계 패턴을 Claude tool_use 한 번의 호출로 구현:
      1) Task Planning   → LLM이 사용자 요청 해석
      2) Model Selection → LLM이 적절한 도구 선택
      3) Task Execution  → ToolExecutor가 도구 실행
      4) Response Gen    → LLM이 도구 결과 종합 후 최종 답변
    """

    def __init__(self, tool_executor=None):
        self.history = []
        self.client = None              # 현재 활성 백엔드 클라이언트
        self.anthropic_client = None    # 비전 도구용 (항상 보유 시도)
        self.openai_client = None       # OpenAI 용 (compare/openai 모드용)
        self.tools = tool_executor
        self._init_backend()

    def _init_backend(self):
        backend = cfg.llm_backend

        # compare 모드는 Claude + OpenAI 둘 다 필요
        if backend == "compare":
            self._ensure_anthropic()
            self._ensure_openai()
            self.client = self.anthropic_client  # 비전 도구용 기본
            if self.anthropic_client is None and self.openai_client is None:
                print("[Brain] 경고: compare 모드에 Anthropic, OpenAI 키 모두 없음")
            return

        if backend == "claude":
            self._ensure_anthropic()
            self.client = self.anthropic_client
            if self.client is None:
                print("[Brain] 경고: ANTHROPIC_API_KEY가 없습니다.")
        elif backend == "openai":
            self._ensure_openai()
            self.client = self.openai_client
            # 비전 도구는 Claude를 쓰므로 Anthropic도 가능하면 로드
            self._ensure_anthropic()
            if self.client is None:
                print("[Brain] 경고: OPENAI_API_KEY가 없습니다.")
        elif backend == "ollama":
            import ollama
            self.client = ollama.Client(host=cfg.ollama_host)
            try:
                self.client.show(cfg.ollama_model)
            except Exception:
                print(f"[Brain] Ollama 모델 '{cfg.ollama_model}' 다운로드 중...")
                self.client.pull(cfg.ollama_model)
        else:
            raise ValueError(f"알 수 없는 백엔드: {backend}")

    def _ensure_anthropic(self):
        if self.anthropic_client is not None:
            return
        if not cfg.anthropic_api_key:
            return
        try:
            from anthropic import Anthropic
            self.anthropic_client = Anthropic(api_key=cfg.anthropic_api_key)
        except Exception as e:
            print(f"[Brain] Anthropic 클라이언트 초기화 실패: {e}")

    def _ensure_openai(self):
        if self.openai_client is not None:
            return
        if not cfg.openai_api_key:
            return
        try:
            from openai import OpenAI
            self.openai_client = OpenAI(api_key=cfg.openai_api_key)
        except Exception as e:
            print(f"[Brain] OpenAI 클라이언트 초기화 실패: {e}")

    def get_client(self):
        """비전 도구는 항상 Anthropic 사용 (있으면)."""
        return self.anthropic_client

    def think(
        self, user_message: str, context: Optional[str] = None
    ) -> Tuple[Emotion, str]:
        if context:
            user_message = f"[컨텍스트: {context}]\n\n{user_message}"
        self.history.append({"role": "user", "content": user_message})

        try:
            backend = cfg.llm_backend
            if backend == "compare":
                # 음성 흐름에서는 비교 모드를 지원하지 않으므로 Claude로 폴백
                if self.anthropic_client is not None:
                    self.client = self.anthropic_client
                    if self.tools is not None:
                        return self._think_with_tools()
                    return self._think_claude_simple()
                if self.openai_client is not None:
                    self.client = self.openai_client
                    return self._think_openai_simple()
                return Emotion.CONCERNED, "비교 모드용 API 키가 없습니다."

            if self.client is None:
                return Emotion.CONCERNED, (
                    f"{backend.upper()} 백엔드의 API 키가 없습니다. "
                    "환경변수를 설정하고 서버를 재시작해주세요."
                )
            if backend == "claude" and self.tools is not None:
                return self._think_with_tools()
            elif backend == "claude":
                return self._think_claude_simple()
            elif backend == "openai":
                return self._think_openai_simple()
            else:
                return self._think_ollama()
        except Exception as e:
            traceback.print_exc()
            return Emotion.CONCERNED, f"AI 통신 오류가 발생했어요. {e}"

    # ============================================================
    # OpenAI 단순 채팅 (도구 없음)
    # ============================================================
    def _think_openai_simple(self) -> Tuple[Emotion, str]:
        messages = [{"role": "system", "content": cfg.system_prompt}]
        for h in self.history:
            content = h["content"]
            if isinstance(content, str):
                messages.append({"role": h["role"], "content": content})
            elif isinstance(content, list):
                # Claude tool 블록을 평탄화
                parts = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append(b.get("text", ""))
                if parts:
                    messages.append({"role": h["role"], "content": " ".join(parts)})

        resp = self.openai_client.chat.completions.create(
            model=cfg.openai_model,
            messages=messages,
            max_tokens=400,
            temperature=0.7,
        )
        raw = (resp.choices[0].message.content or "").strip()
        self.history.append({"role": "assistant", "content": raw})
        emotion, body = parse_emotion(raw)
        self._trim_history()
        return emotion, body

    # ============================================================
    # Claude + Tool Use (메인 경로)
    # ============================================================
    def _think_with_tools(self) -> Tuple[Emotion, str]:
        max_iters = 8
        for _ in range(max_iters):
            response = self.client.messages.create(
                model=cfg.claude_model,
                max_tokens=800,
                system=cfg.system_prompt,
                tools=self.tools.definitions(),
                messages=self.history,
            )

            # 응답을 dict로 변환해서 history에 추가 (다음 호출에서 사용)
            content_dicts = []
            tool_use_blocks = []
            for block in response.content:
                if block.type == "text":
                    content_dicts.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    content_dicts.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
                    tool_use_blocks.append(block)

            self.history.append({"role": "assistant", "content": content_dicts})

            if response.stop_reason != "tool_use":
                # 종료: 최종 텍스트 응답
                final_text = "".join(
                    b["text"] for b in content_dicts if b["type"] == "text"
                )
                emotion, body = parse_emotion(final_text)
                self._trim_history()
                return emotion, body

            # 도구 실행 → tool_result 추가
            tool_results = []
            for tu in tool_use_blocks:
                print(f"[TOOL] {tu.name}({tu.input})")
                result = self.tools.execute(tu.name, tu.input)
                print(f"[TOOL] → {result[:120]}{'...' if len(result) > 120 else ''}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": str(result),
                })
            self.history.append({"role": "user", "content": tool_results})

        # 안전장치
        return Emotion.CONCERNED, "도구 호출이 너무 많이 반복됐어요."

    # ============================================================
    # Claude 단순 채팅 (도구 없을 때)
    # ============================================================
    def _think_claude_simple(self) -> Tuple[Emotion, str]:
        msg = self.client.messages.create(
            model=cfg.claude_model,
            max_tokens=400,
            system=cfg.system_prompt,
            messages=self.history,
        )
        raw = msg.content[0].text.strip()
        self.history.append({"role": "assistant", "content": raw})
        emotion, body = parse_emotion(raw)
        self._trim_history()
        return emotion, body

    # ============================================================
    # Ollama (도구 없음, 단순 채팅)
    # ============================================================
    def _think_ollama(self) -> Tuple[Emotion, str]:
        # Ollama 히스토리는 단순 텍스트만 — Claude tool_use 메시지는 텍스트로 평탄화
        simple_history = []
        for h in self.history:
            content = h["content"]
            if isinstance(content, str):
                simple_history.append({"role": h["role"], "content": content})
            elif isinstance(content, list):
                # tool_use / tool_result 블록을 자연어로 합치기
                parts = []
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        parts.append(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        parts.append(f"[도구 호출: {b.get('name')}]")
                    elif b.get("type") == "tool_result":
                        parts.append(f"[도구 결과: {b.get('content', '')}]")
                flat = " ".join(p for p in parts if p).strip()
                if flat:
                    simple_history.append({"role": h["role"], "content": flat})
        messages = [{"role": "system", "content": cfg.system_prompt}] + simple_history
        res = self.client.chat(
            model=cfg.ollama_model,
            messages=messages,
            options={"num_predict": 400, "temperature": 0.7},
        )
        raw = res["message"]["content"].strip()
        self.history.append({"role": "assistant", "content": raw})
        emotion, body = parse_emotion(raw)
        self._trim_history()
        return emotion, body

    # ============================================================
    # 스트리밍 (Claude simple / Ollama)
    # ============================================================
    def think_stream(
        self, user_message: str, context: Optional[str] = None
    ) -> Iterator[Tuple[Optional[str], Optional[Emotion], Optional[str]]]:
        """동기 제너레이터 — 청크마다 (chunk, None, None) yield.
        완료 시 (None, emotion, clean_body) yield.
        tool_use 모드는 비스트리밍 폴백."""
        if context:
            user_message = f"[컨텍스트: {context}]\n\n{user_message}"
        self.history.append({"role": "user", "content": user_message})

        backend = cfg.llm_backend

        def _rollback_user():
            """orphan user 턴 제거 — 다음 호출에서 consecutive-user 에러 방지."""
            if self.history and self.history[-1].get("role") == "user":
                self.history.pop()

        # compare 모드는 음성 흐름에서 호출되지 않음 — Claude 우선 폴백
        if backend == "compare":
            if self.anthropic_client is not None:
                self.client = self.anthropic_client
                try:
                    if self.tools is not None:
                        emotion, body = self._think_with_tools()
                        yield None, emotion, body
                        return
                    yield from self._stream_claude()
                    return
                except Exception as e:
                    traceback.print_exc()
                    _rollback_user()
                    yield None, Emotion.CONCERNED, _friendly_error(e, "claude")
                    return
            if self.openai_client is not None:
                self.client = self.openai_client
                try:
                    yield from self._stream_openai()
                    return
                except Exception as e:
                    traceback.print_exc()
                    _rollback_user()
                    yield None, Emotion.CONCERNED, _friendly_error(e, "openai")
                    return
            _rollback_user()
            yield None, Emotion.CONCERNED, "비교 모드용 API 키가 없습니다."
            return

        if self.client is None:
            _rollback_user()
            yield None, Emotion.CONCERNED, (
                f"{backend.upper()} 백엔드의 API 키가 없습니다. "
                "환경변수를 설정하고 재시작해주세요."
            )
            return

        try:
            if backend == "claude" and self.tools is not None:
                emotion, body = self._think_with_tools()
                yield None, emotion, body
            elif backend == "claude":
                yield from self._stream_claude()
            elif backend == "openai":
                yield from self._stream_openai()
            else:
                yield from self._stream_ollama()
        except Exception as e:
            traceback.print_exc()
            _rollback_user()
            yield None, Emotion.CONCERNED, _friendly_error(e, backend)

    def _stream_openai(self):
        """OpenAI ChatCompletion 스트리밍 — 도구 없이 단순 응답."""
        messages = [{"role": "system", "content": cfg.system_prompt}]
        for h in self.history:
            content = h["content"]
            if isinstance(content, str):
                messages.append({"role": h["role"], "content": content})
            elif isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append(b.get("text", ""))
                if parts:
                    messages.append({"role": h["role"], "content": " ".join(parts)})

        full = ""
        prefix_buf = ""
        prefix_cleared = False
        MAX_PREFIX = 30

        stream = self.openai_client.chat.completions.create(
            model=cfg.openai_model,
            messages=messages,
            max_tokens=600,
            temperature=0.7,
            stream=True,
        )
        for chunk in stream:
            try:
                delta = chunk.choices[0].delta
                text = delta.content or ""
            except (AttributeError, IndexError):
                text = ""
            if not text:
                continue
            full += text
            if not prefix_cleared:
                prefix_buf += text
                if len(prefix_buf) >= MAX_PREFIX or "\n" in prefix_buf or (
                    "]" in prefix_buf and "[" in prefix_buf
                ):
                    m = _EMOTION_PREFIX_RE.match(prefix_buf)
                    to_emit = prefix_buf[m.end():] if m else prefix_buf
                    if to_emit:
                        yield to_emit, None, None
                    prefix_cleared = True
            else:
                yield text, None, None

        if not prefix_cleared and prefix_buf:
            m = _EMOTION_PREFIX_RE.match(prefix_buf)
            to_emit = prefix_buf[m.end():] if m else prefix_buf
            if to_emit:
                yield to_emit, None, None

        self.history.append({"role": "assistant", "content": full})
        emotion, body = parse_emotion(full)
        self._trim_history()
        yield None, emotion, body

    # ============================================================
    # A/B 비교: Claude + OpenAI 병렬 호출
    # ============================================================
    def compare_stream(
        self, user_message: str, context: Optional[str] = None
    ):
        """제너레이터 — (source, chunk, emotion, body) tuple yield.
        - chunk != None: 스트리밍 중 (source 별로 계속)
        - chunk == None & body != None: 해당 source 완료
        도구 없음, 히스토리 미업데이트 (공평한 비교)."""
        import queue, threading

        if context:
            user_message = f"[컨텍스트: {context}]\n\n{user_message}"

        # 비교 모드는 히스토리에 영향을 주지 않도록 임시 메시지 리스트 사용
        base_history = list(self.history) + [{"role": "user", "content": user_message}]

        q: "queue.Queue" = queue.Queue()
        active = []

        def run_claude():
            try:
                if self.anthropic_client is None:
                    q.put(("claude", None, Emotion.CONCERNED, "Anthropic 키 없음"))
                    return
                full, prefix_buf, prefix_cleared = "", "", False
                MAX_PREFIX = 30
                with self.anthropic_client.messages.stream(
                    model=cfg.claude_model,
                    max_tokens=600,
                    system=cfg.system_prompt,
                    messages=base_history,
                ) as stream:
                    for chunk in stream.text_stream:
                        full += chunk
                        if not prefix_cleared:
                            prefix_buf += chunk
                            if len(prefix_buf) >= MAX_PREFIX or "\n" in prefix_buf or (
                                "]" in prefix_buf and "[" in prefix_buf
                            ):
                                m = _EMOTION_PREFIX_RE.match(prefix_buf)
                                to_emit = prefix_buf[m.end():] if m else prefix_buf
                                if to_emit:
                                    q.put(("claude", to_emit, None, None))
                                prefix_cleared = True
                        else:
                            q.put(("claude", chunk, None, None))
                if not prefix_cleared and prefix_buf:
                    m = _EMOTION_PREFIX_RE.match(prefix_buf)
                    to_emit = prefix_buf[m.end():] if m else prefix_buf
                    if to_emit:
                        q.put(("claude", to_emit, None, None))
                emo, body = parse_emotion(full)
                q.put(("claude", None, emo, body))
            except Exception as e:
                traceback.print_exc()
                q.put(("claude", None, Emotion.CONCERNED, _friendly_error(e, "claude")))

        def run_openai():
            try:
                if self.openai_client is None:
                    q.put(("openai", None, Emotion.CONCERNED, "OpenAI 키 없음"))
                    return
                messages = [{"role": "system", "content": cfg.system_prompt}]
                for h in base_history:
                    content = h["content"]
                    if isinstance(content, str):
                        messages.append({"role": h["role"], "content": content})
                    elif isinstance(content, list):
                        parts = [b.get("text", "") for b in content
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        if parts:
                            messages.append({"role": h["role"], "content": " ".join(parts)})

                full, prefix_buf, prefix_cleared = "", "", False
                MAX_PREFIX = 30
                stream = self.openai_client.chat.completions.create(
                    model=cfg.openai_model,
                    messages=messages,
                    max_tokens=600,
                    temperature=0.7,
                    stream=True,
                )
                for chunk in stream:
                    try:
                        text = chunk.choices[0].delta.content or ""
                    except (AttributeError, IndexError):
                        text = ""
                    if not text:
                        continue
                    full += text
                    if not prefix_cleared:
                        prefix_buf += text
                        if len(prefix_buf) >= MAX_PREFIX or "\n" in prefix_buf or (
                            "]" in prefix_buf and "[" in prefix_buf
                        ):
                            m = _EMOTION_PREFIX_RE.match(prefix_buf)
                            to_emit = prefix_buf[m.end():] if m else prefix_buf
                            if to_emit:
                                q.put(("openai", to_emit, None, None))
                            prefix_cleared = True
                    else:
                        q.put(("openai", text, None, None))
                if not prefix_cleared and prefix_buf:
                    m = _EMOTION_PREFIX_RE.match(prefix_buf)
                    to_emit = prefix_buf[m.end():] if m else prefix_buf
                    if to_emit:
                        q.put(("openai", to_emit, None, None))
                emo, body = parse_emotion(full)
                q.put(("openai", None, emo, body))
            except Exception as e:
                traceback.print_exc()
                q.put(("openai", None, Emotion.CONCERNED, _friendly_error(e, "openai")))

        t1 = threading.Thread(target=run_claude, daemon=True)
        t2 = threading.Thread(target=run_openai, daemon=True)
        t1.start(); t2.start()
        active = {"claude", "openai"}

        # 비교 모드는 history 에 영향 없음 — 두 후보 중 어떤 응답이 "정답"인지
        # 알 수 없으므로 user 메시지조차 저장하지 않는다 (다음 turn 에서 orphan user 가
        # 누적되는 것을 방지).

        while active:
            source, chunk, emo, body = q.get()
            yield source, chunk, emo, body
            if chunk is None:  # 해당 source 종료
                active.discard(source)

        t1.join(timeout=1); t2.join(timeout=1)

    def _stream_claude(self):
        full = ""
        prefix_buf = ""
        prefix_cleared = False
        MAX_PREFIX = 30  # [emotion:concerned] ≈ 20자

        with self.client.messages.stream(
            model=cfg.claude_model,
            max_tokens=600,
            system=cfg.system_prompt,
            messages=self.history,
        ) as stream:
            for chunk in stream.text_stream:
                full += chunk
                if not prefix_cleared:
                    prefix_buf += chunk
                    # 감정 태그가 완전히 들어왔는지 확인
                    if len(prefix_buf) >= MAX_PREFIX or "\n" in prefix_buf or (
                        "]" in prefix_buf and "[" in prefix_buf
                    ):
                        m = _EMOTION_PREFIX_RE.match(prefix_buf)
                        to_emit = prefix_buf[m.end():] if m else prefix_buf
                        if to_emit:
                            yield to_emit, None, None
                        prefix_cleared = True
                else:
                    yield chunk, None, None

        # 스트림이 짧게 끝나 prefix_buf 가 아직 비워지지 않은 경우
        if not prefix_cleared and prefix_buf:
            m = _EMOTION_PREFIX_RE.match(prefix_buf)
            to_emit = prefix_buf[m.end():] if m else prefix_buf
            if to_emit:
                yield to_emit, None, None

        self.history.append({"role": "assistant", "content": full})
        emotion, body = parse_emotion(full)
        self._trim_history()
        yield None, emotion, body

    def _stream_ollama(self):
        simple_history = []
        for h in self.history:
            content = h["content"]
            if isinstance(content, str):
                simple_history.append({"role": h["role"], "content": content})
            elif isinstance(content, list):
                parts = []
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        parts.append(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        parts.append(f"[도구: {b.get('name')}]")
                    elif b.get("type") == "tool_result":
                        parts.append(f"[결과: {b.get('content', '')}]")
                flat = " ".join(p for p in parts if p).strip()
                if flat:
                    simple_history.append({"role": h["role"], "content": flat})

        messages = [{"role": "system", "content": cfg.system_prompt}] + simple_history

        full = ""
        prefix_buf = ""
        prefix_cleared = False
        MAX_PREFIX = 30

        for chunk in self.client.chat(
            model=cfg.ollama_model,
            messages=messages,
            stream=True,
            options={"num_predict": 600, "temperature": 0.7},
        ):
            # Ollama 스트리밍은 ChatResponse 객체 반환
            try:
                text = chunk.message.content or ""
            except AttributeError:
                text = (chunk.get("message") or {}).get("content", "") if isinstance(chunk, dict) else ""
            if not text:
                continue
            full += text
            if not prefix_cleared:
                prefix_buf += text
                if len(prefix_buf) >= MAX_PREFIX or "\n" in prefix_buf or (
                    "]" in prefix_buf and "[" in prefix_buf
                ):
                    m = _EMOTION_PREFIX_RE.match(prefix_buf)
                    to_emit = prefix_buf[m.end():] if m else prefix_buf
                    if to_emit:
                        yield to_emit, None, None
                    prefix_cleared = True
            else:
                yield text, None, None

        # 짧은 응답으로 prefix_buf 가 비워지지 않은 경우
        if not prefix_cleared and prefix_buf:
            m = _EMOTION_PREFIX_RE.match(prefix_buf)
            to_emit = prefix_buf[m.end():] if m else prefix_buf
            if to_emit:
                yield to_emit, None, None

        self.history.append({"role": "assistant", "content": full})
        emotion, body = parse_emotion(full)
        self._trim_history()
        yield None, emotion, body

    # ============================================================
    # 유틸
    # ============================================================
    def _trim_history(self):
        if len(self.history) > 60:
            self.history = self.history[-60:]

    def switch_backend(self, backend: str):
        if backend not in ("claude", "openai", "ollama", "compare"):
            raise ValueError(f"지원하지 않는 백엔드: {backend}")
        cfg.llm_backend = backend
        self._init_backend()

    def reset_history(self):
        self.history = []

    # ============================================================
    # Expert Pool — 자동 폴백 체인
    # ============================================================
    def available_backends(self) -> list:
        """현재 사용 가능한 백엔드 목록 (키/클라이언트 보유 기준).

        사이클 #3 #2: Ollama 는 헬스체크 (60초 캐시) 통과 시 항상 후보 포함.
        """
        out = []
        if self.anthropic_client is not None:
            out.append("claude")
        if self.openai_client is not None:
            out.append("openai")
        # ollama: 현재 backend 가 ollama 면 client 직접, 아니면 헬스체크
        if cfg.llm_backend == "ollama" and self.client is not None:
            out.append("ollama")
        elif _ollama_healthcheck():
            out.append("ollama")
        return out

    def _fallback_chain(self, primary: str) -> list:
        """primary → 사용 가능한 다른 백엔드 순 (compare 제외)."""
        avail = self.available_backends()
        chain = []
        if primary in avail:
            chain.append(primary)
        for b in avail:
            if b != primary and b not in chain:
                chain.append(b)
        return chain

    def think_stream_with_fallback(
        self, user_message: str, context: Optional[str] = None,
        on_fallback=None,
    ):
        """think_stream 의 폴백 버전.

        설계 원칙 (architect P1 피드백 반영):
          1) **전역 `cfg.llm_backend` 를 변경하지 않는다.** 후보 백엔드별 클라이언트는
             Brain 인스턴스가 보유한 anthropic_client/openai_client/(ollama)client 를
             직접 선택하여 _stream_* 메서드에 일시 바인딩한다. 다른 세션과의 경합 없음.
          2) **실패 판정은 예외 기반.** "⚠ 시작 휴리스틱" 제거 — 정상 응답 오탐 위험 차단.
             각 _stream_* 가 raise 하면 다음 후보로, 정상 yield 완료시 종료.
          3) compare 모드는 폴백 비대상 (think_stream 위임).
        """
        backend = cfg.llm_backend
        if backend == "compare":
            yield from self.think_stream(user_message, context)
            return

        # 컨텍스트 결합 + user 턴 1회만 추가 (모든 후보가 같은 history 공유)
        merged = user_message
        if context:
            merged = f"[컨텍스트: {context}]\n\n{user_message}"

        chain = self._fallback_chain(backend)
        if not chain:
            yield None, Emotion.CONCERNED, (
                "사용 가능한 LLM 백엔드가 없습니다. API 키를 설정해주세요."
            )
            return

        # user 턴은 단 한 번 추가 — 후보 시도 사이에 중복 추가/삭제 안 함
        self.history.append({"role": "user", "content": merged})
        history_snapshot_len = len(self.history)

        last_friendly_error = None
        original_client = self.client  # 인스턴스 단위 임시 바인딩 — cfg 미변경

        try:
            for idx, candidate in enumerate(chain):
                # 후보용 client 선택
                cand_client = self._client_for(candidate)
                if cand_client is None:
                    last_friendly_error = (
                        f"⚠ {candidate.upper()} 백엔드 클라이언트를 찾을 수 없습니다."
                    )
                    continue

                # 폴백 알림 (1차 후보 외)
                if idx > 0 and on_fallback is not None:
                    try:
                        on_fallback(chain[idx - 1], candidate, last_friendly_error or "primary_failed")
                    except (TypeError, ValueError):
                        pass

                # 인스턴스 단위 임시 바인딩 — 같은 Brain 의 다른 메서드 호출 시까지만 유지
                self.client = cand_client
                try:
                    stream_iter = self._dispatch_stream(candidate)
                    for item in stream_iter:
                        yield item
                    return  # 정상 완료
                except Exception as e:
                    traceback.print_exc()
                    last_friendly_error = _friendly_error(e, candidate)
                    # _stream_* 가 self.history 에 assistant 턴을 부분 추가했을 수 있음 → 롤백
                    while len(self.history) > history_snapshot_len:
                        self.history.pop()
                    # 다음 후보로

            # 모든 후보 실패
            yield None, Emotion.CONCERNED, (
                last_friendly_error
                or "모든 LLM 백엔드가 응답하지 않습니다. 잠시 후 다시 시도해주세요."
            )
        finally:
            # 클라이언트 원복
            self.client = original_client

    def _client_for(self, backend: str):
        """backend 이름 → 해당 클라이언트. 없으면 None.

        사이클 #3 #2: ollama 가 다른 모드에서도 후보일 때 즉석 client 반환 (캐시).
        """
        if backend == "claude":
            return self.anthropic_client
        if backend == "openai":
            return self.openai_client
        if backend == "ollama":
            if cfg.llm_backend == "ollama" and self.client is not None:
                return self.client
            # 헬스체크 캐시에 client 가 있으면 재사용
            if _ollama_health_cache.get("ok") and _ollama_health_cache.get("client") is not None:
                return _ollama_health_cache["client"]
            return None
        return None

    def _dispatch_stream(self, backend: str):
        """후보 백엔드의 _stream_* 호출. self.client 는 호출 전에 바인딩되어 있어야 함."""
        if backend == "claude":
            if self.tools is not None:
                # 도구 모드는 비스트리밍 — 한 번에 결과 yield
                emotion, body = self._think_with_tools()
                yield None, emotion, body
                return
            yield from self._stream_claude()
        elif backend == "openai":
            yield from self._stream_openai()
        elif backend == "ollama":
            yield from self._stream_ollama()
        else:
            raise ValueError(f"지원하지 않는 백엔드: {backend}")

    # ============================================================
    # Generate-Verify 보강 — TTS 차단 시 안전 재작성 (사이클 #3 #1)
    # ============================================================
    def regenerate_safe_tts(self, original: str, reason: str) -> str:
        """TTS 차단 응답을 안전·간결하게 재작성. 1회 호출, 짧은 응답.

        - 히스토리/도구를 우회하고 직접 LLM 1회 호출 (재귀 폴백 없음).
        - 가용 backend (anthropic 우선, 없으면 openai) 선택.
        - 실패 시 빈 문자열 → 호출자 (audio_io) 가 합성 포기.
        """
        if not original or not original.strip():
            return ""

        prompt = (
            "다음 한국어 응답을 음성 합성에 적합하도록 다시 써줘. 규칙:\n"
            "1) 핵심 의미는 유지하되 더 짧고 간결하게 (3문장 이내)\n"
            "2) 민감정보·시크릿·URL·제어문자 제거\n"
            "3) 마크다운/코드블록/이모지 제거, 일반 한국어 문장만\n"
            f"4) 차단 사유: {reason}\n\n"
            f"원본:\n{original[:1500]}\n\n재작성:"
        )

        try:
            if self.anthropic_client is not None:
                msg = self.anthropic_client.messages.create(
                    model=cfg.claude_model,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                )
                parts = []
                for b in msg.content:
                    if hasattr(b, "text"):
                        parts.append(b.text)
                return "".join(parts).strip()

            if self.openai_client is not None:
                resp = self.openai_client.chat.completions.create(
                    model=cfg.openai_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=400,
                )
                return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"[Brain.regenerate_safe_tts] 실패: {type(e).__name__}: {e}")
        return ""


# ============================================================
# 모듈 레벨 — Ollama 헬스체크 (캐시 60s) — 사이클 #3 #2
# ============================================================
def _ollama_healthcheck() -> bool:
    """Ollama 호스트 도달 여부 (60s 캐시). 성공 시 client 도 캐시.

    cfg.ollama_host (예: http://localhost:11434) 의 /api/tags 를 호출.
    네트워크/모듈 미설치/타임아웃 모두 False 반환 (절대 raise 하지 않음).

    architect 사이클 #3 P2 피드백: timeout 0.3 → 1.2초 + 1회 재시도로 false-negative 감소.
    캐시 TTL 60s 라 1.2s × 2 = 최대 2.4초 비용은 60초마다 1회만 발생.
    """
    now = time.time()
    if (now - _ollama_health_cache["checked_at"]) < _OLLAMA_HEALTH_TTL:
        return _ollama_health_cache["ok"]

    _ollama_health_cache["checked_at"] = now
    last_err = None
    for attempt in (1, 2):
        try:
            import ollama
            client = ollama.Client(host=cfg.ollama_host, timeout=1.2)
            # /api/tags — 빠른 ping. 모델 미존재여도 200 반환.
            client.list()
            _ollama_health_cache["ok"] = True
            _ollama_health_cache["client"] = client
            return True
        except Exception as e:
            last_err = e
            if attempt == 1:
                time.sleep(0.1)  # 짧은 backoff 후 1회 재시도
                continue
    _ollama_health_cache["ok"] = False
    _ollama_health_cache["client"] = None
    return False


def reset_ollama_health_cache() -> None:
    """관리/테스트용 — 다음 호출 시 강제 재체크."""
    _ollama_health_cache["checked_at"] = 0.0
    _ollama_health_cache["ok"] = False
    _ollama_health_cache["client"] = None
