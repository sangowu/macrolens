#!/usr/bin/env python3
"""
将 data/events.json 导入 PostgreSQL events 表。

用法:
    uv run ingestion/ingest_events.py
    uv run ingestion/ingest_events.py --no-embed
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

import psycopg
from pgvector.psycopg import register_vector
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from models.config import load_config
from models.factory import create_embedding

EVENTS_FILE = Path(__file__).parent.parent / "data" / "events.json"

INSERT_SQL = """
INSERT INTO events
    (event_id, date, category, entity, severity, title, description, source_url, embedding)
VALUES
    (%(event_id)s, %(date)s, %(category)s, %(entity)s, %(severity)s,
     %(title)s, %(description)s, %(source_url)s, %(embedding)s)
ON CONFLICT (event_id)
DO UPDATE SET
    date        = EXCLUDED.date,
    category    = EXCLUDED.category,
    entity      = EXCLUDED.entity,
    severity    = EXCLUDED.severity,
    title       = EXCLUDED.title,
    description = EXCLUDED.description,
    source_url  = EXCLUDED.source_url,
    embedding   = EXCLUDED.embedding
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-embed", action="store_true", help="跳过 embedding 生成")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    events = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
    print(f"读取 {len(events)} 条事件")

    cfg = load_config(args.config)

    vectors: list = [None] * len(events)
    if not args.no_embed:
        print("生成 embedding ...")
        embedder = create_embedding(cfg)
        texts = [f"{e['title']}: {e['description']}" for e in events]
        vectors = embedder.encode(texts, batch_size=32)

    with psycopg.connect(cfg.db.dsn) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            with tqdm(zip(events, vectors), total=len(events), desc="写入", unit="event") as pbar:
                for event, vec in pbar:
                    pbar.set_postfix(id=event["event_id"])
                    cur.execute(INSERT_SQL, {**event, "embedding": vec})
        conn.commit()

    print(f"\n[OK] {len(events)} 条事件写入完成")


if __name__ == "__main__":
    main()
