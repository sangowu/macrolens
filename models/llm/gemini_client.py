from __future__ import annotations

from google import genai
from google.genai import types


class GeminiClient:
    provider = "gemini"

    def __init__(self, model: str, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self.model = model

    def chat(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        # Gemini 用 contents 列表，system 单独传
        contents = [
            types.Content(
                role=m["role"],
                parts=[types.Part(text=m["content"])],
            )
            for m in messages
        ]

        resp = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                temperature=temperature,
            ),
        )
        return resp.text.strip()
