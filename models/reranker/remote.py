from __future__ import annotations

try:
    import httpx
except ImportError:
    raise ImportError("httpx 未安装，请执行: pip install httpx")


class RemoteReranker:
    """HTTP 客户端，调用 cloud_server FastAPI 的 /rerank 端点。"""

    def __init__(self, base_url: str = "http://localhost:18000", timeout: float = 60.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        resp = self._client.post(
            "/rerank",
            json={"query": query, "documents": documents},
        ).raise_for_status()
        return resp.json()["scores"]

    def close(self) -> None:
        self._client.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
