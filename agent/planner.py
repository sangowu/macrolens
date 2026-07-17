"""
Planner: 将用户问题拆解为结构化子查询。
使用 Tool Use 替代正则解析，保证输出格式合法。
"""
from __future__ import annotations

import logging

from models.llm.base import LLMClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a financial research planner for MacroLens, a RAG system covering five data sources.

DATA SOURCES:
- `sec_chunks`:        SEC filings (10-K / 10-Q / 8-K) for MAG7 companies
- `events`:            Macroeconomic & industry events (Fed policy, earnings, antitrust)
- `macro_indicators`:  US macro time-series (GDP, CPI, UNRATE, FEDFUNDS, etc.)
- `price_history`:     Stock price history and valuation ratios (P/E, P/S)
- `earnings_history`:  Quarterly/annual earnings with EPS actual vs estimate

Supported tickers (sec_chunks, price_history, earnings_history): GOOGL, MSFT, META, AMZN, AAPL, NVDA, TSLA
Available macro series: GDP, GDPC1, CPIAUCSL, UNRATE, FEDFUNDS, T10Y2Y, PAYEMS, HOUST, INDPRO, RSAFS, UMCSENT, DCOILWTICO
Available event categories: fed_policy, company_action, macro_shock, industry

Decompose the user question into 1-4 sub-queries using the create_query_plan tool.

SCOPE GATING (evaluate this FIRST):
Before decomposing, decide whether the question belongs to MacroLens's domain:
MAG7 companies (GOOGL, MSFT, META, AMZN, AAPL, NVDA, TSLA), US macroeconomics, SEC filings, stock prices, and earnings.
- If the question is ENTIRELY outside this domain (e.g. weather, cooking, general coding help,
  unrelated private companies, casual chitchat), set in_scope=false, write a one-sentence
  reject_reason for the user, and return an EMPTY sub_queries list.
- Otherwise set in_scope=true and decompose as usual.
- IMPORTANT: a question is still IN scope even when the data may be unavailable. Future or
  speculative questions ("stock price in 2030", "if the Fed cuts rates to zero"), tickers that
  may not be ingested yet, and cross-source comparisons are ALL in scope — downstream retrieval
  and synthesis handle missing data. Reject ONLY questions that have nothing to do with MAG7
  companies or US macroeconomics.

SOURCE ROUTING RULES:
1. Use "macro_indicators" for questions about economic data series: interest rates, GDP, CPI, unemployment, Fed funds rate, inflation, retail sales, oil prices, yield curve, etc.
   - Always include "series" (list of series IDs) and optionally "date_from"/"date_to" (YYYY-MM-DD).
2. Use "events" for questions about specific named events: Fed policy decisions, earnings announcements, antitrust actions.
   - Optionally include "category".
3. Use "sec_chunks" for questions about company financial results, risk factors, business descriptions from filings.
   - ALWAYS include "fiscal_year" (integer) when the question mentions a specific year.
   - For competitor comparisons, include multiple tickers in filters.company (e.g. ["GOOGL", "MSFT"]).
   - If no company is specified, default to filters.company = ["GOOGL"].
4. Use "price_history" for questions about stock price trends, P/E ratio, valuation, or stock-price performance.
   - Always include "tickers" (default ["GOOGL"]), "date_from", "date_to".
5. Use "earnings_history" for questions about EPS, earnings beat/miss, revenue trends, quarterly results.
   - Always include "tickers", "period_type" ("quarterly"/"annual"), "year_from", "year_to".

MANDATORY MULTI-SOURCE RULE:
- For ANY question asking about correlation, relationship, or comparison between a stock metric
  (price, return, performance) and a macro indicator (interest rate, Fed funds rate, CPI, GDP, etc.),
  you MUST generate SEPARATE sub-queries for BOTH sources:
  * One sub-query with sources=["price_history"] for the stock data
  * One sub-query with sources=["macro_indicators"] for the macro series
  Failing to include both sub-queries will make the correlation analysis impossible.

DATE SCOPING:
- When a question mentions a specific year (e.g. "in 2022"), set date_from="YYYY-01-01" and date_to="YYYY-12-31".
- When a question mentions a range (e.g. "2021 to 2023"), use the full span.
- Do not expand the date range beyond what the question asks.

Example — "What was the Federal Funds Rate in December 2022?":
[{"query": "Federal Funds Rate December 2022", "sources": ["macro_indicators"], "filters": {"series": ["FEDFUNDS"], "date_from": "2022-01-01", "date_to": "2022-12-31"}}]

Example — "How did rate hikes in 2022 affect Google revenue?":
[{"query": "Federal Reserve rate hikes 2022", "sources": ["events"], "filters": {"category": "fed_policy"}},
 {"query": "Google advertising revenue 2022", "sources": ["sec_chunks"], "filters": {"fiscal_year": 2022, "company": ["GOOGL"]}},
 {"query": "Federal Funds Rate 2022", "sources": ["macro_indicators"], "filters": {"series": ["FEDFUNDS"], "date_from": "2022-01-01", "date_to": "2022-12-31"}}]

