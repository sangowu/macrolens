#!/usr/bin/env python3
"""
拉取 MAG7 股票历史价格和季度财报数据，写入 price_history / earnings_history。

用法:
    uv run ingestion/ingest_prices.py
    uv run ingestion/ingest_prices.py --tickers GOOGL MSFT META
    uv run ingestion/ingest_prices.py --start 2015-01-01 --no-pe
    uv run ingestion/ingest_prices.py --tickers GOOGL --earnings-only
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

import pandas as pd
import psycopg
import yfinance as yf
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from models.config import load_config

logger = logging.getLogger(__name__)

MAG7 = ["GOOGL", "MSFT", "META", "AMZN", "AAPL", "NVDA", "TSLA"]
DEFAULT_START = "2015-01-01"


# ── 数据拉取 ───────────────────────────────────────────────

def fetch_price_history(ticker: str, start: str, end: str) -> pd.DataFrame:
    """调用 yfinance 拉取日线 OHLCV，返回标准化 DataFrame。"""
    raw = yf.download(ticker, start=start, end=end, auto_adjust=False, progress=False)
    if raw.empty:
        return pd.DataFrame()

    # yfinance 多列时列名为 MultiIndex，单 ticker 时为普通 Index
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.droplevel(1)

    df = pd.DataFrame({
        "ticker":    ticker,
        "date":      raw.index.date,
        "open":      raw["Open"].round(4),
        "high":      raw["High"].round(4),
        "low":       raw["Low"].round(4),
        "close":     raw["Close"].round(4),
        "adj_close": raw["Adj Close"].round(4),
        "volume":    raw["Volume"].astype("Int64"),
    })
    return df.dropna(subset=["close"])


def _ann_date_to_quarter_end(ann_date: date) -> date | None:
    """将财报公告日期映射到对应的财季末日期。
    MAG7 均按标准日历季度报告：
      Jan-Mar 公告 → 上一年 Q4 (Dec 31)
      Apr-Jun 公告 → 当年 Q1  (Mar 31)
      Jul-Sep 公告 → 当年 Q2  (Jun 30)
      Oct-Dec 公告 → 当年 Q3  (Sep 30)
    """
    m, y = ann_date.month, ann_date.year
    if m <= 3:
        return date(y - 1, 12, 31)
    elif m <= 6:
        return date(y, 3, 31)
    elif m <= 9:
        return date(y, 6, 30)
    else:
        return date(y, 9, 30)


def fetch_earnings_history(ticker: str) -> pd.DataFrame:
    """
    调用 yfinance 获取季度财报历史，返回标准化 DataFrame。
    EPS 来源：tk.get_earnings_dates()，覆盖 2020 年至今。
    Revenue/Income 来源：tk.quarterly_income_stmt，覆盖最近 ~6 季度。
    """
    tk = yf.Ticker(ticker)

    # EPS（含 estimate）：yfinance 1.3+ 用 get_earnings_dates，覆盖 ~25 季度
    earnings_dates = tk.get_earnings_dates(limit=40)

    # 收入/利润：quarterly_income_stmt，覆盖最近 ~6 季度
    quarterly = tk.quarterly_income_stmt

    # 用 income_stmt 列（财季末日期）建立快速查找字典
    income_by_qend: dict[date, object] = {}
    if quarterly is not None and not quarterly.empty:
        for col in quarterly.columns:
            qend = col.date() if hasattr(col, "date") else col
            income_by_qend[qend] = col

    # 以 earnings_dates 为主迭代，补充 income_stmt 数据
    rows = []
    if earnings_dates is not None and not earnings_dates.empty:
        for ann_ts, eps_row in earnings_dates.iterrows():
            ann_d = ann_ts.date() if hasattr(ann_ts, "date") else ann_ts
            period_end = _ann_date_to_quarter_end(ann_d)
            if period_end is None:
                continue

            fy = period_end.year
            fq = period_end.month // 3  # 3→1, 6→2, 9→3, 12→4

            eps_actual = float(eps_row["Reported EPS"]) if pd.notna(eps_row.get("Reported EPS")) else None
            eps_estimate = float(eps_row["EPS Estimate"]) if pd.notna(eps_row.get("EPS Estimate")) else None
            surprise_pct_raw = eps_row.get("Surprise(%)")
            if pd.notna(surprise_pct_raw):
                eps_surprise_pct = float(surprise_pct_raw)
                eps_surprise = round(eps_actual - eps_estimate, 4) if (eps_actual is not None and eps_estimate is not None) else None
            elif eps_actual is not None and eps_estimate is not None:
                eps_surprise = round(eps_actual - eps_estimate, 4)
                eps_surprise_pct = round((eps_surprise / abs(eps_estimate)) * 100, 2) if eps_estimate != 0 else None
            else:
                eps_surprise = eps_surprise_pct = None

            # 补充 income_stmt 数据（仅最近 ~6 季度有）
            revenue = net_income = op_income = gross_profit = None
            gross_margin = op_margin = None
            income_col = income_by_qend.get(period_end)
            if income_col is not None:
                def _get(label: str):
                    if label in quarterly.index:
                        v = quarterly.loc[label, income_col]
                        return float(v) / 1000 if pd.notna(v) else None
                    return None
                revenue = _get("Total Revenue")
                net_income = _get("Net Income")
                op_income = _get("Operating Income")
                gross_profit = _get("Gross Profit")
                gross_margin = (gross_profit / revenue) if (gross_profit and revenue) else None
                op_margin = (op_income / revenue) if (op_income and revenue) else None

            rows.append({
                "ticker":           ticker,
                "period_end":       period_end.isoformat(),
                "fiscal_year":      fy,
                "fiscal_quarter":   fq,
                "period_type":      "quarterly",
                "revenue":          revenue,
                "net_income":       net_income,
                "operating_income": op_income,
                "eps_actual":       eps_actual,
                "eps_estimate":     eps_estimate,
                "eps_surprise":     eps_surprise,
                "eps_surprise_pct": eps_surprise_pct,
                "cloud_revenue":    None,
                "ads_revenue":      None,
                "gross_profit":     gross_profit,
                "gross_margin":     gross_margin,
                "operating_margin": op_margin,
            })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ── 估值衍生字段 ───────────────────────────────────────────

def compute_pe_ps_ratios(
    prices_df: pd.DataFrame,
    earnings_df: pd.DataFrame,
    ticker: str,
) -> pd.DataFrame:
    """
    用滚动 TTM EPS 和 Revenue 计算每日 P/E、P/S。
    财报发布前使用前一期 TTM，财报当日起使用新 TTM。
    无对应季度数据时为 NULL。
    """
    if earnings_df.empty or prices_df.empty:
        prices_df = prices_df.copy()
        prices_df["pe_ratio"] = None
        prices_df["ps_ratio"] = None
        return prices_df

    eq = earnings_df[earnings_df["period_type"] == "quarterly"].copy()
    eq["period_end"] = pd.to_datetime(eq["period_end"])
    eq = eq.sort_values("period_end")

    # TTM EPS：过去 4 个季度 eps_actual 之和
    eq["ttm_eps"] = eq["eps_actual"].rolling(4, min_periods=4).sum()
    # TTM Revenue per share：过去 4 季度 revenue 之和（后续除以股数，这里先存总量）
    eq["ttm_revenue"] = eq["revenue"].rolling(4, min_periods=4).sum()

    prices = prices_df.copy()
    prices["date"] = pd.to_datetime(prices["date"])
    prices = prices.sort_values("date")
    prices["pe_ratio"] = None
    prices["ps_ratio"] = None

    # 以季报发布日期为分割点，每段使用对应的 TTM 值
    for _, eq_row in eq.iterrows():
        period_date = eq_row["period_end"]
        ttm_eps = eq_row["ttm_eps"]
        ttm_rev = eq_row["ttm_revenue"]

        mask = prices["date"] >= period_date
        if pd.notna(ttm_eps) and ttm_eps != 0:
            prices.loc[mask, "pe_ratio"] = (prices.loc[mask, "close"] / ttm_eps).round(2)
        if pd.notna(ttm_rev) and ttm_rev != 0:
            # revenue 是千美元总量，此处仅存比值供相对比较，不做每股换算
            # 实际 P/S 需要股数，暂时跳过精确计算
            pass

    prices["pe_ratio"] = pd.to_numeric(prices["pe_ratio"], errors="coerce")
    # 过滤异常值（P/E 超出 0-1000 视为无效）
    prices.loc[~prices["pe_ratio"].between(0, 1000), "pe_ratio"] = None

    return prices


# ── 数据库写入 ─────────────────────────────────────────────

def ingest_prices(conn: psycopg.Connection, df: pd.DataFrame, ticker: str) -> int:
    """批量 upsert price_history，幂等安全。返回写入行数。"""
    if df.empty:
        return 0

    rows = [
        (
            row["ticker"], str(row["date"]),
            _float(row.get("open")), _float(row.get("high")),
            _float(row.get("low")), _float(row["close"]),
            _float(row.get("adj_close")), _int(row.get("volume")),
            _float(row.get("pe_ratio")), _float(row.get("ps_ratio")),
        )
        for _, row in df.iterrows()
    ]

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO price_history
                (ticker, date, open, high, low, close, adj_close, volume, pe_ratio, ps_ratio)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker, date)
            DO UPDATE SET
                close     = EXCLUDED.close,
                adj_close = EXCLUDED.adj_close,
                volume    = EXCLUDED.volume,
                pe_ratio  = EXCLUDED.pe_ratio,
                ps_ratio  = EXCLUDED.ps_ratio,
                ingested_at = now()
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def ingest_earnings(conn: psycopg.Connection, df: pd.DataFrame, ticker: str) -> int:
    """批量 upsert earnings_history，幂等安全。返回写入行数。"""
    if df.empty:
        return 0

    rows = [
        (
            row["ticker"], row["period_end"], int(row["fiscal_year"]),
            _int(row.get("fiscal_quarter")), row["period_type"],
            _float(row.get("revenue")), _float(row.get("net_income")),
            _float(row.get("operating_income")),
            _float(row.get("eps_actual")), _float(row.get("eps_estimate")),
            _float(row.get("eps_surprise")), _float(row.get("eps_surprise_pct")),
            _float(row.get("cloud_revenue")), _float(row.get("ads_revenue")),
            _float(row.get("gross_profit")), _float(row.get("gross_margin")),
            _float(row.get("operating_margin")),
        )
        for _, row in df.iterrows()
    ]

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO earnings_history (
                ticker, period_end, fiscal_year, fiscal_quarter, period_type,
                revenue, net_income, operating_income,
                eps_actual, eps_estimate, eps_surprise, eps_surprise_pct,
                cloud_revenue, ads_revenue,
                gross_profit, gross_margin, operating_margin
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s
            )
            ON CONFLICT (ticker, period_end, period_type)
            DO UPDATE SET
                revenue          = EXCLUDED.revenue,
                net_income       = EXCLUDED.net_income,
                eps_actual       = EXCLUDED.eps_actual,
                eps_estimate     = EXCLUDED.eps_estimate,
                eps_surprise     = EXCLUDED.eps_surprise,
                eps_surprise_pct = EXCLUDED.eps_surprise_pct,
                gross_margin     = EXCLUDED.gross_margin,
                operating_margin = EXCLUDED.operating_margin,
                ingested_at      = now()
            """,
            rows,
        )
    conn.commit()
    return len(rows)


