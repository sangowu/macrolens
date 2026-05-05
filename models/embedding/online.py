from __future__ import annotations

from typing import Literal


class OnlineEmbedding:
    """商业 Embedding API：Voyage AI 或 OpenAI。

    - Voyage AI: pip install voyageai
    - OpenAI:    pip install openai
    """

    def __init__(
        self,
        provider: Literal["voyage", "openai"],
        model: str,
        dim: int,
        api_key: str,
        base_url: str | None = None,
    ):
        self.dim = dim
        self._provider = provider
        self._model = model

        if provider == "voyage":
            import voyageai
            self._client = voyageai.Client(api_key=api_key)
        elif provider == "openai":
            from openai import OpenAI
            self._client = OpenAI(api_key=api_key, base_url=base_url)
        else:
            raise ValueError(f"不支持的 online provider: {provider!r}")

    def encode(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        results: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            if self._provider == "voyage":
                resp = self._client.embed(batch, model=self._model)
                results.extend(resp.embeddings)
            else:
                resp = self._client.embeddings.create(input=batch, model=self._model)
                results.extend(d.embedding for d in resp.data)
        return results
