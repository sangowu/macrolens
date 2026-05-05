from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingBackend(Protocol):
    dim: int

    def encode(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        ...


@runtime_checkable
class RerankerBackend(Protocol):
    def rerank(self, query: str, documents: list[str]) -> list[float]:
        ...
