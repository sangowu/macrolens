"""
Phase 0 单元测试：price_history / earnings_history 数据处理逻辑。
不依赖真实 DB 或 yfinance API，全部使用 mock 数据。

运行：
    uv run pytest tests/test_price_earnings.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import date

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingestion.ingest_prices import (
    _float,
    _int,
    compute_pe_ps_ratios,
    ingest_prices,
    ingest_earnings,
    fetch_price_history,
)
from agent.tools.code_executor import execute_python


# ── 辅助函数 ──────────────────────────────────────────────

class TestHelpers:
    def test_float_normal(self):
        assert _float(3.14) == pytest.approx(3.14)

    def test_float_none(self):
        assert _float(None) is None

    def test_float_nan(self):
        import math
        assert _float(float("nan")) is None

    def test_float_string(self):
        assert _float("bad") is None

    def test_int_normal(self):
        assert _int(42) == 42

    def test_int_none(self):
        assert _int(None) is None

    def test_int_float(self):
        assert _int(3.9) == 3


# ── P/E 计算 ──────────────────────────────────────────────

class TestComputePeRatios:
    def _make_prices(self, close_values: list[float], start="2022-01-03") -> pd.DataFrame:
        dates = pd.date_range(start, periods=len(close_values), freq="B")
        return pd.DataFrame({
            "ticker":    "GOOGL",
            "date":      dates,
            "open":      close_values,
            "high":      close_values,
            "low":       close_values,
            "close":     close_values,
            "adj_close": close_values,
            "volume":    [1_000_000] * len(close_values),
        })

    def _make_earnings(self, quarters: list[tuple]) -> pd.DataFrame:
        """quarters: [(period_end_str, eps_actual), ...]"""
        rows = []
        for period_end, eps in quarters:
            rows.append({
                "ticker":           "GOOGL",
                "period_end":       period_end,
                "fiscal_year":      int(period_end[:4]),
                "fiscal_quarter":   1,
                "period_type":      "quarterly",
                "revenue":          50_000_000,
                "net_income":       None,
                "operating_income": None,
                "eps_actual":       eps,
                "eps_estimate":     None,
                "eps_surprise":     None,
                "eps_surprise_pct": None,
                "cloud_revenue":    None,
                "ads_revenue":      None,
                "gross_profit":     None,
                "gross_margin":     None,
                "operating_margin": None,
            })
        return pd.DataFrame(rows)

    def test_pe_computed_after_four_quarters(self):
        """四个季度 TTM EPS 累积后，P/E = close / ttm_eps。"""
        quarters = [
            ("2021-03-31", 1.0),
            ("2021-06-30", 1.0),
            ("2021-09-30", 1.0),
            ("2021-12-31", 1.0),   # TTM EPS = 4.0 从这一期起可用
            ("2022-03-31", 1.5),
        ]
        earnings_df = self._make_earnings(quarters)
        prices_df = self._make_prices([100.0, 100.0, 100.0], start="2022-04-01")
        result = compute_pe_ps_ratios(prices_df, earnings_df, "GOOGL")
        # 2022-03-31 起 TTM = 1.0*3 + 1.5 = 4.5
        pe_vals = result["pe_ratio"].dropna()
        assert len(pe_vals) > 0
        assert all(pe_vals > 0)

    def test_pe_none_before_four_quarters(self):
        """不足四个季度时 P/E 应为 NULL。"""
        quarters = [
            ("2022-01-01", 1.0),
            ("2022-04-01", 1.0),
        ]
        earnings_df = self._make_earnings(quarters)
        prices_df = self._make_prices([100.0, 100.0, 100.0], start="2021-01-04")
        result = compute_pe_ps_ratios(prices_df, earnings_df, "GOOGL")
        # 2021 日期早于任何季报，P/E 应全为 NULL
        early = result[result["date"] < pd.Timestamp("2022-01-01")]
        assert early["pe_ratio"].isna().all()

    def test_empty_earnings_returns_null_pe(self):
        prices_df = self._make_prices([100.0, 200.0])
        result = compute_pe_ps_ratios(prices_df, pd.DataFrame(), "GOOGL")
        assert result["pe_ratio"].isna().all()

    def test_pe_outlier_filtered(self):
        """P/E > 1000 视为无效，应置为 NULL。"""
        quarters = [
            ("2020-03-31", 0.01),  # 极小 EPS → 极大 P/E
            ("2020-06-30", 0.01),
            ("2020-09-30", 0.01),
            ("2020-12-31", 0.01),  # TTM EPS = 0.04
        ]
        earnings_df = self._make_earnings(quarters)
        prices_df = self._make_prices([500.0], start="2021-01-04")  # P/E = 500/0.04 = 12500
        result = compute_pe_ps_ratios(prices_df, earnings_df, "GOOGL")
        assert result["pe_ratio"].isna().all()


# ── ingest_prices（mock DB）────────────────────────────────

class TestIngestPrices:
    def _make_price_df(self) -> pd.DataFrame:
        return pd.DataFrame({
            "ticker":    ["GOOGL", "GOOGL"],
            "date":      [date(2024, 1, 2), date(2024, 1, 3)],
            "open":      [140.0, 141.0],
            "high":      [142.0, 143.0],
            "low":       [139.0, 140.0],
            "close":     [141.5, 142.5],
            "adj_close": [141.5, 142.5],
            "volume":    [10_000_000, 11_000_000],
            "pe_ratio":  [25.3, 25.5],
            "ps_ratio":  [None, None],
        })

    def test_ingest_prices_calls_executemany(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        n = ingest_prices(conn, self._make_price_df(), "GOOGL")
        assert n == 2
        cur.executemany.assert_called_once()

    def test_ingest_prices_empty_df_returns_zero(self):
        conn = MagicMock()
        n = ingest_prices(conn, pd.DataFrame(), "GOOGL")
        assert n == 0
        conn.cursor.assert_not_called()

    def test_ingest_prices_sql_contains_on_conflict(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        ingest_prices(conn, self._make_price_df(), "GOOGL")
        sql = cur.executemany.call_args[0][0]
        assert "ON CONFLICT" in sql
        assert "DO UPDATE" in sql


# ── ingest_earnings（mock DB）────────────────────────────

class TestIngestEarnings:
    def _make_earnings_df(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "ticker":           "GOOGL",
            "period_end":       "2023-09-30",
            "fiscal_year":      2023,
            "fiscal_quarter":   3,
            "period_type":      "quarterly",
            "revenue":          76_693_000,
            "net_income":       19_689_000,
            "operating_income": 21_343_000,
            "eps_actual":       1.55,
            "eps_estimate":     1.45,
            "eps_surprise":     0.10,
            "eps_surprise_pct": 6.9,
            "cloud_revenue":    None,
            "ads_revenue":      None,
            "gross_profit":     None,
            "gross_margin":     0.558,
            "operating_margin": 0.278,
        }])

    def test_ingest_earnings_calls_executemany(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        n = ingest_earnings(conn, self._make_earnings_df(), "GOOGL")
        assert n == 1
        cur.executemany.assert_called_once()

    def test_ingest_earnings_sql_on_conflict(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        ingest_earnings(conn, self._make_earnings_df(), "GOOGL")
        sql = cur.executemany.call_args[0][0]
        assert "ON CONFLICT" in sql
        assert "ticker, period_end, period_type" in sql

    def test_surprise_pct_positive_when_beat(self):
        df = self._make_earnings_df()
        assert float(df.iloc[0]["eps_actual"]) > float(df.iloc[0]["eps_estimate"])
        assert float(df.iloc[0]["eps_surprise_pct"]) > 0

    def test_ingest_earnings_empty_df_returns_zero(self):
        conn = MagicMock()
        n = ingest_earnings(conn, pd.DataFrame(), "GOOGL")
        assert n == 0


# ── per_loop 去重 key ─────────────────────────────────────

class TestPerLoopDedup:
    """验证修复后的去重 key 不会导致不同 ticker 同日期碰撞。"""

    def _make_key(self, item: dict) -> str:
        return (item.get("id") or item.get("event_id")
                or item.get("ticker", "") + str(item.get("date", "")) + item.get("series_id", ""))

    def test_same_date_different_ticker_not_deduped(self):
        googl = {"source": "price_history", "ticker": "GOOGL", "date": "2022-01-03", "close": 140.0}
        msft  = {"source": "price_history", "ticker": "MSFT",  "date": "2022-01-03", "close": 310.0}
        assert self._make_key(googl) != self._make_key(msft)

    def test_same_ticker_same_date_deduped(self):
        a = {"source": "price_history", "ticker": "GOOGL", "date": "2022-01-03", "close": 140.0}
        b = {"source": "price_history", "ticker": "GOOGL", "date": "2022-01-03", "close": 140.0}
        assert self._make_key(a) == self._make_key(b)

    def test_macro_key_unchanged(self):
        macro = {"source": "macro_indicators", "series_id": "FEDFUNDS", "date": "2022-01-01", "value": 0.08}
        assert self._make_key(macro) == "2022-01-01FEDFUNDS"

    def test_sec_chunk_key_unchanged(self):
        chunk = {"source": "sec_chunks", "id": 42, "content": "..."}
        assert self._make_key(chunk) == 42


# ── compute tool 相关性计算 ────────────────────────────────

class TestCorrelationCompute:
    """验证 compute tool（execute_python）能正确执行相关性计算。"""

    def test_perfect_positive_correlation(self):
        # np 由沙箱预注入，不需要 import
        code = """
