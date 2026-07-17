from __future__ import annotations


class LocalServerEmbedding:
    """本地 llama.cpp embedding server（OpenAI 兼容 /v1/embeddings）。

    连接本机常驻的 llama-server，无需任何外部 API key。
    模型：Qwen3-Embedding-0.6B（F16 GGUF），last-token pooling，1024 维，
    与库里已入库向量逐位兼容（doc-side cosine ≈ 0.99998）。

    启动服务（见 README）：
        llama-server.exe -m Qwen3-Embedding-0.6B-f16.gguf \\
            --embedding --pooling last -ngl 99 -c 2048 -b 2048 -ub 2048 \\
            --host 127.0.0.1 --port 8081
    """

    def __init__(self, base_url: str, model: str, dim: int = 1024) -> None:
        from openai import OpenAI

        self.dim = dim
        self._model = model
        # llama.cpp 不校验 key，占位符即可
        self._client = OpenAI(api_key="sk-no-key-required", base_url=base_url)

    def encode(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        results: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = self._client.embeddings.create(input=batch, model=self._model)
            results.extend(d.embedding for d in resp.data)
        return results
