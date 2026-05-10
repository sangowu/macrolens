"""
Task Worker: 轮询 tasks 表，执行 PER Loop，写回报告。

启动方式:
    uv run worker/task_worker.py
    uv run worker/task_worker.py --poll-interval 3 --verbose
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg

from agent.memory import extract_and_store, retrieve
from agent.per_loop import run as per_loop_run
from agent.report_writer import write_report
from models.config import load_config
from models.factory import create_embedding, create_llm_client


def _pick_task(conn: psycopg.Connection) -> dict | None:
    """原子地取一个 pending 任务并标记为 running（SKIP LOCKED 防并发重复）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tasks SET status = 'running'
            WHERE id = (
                SELECT id FROM tasks
                WHERE status = 'pending'
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, question
            """,
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        return None
    return {"id": str(row[0]), "question": row[1]}


def _complete_task(conn: psycopg.Connection, task_id: str, report_md: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status='completed', report_md=%s, completed_at=now() WHERE id=%s",
            (report_md, task_id),
        )
    conn.commit()


def _fail_task(conn: psycopg.Connection, task_id: str, error_msg: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status='failed', error_msg=%s, completed_at=now() WHERE id=%s",
            (error_msg, task_id),
        )
    conn.commit()


def _run_task(task: dict, cfg, conn: psycopg.Connection, embedder, llm, verbose: bool) -> None:
    task_id = task["id"]
    question = task["question"]

    if verbose:
        print(f"[worker] Running task {task_id}: {question[:60]}...")

    # 注入历史记忆作为额外 context 提示（追加到问题末尾）
    memories = retrieve(question, conn, embedder, top_k=3)
    enriched_question = question
    if memories:
        mem_text = "\n".join(
            f"- [{m['memory_type']}] {m['content']}" for m in memories
        )
        enriched_question = (
            f"{question}\n\n"
            f"Relevant prior findings (from previous research sessions):\n{mem_text}"
        )

    t0 = time.perf_counter()
    answer, context = per_loop_run(
        enriched_question, cfg, conn, embedder, llm,
        max_iter=cfg.llm.max_iter if hasattr(cfg.llm, "max_iter") else 3,
        verbose=verbose,
    )
    elapsed = time.perf_counter() - t0

    report_md = write_report(
        question=question,
        answer=answer,
        context=context,
        iterations=3,
        elapsed=elapsed,
    )

    _complete_task(conn, task_id, report_md)

    # 提取记忆
    stored = extract_and_store(task_id, question, answer, conn, embedder, llm)
    if verbose:
        print(f"[worker] Task {task_id} completed in {elapsed:.1f}s | memories stored: {stored}")


async def worker_loop(cfg, poll_interval: int, verbose: bool) -> None:
    embedder = create_embedding(cfg)
    llm = create_llm_client(cfg)

    print(f"[worker] Started — polling every {poll_interval}s")

    with psycopg.connect(cfg.db.dsn) as conn:
        # 启动时检查数据新鲜度（只读，不触发更新）
        try:
            from worker.data_refresh_worker import check_and_warn_freshness
            check_and_warn_freshness(conn)
        except Exception:
            pass
        while True:
            task = _pick_task(conn)
            if task:
                try:
                    _run_task(task, cfg, conn, embedder, llm, verbose)
                except Exception as exc:
                    _fail_task(conn, task["id"], str(exc))
                    print(f"[worker] ERROR task {task['id']}: {exc}", file=sys.stderr)
            else:
                await asyncio.sleep(poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--poll-interval", type=int, default=2)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    api_key = os.environ.get(cfg.llm.api_key_env, "")
    if not api_key:
        sys.exit(f"Error: {cfg.llm.api_key_env} not set")

    asyncio.run(worker_loop(cfg, args.poll_interval, args.verbose))


if __name__ == "__main__":
    main()
