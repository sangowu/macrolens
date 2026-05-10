#!/usr/bin/env python3
"""
数据刷新 Worker：增量更新 price_history / earnings_history。

触发方式：
  - 手动：uv run worker/data_refresh_worker.py
  - 定时：Windows Task Scheduler 每周日 02:00
  - 启动检查（只读）：check_and_warn_freshness(conn)，被 ui/app.py 和 task_worker.py 调用

刷新逻辑：
  - price_history:    最新日期 < today - 3 天 → 触发增量更新
  - earnings_history: 最新记录 < today - 30 天 → 触发更新

用法:
    uv run worker/data_refresh_worker.py                    # 刷新全部 MAG7
    uv run worker/data_refresh_worker.py --tickers GOOGL    # 只刷新 GOOGL
    uv run worker/data_refresh_worker.py --dry-run          # 只检查，不更新
    uv run worker/data_refresh_worker.py --force            # 强制刷新（忽略新鲜度）
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

import psycopg

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingestion.ingest_prices import (
    MAG7,
    fetch_earnings_history,
    fetch_price_history,
    ingest_earnings,
    ingest_prices,
    compute_pe_ps_ratios,
)
from models.config import load_config

logger = logging.getLogger(__name__)

PRICE_STALE_DAYS    = 3    # price_history 超过 3 天未更新视为过期
EARNINGS_STALE_DAYS = 30   # earnings_history 超过 30 天未更新视为过期


# ── 新鲜度检查 ─────────────────────────────────────────────

def check_price_freshness(conn: psycopg.Connection, ticker: str) -> bool:
    """返回 True 表示需要更新（数据过期或不存在）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(date) FROM price_history WHERE ticker = %s", (ticker,))
        latest = cur.fetchone()[0]
    if latest is None:
        return True
    return (date.today() - latest).days > PRICE_STALE_DAYS


def check_earnings_freshness(conn: psycopg.Connection, ticker: str) -> bool:
    """返回 True 表示需要更新（数据过期或不存在）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(period_end) FROM earnings_history WHERE ticker = %s AND period_type = 'quarterly'",
            (ticker,),
        )
        latest = cur.fetchone()[0]
    if latest is None:
        return True
    return (date.today() - latest).days > EARNINGS_STALE_DAYS


def check_and_warn_freshness(conn: psycopg.Connection, stale_days: int = 10) -> None:
    """
    只读检查 price_history 最新日期（单条 SELECT MAX，< 5ms）。
    超过 stale_days 天则打印 WARNING，不触发更新，不阻塞调用方。
    供 ui/app.py 和 task_worker.py 启动时调用。
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(date) FROM price_history WHERE ticker='GOOGL'")
            latest = cur.fetchone()[0]
        if latest is None:
            logger.warning(
                "price_history is empty. Run: uv run worker/data_refresh_worker.py"
            )
        elif (date.today() - latest).days > stale_days:
            logger.warning(
                "price_history data is %d days old (last: %s). "
                "Run: uv run worker/data_refresh_worker.py",
                (date.today() - latest).days,
                latest,
            )
    except Exception as e:
        logger.debug("Freshness check skipped: %s", e)


# ── 刷新逻辑 ──────────────────────────────────────────────

