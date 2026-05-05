from __future__ import annotations

try:
    from FlagEmbedding import BGEM3FlagModel
except ImportError:
    raise ImportError("FlagEmbedding 未安装，请执行: pip install FlagEmbedding")


class LocalBGEEmbedding:
    """BGE-M3 dense embedding，基于 FlagEmbedding。

    BGE-M3 同时支持 dense / sparse / colbert，MVP 只用 dense。
    sparse 留作后续 hybrid 增强实验（见设计文档 §9.1）。
    """

    dim: int = 1024

    def __init__(self, model_id: str = "BAAI/bge-m3", device: str = "cuda"):
        self._model = BGEM3FlagModel(model_id, use_fp16=True, device=device)

    def encode(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        out = self._model.encode(
            texts,
            batch_size=batch_size,
            max_length=512,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        return out["dense_vecs"].tolist()
