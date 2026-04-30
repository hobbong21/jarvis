"""두뇌 — Claude tool_use 루프 / Ollama 단순 채팅. 감정 태그 파싱 포함."""
import re
import traceback
from typing import Generator, Iterator, Optional, Tuple

from config import cfg
from emotion import Emotion, parse_emotion

_EMOTION_PREFIX_RE = re.compile(r"^\s*\[emotion:\w+\]\s*", re.IGNORECASE)


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

        # compare 모드는 음성 흐름에서 호출되지 않음 — Claude 우선 폴백
        if backend == "compare":
            if self.anthropic_client is not None:
                self.client = self.anthropic_client
                if self.tools is not None:
                    emotion, body = self._think_with_tools()
                    yield None, emotion, body
                    return
                yield from self._stream_claude()
                return
            if self.openai_client is not None:
                self.client = self.openai_client
                yield from self._stream_openai()
                return
            yield None, Emotion.CONCERNED, "비교 모드용 API 키가 없습니다."
            return

        if self.client is None:
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
            yield None, Emotion.CONCERNED, f"AI 통신 오류: {e}"

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
                q.put(("claude", None, Emotion.CONCERNED, f"Claude 오류: {e}"))

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
                q.put(("openai", None, Emotion.CONCERNED, f"OpenAI 오류: {e}"))

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
