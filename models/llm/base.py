from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    """统一 LLM 调用接口。当前实现 Gemini；新增 provider 只需实现本 Protocol。"""

    provider: str  # "gemini"

    def chat(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str: ...

    def chat_with_tools(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_choice: dict,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> dict:
        """单次结构化输出：强制调用指定 tool，返回 tool input dict。"""
        ...

    def chat_agentic(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_executor: Callable[[str, dict], str],
        max_tokens: int = 4096,
        max_turns: int = 10,
    ) -> str:
        """多轮 agentic loop：LLM 可反复调用 tools，直到 end_turn，返回最终文本。"""
        ...
