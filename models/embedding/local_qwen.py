from __future__ import annotations

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    raise ImportError("sentence-transformers 未安装，请执行: pip install sentence-transformers")


class LocalQwenEmbedding:
    """Qwen3-Embedding via sentence-transformers。

    同样适用于 BGE-large-en-v1.5 / BGE-small-en-v1.5 等
    sentence-transformers 兼容模型。
    """

    def __init__(self, model_id: str = "Qwen/Qwen3-Embedding-0.6B", device: str = "cuda"):
        self._model = SentenceTransformer(model_id, device=device)
        self.dim: int = self._model.get_sentence_embedding_dimension()

    def encode(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        vecs = self._model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vecs.tolist()
