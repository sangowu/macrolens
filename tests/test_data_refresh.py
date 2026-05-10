"""
Phase 5 单元测试：data_refresh_worker 新鲜度检查逻辑。
不依赖真实 DB，全部使用 mock。

运行：
    uv run pytest tests/test_data_refresh.py -v
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from worker.data_refresh_worker import (
    check_price_freshness,
    check_earnings_freshness,
    check_and_warn_freshness,
    PRICE_STALE_DAYS,
    EARNINGS_STALE_DAYS,
)


# ── mock conn 工厂 ────────────────────────────────────────

def _mock_conn(max_date):
    """返回一个 mock psycopg.Connection，SELECT MAX 返回 max_date。"""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = (max_date,)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


# ── check_price_freshness ─────────────────────────────────

class TestCheckPriceFreshness:
    def test_fresh_data_returns_false(self):
        """最新数据就是今天 → 不需要更新。"""
        conn = _mock_conn(date.today())
        assert check_price_freshness(conn, "GOOGL") is False

    def test_stale_data_returns_true(self):
        """数据超过 PRICE_STALE_DAYS 天 → 需要更新。"""
        stale_date = date.today() - timedelta(days=PRICE_STALE_DAYS + 1)
        conn = _mock_conn(stale_date)
        assert check_price_freshness(conn, "GOOGL") is True

    def test_exactly_at_boundary_returns_false(self):
        """恰好 PRICE_STALE_DAYS 天前 → 不需要更新（边界值：差值 == days，不超过）。"""
        boundary_date = date.today() - timedelta(days=PRICE_STALE_DAYS)
        conn = _mock_conn(boundary_date)
        assert check_price_freshness(conn, "GOOGL") is False

    def test_null_date_returns_true(self):
        """数据表为空（MAX 返回 NULL）→ 需要更新。"""
        conn = _mock_conn(None)
        assert check_price_freshness(conn, "GOOGL") is True


# ── check_earnings_freshness ──────────────────────────────

class TestCheckEarningsFreshness:
    def test_fresh_earnings_returns_false(self):
        recent = date.today() - timedelta(days=10)
        conn = _mock_conn(recent)
        assert check_earnings_freshness(conn, "GOOGL") is False

    def test_stale_earnings_returns_true(self):
        stale = date.today() - timedelta(days=EARNINGS_STALE_DAYS + 1)
        conn = _mock_conn(stale)
        assert check_earnings_freshness(conn, "GOOGL") is True

    def test_null_earnings_returns_true(self):
        conn = _mock_conn(None)
        assert check_earnings_freshness(conn, "GOOGL") is True


# ── check_and_warn_freshness ──────────────────────────────

class TestCheckAndWarnFreshness:
    def test_fresh_data_no_warning(self, caplog):
        """数据新鲜时，不发出 WARNING。"""
        import logging
        conn = _mock_conn(date.today())
        with caplog.at_level(logging.WARNING):
            check_and_warn_freshness(conn, stale_days=10)
        assert len(caplog.records) == 0

    def test_stale_data_emits_warning(self, caplog):
        """数据过期时，应发出 WARNING 且包含天数信息。"""
        import logging
        stale = date.today() - timedelta(days=15)
        conn = _mock_conn(stale)
        with caplog.at_level(logging.WARNING):
            check_and_warn_freshness(conn, stale_days=10)
        assert any("days old" in r.message for r in caplog.records)

    def test_null_data_emits_warning(self, caplog):
        """数据表为空时，应发出 WARNING。"""
        import logging
        conn = _mock_conn(None)
        with caplog.at_level(logging.WARNING):
            check_and_warn_freshness(conn, stale_days=10)
        assert len(caplog.records) > 0

    def test_db_error_does_not_raise(self):
        """DB 异常时静默处理，不向上抛出。"""
        conn = MagicMock()
        conn.cursor.side_effect = Exception("DB connection lost")
        check_and_warn_freshness(conn)  # 不应抛出


# ── refresh_ticker 逻辑（干运行）────────────────────────────

class TestRefreshTickerDryRun:
    def test_dry_run_returns_zero_rows(self):
        """干运行模式不实际写入，返回 0 行。"""
        from worker.data_refresh_worker import refresh_ticker

        conn = _mock_conn(date.today() - timedelta(days=10))  # 数据过期

        with patch("worker.data_refresh_worker.fetch_price_history") as mock_p, \
             patch("worker.data_refresh_worker.fetch_earnings_history") as mock_e:
            result = refresh_ticker(conn, "GOOGL", force=False, dry_run=True)

        # dry_run 下不调用 fetch
        mock_p.assert_not_called()
        mock_e.assert_not_called()
        assert result["price_rows"] == 0
        assert result["earnings_rows"] == 0

    def test_fresh_data_skipped(self):
        """数据新鲜时，不触发任何更新。"""
        from worker.data_refresh_worker import refresh_ticker

        conn = _mock_conn(date.today())  # 数据新鲜

        with patch("worker.data_refresh_worker.fetch_price_history") as mock_p, \
             patch("worker.data_refresh_worker.fetch_earnings_history") as mock_e, \
             patch("worker.data_refresh_worker._log_result"):
            result = refresh_ticker(conn, "GOOGL", force=False, dry_run=False)

        mock_p.assert_not_called()
        mock_e.assert_not_called()


# ── MAG7 常量 ─────────────────────────────────────────────

class TestMag7Constants:
    def test_mag7_list_complete(self):
        from worker.data_refresh_worker import MAG7
        for t in ["GOOGL", "MSFT", "META", "AMZN", "AAPL", "NVDA", "TSLA"]:
            assert t in MAG7

    def test_stale_days_reasonable(self):
        assert 1 <= PRICE_STALE_DAYS <= 7
        assert 7 <= EARNINGS_STALE_DAYS <= 90
