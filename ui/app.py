"""
MacroLens Gradio UI

启动方式:
    uv run ui/app.py

两种模式:
    Chat     — 同步聊天，即时回答
    Task     — 异步任务，生成结构化研究报告
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import psycopg
import gradio as gr
from pgvector.psycopg import register_vector

from agent.planner import plan
from agent.executor import execute
from agent.critic import critique
from agent.synthesizer import synthesize, _format_context
from agent.per_loop import run as per_loop_run
from agent.report_writer import write_report
from agent.memory import extract_and_store, retrieve
from models.config import load_config
from models.factory import create_embedding, create_llm_client

# ── 日志配置 ───────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"query_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("macrolens")

# ── 全局初始化 ─────────────────────────────────────────────
cfg = load_config("config.yaml")
embedder = create_embedding(cfg)
llm = create_llm_client(cfg)


def _count_tokens_approx(text: str) -> int:
    return len(text) // 4


# ══════════════════════════════════════════════════════════
# Chat 模式
# ══════════════════════════════════════════════════════════

def run_query(
    question: str,
    history: list[dict],
    max_iter: int,
) -> tuple[list[dict], str, str, str]:
    if not question.strip():
        return history, "", "", "请输入问题"

    t_start = time.time()
    all_context: list[dict] = []
    missing_hint = ""
    iterations_done = 0
    input_tokens_approx = 0
    output_tokens_approx = 0
    searched_queries: list[str] = []

    logger.info("=" * 60)
    logger.info(f"[QUERY] {question}")
    logger.info(f"[CONFIG] max_iter={max_iter}")

    with psycopg.connect(cfg.db.dsn) as conn:
        register_vector(conn)

        for iteration in range(1, max_iter + 1):
            iterations_done = iteration
            logger.info(f"── Iteration {iteration}/{max_iter} ──")

            if iteration == 1:
                prompt = question
            else:
                already = ", ".join(f'"{q}"' for q in searched_queries)
                prompt = (
                    f"{question}\n\n"
                    f"Focus on what's still missing: {missing_hint}\n"
                    f"Already searched (do NOT repeat these queries): [{already}]"
                )

            sub_queries = plan(prompt, llm)
            input_tokens_approx += _count_tokens_approx(prompt)
            searched_queries.extend(sq["query"] for sq in sub_queries)

            logger.info(f"[PLAN] {len(sub_queries)} sub-queries:")
            for sq in sub_queries:
                logger.info(f"  sources={sq.get('sources')} filters={sq.get('filters')} | {sq.get('query', '')[:80]}")

            new_context = execute(sub_queries, conn, embedder, cfg.llm)

            seen = {
                (c.get("id") or c.get("event_id") or c.get("date", "") + c.get("series_id", ""))
                for c in all_context
            }
            added = 0
            for c in new_context:
                key = c.get("id") or c.get("event_id") or c.get("date", "") + c.get("series_id", "")
                if key not in seen:
                    all_context.append(c)
                    seen.add(key)
                    added += 1

            logger.info(f"[EXEC] retrieved={len(new_context)} new={added} total_context={len(all_context)}")

            is_sufficient, missing_hint = critique(question, all_context, llm)
            input_tokens_approx += _count_tokens_approx(_format_context(all_context[:20]))

            logger.info(f"[CRITIC] sufficient={is_sufficient} missing={missing_hint!r}")

            if is_sufficient or iteration == max_iter:
                break

        answer = synthesize(question, all_context, llm, max_tokens=cfg.llm.max_tokens)
        output_tokens_approx += _count_tokens_approx(answer)

    t_elapsed = time.time() - t_start

    logger.info(f"[ANSWER] {answer[:200].replace(chr(10), ' ')}...")
    logger.info(f"[STATS] iter={iterations_done} context={len(all_context)} in_tok~{input_tokens_approx} out_tok~{output_tokens_approx} time={t_elapsed:.1f}s")

    history = history + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]

    sources_md = _build_sources_md(all_context, answer)
    stats_md = _build_stats_md(
        iterations=iterations_done,
        n_context=len(all_context),
        input_tokens=input_tokens_approx,
        output_tokens=output_tokens_approx,
        elapsed=t_elapsed,
    )

    return history, sources_md, stats_md, ""


def _build_sources_md(context: list[dict], answer: str = "") -> str:
    if not context:
        return "_无检索结果_"

    # 只展示答案中实际引用的 chunk，过滤未被引用的噪音
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)} if answer else set()
    items = [(i, item) for i, item in enumerate(context, 1) if not cited or i in cited]

    if not items:
        return "_无检索结果_"

    parts = []
    for i, item in items:
        src = item["source"]
        if src == "sec_chunks":
            header = f"**[{i}] SEC {item.get('doc_type', '')} FY{item.get('fiscal_year', '')} — {item.get('section', '')}**"
            date = f"Period end: {item.get('period_end', 'N/A')}"
            preview = item.get("content", "")[:300].replace("\n", " ")
            parts.append(f"{header}\n{date}\n\n> {preview}...")
        elif src == "events":
            header = f"**[{i}] Event [{item.get('date', '')}] {item.get('category', '')}**"
            title = item.get("title", "")
            desc = item.get("description", "")[:200].replace("\n", " ")
            parts.append(f"{header}\n{title}\n\n> {desc}...")
        elif src == "macro_indicators":
            header = f"**[{i}] {item.get('title', item.get('series_id', ''))}**"
            val = f"{item.get('date', '')}: **{item.get('value', 'N/A')}** {item.get('units', '')}"
            parts.append(f"{header}\n{val}")

    return "\n\n---\n\n".join(parts)


def _build_stats_md(
    iterations: int,
    n_context: int,
    input_tokens: int,
    output_tokens: int,
    elapsed: float,
) -> str:
    return f"""| 指标 | 值 |
