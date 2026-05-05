"""
三种 chunking 策略，统一接口：chunk(text) -> list[str]
"""
from __future__ import annotations

import re

import tiktoken


def _count_tokens(text: str, enc: tiktoken.Encoding) -> int:
    return len(enc.encode(text))


# ── 1. Fixed sliding window（当前方案）────────────────────

class FixedChunker:
    """固定 token 滑窗切分。"""

    name = "fixed"

    def __init__(self, chunk_tokens: int = 512, chunk_overlap: int = 128):
        self.chunk_tokens = chunk_tokens
        self.chunk_overlap = chunk_overlap
        self.enc = tiktoken.get_encoding("cl100k_base")

    def chunk(self, text: str) -> list[str]:
        tokens = self.enc.encode(text)
        chunks, start = [], 0
        while start < len(tokens):
            end = min(start + self.chunk_tokens, len(tokens))
            chunks.append(self.enc.decode(tokens[start:end]))
            if end == len(tokens):
                break
            start += self.chunk_tokens - self.chunk_overlap
        return chunks


# ── 2. Recursive character splitter ──────────────────────

class RecursiveChunker:
    """
    按层级分隔符递归切分，优先保持段落完整性。
    分隔符优先级: \\n\\n → \\n → '. ' → ' ' → 字符
    """

    name = "recursive"

    def __init__(self, max_tokens: int = 512, overlap_tokens: int = 64):
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.enc = tiktoken.get_encoding("cl100k_base")
        self._separators = ["\n\n", "\n", ". ", " ", ""]

    def _split_by_sep(self, text: str, sep: str) -> list[str]:
        if sep == "":
            # 字符级兜底：按 max_tokens 硬切
            tokens = self.enc.encode(text)
            return [self.enc.decode(tokens[i:i + self.max_tokens])
                    for i in range(0, len(tokens), self.max_tokens)]
        return [s for s in text.split(sep) if s.strip()]

    def _merge_with_overlap(self, pieces: list[str], sep: str) -> list[str]:
        """把小片段合并成不超过 max_tokens 的 chunk，末尾保留 overlap。"""
        chunks: list[str] = []
        current_pieces: list[str] = []
        current_tokens = 0

        for piece in pieces:
            piece_tokens = _count_tokens(piece, self.enc)
            if current_tokens + piece_tokens > self.max_tokens and current_pieces:
                chunks.append(sep.join(current_pieces))
                # 保留末尾 overlap
                overlap_pieces: list[str] = []
                overlap_total = 0
                for p in reversed(current_pieces):
                    pt = _count_tokens(p, self.enc)
                    if overlap_total + pt > self.overlap_tokens:
                        break
                    overlap_pieces.insert(0, p)
                    overlap_total += pt
                current_pieces = overlap_pieces
                current_tokens = overlap_total

            current_pieces.append(piece)
            current_tokens += piece_tokens

        if current_pieces:
            chunks.append(sep.join(current_pieces))

        return chunks

    def _recursive_split(self, text: str, sep_idx: int = 0) -> list[str]:
        if _count_tokens(text, self.enc) <= self.max_tokens:
            return [text]

        sep = self._separators[sep_idx]
        pieces = self._split_by_sep(text, sep)

        # 对超大片段递归细分
        final_pieces: list[str] = []
        for p in pieces:
            if _count_tokens(p, self.enc) > self.max_tokens:
                final_pieces.extend(self._recursive_split(p, sep_idx + 1))
            else:
                final_pieces.append(p)

        return self._merge_with_overlap(final_pieces, sep if sep else "")

    def chunk(self, text: str) -> list[str]:
        return [c for c in self._recursive_split(text) if c.strip()]


# ── 3. Semantic chunker ───────────────────────────────────

class SemanticChunker:
    """
    按句子语义相似度聚合：相邻句子 cosine 相似度低于阈值时切断。
    需要外部传入 embedder（避免在 chunker 内部加载模型）。
    """

    name = "semantic"

    def __init__(
        self,
        embedder,
        similarity_threshold: float = 0.75,
        max_tokens: int = 512,
        min_tokens: int = 50,
    ):
        self.embedder = embedder
        self.threshold = similarity_threshold
        self.max_tokens = max_tokens
        self.min_tokens = min_tokens
        self.enc = tiktoken.get_encoding("cl100k_base")

    def _split_sentences(self, text: str) -> list[str]:
        # 按句号/问号/感叹号切句，保留标点
        sentences = re.split(r'(?<=[.!?])\s+', text)
        return [s.strip() for s in sentences if s.strip()]

    def _cosine(self, a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb + 1e-9)

    def chunk(self, text: str) -> list[str]:
        sentences = self._split_sentences(text)
        if not sentences:
            return []
        if len(sentences) == 1:
            return sentences

        # 批量 embed 所有句子
        vecs = self.embedder.encode(sentences, batch_size=64)

        # 按相似度贪婪聚合
        chunks: list[str] = []
        group: list[str] = [sentences[0]]
        group_tokens = _count_tokens(sentences[0], self.enc)

        for i in range(1, len(sentences)):
            sim = self._cosine(vecs[i - 1], vecs[i])
            s_tokens = _count_tokens(sentences[i], self.enc)

            if (sim < self.threshold or group_tokens + s_tokens > self.max_tokens) \
                    and group_tokens >= self.min_tokens:
                chunks.append(" ".join(group))
                group = [sentences[i]]
                group_tokens = s_tokens
            else:
                group.append(sentences[i])
                group_tokens += s_tokens

        if group:
            chunks.append(" ".join(group))

        return chunks
