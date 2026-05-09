"""
Executor: 根据子查询执行混合检索（向量 + 全文 RRF），返回结构化 context。
"""
from __future__ import annotations

from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from models.base import EmbeddingBackend
from models.config import LLMConfig

# ── RRF 常量 ──────────────────────────────────────────────
RRF_K = 60


# ── SEC chunks 混合检索 ────────────────────────────────────

SEC_RRF_SQL = """
WITH semantic AS (
    SELECT id, content, section, doc_type, period_end, fiscal_year,
           ROW_NUMBER() OVER (ORDER BY embedding <=> %(vec)s::vector) AS sem_rank
    FROM sec_chunks
    WHERE embedding IS NOT NULL
      {section_filter}
      {company_filter}
      {year_filter}
    ORDER BY embedding <=> %(vec)s::vector
    LIMIT %(candidate_k)s
),
lexical AS (
    SELECT id, content, section, doc_type, period_end, fiscal_year,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank(content_tsv, websearch_to_tsquery('english', %(query)s)) DESC
           ) AS lex_rank
    FROM sec_chunks
    WHERE content_tsv @@ websearch_to_tsquery('english', %(query)s)
      {section_filter}
      {company_filter}
      {year_filter}
    LIMIT %(candidate_k)s
),
rrf AS (
    SELECT
        COALESCE(s.id, l.id)               AS id,
        COALESCE(s.content, l.content)     AS content,
        COALESCE(s.section, l.section)     AS section,
        COALESCE(s.doc_type, l.doc_type)   AS doc_type,
        COALESCE(s.period_end, l.period_end) AS period_end,
        COALESCE(s.fiscal_year, l.fiscal_year) AS fiscal_year,
        (COALESCE(1.0/({rrf_k}+s.sem_rank), 0)
         + COALESCE(1.0/({rrf_k}+l.lex_rank), 0)) AS rrf_score
    FROM semantic s
    FULL OUTER JOIN lexical l ON s.id = l.id
)
SELECT id, content, section, doc_type, period_end, fiscal_year, rrf_score
FROM rrf
ORDER BY rrf_score DESC
LIMIT %(top_k)s
"""

# ── Events 混合检索 ────────────────────────────────────────

EVENTS_RRF_SQL = """
WITH semantic AS (
    SELECT event_id, date, category, entity, severity, title, description,
           ROW_NUMBER() OVER (ORDER BY embedding <=> %(vec)s::vector) AS sem_rank
    FROM events
    WHERE embedding IS NOT NULL
      {category_filter}
    ORDER BY embedding <=> %(vec)s::vector
    LIMIT %(candidate_k)s
),
lexical AS (
    SELECT event_id, date, category, entity, severity, title, description,
           ROW_NUMBER() OVER (
               ORDER BY ts_rank(description_tsv, websearch_to_tsquery('english', %(query)s)) DESC
           ) AS lex_rank
    FROM events
    WHERE description_tsv @@ websearch_to_tsquery('english', %(query)s)
      {category_filter}
    LIMIT %(candidate_k)s
),
rrf AS (
    SELECT
        COALESCE(s.event_id, l.event_id)       AS event_id,
        COALESCE(s.date, l.date)               AS date,
        COALESCE(s.category, l.category)       AS category,
        COALESCE(s.entity, l.entity)           AS entity,
        COALESCE(s.severity, l.severity)       AS severity,
        COALESCE(s.title, l.title)             AS title,
        COALESCE(s.description, l.description) AS description,
        (COALESCE(1.0/({rrf_k}+s.sem_rank), 0)
         + COALESCE(1.0/({rrf_k}+l.lex_rank), 0)) AS rrf_score
    FROM semantic s
    FULL OUTER JOIN lexical l ON s.event_id = l.event_id
)
SELECT event_id, date, category, entity, severity, title, description, rrf_score
FROM rrf
ORDER BY rrf_score DESC
LIMIT %(top_k)s
"""

# ── Macro series 关键词推断 ───────────────────────────────

_SERIES_KEYWORDS: list[tuple[list[str], str]] = [
    (["fedfunds", "federal funds", "fed funds", "fed rate", "interest rate", "funds rate"], "FEDFUNDS"),
    (["unrate", "unemployment"], "UNRATE"),
    (["cpiaucsl", "cpi", "inflation", "consumer price"], "CPIAUCSL"),
    (["gdpc1", "real gdp"], "GDPC1"),
    (["gdp", "gross domestic"], "GDP"),
    (["payems", "nonfarm", "payroll", "employment"], "PAYEMS"),
    (["t10y2y", "yield curve", "10 year", "2 year spread"], "T10Y2Y"),
    (["houst", "housing start"], "HOUST"),
    (["indpro", "industrial production"], "INDPRO"),
    (["rsafs", "retail sales", "retail spending"], "RSAFS"),
    (["umcsent", "consumer sentiment", "michigan"], "UMCSENT"),
    (["dcoilwtico", "oil price", "crude oil", "wti"], "DCOILWTICO"),
]


