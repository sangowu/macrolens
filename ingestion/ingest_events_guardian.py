#!/usr/bin/env python3
"""
从 The Guardian API 拉取宏观/行业/公司事件，写入 events 表。

流程:
  1. 按关键词组分批搜索 Guardian 文章
  2. 关键词规则自动分类 category / severity
  3. 去重（按 URL）
  4. embed + upsert 进 events 表

用法:
    uv run ingestion/ingest_events_guardian.py
    uv run ingestion/ingest_events_guardian.py --no-embed --dry-run

注册免费 API key: https://open-platform.theguardian.com/access/
限额: 5000 次/天，每次最多 200 条结果
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

import httpx
import psycopg
from pgvector.psycopg import register_vector
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from models.config import load_config
from models.factory import create_embedding

# ── 搜索配置 ──────────────────────────────────────────────
GUARDIAN_BASE = "https://content.guardianapis.com/search"

# 每组：(搜索词, 默认 category)
SEARCH_QUERIES = [
    ("Federal Reserve interest rate cut hike",       "fed_policy"),
    ("Federal Reserve quantitative easing tapering", "fed_policy"),
    ("FOMC monetary policy inflation",               "fed_policy"),
    ("Google Alphabet layoffs earnings revenue",     "company_action"),
    ("Google Cloud AI Gemini Bard",                  "company_action"),
    ("Google antitrust DOJ monopoly",                "industry"),
    ("ChatGPT OpenAI artificial intelligence",       "industry"),
    ("Microsoft Bing AI Copilot",                    "industry"),
    ("US recession GDP unemployment",                "macro_shock"),
    ("inflation CPI Federal Reserve pandemic",       "macro_shock"),
]

DATE_FROM = "2019-01-01"
MAX_PER_QUERY = 50   # 每组最多拉取条数，控制 API 用量

# ── 分类规则 ──────────────────────────────────────────────
CATEGORY_RULES: list[tuple[list[str], str]] = [
    (["federal reserve", "fed ", "fomc", "interest rate", "rate cut", "rate hike",
      "quantitative easing", "tapering", "monetary policy"], "fed_policy"),
    (["google", "alphabet", "sundar pichai", "youtube", "google cloud",
      "google search", "waymo", "deepmind"], "company_action"),
    (["openai", "chatgpt", "microsoft", "bing", "anthropic", "meta ai",
      "artificial intelligence", "large language model", "llm", "gemini",
      "bard", "gpt-4"], "industry"),
    (["recession", "gdp", "unemployment", "inflation", "cpi",
      "pandemic", "covid", "supply chain", "fiscal stimulus"], "macro_shock"),
]

SEVERITY_RULES: list[tuple[list[str], int]] = [
    (["emergency", "crash", "pandemic", "crisis", "collapse",
      "historic", "largest ever", "first time since"], 5),
    (["layoffs", "antitrust", "monopoly", "rate hike", "recession",
      "significant", "major", "landmark"], 4),
]


def classify(text: str) -> tuple[str, int]:
    lower = text.lower()
    category = "industry"
    for keywords, cat in CATEGORY_RULES:
        if any(kw in lower for kw in keywords):
            category = cat
            break
    severity = 3
    for keywords, sev in SEVERITY_RULES:
        if any(kw in lower for kw in keywords):
            severity = sev
            break
    return category, severity


def make_event_id(url: str) -> str:
    return "grd_" + hashlib.md5(url.encode()).hexdigest()[:10]


def fetch_query(
    client: httpx.Client,
    api_key: str,
    query: str,
    date_from: str,
    page_size: int = 50,
) -> list[dict]:
    params = {
        "q": query,
        "from-date": date_from,
        "order-by": "relevance",
        "show-fields": "trailText",
        "page-size": min(page_size, 200),
        "api-key": api_key,
    }
    try:
        resp = client.get(GUARDIAN_BASE, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("response", {}).get("results", [])
    except Exception as e:
        tqdm.write(f"  [ERR] 查询失败 [{query[:40]}]: {e}")
        return []


def results_to_events(results: list[dict], default_category: str) -> list[dict]:
    events = []
    for r in results:
        title = r.get("webTitle", "").strip()
        date  = r.get("webPublicationDate", "")[:10]
        url   = r.get("webUrl", "")
        desc  = r.get("fields", {}).get("trailText", "") or title

        if not title or not date or not url:
            continue

        category, severity = classify(title + " " + desc)
        # 若规则未命中，用本次查询的默认分类
        if category == "industry" and default_category != "industry":
            category = default_category

        events.append({
            "event_id":   make_event_id(url),
            "date":       date,
            "category":   category,
            "entity":     _infer_entity(title + " " + desc),
            "severity":   severity,
            "title":      title[:200],
            "description": desc[:1000],
            "source_url": url,
        })
    return events


def _infer_entity(text: str) -> str:
    lower = text.lower()
    if "federal reserve" in lower or "fed " in lower or "fomc" in lower:
        return "FED"
    if "google" in lower or "alphabet" in lower:
        return "GOOGL"
    if "openai" in lower or "chatgpt" in lower:
        return "OPENAI"
    if "microsoft" in lower or "bing" in lower:
        return "MSFT"
    if "doj" in lower or "antitrust" in lower or "justice department" in lower:
        return "DOJ"
    return "MACRO"


INSERT_SQL = """
INSERT INTO events
    (event_id, date, category, entity, severity, title, description, source_url, embedding)
