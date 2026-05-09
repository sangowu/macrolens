#!/usr/bin/env python3
"""
多 Ticker SEC 入库脚本：支持 MAG7 全部公司。

与 ingest_sec.py 的关键差异：
  - TICKER/CIK 通过 CLI 参数传入，不硬编码
  - 使用 DELETE WHERE company=ticker 替代 TRUNCATE（安全支持多公司共存）
  - 入库前给出确认提示，防止误删

用法:
    uv run ingestion/ingest_sec_multi.py --tickers MSFT META AMZN AAPL NVDA TSLA
    uv run ingestion/ingest_sec_multi.py --tickers MSFT --ingest-only
    uv run ingestion/ingest_sec_multi.py --tickers GOOGL --download-only
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

import psycopg
import tiktoken
from pgvector.psycopg import register_vector
from sec_edgar_downloader import Downloader
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from models.config import load_config
from models.factory import create_embedding

# ── Ticker → CIK 映射 ─────────────────────────────────────
TICKER_CIK_MAP: dict[str, str] = {
    "GOOGL": "0001652044",
    "MSFT":  "0000789019",
    "META":  "0001326801",
    "AMZN":  "0001018724",
    "AAPL":  "0000320193",
    "NVDA":  "0001045810",
    "TSLA":  "0001318605",
}

FORM_TYPES    = ["10-K", "10-Q", "8-K"]
DOWNLOAD_YEARS = 5

SECTION_MAP = {
    "item 1":   "Business",
    "item 1a":  "Risk Factors",
    "item 7":   "MD&A",
    "item 7a":  "Market Risk",
    "item 8":   "Financial Statements",
}

RAW_DIR = Path(__file__).parent.parent / "data" / "sec_raw"

INSERT_SQL = """
INSERT INTO sec_chunks
    (doc_type, filing_date, period_end, fiscal_year, fiscal_quarter,
     section, subsection, company, source_url, chunk_index, token_count,
     content, embedding)
VALUES
    (%(doc_type)s, %(filing_date)s, %(period_end)s, %(fiscal_year)s, %(fiscal_quarter)s,
     %(section)s, %(subsection)s, %(company)s, %(source_url)s, %(chunk_index)s, %(token_count)s,
     %(content)s, %(embedding)s)
ON CONFLICT DO NOTHING
"""


# ── 下载 ──────────────────────────────────────────────────

def download_filings(ticker: str, limit_per_form: int = DOWNLOAD_YEARS * 4) -> None:
    """下载单个 ticker 的 SEC 文件到 data/sec_raw/。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    dl = Downloader(ticker, "research@macrolens.local", RAW_DIR)
    for form in FORM_TYPES:
        print(f"  下载 {ticker} {form} (最多 {limit_per_form} 份) ...")
        try:
            dl.get(form, ticker, limit=limit_per_form, download_details=True)
        except Exception as e:
            print(f"  [WARN] {ticker} {form} 下载异常: {e}")


# ── 解析 ──────────────────────────────────────────────────

def iter_filing_files(ticker: str) -> list[Path]:
    files = []
    for form in FORM_TYPES:
        files.extend(RAW_DIR.glob(
            f"sec-edgar-filings/{ticker}/{form}/**/primary-document.html"
        ))
    return sorted(files)


def parse_filing_meta(filing_dir: Path, ticker: str, cik: str) -> dict:
    meta = {
        "doc_type":       filing_dir.parent.name,
        "filing_date":    None,
        "period_end":     None,
        "fiscal_year":    None,
        "fiscal_quarter": None,
        "company":        ticker,
        "source_url":     f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
    }

    submission_file = filing_dir / "full-submission.txt"
    if submission_file.exists():
        try:
            header = submission_file.read_text(encoding="utf-8", errors="ignore")[:4000]
            filed_match  = re.search(r"FILED AS OF DATE:\s*(\d{8})", header)
            period_match = re.search(r"PERIOD OF REPORT:\s*(\d{8})", header)
            if filed_match:
                d = filed_match.group(1)
                meta["filing_date"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            if period_match:
                d = period_match.group(1)
                meta["period_end"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                from datetime import date
                pd_date = date.fromisoformat(meta["period_end"])
                meta["fiscal_year"]    = pd_date.year
                meta["fiscal_quarter"] = (pd_date.month - 1) // 3 + 1
        except Exception:
            pass

    return meta


def chunk_text(text: str, enc: tiktoken.Encoding, chunk_tokens: int = 512, chunk_overlap: int = 128) -> list[str]:
    tokens = enc.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_tokens, len(tokens))
        chunks.append(enc.decode(tokens[start:end]))
        if end == len(tokens):
            break
        start += chunk_tokens - chunk_overlap
    return chunks


def parse_and_chunk(html_path: Path, enc: tiktoken.Encoding, ticker: str, cik: str,
                    chunk_tokens: int = 512, chunk_overlap: int = 128) -> list[dict]:
    from bs4 import BeautifulSoup

    filing_dir = html_path.parent
    meta = parse_filing_meta(filing_dir, ticker, cik)

    try:
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "ix:header", "ix:hidden"]):
            tag.decompose()
        full_text = soup.get_text(separator="\n")
        full_text = full_text.replace("\xa0", " ")
    except Exception as e:
        print(f"  [ERR] 解析失败 {html_path.parent.name}: {e}")
        return []

    item_pattern = re.compile(
        r"(?:^|\n)[ \t]*(Item\s+\d+[A-Za-z]?[.\s]+[A-Za-z][^\n]{3,80})",
        re.MULTILINE | re.IGNORECASE,
    )
    seen: dict[str, tuple[int, str]] = {}
    for m in item_pattern.finditer(full_text):
        title = " ".join(m.group(1).split())
        key_m = re.match(r"(item\s+\d+[a-z]?)", title, re.I)
        key = key_m.group(1).lower() if key_m else title[:10].lower()
        seen[key] = (m.start(), title)
    boundaries = sorted(seen.values())
    boundaries.append((len(full_text), "END"))

    chunks_out = []

    def emit(section: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        matched_section = next(
            (v for k, v in SECTION_MAP.items()
             if re.match(rf"{re.escape(k)}[\s.]", section.lower())),
            section[:60],
        )
        for idx, chunk in enumerate(chunk_text(text, enc, chunk_tokens, chunk_overlap)):
            chunks_out.append({
                "content":        chunk,
                "doc_type":       meta["doc_type"],
                "filing_date":    meta["filing_date"],
                "period_end":     meta["period_end"],
                "fiscal_year":    meta["fiscal_year"],
                "fiscal_quarter": meta["fiscal_quarter"],
                "section":        matched_section,
                "subsection":     "",
                "company":        ticker,
                "source_url":     meta["source_url"],
                "chunk_index":    idx,
                "token_count":    len(enc.encode(chunk)),
            })

    if not boundaries or boundaries[0][0] > 0:
        end = boundaries[0][0] if boundaries else len(full_text)
        emit("Unknown", full_text[:end])

    for i, (start, title) in enumerate(boundaries[:-1]):
        end = boundaries[i + 1][0]
        emit(title, full_text[start:end])

    return chunks_out


# ── 入库 ──────────────────────────────────────────────────

def ingest_filing(conn: psycopg.Connection, chunks: list[dict], embedder) -> int:
    """批量 embed + insert 一份 filing 的所有 chunk。"""
    if not chunks:
        return 0

    texts = [c["content"] for c in chunks]
    try:
        vectors = embedder.encode(texts, batch_size=32)
    except Exception:
        vectors = []
        for text in texts:
            try:
                vectors.append(embedder.encode([text])[0])
            except Exception as e:
                print(f"  [SKIP] embedding failed: {str(e)[:80]}")
                vectors.append(None)

    rows = [{**c, "embedding": v} for c, v in zip(chunks, vectors) if v is not None]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, rows)
    conn.commit()
    return len(rows)