def _infer_series(query: str) -> list[str]:
    """Planner 未填写 series 时，从 query 文本推断 macro series ID。"""
    q = query.lower()
    found = []
    for keywords, series_id in _SERIES_KEYWORDS:
        if any(kw in q for kw in keywords):
            found.append(series_id)
    return found


# ── Macro indicators 精确查询 ──────────────────────────────

MACRO_SQL = """
SELECT m.series_id, m.date, m.value, s.name, s.unit
FROM macro_indicators m
JOIN macro_series_meta s ON m.series_id = s.series_id
WHERE m.series_id = ANY(%(series)s)
  AND m.date >= %(date_from)s
  AND m.date <= %(date_to)s
ORDER BY m.series_id, m.date
"""


_ALLOWED_COMPANIES = frozenset({"GOOGL", "MSFT", "META", "AMZN", "AAPL", "NVDA", "TSLA"})


def _build_company_filter(filters: dict) -> str:
    """从 filters['company'] 构建 SQL WHERE 片段，使用白名单防止注入。"""
    raw = filters.get("company", [])
    if isinstance(raw, str):
        raw = [raw]
    companies = [c for c in raw if c in _ALLOWED_COMPANIES]
    if not companies:
        return ""
    quoted = ", ".join(f"'{c}'" for c in companies)
    return f"AND company IN ({quoted})"


def _search_sec(
    conn: psycopg.Connection,
    embedder: EmbeddingBackend,
    query: str,
    filters: dict,
    candidate_k: int,
    top_k: int,
) -> list[dict]:
    vec = embedder.encode([query])[0]
    year_filter = ""
    if "fiscal_year" in filters:
        year_filter = f"AND fiscal_year = {int(filters['fiscal_year'])}"

    company_filter = _build_company_filter(filters)

    sql = SEC_RRF_SQL.format(
        section_filter="",
        company_filter=company_filter,
        year_filter=year_filter,
        rrf_k=RRF_K,
    )
    rows = conn.execute(sql, {"vec": vec, "query": query, "candidate_k": candidate_k, "top_k": top_k}).fetchall()

    return [
        {
            "source": "sec_chunks",
            "id": r[0],
            "content": r[1],
            "section": r[2],
            "doc_type": r[3],
            "period_end": str(r[4]) if r[4] else None,
            "fiscal_year": r[5],
            "rrf_score": float(r[6]),
        }
        for r in rows
    ]


def _search_events(
    conn: psycopg.Connection,
    embedder: EmbeddingBackend,
    query: str,
    filters: dict,
    candidate_k: int,
    top_k: int,
) -> list[dict]:
    vec = embedder.encode([query])[0]
    category_filter = ""
    if "category" in filters:
        category_filter = f"AND category = '{filters['category']}'"

    sql = EVENTS_RRF_SQL.format(category_filter=category_filter, rrf_k=RRF_K)
    rows = conn.execute(sql, {"vec": vec, "query": query, "candidate_k": candidate_k, "top_k": top_k}).fetchall()

    return [
        {
            "source": "events",
            "event_id": r[0],
            "date": str(r[1]),
            "category": r[2],
            "entity": r[3],
            "severity": r[4],
            "title": r[5],
            "description": r[6],
            "rrf_score": float(r[7]),
        }
        for r in rows
    ]


def _search_macro(
    conn: psycopg.Connection,
    filters: dict,
) -> list[dict]:
    series = filters.get("series", [])
    if isinstance(series, str):
        series = [series]

    # Planner 未填写 series 时，从 query 文本推断
    if not series:
        series = _infer_series(filters.get("_query", ""))

    date_from = filters.get("date_from", "2019-01-01")
    date_to = filters.get("date_to", "2026-12-31")

    if not series:
        return []

    rows = conn.execute(MACRO_SQL, {"series": series, "date_from": date_from, "date_to": date_to}).fetchall()
    return [
        {
            "source": "macro_indicators",
            "series_id": r[0],
            "date": str(r[1]),
            "value": float(r[2]) if r[2] is not None else None,
            "title": r[3],
            "units": r[4],
        }
        for r in rows
    ]


PRICE_HISTORY_SQL = """
SELECT ticker, date, close, adj_close, volume, pe_ratio, ps_ratio
FROM price_history
WHERE ticker = ANY(%(tickers)s)
  AND date >= %(date_from)s
  AND date <= %(date_to)s
ORDER BY ticker, date
"""

