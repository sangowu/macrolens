"""UI 纯展示格式化函数（无外部依赖，可独立单元测试）。

从 ui/app.py 抽离：这些函数只做字符串格式化，不触碰 Gradio / DB / embedder。
因此单元测试可直接 import 本模块，而无需 import 整个 UI（后者在模块级构建
Gradio Blocks 并发起 analytics 网络请求，会拖慢甚至阻塞测试）。
"""

from __future__ import annotations

import re


def _count_tokens_approx(text: str) -> int:
    return len(text) // 4


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
