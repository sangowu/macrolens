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

    return context