|------|-----|
| PER 迭代次数 | {iterations} |
| Context 条数 | {n_context} |
| 输入 Token（估算） | ~{input_tokens:,} |
| 输出 Token（估算） | ~{output_tokens:,} |
| 总耗时 | {elapsed:.1f}s |"""


# ══════════════════════════════════════════════════════════
# Task 模式（异步）
# ══════════════════════════════════════════════════════════

def _bg_run_task(task_id: str, question: str) -> None:
    """在后台线程中执行 PER Loop，结果写回 tasks 表。"""
    try:
        with psycopg.connect(cfg.db.dsn) as conn:
            register_vector(conn)

            memories = retrieve(question, conn, embedder, top_k=3)
            enriched = question
            if memories:
                mem_text = "\n".join(f"- [{m['memory_type']}] {m['content']}" for m in memories)
                enriched = f"{question}\n\nRelevant prior findings:\n{mem_text}"

            t0 = time.time()
            answer, context = per_loop_run(enriched, cfg, conn, embedder, llm, max_iter=3, verbose=False)
            elapsed = time.time() - t0

            report_md = write_report(question, answer, context, 3, elapsed)

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE tasks SET status='completed', report_md=%s, completed_at=now() WHERE id=%s",
                    (report_md, task_id),
                )
            conn.commit()

            extract_and_store(task_id, question, answer, conn, embedder, llm)
            logger.info(f"[TASK] {task_id} completed in {elapsed:.1f}s")

    except Exception as exc:
        logger.error(f"[TASK] {task_id} failed: {exc}")
        try:
            with psycopg.connect(cfg.db.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE tasks SET status='failed', error_msg=%s, completed_at=now() WHERE id=%s",
                        (str(exc), task_id),
                    )
                conn.commit()
        except Exception:
            pass


def submit_task(question: str) -> tuple[str, str, str]:
    """提交任务：写入 DB，启动后台线程，返回 (task_id, status_text, report_md)。"""
    if not question.strip():
        return "", "请输入问题", ""

    with psycopg.connect(cfg.db.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (question) VALUES (%s) RETURNING id",
                (question.strip(),),
            )
            task_id = str(cur.fetchone()[0])
        conn.commit()

    threading.Thread(target=_bg_run_task, args=(task_id, question.strip()), daemon=True).start()
    logger.info(f"[TASK] submitted {task_id}: {question[:60]}")

    return task_id, f"⟳ running — Task ID: {task_id}", ""


def poll_task(task_id: str) -> tuple[str, str]:
    """轮询任务状态，返回 (status_text, report_md)。"""
    if not task_id:
        return "", ""

    with psycopg.connect(cfg.db.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, report_md, error_msg, completed_at FROM tasks WHERE id=%s",
                (task_id,),
            )
            row = cur.fetchone()

    if row is None:
        return "Task not found", ""

    status, report_md, error_msg, completed_at = row

    if status == "completed":
        done_time = str(completed_at)[:19] if completed_at else ""
        return f"✓ completed — {done_time}", report_md or ""
    elif status == "failed":
        return f"✗ failed — {error_msg or ''}", ""
    elif status == "running":
        return f"⟳ running — Task ID: {task_id}", ""
    else:
        return f"● pending — Task ID: {task_id}", ""


def list_recent_tasks() -> list[list[str]]:
    """返回最近 10 条任务，用于 Dataframe 展示。"""
    try:
        with psycopg.connect(cfg.db.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, status, question, created_at
                       FROM tasks ORDER BY created_at DESC LIMIT 10""",
                )
                rows = cur.fetchall()
        return [
            [str(r[0])[:8] + "...", r[1], r[2][:60] + ("..." if len(r[2]) > 60 else ""), str(r[3])[:16]]
            for r in rows
        ]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════
# Gradio UI
# ══════════════════════════════════════════════════════════