def _log_result(
    conn: psycopg.Connection,
    data_type: str,
    ticker: str,
    rows_added: int,
    rows_updated: int,
    status: str,
    error_msg: str | None = None,
) -> None:
    """写入 data_refresh_log（失败时静默，不影响主流程）。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_refresh_log
                    (data_type, ticker, rows_added, rows_updated, status, error_msg)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (data_type, ticker, rows_added, rows_updated, status, error_msg),
            )
        conn.commit()
    except Exception as e:
        logger.debug("Failed to write refresh log: %s", e)


def refresh_ticker(
    conn: psycopg.Connection,
    ticker: str,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """
    对单个 ticker 做增量刷新。
    返回 {'price_rows': n, 'earnings_rows': n}。
    """
    today = date.today().isoformat()
    result = {"price_rows": 0, "earnings_rows": 0}

    # ① 财报数据
    needs_earnings = force or check_earnings_freshness(conn, ticker)
    if needs_earnings:
        if dry_run:
            logger.info("[DRY-RUN] %s earnings_history would be updated", ticker)
        else:
            try:
                earnings_df = fetch_earnings_history(ticker)
                n = ingest_earnings(conn, earnings_df, ticker)
                result["earnings_rows"] = n
                _log_result(conn, "earnings_history", ticker, n, 0, "success")
                logger.info("[OK] %s earnings_history: %d rows", ticker, n)
            except Exception as e:
                _log_result(conn, "earnings_history", ticker, 0, 0, "failed", str(e))
                logger.error("[ERR] %s earnings: %s", ticker, e)
    else:
        logger.info("[SKIP] %s earnings_history is fresh", ticker)
        _log_result(conn, "earnings_history", ticker, 0, 0, "skipped")

    # ② 价格数据（增量：从最新日期 +1 天开始拉）
    needs_price = force or check_price_freshness(conn, ticker)
    if needs_price:
        if dry_run:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(date) FROM price_history WHERE ticker = %s", (ticker,))
                latest = cur.fetchone()[0]
            logger.info("[DRY-RUN] %s price_history would fetch from %s to %s",
                        ticker, latest, today)
        else:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT MAX(date) FROM price_history WHERE ticker = %s", (ticker,))
                    latest = cur.fetchone()[0]

                start = (latest + timedelta(days=1)).isoformat() if latest else "2015-01-01"
                price_df = fetch_price_history(ticker, start, today)

                # 重新计算 P/E（用刚刚更新的 earnings 数据）
                from ingestion.ingest_prices import fetch_earnings_history as _fetch_e
                earnings_df = _fetch_e(ticker)
                if not earnings_df.empty and not price_df.empty:
                    price_df = compute_pe_ps_ratios(price_df, earnings_df, ticker)

                n = ingest_prices(conn, price_df, ticker)
                result["price_rows"] = n
                _log_result(conn, "price_history", ticker, n, 0, "success")
                logger.info("[OK] %s price_history: +%d rows (from %s)", ticker, n, start)
            except Exception as e:
                _log_result(conn, "price_history", ticker, 0, 0, "failed", str(e))
                logger.error("[ERR] %s price: %s", ticker, e)
    else:
        logger.info("[SKIP] %s price_history is fresh", ticker)
        _log_result(conn, "price_history", ticker, 0, 0, "skipped")

    return result


def refresh_all(
    cfg,
    tickers: list[str] = MAG7,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, dict[str, int]]:
    """刷新所有 tickers，返回 {ticker: {price_rows, earnings_rows}}。"""
    summary: dict[str, dict[str, int]] = {}

    with psycopg.connect(cfg.db.dsn) as conn:
        for ticker in tickers:
            logger.info("── %s ──────────────────────────", ticker)
            summary[ticker] = refresh_ticker(conn, ticker, force=force, dry_run=dry_run)

    return summary


# ── 主入口 ────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="MacroLens data refresh worker")
    parser.add_argument("--tickers", nargs="+", default=MAG7,
                        help="要刷新的 ticker（默认 MAG7 全部）")
    parser.add_argument("--dry-run",  action="store_true",
                        help="只检查新鲜度，不实际更新")
    parser.add_argument("--force",    action="store_true",
                        help="强制刷新，忽略新鲜度检查")
    parser.add_argument("--config",   default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)

    mode = "DRY-RUN" if args.dry_run else ("FORCE" if args.force else "NORMAL")
    logger.info("MacroLens Data Refresh Worker — mode=%s tickers=%s", mode, args.tickers)

    summary = refresh_all(cfg, tickers=args.tickers, force=args.force, dry_run=args.dry_run)

    print("\n── Summary ───────────────────────────────")
    for ticker, counts in summary.items():
        print(f"  {ticker:6s}  price={counts['price_rows']:4d} rows  "
              f"earnings={counts['earnings_rows']:3d} rows")
    print("\n[OK] data_refresh_worker completed")


if __name__ == "__main__":
    main()
