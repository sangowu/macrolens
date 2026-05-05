from __future__ import annotations

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
