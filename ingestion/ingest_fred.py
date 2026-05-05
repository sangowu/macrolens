#!/usr/bin/env python3
"""
拉取 12 个 FRED 宏观指标写入 macro_indicators，
并为 macro_series_meta 的 embedding 列生成向量。

用法:
    uv run ingestion/ingest_fred.py
    uv run ingestion/ingest_fred.py --start 2018-01-01 --no-embed

FRED_API_KEY 从项目根目录的 .env 文件自动加载。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# encoding='utf-8' 避免 Windows GBK 默认编码导致的解析错误
load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

import time

import pandas as pd
import psycopg
from fredapi import Fred
from pgvector.psycopg import register_vector
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from models.config import load_config
from models.factory import create_embedding

SERIES_IDS = [
    "GDP", "GDPC1",
    "CPIAUCSL", "PCEPI",
    "UNRATE", "PAYEMS",
    "FEDFUNDS", "DGS10",
    "M2SL",
    "VIXCLS", "UMCSENT",
    "USREC",
]


def ingest_indicators(conn: psycopg.Connection, fred: Fred, start: str) -> None:
    """
    从 FRED API 拉取每个 series 的历史数值，批量 upsert 进 macro_indicators。
    ON CONFLICT DO UPDATE 保证重复运行是幂等的。
    """
    total = 0

    with tqdm(SERIES_IDS, desc="FRED series", unit="series") as pbar:
        for series_id in pbar:
            pbar.set_postfix(series=series_id)
            data = None
            for attempt in range(3):
                try:
                    data = fred.get_series(series_id, observation_start=start)
                    break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                    else:
                        tqdm.write(f"  [ERR] {series_id} 拉取失败（重试3次）: {e}")
            if data is None:
                continue

            rows = [
                (series_id, dt.date().isoformat(), float(v))
                for dt, v in data.items()
                if pd.notna(v)
            ]

            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO macro_indicators (series_id, date, value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (series_id, date)
                    DO UPDATE SET value = EXCLUDED.value, ingested_at = now()
                    """,
                    rows,
                )
            conn.commit()
            tqdm.write(f"  [OK] {series_id:<10} {len(rows):>5} 条")
            total += len(rows)

    print(f"\nmacro_indicators 共写入 {total} 条")


def embed_series_meta(conn: psycopg.Connection, cfg) -> None:
    """
    为 macro_series_meta 的 12 条描述文本生成 embedding，
    用于后续"通胀指标"→ CPIAUCSL/PCEPI 的语义映射。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT series_id, name, description FROM macro_series_meta WHERE embedding IS NULL"
        )
        rows = cur.fetchall()

    if not rows:
        print("macro_series_meta embedding 已存在，跳过")
        return

    print(f"为 {len(rows)} 个 series 生成 embedding ...")
    embedder = create_embedding(cfg)

    texts = [f"{name}: {desc}" for _, name, desc in rows]
    vectors = embedder.encode(texts, batch_size=len(texts))

    with conn.cursor() as cur:
        for (series_id, _, _), vec in zip(rows, vectors):
            cur.execute(
                "UPDATE macro_series_meta SET embedding = %s WHERE series_id = %s",
                (vec, series_id),
            )
    conn.commit()
    print("macro_series_meta embedding 写入完成")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest FRED macroeconomic data")
    parser.add_argument("--start", default="2018-01-01", help="数据起始日期 (YYYY-MM-DD)")
    parser.add_argument("--no-embed", action="store_true", help="跳过 embedding 生成（模型未装时使用）")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        sys.exit("错误：FRED_API_KEY 未设置，请检查 .env 文件")

    cfg = load_config(args.config)
    fred = Fred(api_key=api_key)

    print(f"连接数据库 {cfg.db.host}:{cfg.db.port}/{cfg.db.dbname} ...")
    with psycopg.connect(cfg.db.dsn) as conn:
        register_vector(conn)

        print(f"\n── 拉取 FRED 数据（起始: {args.start}）──")
        ingest_indicators(conn, fred, start=args.start)

        if not args.no_embed:
            print("\n── 生成 macro_series_meta embedding ──")
            embed_series_meta(conn, cfg)

    print("\n[OK] ingest_fred 完成")


if __name__ == "__main__":
    main()
