"""
Critic: 判断检索到的 context 是否足以回答原始问题。
"""
from __future__ import annotations

import json
import re

from models.llm.base import LLMClient

SYSTEM_PROMPT = """\
You are a sufficiency critic for a financial RAG system.
Given a user question and retrieved context, judge whether the context contains enough information to give a complete, accurate answer.

Respond with JSON only:
{
  "is_sufficient": true or false,
  "missing": "one sentence describing what key information is still missing, or empty string if sufficient"
}

Be strict: if specific numbers, dates, or causal explanations are needed but absent, mark as insufficient."""


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
    return "\n\n".join(parts)


def critique(question: str, context: list[dict], llm: LLMClient) -> tuple[bool, str]:
    """返回 (is_sufficient, missing_hint)。"""
    context_text = _format_context(context)
    user_msg = f"Question: {question}\n\nRetrieved context:\n{context_text}"

    raw = llm.chat(system=SYSTEM_PROMPT, messages=[{"role": "user", "content": user_msg}], max_tokens=256, temperature=0.0)

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        raw = match.group(0)

    result = json.loads(raw)
    return result.get("is_sufficient", True), result.get("missing", "")
