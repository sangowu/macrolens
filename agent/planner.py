"""
Planner: 将用户问题拆解为结构化子查询。
使用 Tool Use 替代正则解析，保证输出格式合法。
"""
from __future__ import annotations

import logging

from models.llm.base import LLMClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a financial research planner for MacroLens, a RAG system covering:
- GOOGL SEC filings (10-K / 10-Q / 8-K) in table `sec_chunks`
- Macroeconomic & industry events (Fed policy, earnings, antitrust) in table `events`
- US macro time-series (GDP, CPI, UNRATE, etc.) in table `macro_indicators`

Available macro series: GDP, GDPC1, CPIAUCSL, UNRATE, FEDFUNDS, T10Y2Y, PAYEMS, HOUST, INDPRO, RSAFS, UMCSENT, DCOILWTICO

Available event categories: fed_policy, company_action, macro_shock, industry

Decompose the user question into 1-4 sub-queries using the create_query_plan tool.

Source routing rules:
- Use "macro_indicators" for any question about specific economic data series: interest rates, GDP, CPI, unemployment, Fed funds rate, inflation, retail sales, oil prices, etc.
- Use "events" for questions about specific events: Fed policy decisions, earnings announcements, antitrust actions.
- Use "sec_chunks" for questions about Google/Alphabet financial results, risk factors, business descriptions.

For macro_indicators: always include "series" (list of series IDs) and optionally "date_from"/"date_to" (YYYY-MM-DD).
For sec_chunks: ALWAYS include "fiscal_year" (integer) when the question mentions a specific year. Only add "section" when explicitly asked.
For events: optionally include "category".

Example — "What was the Federal Funds Rate in December 2022?":
[{"query": "Federal Funds Rate December 2022", "sources": ["macro_indicators"], "filters": {"series": ["FEDFUNDS"], "date_from": "2022-01-01", "date_to": "2022-12-31"}}]

Example — "How did rate hikes in 2022 affect Google revenue?":
[{"query": "Federal Reserve rate hikes 2022", "sources": ["events"], "filters": {"category": "fed_policy"}},
 {"query": "Google advertising revenue 2022", "sources": ["sec_chunks"], "filters": {"fiscal_year": 2022}},
 {"query": "Federal Funds Rate 2022", "sources": ["macro_indicators"], "filters": {"series": ["FEDFUNDS"], "date_from": "2022-01-01", "date_to": "2022-12-31"}}]"""

_PLAN_TOOL = {
    "name": "create_query_plan",
    "description": "Decompose the research question into structured retrieval sub-queries.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sub_queries": {
                "type": "array",
                "description": "List of 1-4 sub-queries to retrieve relevant evidence.",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language retrieval query.",
                        },
                        "sources": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["sec_chunks", "events", "macro_indicators"],
                            },
                            "description": "Data sources to search.",
                        },
                        "filters": {
                            "type": "object",
                            "description": "Filters for the query. For macro_indicators, series is required.",
                            "properties": {
                                "series": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "REQUIRED for macro_indicators. Series IDs from: GDP, GDPC1, CPIAUCSL, UNRATE, FEDFUNDS, T10Y2Y, PAYEMS, HOUST, INDPRO, RSAFS, UMCSENT, DCOILWTICO",
                                },
                                "date_from": {
                                    "type": "string",
                                    "description": "Start date YYYY-MM-DD for macro_indicators",
                                },
                                "date_to": {
                                    "type": "string",
                                    "description": "End date YYYY-MM-DD for macro_indicators",
                                },
                                "fiscal_year": {
                                    "type": "integer",
                                    "description": "REQUIRED for sec_chunks when question mentions a specific year",
                                },
                                "section": {
                                    "type": "string",
                                    "description": "Optional for sec_chunks: e.g. 'MD&A', 'Risk Factors'",
                                },
                                "category": {
                                    "type": "string",
                                    "description": "Optional for events: fed_policy, company_action, macro_shock, industry",
                                },
                            },
                        },
                    },
                    "required": ["query", "sources"],
                },
            }
        },
        "required": ["sub_queries"],
    },
}


def plan(question: str, llm: LLMClient, history: list[dict] | None = None) -> list[dict]:
    """将 question 拆解为子查询列表。history 用于多轮精化。"""
    messages: list[dict] = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    result = llm.chat_with_tools(
        system=SYSTEM_PROMPT,
        messages=messages,
        tools=[_PLAN_TOOL],
        tool_choice={"type": "tool", "name": "create_query_plan"},
        max_tokens=1024,
        temperature=0.0,
    )

    sub_queries = result.get("sub_queries", [])
    for sq in sub_queries:
        logger.info("[PLAN] sources=%s filters=%s | %s", sq.get("sources"), sq.get("filters"), sq.get("query", "")[:80])
    return sub_queries
