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
from agent.planner import plan
from agent.synthesizer import synthesize
from models.config import load_config
from models.factory import create_embedding, create_llm_client


def run(question: str, cfg, conn: psycopg.Connection, embedder, llm, max_iter: int = 3, verbose: bool = False) -> str:
    all_context: list[dict] = []
    history: list[dict] = []
    missing_hint = ""
    searched_queries: list[str] = []

    for iteration in range(1, max_iter + 1):
        if verbose:
            print(f"\n── Iteration {iteration}/{max_iter} ──────────────────────")

        if iteration == 1:
            prompt = question
        else:
            already = ", ".join(f'"{q}"' for q in searched_queries)
            prompt = (
                f"{question}\n\n"
                f"Focus on what's still missing: {missing_hint}\n"
                f"Already searched (do NOT repeat these queries): [{already}]"
            )

        sub_queries = plan(prompt, llm, history=history if iteration > 1 else None)

        if verbose:
            print(f"Plan: {len(sub_queries)} sub-queries")
            for sq in sub_queries:
                print(f"  - [{','.join(sq['sources'])}] {sq['query']}")

        searched_queries.extend(sq["query"] for sq in sub_queries)

        new_context = execute(sub_queries, conn, embedder, cfg.llm)

        seen = {(c.get("id") or c.get("event_id") or c.get("date", "") + c.get("series_id", "")) for c in all_context}
        for c in new_context:
            key = c.get("id") or c.get("event_id") or c.get("date", "") + c.get("series_id", "")
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

    return synthesize(question, all_context, llm, max_tokens=cfg.llm.max_tokens)


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

    with psycopg.connect(cfg.db.dsn) as conn:
        answer = run(question, cfg, conn, embedder, llm, max_iter=args.max_iter, verbose=args.verbose)

    print("\n" + "=" * 60)
    print(answer)
    print("=" * 60)


if __name__ == "__main__":
    main()