x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
y = x * 2.0 + 1.0
r = np.corrcoef(x, y)[0, 1]
print(f"correlation={r:.4f}")
"""
        result = execute_python(code)
        assert result["error"] is None
        assert "correlation=1.0000" in result["stdout"]

    def test_negative_correlation(self):
        code = """
x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
y = -x + 10.0
r = np.corrcoef(x, y)[0, 1]
print(f"correlation={r:.4f}")
"""
        result = execute_python(code)
        assert result["error"] is None
        assert "correlation=-1.0000" in result["stdout"]

    def test_pe_percentile_calculation(self):
        code = """
pe_history = [15.0, 18.0, 22.0, 25.0, 28.0, 19.0, 16.0, 30.0, 27.0, 20.0]
current_pe = 25.0
pct = float(np.mean([v <= current_pe for v in pe_history]))
print(f"percentile={pct:.2f}")
"""
        result = execute_python(code)
        assert result["error"] is None
        assert "percentile=0.70" in result["stdout"]

    def test_pandas_resample(self):
        """验证 pd/np 可在沙箱内执行月度 resample。"""
        code = """
dates = pd.date_range("2022-01-01", periods=12, freq="MS")
prices = pd.Series(np.arange(100, 112, dtype=float), index=dates)
monthly_ret = prices.pct_change().dropna()
print(f"mean_return={monthly_ret.mean():.4f}")
"""
        result = execute_python(code)
        assert result["error"] is None
        assert "mean_return=" in result["stdout"]