Example — "Compare Google Cloud and Microsoft Azure revenue 2023":
[{"query": "Google Cloud revenue growth 2023", "sources": ["sec_chunks"], "filters": {"fiscal_year": 2023, "company": ["GOOGL"]}},
 {"query": "Microsoft Azure cloud revenue 2023", "sources": ["sec_chunks"], "filters": {"fiscal_year": 2023, "company": ["MSFT"]}}]

Example — "Is GOOGL expensive right now?":
[{"query": "GOOGL stock price and P/E ratio 2019-2025", "sources": ["price_history"], "filters": {"tickers": ["GOOGL"], "date_from": "2019-01-01", "date_to": "2025-12-31"}},
 {"query": "GOOGL quarterly EPS history", "sources": ["earnings_history"], "filters": {"tickers": ["GOOGL"], "period_type": "quarterly", "year_from": 2019, "year_to": 2025}},
 {"query": "Alphabet business outlook 2024", "sources": ["sec_chunks"], "filters": {"fiscal_year": 2024, "company": ["GOOGL"]}}]

Example — "What was the correlation between Federal Funds Rate changes and GOOGL monthly stock returns in 2022?":
[{"query": "GOOGL monthly stock returns 2022", "sources": ["price_history"], "filters": {"tickers": ["GOOGL"], "date_from": "2022-01-01", "date_to": "2022-12-31"}},
 {"query": "Federal Funds Rate monthly changes 2022", "sources": ["macro_indicators"], "filters": {"series": ["FEDFUNDS"], "date_from": "2022-01-01", "date_to": "2022-12-31"}}]

Example — "How did GOOGL stock perform relative to CPI inflation from 2021 to 2023?":
[{"query": "GOOGL monthly stock price 2021 to 2023", "sources": ["price_history"], "filters": {"tickers": ["GOOGL"], "date_from": "2021-01-01", "date_to": "2023-12-31"}},
 {"query": "US CPI inflation monthly 2021 to 2023", "sources": ["macro_indicators"], "filters": {"series": ["CPIAUCSL"], "date_from": "2021-01-01", "date_to": "2023-12-31"}}]\
"""

_PLAN_TOOL = {
    "name": "create_query_plan",
    "description": "Decompose the research question into structured retrieval sub-queries.",
    "input_schema": {
        "type": "object",
        "properties": {
            "in_scope": {
                "type": "boolean",
                "description": (
                    "False ONLY if the question is entirely outside MacroLens's domain "
                    "(MAG7 companies + US macroeconomics + SEC filings + stock/earnings data). "
                    "A question is IN scope even if its answer may be unavailable "
                    "(future/speculative questions, un-ingested tickers, cross-source comparisons)."
                ),
            },
            "reject_reason": {
                "type": "string",
                "description": "One sentence explaining the out-of-scope rejection for the user. Empty string when in_scope is true.",
            },
            "sub_queries": {
                "type": "array",
                "description": "List of 0-4 sub-queries to retrieve relevant evidence. Empty when in_scope is false.",
                "minItems": 0,
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
                                "enum": ["sec_chunks", "events", "macro_indicators", "price_history", "earnings_history"],
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
                                "company": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "For sec_chunks: ticker(s) to filter, e.g. ['GOOGL'] or ['GOOGL','MSFT']. Supported: GOOGL, MSFT, META, AMZN, AAPL, NVDA, TSLA. Defaults to ['GOOGL'].",
                                },
                                "tickers": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "For price_history and earnings_history: list of stock tickers, e.g. ['GOOGL','MSFT']. Defaults to ['GOOGL'].",
                                },
                                "period_type": {
                                    "type": "string",
                                    "enum": ["quarterly", "annual"],
                                    "description": "For earnings_history: 'quarterly' or 'annual'. Defaults to 'quarterly'.",
                                },
                                "year_from": {
                                    "type": "integer",
                                    "description": "For earnings_history: fiscal year range start.",
                                },
                                "year_to": {
                                    "type": "integer",
                                    "description": "For earnings_history: fiscal year range end.",
                                },
                            },
                        },
                    },
                    "required": ["query", "sources"],
                },
            }
        },
        "required": ["in_scope", "sub_queries"],
    },
}


def plan_scoped(
    question: str, llm: LLMClient, history: list[dict] | None = None
) -> tuple[bool, str, list[dict]]:
    """将 question 拆解为子查询，并附带域内判断。

    返回 (in_scope, reject_reason, sub_queries)：
    - in_scope=False 时 sub_queries 为空，reject_reason 是给用户的一句话拒答理由。
    - LLM 未返回 in_scope 字段时默认 True（向后兼容旧 mock / 测试）。
    """
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

    in_scope = result.get("in_scope", True)
    reject_reason = result.get("reject_reason", "")
    sub_queries = result.get("sub_queries", [])

    if not in_scope:
        logger.info("[PLAN] out-of-scope, rejecting | reason=%s", reject_reason)
        return False, reject_reason, []

    for sq in sub_queries:
        logger.info("[PLAN] sources=%s filters=%s | %s", sq.get("sources"), sq.get("filters"), sq.get("query", "")[:80])
    return True, "", sub_queries


def plan(question: str, llm: LLMClient, history: list[dict] | None = None) -> list[dict]:
    """向后兼容入口：只返回子查询列表，忽略域内判断。"""
    _, _, sub_queries = plan_scoped(question, llm, history=history)
    return sub_queries
