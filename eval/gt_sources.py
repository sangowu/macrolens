"""真值表访问层：从 PostgreSQL 拉可用作 ground truth 的行。

生成器不直接写 SQL，统一走这里，原因是**真值表本身需要被审计**。
每个 fetch 函数都负责：
  1. 过滤掉 NULL（yfinance 的 eps_estimate 覆盖不全）
  2. 强制财年白名单（见下方 CALENDAR_FY_TICKERS）
  3. 返回带溯源信息的 dict

── 财年陷阱（必读）────────────────────────────────────────
`ingestion/ingest_prices.py::_ann_date_to_quarter_end` 的注释声称
"MAG7 均按标准日历季度报告"，但这是错的：

    fy = period_end.year
    fq = period_end.month // 3        # 纯日历季度

对 GOOGL/META/AMZN/TSLA（12 月财年）恰好正确；
对 AAPL（9 月）/ MSFT（6 月）/ NVDA（1 月）**系统性错误**。

例：Apple FY2024 Q1 = 2023 年 10-12 月，2024 年 2 月公告
    → 这段代码算成 fy=2023, fq=4，与官方口径差整整一年。

因此自动出题只覆盖日历财年公司。对其余三家自动生成，
只会得到"100% 自动化、100% 可靠地错"的评测集。
修复入口在 ingestion 层（per-ticker 财年末月份映射），不在这里绕过。
"""
from __future__ import annotations

import psycopg

# 财年 == 日历年的公司。只有这些公司的 fiscal_year/fiscal_quarter 列可信。
CALENDAR_FY_TICKERS = ("GOOGL", "META", "AMZN", "TSLA")

# 已知财年错配、暂不自动出题的公司 → 财年结束月份（修 ingestion 时用）
MISALIGNED_FY_TICKERS = {"AAPL": 9, "MSFT": 6, "NVDA": 1}


def fetch_eps_quarters(
    conn: psycopg.Connection,
    tickers: tuple[str, ...] = CALENDAR_FY_TICKERS,
    require_estimate: bool = False,
) -> list[dict]:
    """拉取季度 EPS 行，按 (ticker, period_end) 升序。

    Args:
        require_estimate: True 时只返回 eps_estimate 和 eps_surprise_pct
            都非 NULL 的行（beat/miss 类题目需要）。
    """
    bad = set(tickers) & set(MISALIGNED_FY_TICKERS)
    if bad:
        raise ValueError(
            f"拒绝为财年错配的公司生成真值: {sorted(bad)}。"
            f"这些公司的 fiscal_year/fiscal_quarter 列是日历季度，不是真实财季。"
            f"先修 ingestion/ingest_prices.py::_ann_date_to_quarter_end。"
        )

    where = [
        "period_type = 'quarterly'",
        "ticker = ANY(%(tickers)s)",
        "eps_actual IS NOT NULL",
        "fiscal_quarter IS NOT NULL",
    ]
    if require_estimate:
        where += ["eps_estimate IS NOT NULL", "eps_surprise_pct IS NOT NULL"]

    sql = f"""
        SELECT ticker, period_end, fiscal_year, fiscal_quarter,
               eps_actual, eps_estimate, eps_surprise, eps_surprise_pct
          FROM earnings_history
         WHERE {' AND '.join(where)}
         ORDER BY ticker, period_end
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"tickers": list(tickers)})
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]

    # NUMERIC → float，避免 Decimal 混进 JSON
    for r in rows:
        for k in ("eps_actual", "eps_estimate", "eps_surprise", "eps_surprise_pct"):
            if r[k] is not None:
                r[k] = float(r[k])
    return rows


def audit_eps_coverage(conn: psycopg.Connection) -> list[dict]:
    """按 ticker 统计 EPS 数据覆盖度，用于出题前的可得性探测。"""
    sql = """
        SELECT ticker,
               count(*)                                            AS quarters,
               count(eps_actual)                                   AS has_actual,
               count(eps_estimate)                                 AS has_estimate,
               count(eps_surprise_pct)                             AS has_surprise,
               min(period_end)                                     AS earliest,
               max(period_end)                                     AS latest
          FROM earnings_history
         WHERE period_type = 'quarterly'
         GROUP BY ticker
         ORDER BY ticker
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
