"""
Synthesizer: 基于检索到的 context 生成最终回答。
"""
from __future__ import annotations

from models.llm.base import LLMClient

SYSTEM_PROMPT = """\
You are MacroLens, a financial research assistant specializing in Alphabet/Google (GOOGL) and US macroeconomics.

Treat the retrieved context as your ONLY source of factual information. Your background knowledge about finance or Alphabet must not be used to supplement missing context.

You have access to:
- GOOGL SEC filings (10-K / 10-Q / 8-K) from 2015 onwards
- Macroeconomic events (Fed policy, earnings, antitrust actions)
- US macro time-series (GDP, CPI, unemployment, Fed funds rate, etc.)

Answer in clear, structured prose. Cite sources using [n] notation matching the context numbers.

Rules:
1. Every specific number, date, percentage, and causal claim MUST be directly supported by a cited source [n]. If you cannot point to a source, do not make the claim.
2. If specific information is not present in the context, explicitly say: "The provided context does not contain [X]." Do NOT infer, estimate, or extrapolate beyond what is explicitly stated.
3. If the context is insufficient for a complete answer, answer only the parts that are supported, then list what is missing."""


def _format_context(context: list[dict]) -> str:
    parts = []
    for i, item in enumerate(context, 1):
        src = item["source"]
        if src == "sec_chunks":
            meta = f"SEC {item.get('doc_type','')} FY{item.get('fiscal_year','')} | {item.get('section','')} | {item.get('period_end','')}"
            parts.append(f"[{i}] {meta}\n{item['content']}")
        elif src == "events":
            parts.append(f"[{i}] Event [{item['date']}] [{item['category']}] {item['title']}\n{item.get('description','')}")
        elif src == "macro_indicators":
            parts.append(f"[{i}] {item['title']} ({item['series_id']}) | {item['date']}: {item['value']} {item.get('units','')}")
    return "\n\n".join(parts)


def synthesize(question: str, context: list[dict], llm: LLMClient, max_tokens: int = 4096) -> str:
    context_text = _format_context(context)
    user_msg = f"Context:\n{context_text}\n\nQuestion: {question}"

    return llm.chat(system=SYSTEM_PROMPT, messages=[{"role": "user", "content": user_msg}], max_tokens=max_tokens, temperature=0.0)