# ── 辅助函数 ───────────────────────────────────────────────

def _float(v) -> float | None:
    try:
        f = float(v)
        return None if pd.isna(f) else round(f, 6)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        i = int(v)
        return None if pd.isna(float(v)) else i
    except (TypeError, ValueError):
        return None


# ── 主入口 ─────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest MAG7 price and earnings history")
    parser.add_argument("--tickers", nargs="+", default=MAG7, help="股票代码列表（默认 MAG7 全部）")
    parser.add_argument("--start", default=DEFAULT_START, help="历史数据起始日期（默认 2015-01-01）")
    parser.add_argument("--end", default=date.today().isoformat(), help="历史数据结束日期（默认今天）")
    parser.add_argument("--no-pe", action="store_true", help="跳过 P/E 衍生字段计算")
    parser.add_argument("--earnings-only", action="store_true", help="只入库财报数据，跳过价格数据")
    parser.add_argument("--price-only", action="store_true", help="只入库价格数据，跳过财报数据")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = load_config(args.config)
    print(f"连接数据库 {cfg.db.host}:{cfg.db.port}/{cfg.db.dbname} ...")

    with psycopg.connect(cfg.db.dsn) as conn:
        for ticker in tqdm(args.tickers, desc="ticker"):
            tqdm.write(f"\n── {ticker} ──────────────────────")

            earnings_df = pd.DataFrame()

            # ① 财报数据
            if not args.price_only:
                try:
                    earnings_df = fetch_earnings_history(ticker)
                    n = ingest_earnings(conn, earnings_df, ticker)
                    tqdm.write(f"  earnings_history: {n} 行")
                except Exception as e:
                    tqdm.write(f"  [ERR] earnings {ticker}: {e}")
                    logger.exception("earnings fetch failed for %s", ticker)

            # ② 价格数据
            if not args.earnings_only:
                try:
                    price_df = fetch_price_history(ticker, args.start, args.end)
                    if not args.no_pe and not earnings_df.empty:
                        price_df = compute_pe_ps_ratios(price_df, earnings_df, ticker)
                    n = ingest_prices(conn, price_df, ticker)
                    tqdm.write(f"  price_history:    {n} 行")
                except Exception as e:
                    tqdm.write(f"  [ERR] price {ticker}: {e}")
                    logger.exception("price fetch failed for %s", ticker)

    print("\n[OK] ingest_prices 完成")


if __name__ == "__main__":
    main()
