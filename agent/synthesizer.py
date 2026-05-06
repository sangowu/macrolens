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
- The print output replaces the entire <compute>...</compute> tag inline — the tag MUST sit inside a sentence with words before AND after it
- NEVER place <compute> at the end of a paragraph, after a period, or on its own line
- After a <compute> block produces a result, use ONLY that result in all later references — never restate a different number
- Only use numbers explicitly found in the context above
- Keep blocks to a single line or use semicolons for multi-statement

CORRECT:   "The 2020 operating margin was <compute>print(f'{54606/168635*100:.1f}%')</compute> [1], rising to ..."
INCORRECT: "Operating income was $54,606M ...\n<compute>...</compute>\nThe margin was 32.4%"  ← tag on own line + duplicate
"""

_COMPUTE_RE = re.compile(r"<compute>(.*?)</compute>", re.DOTALL)


def _resolve_compute_blocks(text: str) -> str:
    call_count = 0
    computed_results: list[str] = []

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
        computed_results.append(result)
        return result

    resolved = _COMPUTE_RE.sub(_run, text)

    if call_count == 0:
        logger.debug("[COMPUTE] no compute blocks in this response")
    elif computed_results:
        resolved = _remove_orphaned_results(resolved, computed_results)

    return resolved


def _remove_orphaned_results(text: str, results: list[str]) -> str:
    """删除独立成段的 compute 结果行（被空行包围且内容就是计算结果）。

    LLM 常把 <compute> 放在段落之间，输出替换后变成:
        ...(上文)...

        15.3%          ← 孤立行，后面的句子会重复同一个数字

        Google's CAGR was 15.3%...
    直接删掉孤立行，保留后面完整的句子。
    """
    result_set = set(results)
    lines = text.split("\n")
    cleaned: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped in result_set:
            prev_blank = i == 0 or not lines[i - 1].strip()
            next_blank = i == len(lines) - 1 or not lines[i + 1].strip()
            if prev_blank and next_blank:
                # 跳过这行孤立结果，同时吃掉紧随其后的空行
                i += 1
                if i < len(lines) and not lines[i].strip():
                    i += 1
                continue
        cleaned.append(lines[i])
        i += 1
    return "\n".join(cleaned)


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
