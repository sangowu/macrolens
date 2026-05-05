"""
Chunk 策略对比：Fixed / Recursive / Semantic

流程:
  1. 取前 N 份 10-K 文件，用三种策略各自 chunk + embed → ablation_chunks 表
  2. 对 Set A 每题做向量检索（top-k）
  3. LLM 评估 context_precision / context_recall
  4. 输出对比 CSV + 控制台摘要

用法:
    uv run eval/chunk_ablation.py
    uv run eval/chunk_ablation.py --files 5 --top-k 10
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

import psycopg
from pgvector.psycopg import register_vector
from tqdm import tqdm
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval.metrics import context_precision, context_recall
from eval.questions import SET_A
from ingestion.ingest_sec import iter_filing_files, parse_filing_meta
from ingestion.chunkers import FixedChunker, RecursiveChunker, SemanticChunker
from models.config import load_config
from models.factory import create_embedding, create_llm_client

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS ablation_chunks (
    id          BIGSERIAL PRIMARY KEY,
    strategy    TEXT NOT NULL,
    doc_type    TEXT,
    fiscal_year SMALLINT,
    content     TEXT NOT NULL,
    token_count INTEGER,
    embedding   vector(1024)
)
"""

INSERT_SQL = """
INSERT INTO ablation_chunks (strategy, doc_type, fiscal_year, content, token_count, embedding)
VALUES (%(strategy)s, %(doc_type)s, %(fiscal_year)s, %(content)s, %(token_count)s, %(embedding)s)
"""

SEARCH_SQL = """
SELECT content, doc_type, fiscal_year, embedding <=> %(vec)s::vector AS distance
FROM ablation_chunks
WHERE strategy = %(strategy)s AND embedding IS NOT NULL
ORDER BY embedding <=> %(vec)s::vector
LIMIT %(top_k)s
"""


def extract_text(html_path: Path) -> str:
    """BeautifulSoup 提取纯文本。"""
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "ix:header"]):
        tag.decompose()
    return soup.get_text(separator="\n")


def build_table(conn: psycopg.Connection, embedder, files: list[Path]) -> None:
    """构建三种策略的 ablation_chunks 表。"""
    register_vector(conn)
    conn.execute("DROP TABLE IF EXISTS ablation_chunks")
    conn.execute(CREATE_TABLE)
    conn.commit()

    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")

    chunkers = [
        FixedChunker(chunk_tokens=512, chunk_overlap=128),
        RecursiveChunker(max_tokens=512, overlap_tokens=64),
        SemanticChunker(embedder=embedder, similarity_threshold=0.75, max_tokens=512),
    ]

    for chunker in chunkers:
        print(f"\n  [{chunker.name}] chunking {len(files)} files...")
        total_chunks = 0

        with tqdm(files, desc=f"  {chunker.name}", leave=False) as pbar:
            for html_path in pbar:
                meta = parse_filing_meta(html_path.parent)
                text = extract_text(html_path)

                try:
                    chunks = chunker.chunk(text)
                except Exception as e:
                    tqdm.write(f"  chunk error {html_path.parent.name}: {e}")
                    continue

                if not chunks:
                    continue

                # 过滤过短的 chunk
                chunks = [c for c in chunks if len(c.strip()) > 50]

                # 批量 embed
                vectors = embedder.encode(chunks, batch_size=32)

                rows = []
                for text_chunk, vec in zip(chunks, vectors):
                    token_count = len(enc.encode(text_chunk))
                    rows.append({
                        "strategy":    chunker.name,
                        "doc_type":    meta["doc_type"],
                        "fiscal_year": meta["fiscal_year"],
                        "content":     text_chunk[:2000],
                        "token_count": token_count,
                        "embedding":   vec,
                    })

                with conn.cursor() as cur:
                    cur.executemany(INSERT_SQL, rows)
                conn.commit()
                total_chunks += len(rows)

        print(f"    -> {total_chunks} chunks")