def ingest_ticker(
    ticker: str,
    conn: psycopg.Connection,
    embedder,
    cfg,
    ingest_only: bool = False,
    yes: bool = False,
) -> int:
    """对单个 ticker 完整跑 download→parse→embed→insert 流程。返回写入 chunk 数。"""
    cik = TICKER_CIK_MAP.get(ticker)
    if cik is None:
        print(f"[ERR] 不支持的 ticker: {ticker}，支持: {list(TICKER_CIK_MAP)}")
        return 0

    # ① 下载
    if not ingest_only:
        download_filings(ticker)

    # ② 扫描文件
    files = iter_filing_files(ticker)
    if not files:
        print(f"  [WARN] {ticker}: 未找到任何 HTML 文件，跳过入库")
        return 0

    # ③ 确认提示（防止误操作）
    if not yes:
        ans = input(f"\n  即将删除 {ticker} 的旧 chunks 并重新入库 {len(files)} 份文件。继续？[y/N] ")
        if ans.strip().lower() != "y":
            print(f"  跳过 {ticker}")
            return 0

    # ④ 清除旧数据（安全：只删本 ticker，不影响其他公司）
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sec_chunks WHERE company = %s", (ticker,))
    conn.commit()
    print(f"  [OK] {ticker} 旧 chunks 已清除，开始入库 {len(files)} 份文件 ...")

    # ⑤ 解析 + 入库
    enc = tiktoken.get_encoding("cl100k_base")
    chunk_tokens  = cfg.chunking.chunk_tokens
    chunk_overlap = cfg.chunking.chunk_overlap

    total = 0
    with tqdm(files, desc=f"{ticker} filing", unit="file", leave=False) as pbar:
        for html_path in pbar:
            label = f"{html_path.parent.parent.name}/{html_path.parent.name}"
            pbar.set_postfix(file=label[-30:])
            chunks = parse_and_chunk(html_path, enc, ticker, cik, chunk_tokens, chunk_overlap)
            if not chunks:
                continue
            n = ingest_filing(conn, chunks, embedder)
            total += n
            tqdm.write(f"    [OK] {label}  {n} chunks")

    return total


# ── 主入口 ────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest MAG7 SEC filings (multi-ticker)")
    parser.add_argument("--tickers", nargs="+", required=True,
                        help=f"股票代码，支持: {list(TICKER_CIK_MAP)}")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--ingest-only",   action="store_true")
    parser.add_argument("--yes", "-y",     action="store_true",
                        help="跳过确认提示（批量脚本使用）")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.download_only:
        for ticker in args.tickers:
            print(f"\n── 下载 {ticker} ──")
            download_filings(ticker)
        print("\n[OK] 下载完成")
        return

    embedder = create_embedding(cfg)

    print(f"连接数据库 {cfg.db.host}:{cfg.db.port}/{cfg.db.dbname} ...")
    with psycopg.connect(cfg.db.dsn) as conn:
        register_vector(conn)

        grand_total = 0
        for ticker in args.tickers:
            print(f"\n══ {ticker} ══════════════════════════════")
            n = ingest_ticker(
                ticker, conn, embedder, cfg,
                ingest_only=args.ingest_only,
                yes=args.yes,
            )
            print(f"  → {ticker}: {n} chunks 写入完成")
            grand_total += n

    print(f"\n[OK] ingest_sec_multi 完成，共写入 {grand_total} 个 chunk")


if __name__ == "__main__":
    main()
