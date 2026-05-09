"""
检索评估：Hit@K / MRR@K

针对 Set A 中的 sec_chunks 类问题（A01/A04/A06/A08）。
macro 问题（A02/A05/A07）是精确 SQL 查询，必然命中，不纳入检索评估。

命中判断：top-K 里是否有 chunk 包含全部 key_facts（小写子串匹配）。
K 值：[1, 3, 5, 10, 12]，12 = pipeline 当前 top_k。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

import psycopg
from pgvector.psycopg import register_vector

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.executor import _search_sec
from eval.questions import SET_A
from models.config import load_config
from models.factory import create_embedding

KS = [1, 3, 5, 10, 12]

# 仅 sec_chunks 向量检索的题目（排除 macro/events 精确查询）
SEC_QUESTIONS = [q for q in SET_A if q.qid in {"A01", "A04", "A06", "A08"}]

YEAR_FILTERS = {"A01": 2022, "A04": 2023, "A06": 2021, "A08": 2022}


def chunk_hits(content: str, key_facts: list[str]) -> bool:
    """chunk 包含全部 key_facts → 命中。"""
    text = content.lower()
    return all(kf.lower() in text for kf in key_facts)


def compute_metrics(ranks: list[int | None], ks: list[int]) -> dict:
    n = len(ranks)
    result = {}
    for k in ks:
        result[f"hit@{k}"] = sum(1 for r in ranks if r and r <= k) / n
        result[f"mrr@{k}"] = sum(1 / r for r in ranks if r and r <= k) / n
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--top-k", type=int, default=max(KS),
                        help=f"检索候选数（默认 {max(KS)}，需 >= max(K)）")
    args = parser.parse_args()

    cfg = load_config(args.config)
    embedder = create_embedding(cfg)

    ranks: list[int | None] = []

    with psycopg.connect(cfg.db.dsn) as conn:
        register_vector(conn)

        for q in SEC_QUESTIONS:
            fiscal_year = YEAR_FILTERS[q.qid]
            chunks = _search_sec(
                conn, embedder, q.question,
                filters={"fiscal_year": fiscal_year},
                candidate_k=cfg.llm.candidate_k,
                top_k=args.top_k,
            )

            rank = None
            for i, chunk in enumerate(chunks, 1):
                if chunk_hits(chunk["content"], q.key_facts):
                    rank = i
                    break

            ranks.append(rank)
            hit_info = f"rank={rank}" if rank else "MISS"
            print(f"[{q.qid}] {hit_info:8s} | key_facts={q.key_facts}")
            if rank:
                content = chunks[rank - 1]["content"]
                # 找到第一个 key_fact 的位置，截取上下文
                pos = next((content.lower().find(kf.lower()) for kf in q.key_facts
                           if content.lower().find(kf.lower()) >= 0), 0)
                start = max(0, pos - 40)
                preview = content[start:start + 160].replace("\n", " ")
                preview = preview.encode("ascii", errors="replace").decode()
                print(f"         ...{preview}...")

    metrics = compute_metrics(ranks, KS)
    print("\n── Retrieval Metrics (sec_chunks, 4 SEC questions) ──")
    print(f"  {'K':<4}  {'Hit@K':>6}  {'MRR@K':>6}")
    print("  " + "-" * 22)
    for k in KS:
        print(f"  {k:<4}  {metrics[f'hit@{k}']:>6.3f}  {metrics[f'mrr@{k}']:>6.3f}")


if __name__ == "__main__":
    main()