def retrieve(conn: psycopg.Connection, embedder, strategy: str, query: str, top_k: int) -> list[dict]:
    vec = embedder.encode([query])[0]
    rows = conn.execute(SEARCH_SQL, {"vec": vec, "strategy": strategy, "top_k": top_k}).fetchall()
    return [
        {"source": "sec_chunks", "content": r[0], "doc_type": r[1], "fiscal_year": r[2], "distance": float(r[3])}
        for r in rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=3, help="使用前 N 份 10-K 文件")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default="eval/chunk_ablation.csv")
    args = parser.parse_args()

    cfg = load_config(args.config)
    embedder = create_embedding(cfg)
    llm = create_llm_client(cfg)

    all_10k = [f for f in iter_filing_files() if "10-K" in str(f)]
    files = sorted(all_10k, reverse=True)[:args.files]
    print(f"Files selected: {len(files)}")
    for f in files:
        meta = parse_filing_meta(f.parent)
        print(f"  {meta['doc_type']} FY{meta['fiscal_year']} | {f.parent.name}")

    fieldnames = [
        "qid", "question", "strategy",
        "n_chunks", "avg_tokens",
        "context_precision", "context_recall",
        "precision_reason", "recall_reason",
    ]

    output_path = Path(args.output)
    output_path.parent.mkdir(exist_ok=True)

    strategies = ["fixed", "recursive", "semantic"]

    with psycopg.connect(cfg.db.dsn) as conn:
        register_vector(conn)

        print("\nBuilding ablation table (this may take a few minutes)...")
        build_table(conn, embedder, files)

        # 每种策略的 chunk 统计
        for s in strategies:
            row = conn.execute(
                "SELECT count(*), avg(token_count) FROM ablation_chunks WHERE strategy = %s", (s,)
            ).fetchone()
            print(f"  {s:<12}: {row[0]} chunks, avg {row[1]:.0f} tokens/chunk")

        print("\nRunning retrieval evaluation...")

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for q in SET_A:
                print(f"\n[{q.qid}] {q.question[:60]}...")

                for strategy in strategies:
                    context = retrieve(conn, embedder, strategy, q.question, args.top_k)

                    # chunk 统计
                    n_chunks = len(context)
                    avg_tokens = 0
                    if context:
                        import tiktoken
                        enc = tiktoken.get_encoding("cl100k_base")
                        avg_tokens = sum(len(enc.encode(c["content"])) for c in context) / n_chunks

                    try:
                        prec = context_precision(q.question, context, llm)
                        rec  = context_recall(q.question, q.ground_truth, context, llm)

                        writer.writerow({
                            "qid": q.qid,
                            "question": q.question,
                            "strategy": strategy,
                            "n_chunks": n_chunks,
                            "avg_tokens": round(avg_tokens),
                            "context_precision": prec.get("score"),
                            "context_recall":    rec.get("score"),
                            "precision_reason":  prec.get("reason", ""),
                            "recall_reason":     rec.get("reason", ""),
                        })
                        f.flush()

                        print(f"  {strategy:<12}: precision={prec.get('score', 0):.2f}  recall={rec.get('score', 0):.2f}  avg_tokens={avg_tokens:.0f}")
                        time.sleep(0.5)

                    except Exception as e:
                        print(f"  {strategy}: ERROR {e}")

    print(f"\nResults saved to {output_path}")
    _summary(output_path)


def _summary(path: Path) -> None:
    import statistics
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    print("\n── Chunk Strategy Comparison ───────────────────────")
    print(f"  {'Strategy':<12}  {'Precision':>9}  {'Recall':>7}  {'Avg Tokens':>10}")
    print("  " + "-" * 44)
    for strategy in ["fixed", "recursive", "semantic"]:
        sr = [r for r in rows if r["strategy"] == strategy]
        precs = [float(r["context_precision"]) for r in sr if r.get("context_precision") not in ("", "None", None)]
        recs  = [float(r["context_recall"])    for r in sr if r.get("context_recall")    not in ("", "None", None)]
        tokens = [float(r["avg_tokens"]) for r in sr if r.get("avg_tokens") not in ("", "None", None)]
        if precs:
            print(f"  {strategy:<12}  {statistics.mean(precs):>9.3f}  {statistics.mean(recs):>7.3f}  {statistics.mean(tokens):>10.0f}")


if __name__ == "__main__":
    main()
