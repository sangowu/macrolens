"""
PER Loop 主入口: Plan → Execute → Critique → (refine) → Synthesize

用法:
    uv run agent/per_loop.py "How did Fed rate hikes in 2022 affect Google's revenue?"
    uv run agent/per_loop.py --provider gemini "..."
    uv run agent/per_loop.py --max-iter 3 --verbose "..."
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

import psycopg

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.critic import critique
from agent.executor import execute
from agent.planner import plan, plan_scoped
from agent.synthesizer import synthesize
from models.base import RerankerBackend
from models.config import load_config
from models.factory import create_embedding, create_llm_client, create_reranker
from models.llm.tracing import trace_span


def run(question: str, cfg, conn: psycopg.Connection, embedder, llm, max_iter: int = 3, verbose: bool = False, reranker: RerankerBackend | None = None) -> tuple[str, list]:
    """PER Loop 入口。用一个 Langfuse span 聚合本次问答的全部 LLM 调用为一棵 trace。"""
    with trace_span("per_loop", {"question": question, "max_iter": max_iter}):
        return _run_impl(question, cfg, conn, embedder, llm, max_iter=max_iter, verbose=verbose, reranker=reranker)


def _run_impl(question: str, cfg, conn: psycopg.Connection, embedder, llm, max_iter: int = 3, verbose: bool = False, reranker: RerankerBackend | None = None) -> tuple[str, list]:
    all_context: list[dict] = []
    history: list[dict] = []
    missing_hint = ""
    searched_queries: list[str] = []

    for iteration in range(1, max_iter + 1):
        if verbose:
            print(f"\n── Iteration {iteration}/{max_iter} ──────────────────────")

        if iteration == 1:
            prompt = question
            # 域内判断只在第一轮做：完全越界的问题直接拒答，
            # 避免空跑 3 轮 PER Loop 浪费检索与 LLM 调用。
            in_scope, reject_reason, sub_queries = plan_scoped(prompt, llm)
            if not in_scope:
                if verbose:
                    print(f"Plan: out-of-scope — {reject_reason}")
                fallback = "This question is outside MacroLens's scope (MAG7 companies and US macroeconomics)."
                return (reject_reason or fallback), []
        else:
            already = ", ".join(f'"{q}"' for q in searched_queries)
            prompt = (
                f"{question}\n\n"
                f"Focus on what's still missing: {missing_hint}\n"
                f"Already searched (do NOT repeat these queries): [{already}]"
            )
            sub_queries = plan(prompt, llm, history=history)

        if verbose:
            print(f"Plan: {len(sub_queries)} sub-queries")
            for sq in sub_queries:
                print(f"  - [{','.join(sq['sources'])}] {sq['query']}")

        searched_queries.extend(sq["query"] for sq in sub_queries)

        new_context = execute(sub_queries, conn, embedder, cfg.llm, reranker=reranker)

        def _dedup_key(c: dict) -> str:
            return (
                c.get("id") or c.get("event_id")
                or (
                    c.get("ticker", "")
                    + str(c.get("period_end") or c.get("date", ""))
                    + c.get("series_id", "")
                    + str(c.get("fiscal_quarter", ""))
                )
            )

        seen = {_dedup_key(c) for c in all_context}
        for c in new_context:
            key = _dedup_key(c)
            if key not in seen:
                all_context.append(c)
                seen.add(key)

        if verbose:
            print(f"Context: {len(all_context)} items total")

        is_sufficient, missing_hint = critique(question, all_context, llm)

        if verbose:
            print(f"Critic: sufficient={is_sufficient}, missing={missing_hint!r}")

        if is_sufficient or iteration == max_iter:
            break

        history.append({"role": "assistant", "content": missing_hint})

    if verbose:
        print("\n── Synthesizing ─────────────────────────────────────")

    answer = synthesize(question, all_context, llm, max_tokens=cfg.llm.max_tokens, missing_hint=missing_hint)
    return answer, all_context


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?", help="要回答的问题")
    parser.add_argument("--max-iter", type=int, default=3)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    question = args.question or input("Question: ").strip()
    if not question:
        sys.exit("No question provided.")

    cfg = load_config(args.config)

    api_key = os.environ.get(cfg.llm.api_key_env, "")
    if not api_key or "your_" in api_key:
        sys.exit(f"Error: {cfg.llm.api_key_env} not set in .env")

    embedder = create_embedding(cfg)
    llm = create_llm_client(cfg)
    reranker = create_reranker(cfg)

    with psycopg.connect(cfg.db.dsn) as conn:
        answer, _ = run(question, cfg, conn, embedder, llm, max_iter=args.max_iter, verbose=args.verbose, reranker=reranker)

    print("\n" + "=" * 60)
    print(answer)
    print("=" * 60)


if __name__ == "__main__":
    main()
