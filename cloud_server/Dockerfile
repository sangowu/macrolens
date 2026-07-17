FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    "sentence-transformers<4" \
    "FlagEmbedding>=1.2,<2" \
    modelscope \
    httpx

COPY server.py .

ENV EMBED_MODEL=BAAI/bge-m3
ENV RERANK_MODEL=BAAI/bge-reranker-v2-m3
ENV HF_HUB_ENABLE_HF_TRANSFER=0

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
