from __future__ import annotations

from typing import Callable

import anthropic


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, model: str, api_key: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def chat(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        return resp.content[0].text.strip()

    def chat_with_tools(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_choice: dict,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> dict:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
        )
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        return {}

    def chat_agentic(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_executor: Callable[[str, dict], str],
        max_tokens: int = 4096,
        max_turns: int = 10,
    ) -> str:
        msgs = list(messages)

        for _ in range(max_turns):
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0.0,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=msgs,
                tools=tools,
            )

            if resp.stop_reason == "end_turn":
                return "".join(
                    b.text for b in resp.content if b.type == "text"
                ).strip()

            if resp.stop_reason == "tool_use":
                # 把 assistant 回复（含 tool_use 块）加入历史
                msgs.append({"role": "assistant", "content": _blocks_to_dicts(resp.content)})

                # 执行所有 tool 调用，收集结果
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        result = tool_executor(block.name, dict(block.input))
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                msgs.append({"role": "user", "content": tool_results})
            else:
                # max_tokens 或其他原因停止，提取已有文本
                break

        return "".join(
            b.text for b in resp.content if b.type == "text"
        ).strip()


def _blocks_to_dicts(content_blocks) -> list[dict]:
    """把 Anthropic SDK 返回的 content blocks 转成可序列化的 dict。"""
    result = []
    for b in content_blocks:
        if b.type == "text":
            result.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            result.append({"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input)})
    return result
