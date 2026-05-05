from __future__ import annotations

try:
    import httpx
except ImportError:
    raise ImportError("httpx 未安装，请执行: pip install httpx")


class RemoteEmbedding:
    """HTTP 客户端，调用 cloud_server FastAPI 的 /embed 端点。

    SSH tunnel 由 factory.managed_models() 在外部统一建立，
    此类只需知道本地监听地址（base_url）。
    """

    def __init__(self, base_url: str = "http://localhost:18000", timeout: float = 120.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        info = self._client.get("/info").raise_for_status().json()
        self.dim: int = info["embedding_dim"]

    def encode(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        from tqdm import tqdm
        results = []
        batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]
        for batch in tqdm(batches, desc="embedding", unit="batch", leave=False):
            resp = self._client.post(
                "/embed",
                json={"texts": batch, "batch_size": batch_size},
            ).raise_for_status()
            results.extend(resp.json()["embeddings"])
        return results

    def close(self) -> None:
        self._client.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
