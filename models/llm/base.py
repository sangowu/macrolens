from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    """统一 LLM 调用接口，屏蔽 Anthropic / Gemini 差异。"""

    provider: str  # "anthropic" | "gemini"

    def chat(
        self,
        system: str,
        messages: list[dict],  # [{"role": "user"|"assistant", "content": str}]
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str: ...
