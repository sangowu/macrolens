"""
Synthesizer: 基于检索到的 context 生成最终回答。
支持内联 <compute>...</compute> 代码块用于精确数值计算。
"""
from __future__ import annotations

import logging
import re

from agent.tools.code_executor import execute_python
from models.llm.base import LLMClient

logger = logging.getLogger(__name__)

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
3. If the context is insufficient for a complete answer, answer only the parts that are supported, then list what is missing.

COMPUTATION TOOL:
When the question requires a derived metric (growth rate, CAGR, sum, correlation, change in basis points) that is NOT directly stated in the context, embed a compute block inline:

<compute>data={'r21':182.5,'r22':224.5}; result=(data['r22']/data['r21']-1)*100; print(f'{result:.1f}%')</compute>

Rules for <compute> blocks:
- Pre-injected names (no import needed): pd, np, math, statistics, datetime, data
- NEVER write import statements inside the block
- Always call print() with a self-contained formatted result including units (e.g. "$580,894 million", "7.2%", "402 bps")
- The print output replaces the entire <compute>...</compute> tag inline — so the tag MUST be embedded inside a sentence, never on its own line
- Only use numbers explicitly found in the context above
- Keep blocks to a single line or use semicolons for multi-statement

CORRECT:   "Combined revenue was <compute>print(f'${146924+209497+224473:,} million')</compute> [1][2][3]."
INCORRECT: list the numbers, then put <compute> on a separate line, then restate in prose — this creates duplicate output.
"""

_COMPUTE_RE = re.compile(r"<compute>(.*?)</compute>", re.DOTALL)


def _resolve_compute_blocks(text: str) -> str:
    call_count = 0

    def _run(match: re.Match) -> str:
        nonlocal call_count
        call_count += 1
        code = match.group(1).strip()
        res = execute_python(code)
        if res["error"]:
            logger.warning("[COMPUTE #%d] error: %s | code: %s", call_count, res["error"], code[:120])
            return f"[computation error: {res['error']}]"
        output = res["stdout"].strip()
        result = output if output else str(res["result"]) if res["result"] is not None else "[no output]"
        logger.info("[COMPUTE #%d] code: %s => %s", call_count, code[:120], result)
        return result

    resolved = _COMPUTE_RE.sub(_run, text)
    if call_count == 0:
        logger.debug("[COMPUTE] no compute blocks in this response")
    return resolved


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

    raw = llm.chat(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        max_tokens=max_tokens,
        temperature=0.0,
    )

    if "<compute>" in raw:
        raw = _resolve_compute_blocks(raw)

    return raw