with gr.Blocks(title="MacroLens") as demo:
    gr.Markdown("""
# MacroLens
**GOOGL SEC 财报 + 美国宏观经济 RAG Agent**

数据覆盖：GOOGL 10-K/10-Q/8-K（2019–2024）| 12 个 FRED 宏观指标 | 30 条手工事件时间线
""")

    with gr.Tabs():

        # ── Tab 1: Chat ────────────────────────────────────
        with gr.Tab("Chat"):
            with gr.Row():
                with gr.Column(scale=3):
                    chatbot = gr.Chatbot(label="对话", height=520)
                    with gr.Row():
                        question_box = gr.Textbox(
                            placeholder="例：2022年加息如何影响Google广告收入？",
                            label="",
                            scale=5,
                            lines=2,
                        )
                        with gr.Column(scale=1, min_width=120):
                            submit_btn = gr.Button("发送", variant="primary")
                            clear_btn = gr.Button("清空")

                    max_iter_slider = gr.Slider(
                        minimum=1, maximum=3, value=2, step=1,
                        label="最大检索轮次（PER Loop max_iter）",
                    )
                    status_box = gr.Textbox(label="状态", interactive=False, lines=1)

                with gr.Column(scale=2):
                    stats_box = gr.Markdown("_运行后显示统计信息_", label="统计")
                    gr.Markdown("---")
                    sources_box = gr.Markdown("_运行后显示检索来源_", label="检索来源")

            gr.Examples(
                examples=[
                    "What was Google's total revenue in fiscal year 2022?",
                    "How did Federal Reserve rate hikes in 2022 affect Google's advertising revenue?",
                    "What are the main risk factors Google disclosed in its 2023 10-K?",
                    "How did COVID-19 impact Google's business in 2020?",
                    "What is the trend of US unemployment rate from 2020 to 2023?",
                ],
                inputs=question_box,
                label="示例问题",
            )

            def on_submit(question, history, max_iter):
                return run_query(question, history or [], int(max_iter))

            submit_btn.click(
                fn=on_submit,
                inputs=[question_box, chatbot, max_iter_slider],
                outputs=[chatbot, sources_box, stats_box, status_box],
            ).then(fn=lambda: "", outputs=question_box)

            question_box.submit(
                fn=on_submit,
                inputs=[question_box, chatbot, max_iter_slider],
                outputs=[chatbot, sources_box, stats_box, status_box],
            ).then(fn=lambda: "", outputs=question_box)

            clear_btn.click(
                fn=lambda: ([], "_运行后显示检索来源_", "_运行后显示统计信息_", ""),
                outputs=[chatbot, sources_box, stats_box, status_box],
            )

        # ── Tab 2: Analysis Task ───────────────────────────
        with gr.Tab("Analysis Task"):
            gr.Markdown("""
### 异步研究报告

提交一个分析任务，Agent 在后台运行 PER Loop，完成后生成结构化 markdown 报告。
支持代码执行（增长率、基点变化等派生指标）和跨会话记忆注入。
""")
            with gr.Row():
                task_q_input = gr.Textbox(
                    label="Research Question",
                    placeholder="例：What was Google's advertising revenue CAGR from 2019 to 2023?",
                    lines=3,
                    scale=4,
                )
                with gr.Column(scale=1, min_width=140):
                    task_submit_btn = gr.Button("Submit Task", variant="primary")
                    task_refresh_btn = gr.Button("Refresh Status")

            task_id_state = gr.State("")
            task_status_display = gr.Textbox(
                label="Status", interactive=False, lines=1,
                value="● idle — submit a question to start",
            )

            task_report_display = gr.Markdown("_Report will appear here when the task completes._")

            gr.Markdown("---")
            gr.Markdown("#### Recent Tasks")
            recent_tasks_table = gr.Dataframe(
                headers=["ID", "Status", "Question", "Created"],
                label="",
                interactive=False,
                value=list_recent_tasks,
            )
            refresh_list_btn = gr.Button("Refresh List", size="sm")

            # 定时轮询（每 3 秒）
            timer = gr.Timer(value=3)

            def on_submit_task(question):
                task_id, status, report = submit_task(question)
                return task_id, status, report, ""

            task_submit_btn.click(
                fn=on_submit_task,
                inputs=[task_q_input],
                outputs=[task_id_state, task_status_display, task_report_display, task_q_input],
            )

            def on_poll(task_id):
                status, report = poll_task(task_id)
                return status, report

            timer.tick(
                fn=on_poll,
                inputs=[task_id_state],
                outputs=[task_status_display, task_report_display],
            )

            task_refresh_btn.click(
                fn=on_poll,
                inputs=[task_id_state],
                outputs=[task_status_display, task_report_display],
            )

            refresh_list_btn.click(
                fn=list_recent_tasks,
                outputs=[recent_tasks_table],
            )


if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860, share=False, theme=gr.themes.Soft())
