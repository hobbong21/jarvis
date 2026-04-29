"""두뇌 — Claude tool_use 루프 / Ollama 단순 채팅. 감정 태그 파싱 포함."""
import traceback
from typing import Optional, Tuple

from config import cfg
from emotion import Emotion, parse_emotion


class Brain:
    """JARVIS의 LLM 컨트롤러.

    Microsoft JARVIS의 4단계 패턴을 Claude tool_use 한 번의 호출로 구현:
      1) Task Planning   → LLM이 사용자 요청 해석
      2) Model Selection → LLM이 적절한 도구 선택
      3) Task Execution  → ToolExecutor가 도구 실행
      4) Response Gen    → LLM이 도구 결과 종합 후 최종 답변
    """

    def __init__(self, tool_executor=None):
        self.history = []
        self.client = None
        self.tools = tool_executor
        self._init_backend()

    def _init_backend(self):
        if cfg.llm_backend == "claude":
            from anthropic import Anthropic
            if not cfg.anthropic_api_key:
                print(
                    "[Brain] 경고: ANTHROPIC_API_KEY 환경변수가 없습니다. "
                    "설정 후 재시작하세요."
                )
                self.client = None
                return
            self.client = Anthropic(api_key=cfg.anthropic_api_key)
        elif cfg.llm_backend == "ollama":
            import ollama
            self.client = ollama.Client(host=cfg.ollama_host)
            try:
                self.client.show(cfg.ollama_model)
            except Exception:
                print(f"[Brain] Ollama 모델 '{cfg.ollama_model}' 다운로드 중...")
                self.client.pull(cfg.ollama_model)
        else:
            raise ValueError(f"알 수 없는 백엔드: {cfg.llm_backend}")

    def get_client(self):
        """비전 도구가 같은 Anthropic 클라이언트를 재사용하기 위함"""
        return self.client if cfg.llm_backend == "claude" else None

    def think(
        self, user_message: str, context: Optional[str] = None
    ) -> Tuple[Emotion, str]:
        if context:
            user_message = f"[컨텍스트: {context}]\n\n{user_message}"
        self.history.append({"role": "user", "content": user_message})

        try:
            if self.client is None:
                return Emotion.CONCERNED, (
                    "ANTHROPIC_API_KEY가 설정되지 않았습니다. "
                    "환경변수에 API 키를 추가하고 서버를 재시작해주세요."
                )
            if cfg.llm_backend == "claude" and self.tools is not None:
                return self._think_with_tools()
            elif cfg.llm_backend == "claude":
                return self._think_claude_simple()
            else:
                return self._think_ollama()
        except Exception as e:
            traceback.print_exc()
            return Emotion.CONCERNED, f"AI 통신 오류가 발생했어요. {e}"

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
    # 유틸
    # ============================================================
    def _trim_history(self):
        if len(self.history) > 60:
            self.history = self.history[-60:]

    def switch_backend(self, backend: str):
        cfg.llm_backend = backend
        self._init_backend()

    def reset_history(self):
        self.history = []
