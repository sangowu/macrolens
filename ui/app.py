"""
MacroLens Gradio UI

启动方式:
    uv run ui/app.py
"""
from __future__ import annotations

import json
import logging
import sys
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
    """粗略估算 token 数（4字符≈1token）。"""
    return len(text) // 4


def run_query(
    question: str,
    history: list[list[str]],
    max_iter: int,
) -> tuple[list[list[str]], str, str, str]:
    """
    执行一次 PER Loop，返回：
    - 更新后的 chat history
    - sources markdown
    - stats markdown（iteration / context 数 / token / 时间）
    - 状态信息
    """
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

    # ── 更新对话历史 ──
    history = history + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]

    # ── 构建 Sources 面板 ──
    sources_md = _build_sources_md(all_context)

    # ── 构建 Stats 面板 ──
    stats_md = _build_stats_md(
        iterations=iterations_done,
        n_context=len(all_context),
        input_tokens=input_tokens_approx,
        output_tokens=output_tokens_approx,
        elapsed=t_elapsed,
    )

    return history, sources_md, stats_md, ""


def _build_sources_md(context: list[dict]) -> str:
    if not context:
        return "_无检索结果_"

    parts = []
    for i, item in enumerate(context, 1):
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


# ── Gradio UI ──────────────────────────────────────────────

with gr.Blocks(title="MacroLens") as demo:
    gr.Markdown("""
# MacroLens
**GOOGL SEC 财报 + 美国宏观经济 RAG Agent**

数据覆盖：GOOGL 10-K/10-Q/8-K（2019–2024）| 12 个 FRED 宏观指标 | 30 条手工事件时间线
""")

    with gr.Row():
        # ── 左栏：对话区 ──
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(
                label="对话",
                height=520,
            )
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

        # ── 右栏：Sources + Stats ──
        with gr.Column(scale=2):
            stats_box = gr.Markdown("_运行后显示统计信息_", label="统计")
            gr.Markdown("---")
            sources_box = gr.Markdown("_运行后显示检索来源_", label="检索来源")

    # ── 示例问题 ──
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

    # ── 事件绑定 ──
    def on_submit(question, history, max_iter):
        return run_query(question, history or [], int(max_iter))

    submit_btn.click(
        fn=on_submit,
        inputs=[question_box, chatbot, max_iter_slider],
        outputs=[chatbot, sources_box, stats_box, status_box],
    ).then(
        fn=lambda: "",
        outputs=question_box,
    )

    question_box.submit(
        fn=on_submit,
        inputs=[question_box, chatbot, max_iter_slider],
        outputs=[chatbot, sources_box, stats_box, status_box],
    ).then(
        fn=lambda: "",
        outputs=question_box,
    )

    clear_btn.click(
        fn=lambda: ([], "_运行后显示检索来源_", "_运行后显示统计信息_", ""),
        outputs=[chatbot, sources_box, stats_box, status_box],
    )


if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860, share=False, theme=gr.themes.Soft())
