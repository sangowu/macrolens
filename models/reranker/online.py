from __future__ import annotations

import os
from typing import Literal


class OnlineReranker:
    """商业 Reranker API：Cohere 或 DashScope（阿里云 qwen3-rerank）。

    pip install cohere          # Cohere
    pip install httpx           # DashScope
    """

    DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"

    def __init__(
        self,
        provider: Literal["cohere", "dashscope"],
        model: str,
        api_key: str,
    ):
        self._provider = provider
        self._model = model
        self._api_key = api_key

        if provider == "cohere":
            import cohere
            self._client = cohere.Client(api_key=api_key)
        elif provider == "dashscope":
            import httpx
            self._http = httpx.Client(timeout=60)
        else:
            raise ValueError(f"不支持的 online reranker provider: {provider!r}")

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []

        if self._provider == "cohere":
            resp = self._client.rerank(
                model=self._model,
                query=query,
                documents=documents,
                return_documents=False,
            )
            scores_by_idx = {r.index: r.relevance_score for r in resp.results}
            return [scores_by_idx[i] for i in range(len(documents))]

        if self._provider == "dashscope":
            resp = self._http.post(
                self.DASHSCOPE_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "query": query,
                    "documents": documents,
                    "top_n": len(documents),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # DashScope 返回按相关度排序，还原为原始顺序
            scores_by_idx = {r["index"]: r["relevance_score"] for r in data["results"]}
            return [scores_by_idx[i] for i in range(len(documents))]

        raise ValueError(f"Unknown provider: {self._provider!r}")
