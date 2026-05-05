from __future__ import annotations

try:
    from FlagEmbedding import FlagReranker
except ImportError:
    raise ImportError("FlagEmbedding 未安装，请执行: pip install FlagEmbedding")


class LocalReranker:
    """BGE-Reranker-v2-m3，基于 FlagEmbedding。"""

    def __init__(self, model_id: str = "BAAI/bge-reranker-v2-m3", device: str = "cuda"):
        self._model = FlagReranker(model_id, use_fp16=True, device=device)

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        pairs = [[query, doc] for doc in documents]
        scores = self._model.compute_score(pairs, normalize=True)
        if isinstance(scores, float):
            return [scores]
        return list(scores)