VALUES
    (%(event_id)s, %(date)s, %(category)s, %(entity)s, %(severity)s,
     %(title)s, %(description)s, %(source_url)s, %(embedding)s)
ON CONFLICT (event_id)
DO UPDATE SET
    title       = EXCLUDED.title,
    description = EXCLUDED.description,
    category    = EXCLUDED.category,
    severity    = EXCLUDED.severity,
    embedding   = EXCLUDED.embedding
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-embed", action="store_true", help="跳过 embedding 生成")
    parser.add_argument("--dry-run",  action="store_true", help="只打印，不写库")
    parser.add_argument("--from-date", default=DATE_FROM)
    parser.add_argument("--max-per-query", type=int, default=MAX_PER_QUERY)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    api_key = os.environ.get("GUARDIAN_API_KEY")
    if not api_key or api_key == "your_guardian_key_here":
        sys.exit("错误：请在 .env 中设置 GUARDIAN_API_KEY\n注册地址: https://open-platform.theguardian.com/access/")

    cfg = load_config(args.config)

    # ── 1. 拉取所有查询结果 ──
    all_events: dict[str, dict] = {}   # event_id → event，用于去重

    with httpx.Client() as client:
        with tqdm(SEARCH_QUERIES, desc="Guardian 搜索", unit="query") as pbar:
            for query, default_cat in pbar:
                pbar.set_postfix(q=query[:30])
                results = fetch_query(client, api_key, query, args.from_date, args.max_per_query)
                for ev in results_to_events(results, default_cat):
                    all_events[ev["event_id"]] = ev
                time.sleep(0.3)   # 避免触发速率限制

    events = list(all_events.values())
    events.sort(key=lambda e: e["date"])
    print(f"\n共获取 {len(events)} 条去重事件")

    if args.dry_run:
        for e in events[:10]:
            print(f"  [{e['date']}] [{e['category']}] {e['title'][:70]}")
        print("  ... (dry-run 模式，不写库)")
        return

    # ── 2. 生成 embedding ──
    vectors: list = [None] * len(events)
    if not args.no_embed:
        print("生成 embedding ...")
        embedder = create_embedding(cfg)
        texts = [f"{e['title']}: {e['description']}" for e in events]
        vectors = embedder.encode(texts, batch_size=32)

    # ── 3. 写库 ──
    with psycopg.connect(cfg.db.dsn) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            with tqdm(zip(events, vectors), total=len(events), desc="写入", unit="event") as pbar:
                for event, vec in pbar:
                    cur.execute(INSERT_SQL, {**event, "embedding": vec})
        conn.commit()

    print(f"\n[OK] {len(events)} 条 Guardian 事件写入完成")


if __name__ == "__main__":
    main()