EARNINGS_HISTORY_SQL = """
SELECT ticker, period_end, fiscal_year, fiscal_quarter, period_type,
       revenue, net_income, eps_actual, eps_estimate,
       eps_surprise, eps_surprise_pct,
       cloud_revenue, ads_revenue,
       gross_margin, operating_margin
FROM earnings_history
WHERE ticker = ANY(%(tickers)s)
  AND period_type = %(period_type)s
  AND fiscal_year >= %(year_from)s
  AND fiscal_year <= %(year_to)s
ORDER BY ticker, period_end
"""


def _search_price_history(
    conn: psycopg.Connection,
    filters: dict,
) -> list[dict]:
    """精确范围查询 price_history，返回日线价格和估值数据。"""
    raw_tickers = filters.get("tickers", ["GOOGL"])
    if isinstance(raw_tickers, str):
        raw_tickers = [raw_tickers]
    tickers = [t for t in raw_tickers if t in _ALLOWED_COMPANIES] or ["GOOGL"]

    date_from = filters.get("date_from", "2019-01-01")
    date_to   = filters.get("date_to", "2026-12-31")

    rows = conn.execute(
        PRICE_HISTORY_SQL,
        {"tickers": tickers, "date_from": date_from, "date_to": date_to},
    ).fetchall()

    return [
        {
            "source":    "price_history",
            "ticker":    r[0],
            "date":      str(r[1]),
            "close":     float(r[2]) if r[2] is not None else None,
            "adj_close": float(r[3]) if r[3] is not None else None,
            "volume":    r[4],
            "pe_ratio":  float(r[5]) if r[5] is not None else None,
            "ps_ratio":  float(r[6]) if r[6] is not None else None,
        }
        for r in rows
    ]


def _search_earnings_history(
    conn: psycopg.Connection,
    filters: dict,
) -> list[dict]:
    """精确范围查询 earnings_history，返回季度/年度财报数据。"""
    raw_tickers = filters.get("tickers", ["GOOGL"])
    if isinstance(raw_tickers, str):
        raw_tickers = [raw_tickers]
    tickers = [t for t in raw_tickers if t in _ALLOWED_COMPANIES] or ["GOOGL"]

    period_type = filters.get("period_type", "quarterly")
    year_from   = int(filters.get("year_from", 2018))
    year_to     = int(filters.get("year_to", 2030))

    rows = conn.execute(
        EARNINGS_HISTORY_SQL,
        {"tickers": tickers, "period_type": period_type,
         "year_from": year_from, "year_to": year_to},
    ).fetchall()

    return [
        {
            "source":           "earnings_history",
            "ticker":           r[0],
            "period_end":       str(r[1]),
            "fiscal_year":      r[2],
            "fiscal_quarter":   r[3],
            "period_type":      r[4],
            "revenue":          float(r[5]) if r[5] is not None else None,
            "net_income":       float(r[6]) if r[6] is not None else None,
            "eps_actual":       float(r[7]) if r[7] is not None else None,
            "eps_estimate":     float(r[8]) if r[8] is not None else None,
            "eps_surprise":     float(r[9]) if r[9] is not None else None,
            "eps_surprise_pct": float(r[10]) if r[10] is not None else None,
            "cloud_revenue":    float(r[11]) if r[11] is not None else None,
            "ads_revenue":      float(r[12]) if r[12] is not None else None,
            "gross_margin":     float(r[13]) if r[13] is not None else None,
            "operating_margin": float(r[14]) if r[14] is not None else None,
        }
        for r in rows
    ]


def execute(
    sub_queries: list[dict],
    conn: psycopg.Connection,
    embedder: EmbeddingBackend,
    cfg: LLMConfig,
) -> list[dict[str, Any]]:
    """执行所有子查询，返回合并后的 context 列表。"""
    register_vector(conn)
    context: list[dict] = []

    for sq in sub_queries:
        query = sq["query"]
        sources = sq.get("sources", ["sec_chunks"])
        filters = {**sq.get("filters", {}), "_query": query}

        for source in sources:
            if source == "sec_chunks":
                context.extend(_search_sec(conn, embedder, query, filters, cfg.candidate_k, cfg.top_k))
            elif source == "events":
                context.extend(_search_events(conn, embedder, query, filters, cfg.candidate_k, cfg.top_k))
            elif source == "macro_indicators":
                context.extend(_search_macro(conn, filters))
            elif source == "price_history":
                context.extend(_search_price_history(conn, filters))
            elif source == "earnings_history":
                context.extend(_search_earnings_history(conn, filters))

    return context
