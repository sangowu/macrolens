"""
Phase 4 单元测试：估值决策支持。
验证 P/E 百分位计算、compute tool 集成、synthesizer 格式化。

运行：
    uv run pytest tests/test_valuation.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.tools.code_executor import execute_python
from agent.synthesizer import _format_context, _compute_executor


# ── P/E 百分位 compute tool 计算 ──────────────────────────

class TestValuationCompute:
    def test_pe_percentile_at_70th(self):
        """70th 百分位：7/10 历史值 <= current_pe。"""
        code = """
pe_history = [15.0, 18.0, 22.0, 25.0, 28.0, 19.0, 16.0, 30.0, 27.0, 20.0]
current_pe = 25.0
pct = float(np.mean([v <= current_pe for v in pe_history]))
print(f"percentile={pct:.2f}")
"""
        result = execute_python(code)
        assert result["error"] is None
        assert "percentile=0.70" in result["stdout"]

    def test_pe_percentile_at_100th(self):
        """当前 P/E 为历史最高值时，百分位 = 1.0。"""
        code = """
pe_history = [10.0, 15.0, 20.0, 25.0, 30.0]
current_pe = 30.0
pct = float(np.mean([v <= current_pe for v in pe_history]))
print(f"percentile={pct:.2f}")
"""
        result = execute_python(code)
        assert result["error"] is None
        assert "percentile=1.00" in result["stdout"]

    def test_pe_percentile_at_0th(self):
        """当前 P/E 低于所有历史值时，百分位 = 0.0。"""
        code = """
pe_history = [20.0, 25.0, 30.0, 35.0, 40.0]
current_pe = 5.0
pct = float(np.mean([v <= current_pe for v in pe_history]))
print(f"percentile={pct:.2f}")
"""
        result = execute_python(code)
        assert result["error"] is None
        assert "percentile=0.00" in result["stdout"]

    def test_pe_percentile_accuracy(self):
        """计算结果与 np.percentile 语义一致，误差 < 0.02。"""
        code = """
pe_history = [12.0, 15.0, 17.0, 19.0, 20.0, 22.0, 25.0, 28.0, 30.0, 35.0]
current_pe = 20.0
pct = float(np.mean([v <= current_pe for v in pe_history]))
print(f"percentile={pct:.2f}")
# Expected: 5 values (12,15,17,19,20) <= 20 → 0.50
"""
        result = execute_python(code)
        assert result["error"] is None
        assert "percentile=0.50" in result["stdout"]

    def test_pe_range_description(self):
        """生成 P/E 历史区间描述的完整计算。"""
        code = """
