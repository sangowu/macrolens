"""
Synthesizer: 基于检索到的 context 生成最终回答。

流程：
  1. chat_agentic() — LLM 读全量 context 写答案，遇到计算调用 compute tool，沙箱执行后继续生成
  2. _validate_citations() — 验证所有 [n] 引用是否指向真实存在的证据
"""
from __future__ import annotations

import logging
import re  # 用于 _validate_citations 的引用检查

from agent.tools.code_executor import execute_python
from models.llm.base import LLMClient

logger = logging.getLogger(__name__)

# ── System Prompt ─────────────────────────────────────────

SYSTEM_PROMPT = """\
You are MacroLens, a financial research assistant covering MAG7 companies and US macroeconomics.

Treat the retrieved context as your ONLY source of factual information. Your background knowledge about finance or these companies must not be used to supplement missing context.

You have access to:
- MAG7 SEC filings (10-K / 10-Q / 8-K) from 2015 onwards
- Macroeconomic events (Fed policy, earnings, antitrust actions)
- US macro time-series (GDP, CPI, unemployment, Fed funds rate, etc.)
- Daily stock price history and P/E ratios for MAG7 companies
- Quarterly/annual earnings history with EPS actual vs estimate

Answer in clear, structured prose. Cite sources using [n] notation matching the context numbers.

Rules:
1. NUMBERS AND DATES: Every specific figure (revenue, rate, EPS, employee count, date) you state must appear verbatim in the cited source [n], or be derivable by the compute tool from numbers that appear verbatim in the context. If the exact figure is not present in the context, say "The provided context does not contain [X]." Do NOT recall figures from background knowledge.
2. CAUSAL CLAIMS: Statements like "X caused Y", "due to X, Y occurred", "X impacted Y" require a context source that explicitly states the mechanism. Correlation in the data (e.g., rates rose AND revenue fell) does NOT establish causation. If no source explicitly states the causal link, write: "The provided context does not establish a direct causal link between X and Y."
3. If specific information is not present in the context, explicitly say: "The provided context does not contain [X]." Do NOT infer, estimate, or extrapolate.
4. If the context is insufficient for a complete answer, answer only the parts that are supported, then list what is missing.
5. For derived metrics (growth rate, CAGR, sum, basis point change), call the compute tool with self-contained Python. Data must come from numbers explicitly present in the context.
6. Your general knowledge about world events, economics, or companies does NOT exist for this answer. If it is not in the retrieved context, it did not happen.

When answering valuation questions (e.g. "Is X stock expensive?"):
- Use the compute tool to calculate P/E percentile vs historical range using np.
- State the result clearly: "currently at the Xth percentile of its YYYY-YYYY P/E range."
- Describe the valuation position only (cheap/expensive relative to history). Never say "buy", "sell", or make price predictions."""

# ── Tool 定义 ─────────────────────────────────────────────

_COMPUTE_TOOL = {
    "name": "compute",
    "description": (
        "Execute Python for precise numerical calculations. "
        "CRITICAL: import statements are FORBIDDEN and will raise an error. "
        "The following names are pre-injected and ready to use WITHOUT import: "
        "pd (pandas), np (numpy), math, statistics, datetime. "
        "Always call print() with a formatted result. Data must be inline in the code."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Self-contained Python using ONLY pre-injected names (pd, np, math, statistics, datetime). NO import statements. Must call print() with the result.",
            }
        },
        "required": ["code"],
    },
}


# ── 内部函数 ──────────────────────────────────────────────


def _compute_executor(tool_name: str, tool_input: dict) -> str:
    """agentic loop 的 tool executor：执行 compute 工具。"""
    if tool_name != "compute":
        return f"[unknown tool: {tool_name}]"

    res = execute_python(tool_input.get("code", ""))
    if res["error"]:
        logger.warning("[COMPUTE] error: %s", res["error"])
        return f"[computation error: {res['error']}]"

    output = res["stdout"].strip()
    return output if output else str(res.get("result", "[no output]"))


def _validate_citations(answer: str, context: list[dict]) -> list[str]:
    """检查答案中所有 [n] 引用是否指向真实存在的 context 条目。"""
    citations = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    issues = []
    for n in citations:
        if n < 1 or n > len(context):
            issues.append(f"[{n}] out of range (context has {len(context)} items)")
    return issues


# ── 公开接口 ──────────────────────────────────────────────

def synthesize(question: str, context: list[dict], llm: LLMClient, max_tokens: int = 4096, missing_hint: str = "") -> str:
    context_text = _format_context(context)
    gap_notice = (
        f"RETRIEVAL GAP (read before answering): After exhaustive retrieval, "
        f"the following was confirmed NOT present in the context: {missing_hint}. "
        f"You MUST NOT state these figures or facts. "
        f"If the question requires them, say exactly: "
        f"\"The provided context does not contain [the missing item].\"\n\n"
    ) if missing_hint else ""
    user_msg = f"{gap_notice}Context:\n{context_text}\n\nQuestion: {question}"

    answer = llm.chat_agentic(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        tools=[_COMPUTE_TOOL],
        tool_executor=_compute_executor,
        max_tokens=max_tokens,
    )

    issues = _validate_citations(answer, context)
    if issues:
        logger.warning("[SYNTHESIZE] citation issues: %s", issues)

    return answer


def _format_context(context: list[dict]) -> str:
    parts = []
    for i, item in enumerate(context, 1):
        src = item["source"]
        if src == "sec_chunks":
            meta = f"SEC {item.get('doc_type', '')} FY{item.get('fiscal_year', '')} | {item.get('section', '')} | {item.get('period_end', '')}"
            parts.append(f"[{i}] {meta}\n{item['content']}")
        elif src == "events":
            parts.append(f"[{i}] Event [{item['date']}] [{item['category']}] {item['title']}\n{item.get('description', '')}")
        elif src == "macro_indicators":
            parts.append(f"[{i}] {item['title']} ({item['series_id']}) | {item['date']}: {item['value']} {item.get('units', '')}")
        elif src == "price_history":
            if item.get("_granularity") == "monthly":
                pe = f" | avg_P/E={item['avg_pe']:.1f}" if item.get("avg_pe") else ""
                parts.append(
                    f"[{i}] Price {item['ticker']} {item['date']} (monthly):"
                    f" close=${item['close']:.2f} avg=${item['avg_close']:.2f}{pe}"
                )
            else:
                pe = f" | P/E={item['pe_ratio']:.1f}" if item.get("pe_ratio") else ""
                parts.append(f"[{i}] Price {item['ticker']} {item['date']}: close=${item['close']:.2f}{pe}")
        elif src == "earnings_history":
            surprise = f" | surprise={item['eps_surprise_pct']:+.1f}%" if item.get("eps_surprise_pct") is not None else ""
            rev = f" | Rev=${item['revenue']/1e6:.1f}B" if item.get("revenue") else ""
            eps_actual  = item["eps_actual"]  if item.get("eps_actual")  is not None else "N/A"
            eps_estimate = item["eps_estimate"] if item.get("eps_estimate") is not None else "N/A"
            parts.append(
                f"[{i}] Earnings {item['ticker']} FY{item.get('fiscal_year')}Q{item.get('fiscal_quarter','')}"
                f" | EPS actual={eps_actual} est={eps_estimate}{surprise}{rev}"
            )
    return "\n\n".join(parts)
