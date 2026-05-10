"""
Critic: 判断检索到的 context 是否足以回答原始问题。
使用 Tool Use 替代 json.loads + 正则，保证输出格式合法。
"""
from __future__ import annotations

from models.llm.base import LLMClient

SYSTEM_PROMPT = """\
You are a sufficiency critic for a financial RAG system.
Given a user question and retrieved context, judge whether the context contains enough information to give a complete, accurate answer.

Use the judge_sufficiency tool to return your verdict.

Be strict: if specific numbers, dates, or causal explanations are needed but absent, mark as insufficient."""

_JUDGE_TOOL = {
    "name": "judge_sufficiency",
    "description": "Return whether the retrieved context is sufficient to answer the question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_sufficient": {
                "type": "boolean",
                "description": "True if context contains enough information for a complete, accurate answer.",
            },
            "missing": {
                "type": "string",
                "description": "One sentence describing what key information is still missing. Empty string if sufficient.",
            },
        },
        "required": ["is_sufficient", "missing"],
    },
}


def _format_context(context: list[dict]) -> str:
    parts = []
    for i, item in enumerate(context[:20], 1):
        src = item["source"]
        if src == "sec_chunks":
            parts.append(f"[{i}][SEC {item.get('doc_type','')} {item.get('fiscal_year','')} {item.get('section','')}]\n{item['content'][:400]}")
        elif src == "events":
            parts.append(f"[{i}][Event {item['date']} {item['category']}] {item['title']}\n{item.get('description','')[:200]}")
        elif src == "macro_indicators":
            parts.append(f"[{i}][Macro {item['series_id']} {item['date']}] {item['title']}: {item['value']} {item.get('units','')}")
        elif src == "price_history":
            if item.get("_granularity") == "monthly":
                pe = f" avg_P/E={item['avg_pe']:.1f}" if item.get("avg_pe") else ""
                parts.append(f"[{i}][Price {item['ticker']} {item['date']} monthly] close={item['close']} avg={item.get('avg_close')}{pe}")
            else:
                pe = f" P/E={item['pe_ratio']:.1f}" if item.get("pe_ratio") else ""
                parts.append(f"[{i}][Price {item['ticker']} {item['date']}] close={item['close']}{pe}")
        elif src == "earnings_history":
            parts.append(
                f"[{i}][Earnings {item['ticker']} FY{item.get('fiscal_year')}Q{item.get('fiscal_quarter','')}]"
                f" EPS={item.get('eps_actual','N/A')} est={item.get('eps_estimate','N/A')}"
                f" surprise={item.get('eps_surprise_pct','N/A')}%"
            )
    return "\n\n".join(parts)


def critique(question: str, context: list[dict], llm: LLMClient) -> tuple[bool, str]:
    """返回 (is_sufficient, missing_hint)。"""
    context_text = _format_context(context)
    user_msg = f"Question: {question}\n\nRetrieved context:\n{context_text}"

    result = llm.chat_with_tools(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        tools=[_JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "judge_sufficiency"},
        max_tokens=256,
        temperature=0.0,
    )

    return result.get("is_sufficient", True), result.get("missing", "")