pe_history = [18.0, 20.0, 22.0, 25.0, 28.0, 30.0, 24.0, 19.0, 21.0, 26.0]
current_pe = 28.0
pct = float(np.mean([v <= current_pe for v in pe_history]))
pe_min = min(pe_history)
pe_max = max(pe_history)
pe_mean = sum(pe_history) / len(pe_history)
print(f"percentile={pct:.2f} min={pe_min:.1f} max={pe_max:.1f} mean={pe_mean:.1f}")
"""
        result = execute_python(code)
        assert result["error"] is None
        assert "percentile=" in result["stdout"]
        assert "min=" in result["stdout"]
        assert "max=" in result["stdout"]
        assert "mean=" in result["stdout"]

    def test_compute_executor_returns_output(self):
        """_compute_executor 正常返回 stdout 内容。"""
        result = _compute_executor("compute", {"code": "print('pe=25.3')"})
        assert result == "pe=25.3"

    def test_compute_executor_unknown_tool(self):
        """未知 tool 名返回提示字符串，不抛异常。"""
        result = _compute_executor("unknown_tool", {"code": ""})
        assert "unknown tool" in result


# ── synthesizer _format_context 价格/财报格式 ─────────────

class TestFormatContextNewSources:
    def test_price_history_format_with_pe(self):
        ctx = [{
            "source":    "price_history",
            "ticker":    "GOOGL",
            "date":      "2024-01-15",
            "close":     142.50,
            "adj_close": 142.50,
            "volume":    25_000_000,
            "pe_ratio":  25.3,
            "ps_ratio":  None,
        }]
        text = _format_context(ctx)
        assert "[1] Price GOOGL 2024-01-15" in text
        assert "$142.50" in text
        assert "P/E=25.3" in text

    def test_price_history_format_no_pe(self):
        ctx = [{
            "source":  "price_history",
            "ticker":  "NVDA",
            "date":    "2024-06-01",
            "close":   1100.0,
            "adj_close": 1100.0,
            "volume":  10_000_000,
            "pe_ratio": None,
            "ps_ratio": None,
        }]
        text = _format_context(ctx)
        assert "NVDA" in text
        assert "P/E" not in text

    def test_earnings_history_format_with_surprise(self):
        ctx = [{
            "source":           "earnings_history",
            "ticker":           "GOOGL",
            "period_end":       "2023-09-30",
            "fiscal_year":      2023,
            "fiscal_quarter":   3,
            "period_type":      "quarterly",
            "eps_actual":       1.55,
            "eps_estimate":     1.45,
            "eps_surprise":     0.10,
            "eps_surprise_pct": 6.9,
            "revenue":          76_693_000.0,
            "net_income":       None,
            "cloud_revenue":    None,
            "ads_revenue":      None,
            "gross_margin":     None,
            "operating_margin": None,
        }]
        text = _format_context(ctx)
        assert "[1] Earnings GOOGL FY2023Q3" in text
        assert "EPS actual=1.55" in text
        assert "est=1.45" in text
        assert "+6.9%" in text

    def test_earnings_history_format_no_estimate(self):
        ctx = [{
            "source":           "earnings_history",
            "ticker":           "MSFT",
            "period_end":       "2022-06-30",
            "fiscal_year":      2022,
            "fiscal_quarter":   4,
            "period_type":      "quarterly",
            "eps_actual":       2.23,
            "eps_estimate":     None,
            "eps_surprise":     None,
            "eps_surprise_pct": None,
            "revenue":          51_865_000.0,
            "net_income":       None,
            "cloud_revenue":    None,
            "ads_revenue":      None,
            "gross_margin":     None,
            "operating_margin": None,
        }]
        text = _format_context(ctx)
        assert "MSFT FY2022Q4" in text
        assert "est=N/A" in text

    def test_mixed_sources_all_formatted(self):
        """sec + price + earnings 混合 context 各自正确格式化。"""
        ctx = [
            {"source": "sec_chunks", "id": 1, "content": "Cloud revenue grew.",
             "doc_type": "10-K", "fiscal_year": 2023, "section": "MD&A", "period_end": "2023-12-31", "rrf_score": 0.9},
            {"source": "price_history", "ticker": "GOOGL", "date": "2024-01-02",
             "close": 140.0, "adj_close": 140.0, "volume": 1_000_000, "pe_ratio": 24.0, "ps_ratio": None},
            {"source": "earnings_history", "ticker": "GOOGL", "period_end": "2023-12-31",
             "fiscal_year": 2023, "fiscal_quarter": 4, "period_type": "quarterly",
             "eps_actual": 1.64, "eps_estimate": 1.59, "eps_surprise": 0.05,
             "eps_surprise_pct": 3.1, "revenue": 86_310_000.0, "net_income": None,
             "cloud_revenue": None, "ads_revenue": None, "gross_margin": None, "operating_margin": None},
        ]
        text = _format_context(ctx)
        assert "[1]" in text and "SEC" in text
        assert "[2] Price GOOGL" in text
        assert "[3] Earnings GOOGL FY2023Q4" in text


# ── Planner SYSTEM_PROMPT 包含估值路由 ─────────────────────

class TestPlannerValuationRouting:
    def test_system_prompt_contains_valuation_example(self):
        from agent.planner import SYSTEM_PROMPT
        assert "Is GOOGL expensive" in SYSTEM_PROMPT or "valuation" in SYSTEM_PROMPT.lower()

    def test_system_prompt_contains_price_history_rule(self):
        from agent.planner import SYSTEM_PROMPT
        assert "price_history" in SYSTEM_PROMPT

    def test_system_prompt_contains_earnings_history_rule(self):
        from agent.planner import SYSTEM_PROMPT
        assert "earnings_history" in SYSTEM_PROMPT

    def test_synthesizer_prompt_contains_valuation_guidance(self):
        from agent.synthesizer import SYSTEM_PROMPT
        assert "percentile" in SYSTEM_PROMPT
        assert "buy" not in SYSTEM_PROMPT.lower() or "never say" in SYSTEM_PROMPT.lower() or "Never say" in SYSTEM_PROMPT
