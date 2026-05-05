"""
Planner: 将用户问题拆解为结构化子查询。
"""
from __future__ import annotations

import json
import re

from models.llm.base import LLMClient

SYSTEM_PROMPT = """\
You are a financial research planner for MacroLens, a RAG system covering:
- GOOGL SEC filings (10-K / 10-Q / 8-K) in table `sec_chunks`
- Macroeconomic & industry events (Fed policy, earnings, antitrust) in table `events`
- US macro time-series (GDP, CPI, UNRATE, etc.) in table `macro_indicators`

Available macro series: GDP, GDPC1, CPIAUCSL, UNRATE, FEDFUNDS, T10Y2Y, PAYEMS, HOUST, INDPRO, RSAFS, UMCSENT, DCOILWTICO

Available event categories: fed_policy, company_action, macro_shock, industry

Given a user question, decompose it into 1-4 sub-queries as a JSON array.
Each sub-query has:
  - "query": natural language retrieval query
  - "sources": list from ["sec_chunks", "events", "macro_indicators"]
  - "filters": optional dict of filters to apply

For macro_indicators, always include "series" (list of series IDs) and optionally "date_from"/"date_to" (YYYY-MM-DD).
For sec_chunks, ALWAYS include "fiscal_year" (integer) when the question mentions a specific year. Only add "section" filter when the question explicitly asks about a specific section (e.g. "risk factors", "MD&A"). For general questions, do NOT add section filter.
For events, optionally include "category".

Example output for "How did rate hikes in 2022 affect Google revenue?":
[
  {"query": "Federal Reserve rate hikes monetary policy 2022", "sources": ["events"], "filters": {"category": "fed_policy"}},
  {"query": "Google advertising revenue macroeconomic headwinds", "sources": ["sec_chunks"], "filters": {"fiscal_year": 2022}},
  {"query": "Federal Funds Rate 2022", "sources": ["macro_indicators"], "filters": {"series": ["FEDFUNDS"], "date_from": "2022-01-01", "date_to": "2022-12-31"}}
]

Respond with ONLY valid JSON array, no explanation."""


def plan(question: str, llm: LLMClient, history: list[dict] | None = None) -> list[dict]:
    """将 question 拆解为子查询列表。history 用于多轮精化。"""
    messages = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    raw = llm.chat(system=SYSTEM_PROMPT, messages=messages, max_tokens=1024, temperature=0.0)

    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        raw = match.group(0)

    return json.loads(raw)
