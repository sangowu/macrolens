"""
Phase 1 单元测试：MAG7 多 Ticker SEC 支持。
验证 company filter 构建、SQL 注入防护、Planner 路由。

运行：
    uv run pytest tests/test_sec_multi.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.executor import _build_company_filter, _ALLOWED_COMPANIES
from agent.planner import plan


# ── MockLLM（复用 test_new_components 的模式）────────────

class MockLLM:
    provider = "anthropic"

    def __init__(self, tool_responses: dict | None = None):
        self._tool_responses = tool_responses or {}

    def chat(self, *a, **kw) -> str:
        return ""

    def chat_with_tools(self, system, messages, tools, tool_choice, **kw) -> dict:
        return self._tool_responses.get(tool_choice.get("name", ""), {})

    def chat_agentic(self, *a, **kw) -> str:
        return ""


# ── _build_company_filter ─────────────────────────────────

class TestBuildCompanyFilter:
    def test_single_allowed_ticker(self):
        f = _build_company_filter({"company": ["GOOGL"]})
        assert "AND company IN" in f
        assert "'GOOGL'" in f

    def test_multiple_allowed_tickers(self):
        f = _build_company_filter({"company": ["GOOGL", "MSFT"]})
        assert "'GOOGL'" in f
        assert "'MSFT'" in f

    def test_string_input_converted_to_list(self):
        f = _build_company_filter({"company": "MSFT"})
        assert "'MSFT'" in f

    def test_empty_company_returns_empty_string(self):
        assert _build_company_filter({}) == ""
        assert _build_company_filter({"company": []}) == ""

    def test_unknown_ticker_filtered_out(self):
        f = _build_company_filter({"company": ["UNKNOWN_CORP"]})
        assert f == ""

    def test_sql_injection_blocked(self):
        """恶意 ticker 值应被白名单完全过滤，不出现在 SQL 中。"""
        malicious = ["GOOGL'; DROP TABLE sec_chunks; --"]
        f = _build_company_filter({"company": malicious})
        assert f == ""
        assert "DROP" not in f

    def test_mixed_valid_invalid(self):
        """部分有效 ticker：只保留白名单内的。"""
        f = _build_company_filter({"company": ["GOOGL", "EVIL'; --", "MSFT"]})
        assert "'GOOGL'" in f
        assert "'MSFT'" in f
        assert "EVIL" not in f

    def test_all_mag7_tickers_allowed(self):
        """所有 MAG7 ticker 都在白名单内。"""
        for t in ["GOOGL", "MSFT", "META", "AMZN", "AAPL", "NVDA", "TSLA"]:
            assert t in _ALLOWED_COMPANIES

    def test_filter_format_is_valid_sql_fragment(self):
        """生成的片段以 'AND company IN (' 开头。"""
        f = _build_company_filter({"company": ["GOOGL"]})
        assert f.startswith("AND company IN (")
        assert f.endswith(")")


# ── Planner company filter 路由 ────────────────────────────

class TestPlannerCompanyRouting:
    def test_single_company_plan(self):
        llm = MockLLM(tool_responses={
            "create_query_plan": {
                "sub_queries": [{
                    "query": "Google Cloud revenue 2023",
                    "sources": ["sec_chunks"],
                    "filters": {"fiscal_year": 2023, "company": ["GOOGL"]},
                }]
            }
        })
        result = plan("What was Google Cloud revenue in 2023?", llm)
        assert len(result) == 1
        assert result[0]["filters"]["company"] == ["GOOGL"]

    def test_multi_company_plan(self):
        llm = MockLLM(tool_responses={
            "create_query_plan": {
                "sub_queries": [
                    {
                        "query": "Google Cloud revenue growth 2023",
                        "sources": ["sec_chunks"],
                        "filters": {"fiscal_year": 2023, "company": ["GOOGL"]},
                    },
                    {
                        "query": "Microsoft Azure revenue growth 2023",
                        "sources": ["sec_chunks"],
                        "filters": {"fiscal_year": 2023, "company": ["MSFT"]},
                    },
                ]
            }
        })
        result = plan("Compare Google Cloud and Microsoft Azure revenue 2023", llm)
        assert len(result) == 2
        companies = [sq["filters"]["company"] for sq in result]
        assert ["GOOGL"] in companies
        assert ["MSFT"] in companies

    def test_new_sources_in_enum(self):
        """price_history 和 earnings_history 应在 sources enum 中可用。"""
        llm = MockLLM(tool_responses={
            "create_query_plan": {
                "sub_queries": [
                    {"query": "GOOGL stock price 2022", "sources": ["price_history"],
                     "filters": {"tickers": ["GOOGL"]}},
                    {"query": "GOOGL EPS 2022", "sources": ["earnings_history"],
                     "filters": {"tickers": ["GOOGL"], "period_type": "quarterly"}},
                ]
            }
        })
        result = plan("Is GOOGL expensive?", llm)
        sources_used = {s for sq in result for s in sq["sources"]}
        assert "price_history" in sources_used
        assert "earnings_history" in sources_used


# ── executor SEC_RRF_SQL company_filter 占位符 ─────────────

class TestSecRrfSqlTemplate:
    def test_company_filter_placeholder_exists(self):
        from agent.executor import SEC_RRF_SQL
        assert "{company_filter}" in SEC_RRF_SQL

    def test_sql_formats_without_filter(self):
        from agent.executor import SEC_RRF_SQL, RRF_K
        sql = SEC_RRF_SQL.format(
            section_filter="", company_filter="", year_filter="", rrf_k=RRF_K
        )
        assert "FROM sec_chunks" in sql
        assert "ORDER BY rrf_score DESC" in sql

    def test_sql_formats_with_company_filter(self):
        from agent.executor import SEC_RRF_SQL, RRF_K
        sql = SEC_RRF_SQL.format(
            section_filter="",
            company_filter="AND company IN ('GOOGL','MSFT')",
            year_filter="AND fiscal_year = 2023",
            rrf_k=RRF_K,
        )
        assert "AND company IN ('GOOGL','MSFT')" in sql
        assert "AND fiscal_year = 2023" in sql


# ── ingest_sec_multi ticker map ────────────────────────────

class TestTickerCikMap:
    def test_all_mag7_present(self):
        from ingestion.ingest_sec_multi import TICKER_CIK_MAP
        for ticker in ["GOOGL", "MSFT", "META", "AMZN", "AAPL", "NVDA", "TSLA"]:
            assert ticker in TICKER_CIK_MAP, f"{ticker} missing from TICKER_CIK_MAP"

    def test_cik_format(self):
        """CIK 应为 10 位零填充数字字符串。"""
        from ingestion.ingest_sec_multi import TICKER_CIK_MAP
        for ticker, cik in TICKER_CIK_MAP.items():
            assert len(cik) == 10, f"{ticker} CIK length != 10: {cik}"
            assert cik.isdigit(), f"{ticker} CIK not numeric: {cik}"

    def test_googl_cik_correct(self):
        from ingestion.ingest_sec_multi import TICKER_CIK_MAP
        assert TICKER_CIK_MAP["GOOGL"] == "0001652044"

    def test_unknown_ticker_returns_zero(self):
        """ingest_ticker 对未知 ticker 直接返回 0，不抛异常。"""
        from ingestion.ingest_sec_multi import ingest_ticker
        from unittest.mock import MagicMock
        conn = MagicMock()
        embedder = MagicMock()
        cfg = MagicMock()
        n = ingest_ticker("UNKNOWN", conn, embedder, cfg, ingest_only=True, yes=True)
        assert n == 0
