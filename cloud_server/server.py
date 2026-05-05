"""MacroLens 云服务器推理服务。

部署在云机器上，通过 SSH tunnel 供本机 remote backend 调用。

启动方式:
    EMBED_MODEL=BAAI/bge-m3 RERANK_MODEL=BAAI/bge-reranker-v2-m3 \
    uvicorn server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

_embedder = None
_reranker = None


def _load_model_path(model_id: str) -> str:
    """用 modelscope 下载模型，返回本地路径；若已缓存则直接返回。"""
    from modelscope import snapshot_download
    return snapshot_download(model_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _embedder, _reranker

    from FlagEmbedding import BGEM3FlagModel, FlagReranker

    embed_model_id = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
    rerank_model_id = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")

    print(f"Downloading embed model: {embed_model_id}")
    embed_path = _load_model_path(embed_model_id)
    print(f"Downloading rerank model: {rerank_model_id}")
    rerank_path = _load_model_path(rerank_model_id)

    _embedder = BGEM3FlagModel(embed_path, use_fp16=True)
    _reranker = FlagReranker(rerank_path, use_fp16=True)

    yield

    _embedder = None
    _reranker = None


app = FastAPI(title="MacroLens Model Server", version="0.1.0", lifespan=lifespan)


class EmbedRequest(BaseModel):
    texts: list[str]
    batch_size: int = 32


class RerankRequest(BaseModel):
    query: str
    documents: list[str]


@app.get("/info")
def info():
    return {
        "embedding_dim": 1024,
        "embed_model": os.getenv("EMBED_MODEL", "BAAI/bge-m3"),
        "rerank_model": os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"),
    }


@app.post("/embed")
def embed(req: EmbedRequest):
    out = _embedder.encode(
        req.texts,
        batch_size=req.batch_size,
        max_length=512,
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
    )
    return {"embeddings": out["dense_vecs"].tolist()}


@app.post("/rerank")
def rerank(req: RerankRequest):
    if not req.documents:
        return {"scores": []}
    pairs = [[req.query, doc] for doc in req.documents]
    scores = _reranker.compute_score(pairs, normalize=True)
    if isinstance(scores, float):
        scores = [scores]
    return {"scores": list(scores)}
