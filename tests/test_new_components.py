"""
新组件单元测试：用 mock LLM 验证行为，不调真实 API。

运行：
    uv run pytest tests/test_new_components.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.synthesizer import _validate_citations, _format_context, _compute_executor
from agent.planner import plan


# ── Mock LLM ──────────────────────────────────────────────

class MockLLM:
    provider = "anthropic"

    def __init__(self, tool_responses: dict[str, dict] | None = None, chat_response: str = ""):
        self._tool_responses = tool_responses or {}
        self._chat_response = chat_response

    def chat(self, system, messages, max_tokens=1024, temperature=0.0) -> str:
        return self._chat_response

    def chat_with_tools(self, system, messages, tools, tool_choice, max_tokens=1024, temperature=0.0) -> dict:
        return self._tool_responses.get(tool_choice.get("name", ""), {})

    def chat_agentic(self, system, messages, tools, tool_executor: Callable, max_tokens=4096, max_turns=10) -> str:
        return self._chat_response


# ── Planner ───────────────────────────────────────────────

class TestPlanner:
    def test_returns_sub_queries(self):
        llm = MockLLM(tool_responses={
            "create_query_plan": {
                "sub_queries": [
                    {"query": "Google advertising revenue 2019", "sources": ["sec_chunks"], "filters": {"fiscal_year": 2019}},
                    {"query": "Google advertising revenue 2023", "sources": ["sec_chunks"], "filters": {"fiscal_year": 2023}},
                ]
            }
        })
        result = plan("What was Google's CAGR from 2019 to 2023?", llm)
        assert len(result) == 2
        assert result[0]["sources"] == ["sec_chunks"]
        assert result[0]["filters"]["fiscal_year"] == 2019

    def test_empty_tool_response_returns_empty_list(self):
        llm = MockLLM(tool_responses={"create_query_plan": {}})
        result = plan("Any question", llm)
        assert result == []

    def test_passes_history_in_second_round(self):
        captured = {}

        class CaptureLLM(MockLLM):
            def chat_with_tools(self, system, messages, tools, tool_choice, **kwargs):
                captured["messages"] = messages
                return {"sub_queries": [{"query": "x", "sources": ["sec_chunks"]}]}

        history = [{"role": "assistant", "content": "Need 2019 data"}]
        plan("Question", CaptureLLM(), history=history)

        assert len(captured["messages"]) == 2
        assert captured["messages"][0]["role"] == "assistant"


# ── Synthesizer: 引用验证 ─────────────────────────────────

class TestValidateCitations:
    def _ctx(self, n):
        return [{"source": "sec_chunks", "id": f"c{i}", "content": f"text {i}"} for i in range(1, n + 1)]

    def test_valid_no_issues(self):
        assert _validate_citations("Revenue [1] grew to $200B [2].", self._ctx(3)) == []

    def test_out_of_range_flagged(self):
        issues = _validate_citations("See [5].", self._ctx(2))
        assert len(issues) == 1 and "[5]" in issues[0]

    def test_multiple_bad(self):
        assert len(_validate_citations("See [3] and [4].", self._ctx(2))) == 2

    def test_no_citations(self):
        assert _validate_citations("Revenue grew.", self._ctx(3)) == []


# ── Synthesizer: compute executor ────────────────────────

class TestComputeExecutor:
    def test_basic_calculation(self):
        result = _compute_executor("compute", {"code": "print(f'{(237855/134811)**(1/4)-1:.3f}')"})
        assert "0.15" in result

    def test_unknown_tool(self):
        assert "unknown tool" in _compute_executor("bad_tool", {})

    def test_division_by_zero_returns_error(self):
        result = _compute_executor("compute", {"code": "print(1/0)"})
        assert "error" in result.lower()

    def test_agentic_tool_executor_called(self):
        """验证 synthesize() 中 tool_executor 被正确传入 chat_agentic。"""
        calls = []

        class CaptureLLM(MockLLM):
            def chat_agentic(self, system, messages, tools, tool_executor, **kwargs):
                # 模拟调用一次 compute tool
                result = tool_executor("compute", {"code": "print('42%')"})
                calls.append(result)
                return f"The answer is {result}"

        from agent.synthesizer import synthesize
        context = [{"source": "sec_chunks", "id": "c1", "content": "data", "doc_type": "10-K",
                    "fiscal_year": 2023, "section": "Business", "period_end": "2023-12-31"}]
        answer = synthesize("question", context, CaptureLLM())
        assert calls == ["42%"]
        assert "42%" in answer


# ── Executor: macro series 格式兼容 ───────────────────────

class TestExecutorMacroSeries:
    def test_string_normalized_to_list(self):
        from agent.executor import _search_macro
        from unittest.mock import MagicMock

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []

        _search_macro(mock_conn, {"series": "FEDFUNDS", "_query": "Federal Funds Rate"})

        passed_series = mock_conn.execute.call_args[0][1]["series"]
        assert isinstance(passed_series, list)
        assert passed_series == ["FEDFUNDS"]

    def test_keyword_fallback_when_series_empty(self):
        from agent.executor import _infer_series
        assert "FEDFUNDS" in _infer_series("Federal Funds Rate in December 2022")
        assert "UNRATE" in _infer_series("US unemployment rate January 2023")
        assert "CPIAUCSL" in _infer_series("CPI inflation rate June 2022")


# ── Sources 面板过滤 ──────────────────────────────────────

class TestSourcesFilter:
    def _ctx(self):
        return [
            {"source": "sec_chunks", "id": f"c{i}", "doc_type": "10-K", "fiscal_year": 2020+i,
             "section": "Business", "period_end": f"202{i}-12-31", "content": f"Revenue {i}"}
            for i in range(1, 4)
        ]

    def test_only_cited_shown(self):
        from ui.app import _build_sources_md
        result = _build_sources_md(self._ctx(), answer="Revenue [1] and [3].")
        assert "Revenue 1" in result
        assert "Revenue 3" in result
        assert "Revenue 2" not in result

    def test_no_answer_shows_all(self):
        from ui.app import _build_sources_md
        result = _build_sources_md(self._ctx(), answer="")
        assert "Revenue 1" in result
        assert "Revenue 2" in result
        assert "Revenue 3" in result


# ── Memory: 提取 ──────────────────────────────────────────

class TestMemoryExtract:
    def test_extract_returns_findings(self):
        from unittest.mock import MagicMock
        import psycopg

        llm = MockLLM(tool_responses={
            "extract_findings": {
                "findings": [{"memory_type": "finding", "content": "Google CAGR was 15.3%", "fiscal_year": None}]
            }
        })
        mock_conn = MagicMock(spec=psycopg.Connection)
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_embedder = MagicMock()
        mock_embedder.encode.return_value = [[0.1] * 1024]

        from agent.memory import extract_and_store
        count = extract_and_store("t1", "What is CAGR?", "CAGR was 15.3%", mock_conn, mock_embedder, llm)
        assert count == 1
        mock_cur.execute.assert_called_once()


# ── Format context ────────────────────────────────────────

class TestFormatContext:
    def test_sec_chunk(self):
        ctx = [{"source": "sec_chunks", "id": "c1", "doc_type": "10-K", "fiscal_year": 2022,
                "section": "Business", "period_end": "2022-12-31", "content": "Revenue $100B"}]
        text = _format_context(ctx)
        assert "[1]" in text and "FY2022" in text and "Revenue $100B" in text

    def test_macro_indicator(self):
        ctx = [{"source": "macro_indicators", "title": "Federal Funds Rate",
                "series_id": "FEDFUNDS", "date": "2022-12-01", "value": 4.1, "units": "%"}]
        text = _format_context(ctx)
        assert "[1]" in text and "FEDFUNDS" in text and "4.1" in text
