#!/usr/bin/env python3
"""MacroLens MCP Server (stdio transport).

把 MacroLens 的金融研究能力暴露为 MCP tools，供 Claude Desktop 等 MCP 客户端调用：

- ``macrolens_ask``        — 完整 PER Loop 检索增强问答（带 [n] 引用）
- ``macrolens_search_sec`` — MAG7 SEC filings 混合检索（语义 + 全文 + reranker）
- ``macrolens_get_macro``  — 美国宏观指标时间序列（FRED）

复用现有检索管道（``agent.per_loop`` / ``agent.executor``），不重复实现逻辑。

依赖本地服务：PostgreSQL(:5433) + llama.cpp embedding(:8081)。启动前请确保二者运行；
未就绪时各 tool 返回可操作的错误提示而非崩溃。

运行（供 MCP 客户端作为子进程拉起）：
    uv run mcp_server/server.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env", encoding="utf-8")

import psycopg
from mcp.server.fastmcp import FastMCP
from pgvector.psycopg import register_vector
from pydantic import Field

from agent.executor import _ALLOWED_COMPANIES, execute
from agent.per_loop import run as per_loop_run
from models.config import load_config
from models.factory import create_embedding, create_llm_client, create_reranker

mcp = FastMCP("macrolens_mcp")

_COMPANIES = sorted(_ALLOWED_COMPANIES)

# ── 惰性单例：cfg / embedder / llm / reranker 只初始化一次，跨 tool 复用 ──
_state: dict = {}


def _ensure_init() -> dict:
    """首次调用时初始化无状态依赖（embedder/llm/reranker）；DB 连接每次 tool 单独建。"""
    if "cfg" not in _state:
        cfg = load_config(str(_ROOT / "config.yaml"))
        _state["cfg"] = cfg
        _state["embedder"] = create_embedding(cfg)
        _state["llm"] = create_llm_client(cfg)
        try:
            _state["reranker"] = create_reranker(cfg)
        except Exception:
            _state["reranker"] = None  # reranker 不可用时 executor 自动 fallback 到 RRF
    return _state


def _open_conn(st: dict) -> psycopg.Connection:
    """打开 DB 连接并注册 pgvector。connect_timeout 保证 DB 未起时快速失败而非无限挂起。"""
    conn = psycopg.connect(st["cfg"].db.dsn, connect_timeout=5)
    register_vector(conn)
    return conn


def _handle_error(e: Exception) -> str:
    """把异常转成对 agent 可操作的错误提示。"""
    if isinstance(e, psycopg.OperationalError):
        return (
            "Error: 无法连接 PostgreSQL(:5433)。请先启动数据库容器，例如：\n"
            "  docker start macrolens-pg"
        )
    msg = str(e)
    if "8081" in msg or "Connection" in type(e).__name__ or "connect" in msg.lower():
        return (
            "Error: 无法连接 embedding server(:8081)。请先启动 llama.cpp：\n"
            "  llama-server -m Qwen3-Embedding-0.6B-f16.gguf --embedding --pooling last --port 8081"
        )
    return f"Error: {type(e).__name__}: {msg[:200]}"


def _format_context(context: list[dict], limit: int) -> list[dict]:
    """把内部 context dict 压缩成对客户端友好的精简结构。"""
    out: list[dict] = []
    for c in context[:limit]:
        src = c.get("source")
        if src == "sec_chunks":
            out.append(
                {
                    "type": "sec",
                    "company": c.get("company"),
                    "doc_type": c.get("doc_type"),
                    "fiscal_year": c.get("fiscal_year"),
                    "section": c.get("section"),
                    "excerpt": (c.get("content") or "")[:300],
                }
            )
        elif src == "macro_indicators":
            out.append(
                {
                    "type": "macro",
                    "series_id": c.get("series_id"),
                    "date": c.get("date"),
                    "value": c.get("value"),
                    "title": c.get("title"),
                    "units": c.get("units"),
                }
            )
        elif src == "events":
            out.append({"type": "event", "date": c.get("date"), "title": c.get("title")})
        else:
            keep = {k: c.get(k) for k in ("ticker", "date", "value", "period_end") if k in c}
            out.append({"type": src, **keep})
    return out


# ══════════════════════════════════════════════════════════
# Tool 1: 完整问答（PER Loop）
# ══════════════════════════════════════════════════════════

@mcp.tool(
    name="macrolens_ask",
    annotations={
        "title": "Ask MacroLens (full RAG research)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def macrolens_ask(
    question: Annotated[
        str,
        Field(
            description="关于 MAG7 公司 SEC 文件或美国宏观经济的自然语言问题"
            "（如 'How did 2022 Fed rate hikes affect Google advertising revenue?'）",
            min_length=3,
            max_length=500,
        ),
    ],
    max_iter: Annotated[
        int, Field(description="PER Loop 最大检索迭代轮数（越大越彻底、越慢）", ge=1, le=5)
    ] = 3,
) -> str:
    """对 MAG7 公司 SEC 文件与美国宏观数据做完整的检索增强问答。

    走完整 PER Loop（Plan → Execute → Critique → Synthesize），返回带 [n] 引用的答案及来源。
    域外问题（天气、通用编程等）会被直接拒答。适合"需要综合多来源、给出有依据结论"的问题。

    Returns:
        str: JSON，schema：
        {
          "answer": str,     # 带 [n] 引用的答案（域外则为拒答说明）
          "sources": [ {"type": "sec"|"macro"|"event"|..., ...} ]
        }
        出错时返回以 "Error:" 开头的可操作提示。

    Examples:
        - "What are Google's main AI risk factors in 2023?" → SEC 检索
        - "Correlation between 2022 Fed funds rate and GOOGL stock?" → 宏观 + price
        - 单纯抓某指标原始时间序列请改用 macrolens_get_macro
    """
    try:
        return await asyncio.to_thread(_ask_sync, question, max_iter)
    except Exception as e:
        return _handle_error(e)


def _ask_sync(question: str, max_iter: int) -> str:
    st = _ensure_init()
    with _open_conn(st) as conn:
        answer, context = per_loop_run(
            question, st["cfg"], conn, st["embedder"], st["llm"],
            max_iter=max_iter, reranker=st["reranker"],
        )
    return json.dumps(
        {"answer": answer, "sources": _format_context(context, limit=12)},
        ensure_ascii=False,
        indent=2,
    )


# ══════════════════════════════════════════════════════════
# Tool 2: SEC filings 混合检索
# ══════════════════════════════════════════════════════════

@mcp.tool(
    name="macrolens_search_sec",
    annotations={
        "title": "Search MAG7 SEC filings",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def macrolens_search_sec(
    query: Annotated[
        str,
        Field(
            description="检索关键词或自然语言（如 'advertising revenue', 'AI risk factors'）",
            min_length=2,
            max_length=300,
        ),
    ],
    company: Annotated[
        str | None,
        Field(description=f"限定公司 ticker，可选值：{_COMPANIES}；留空则跨全部 MAG7"),
    ] = None,
    fiscal_year: Annotated[
        int | None, Field(description="限定财年，如 2023；留空则跨全部年份", ge=2018, le=2025)
    ] = None,
    top_k: Annotated[int, Field(description="返回最相关 chunk 数", ge=1, le=20)] = 8,
) -> str:
    """在 MAG7 公司 SEC filings（10-K/10-Q/8-K）中做混合检索并返回原文片段。

    检索管道：pgvector 语义 + tsvector 全文 → RRF 融合 → BGE reranker 精排。
    返回原始 chunk 供你自行阅读/引用，不做 LLM 合成。适合"想拿到证据原文"的场景。

    Returns:
        str: JSON，schema：
        {
          "count": int,
          "chunks": [ {"type": "sec", "company": str, "doc_type": str,
                       "fiscal_year": int, "section": str, "excerpt": str} ]
        }
        无结果时返回 "No SEC chunks found ..."；出错时返回 "Error: ..."。
    """
    try:
        return await asyncio.to_thread(_search_sec_sync, query, company, fiscal_year, top_k)
    except Exception as e:
        return _handle_error(e)


def _search_sec_sync(query: str, company: str | None, fiscal_year: int | None, top_k: int) -> str:
    st = _ensure_init()
    filters: dict = {}
    if company and company.upper() in _ALLOWED_COMPANIES:
        filters["company"] = [company.upper()]
    if fiscal_year:
        filters["fiscal_year"] = fiscal_year
    sub = [{"query": query, "sources": ["sec_chunks"], "filters": filters}]
    with _open_conn(st) as conn:
        ctx = execute(sub, conn, st["embedder"], st["cfg"].llm, reranker=st["reranker"])
    ctx = ctx[:top_k]
    if not ctx:
        return f"No SEC chunks found for query '{query}'."
    return json.dumps(
        {"count": len(ctx), "chunks": _format_context(ctx, limit=top_k)},
        ensure_ascii=False,
        indent=2,
    )


# ══════════════════════════════════════════════════════════
# Tool 3: 宏观指标时间序列
# ══════════════════════════════════════════════════════════

@mcp.tool(
    name="macrolens_get_macro",
    annotations={
        "title": "Get US macro indicator series",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def macrolens_get_macro(
    series_id: Annotated[
        str | None,
        Field(description="FRED series id，如 FEDFUNDS / CPIAUCSL / UNRATE / GDP；留空则从 query 推断"),
    ] = None,
    query: Annotated[
        str | None,
        Field(description="自然语言（如 'federal funds rate'），未指定 series_id 时用于推断指标"),
    ] = None,
    date_from: Annotated[
        str | None, Field(description="起始日期 YYYY-MM-DD，默认 2019-01-01")
    ] = None,
    date_to: Annotated[
        str | None, Field(description="结束日期 YYYY-MM-DD，默认 2026-12-31")
    ] = None,
    limit: Annotated[int, Field(description="返回数据点上限（时间序列可能很长）", ge=1, le=500)] = 60,
) -> str:
    """获取美国宏观指标（FRED）的时间序列数据点。

    直接查库返回原始数值，不做 LLM 合成。适合"需要某指标一段时间的数值"的场景。
    series_id 与 query 至少提供一个。

    Returns:
        str: JSON，schema：
        {
          "count": int,
          "series": [ {"type": "macro", "series_id": str, "date": str,
                       "value": float, "title": str, "units": str} ]
        }
        无结果时返回提示；出错时返回 "Error: ..."。
    """
    if not series_id and not query:
        return "Error: series_id 与 query 至少提供一个。"
    try:
        return await asyncio.to_thread(
            _get_macro_sync, series_id, query, date_from, date_to, limit
        )
    except Exception as e:
        return _handle_error(e)


def _get_macro_sync(
    series_id: str | None, query: str | None,
    date_from: str | None, date_to: str | None, limit: int,
) -> str:
    st = _ensure_init()
    filters: dict = {}
    if series_id:
        filters["series"] = [series_id.upper()]
    if date_from:
        filters["date_from"] = date_from
    if date_to:
        filters["date_to"] = date_to
    sub = [{"query": query or series_id or "", "sources": ["macro_indicators"], "filters": filters}]
    with _open_conn(st) as conn:
        ctx = execute(sub, conn, st["embedder"], st["cfg"].llm, reranker=st["reranker"])
    if not ctx:
        return (
            "No macro data found. 请提供有效的 series_id（如 FEDFUNDS / CPIAUCSL / UNRATE / GDP）"
            "或更明确的 query。"
        )
    return json.dumps(
        {"count": len(ctx[:limit]), "series": _format_context(ctx, limit=limit)},
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()
