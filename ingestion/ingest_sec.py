#!/usr/bin/env python3
"""
下载 GOOGL SEC 文件（10-K / 10-Q）并解析入库。

流程：
  1. sec-edgar-downloader 下载原始 HTML 到 data/sec_raw/
  2. sec-parser 解析 section 结构
  3. tiktoken 按 512 tokens / 128 overlap 切 chunk
  4. embedding 写入 sec_chunks 表

用法:
    uv run ingestion/ingest_sec.py                      # 下载 + 解析 + 入库
    uv run ingestion/ingest_sec.py --download-only      # 只下载，不入库
    uv run ingestion/ingest_sec.py --ingest-only        # 跳过下载，直接解析已有文件
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

# ── 常量 ──────────────────────────────────────────────────
TICKER        = "GOOGL"
CIK           = "0001652044"
FORM_TYPES    = ["10-K", "10-Q", "8-K"]
DOWNLOAD_YEARS = 5          # 最近 5 年
CHUNK_TOKENS  = 512   # default，运行时被 cfg.chunking 覆盖
CHUNK_OVERLAP = 128
COMPANY       = "GOOGL"

# sec-parser 识别的标准 section 名称（用于 metadata）
SECTION_MAP = {
    "item 1":   "Business",
    "item 1a":  "Risk Factors",
    "item 7":   "MD&A",
    "item 7a":  "Market Risk",
    "item 8":   "Financial Statements",
}

RAW_DIR = Path(__file__).parent.parent / "data" / "sec_raw"


# ── 下载 ──────────────────────────────────────────────────

def download_filings(limit_per_form: int = DOWNLOAD_YEARS * 4) -> None:
    """用 sec-edgar-downloader 下载原始 HTML 文件到 data/sec_raw/。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    dl = Downloader(COMPANY, "research@macrolens.local", RAW_DIR)

    for form in FORM_TYPES:
        print(f"下载 {form} (最多 {limit_per_form} 份) ...")
        dl.get(form, TICKER, limit=limit_per_form, download_details=True)
        print(f"  → {form} 下载完成")


# ── 解析 & Chunking ────────────────────────────────────────

def iter_filing_files() -> list[Path]:
    """返回所有下载好的 primary-document HTML 文件。"""
    files = []
    for form in FORM_TYPES:
        files.extend(RAW_DIR.glob(
            f"sec-edgar-filings/{TICKER}/{form}/**/primary-document.html"
        ))
    return sorted(files)


def parse_filing_meta(filing_dir: Path) -> dict:
    """从路径和 full-submission.txt 提取 metadata。"""
    meta = {
        "doc_type": filing_dir.parent.name,  # 路径: sec-edgar-filings/GOOGL/{form}/{accession}/
        "filing_date": None,
        "period_end": None,
        "fiscal_year": None,
        "fiscal_quarter": None,
        "company": COMPANY,
        "source_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={CIK}",
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
                pd = date.fromisoformat(meta["period_end"])
                meta["fiscal_year"] = pd.year
                meta["fiscal_quarter"] = (pd.month - 1) // 3 + 1
        except Exception:
            pass

    return meta


def chunk_text(text: str, enc: tiktoken.Encoding, chunk_tokens: int = CHUNK_TOKENS, chunk_overlap: int = CHUNK_OVERLAP) -> list[str]:
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


# 表格边界标记，不含 NUL，不会出现在 SEC 正文
_TBL = "\x02TBL\x02"


def chunk_text_table_aware(
    text: str,
    enc: tiktoken.Encoding,
    chunk_tokens: int = CHUNK_TOKENS,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """表格感知分块：_TBL 标记的表格区域作为原子单元，不在表格中间切断。"""
    parts = text.split(_TBL)
    # 偶数下标 = 普通文本，奇数下标 = 表格内容

    result: list[str] = []
    buf: list[str] = []   # 当前 chunk 的文本片段
    buf_tok = 0

    def flush(keep_overlap: bool = True) -> None:
        nonlocal buf, buf_tok
        if not buf:
            return
        result.append("".join(buf))
        if keep_overlap:
            overlap: list[str] = []
            acc = 0
            for piece in reversed(buf):
                t = len(enc.encode(piece))
                if acc + t > chunk_overlap:
                    break
                overlap.insert(0, piece)
                acc += t
            buf, buf_tok = overlap, acc
        else:
            buf, buf_tok = [], 0

    for idx, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        is_table = (idx % 2 == 1)
        part_tok = len(enc.encode(part))

        if is_table:
            # 表格整体入 buf；若放不下先 flush
            if buf_tok + part_tok > chunk_tokens and buf:
                flush()
            buf.append(part + "\n")
            buf_tok += part_tok
            # 超大表格（>chunk_tokens）：按行切，不留 overlap
            if buf_tok > chunk_tokens:
                flush(keep_overlap=False)
        else:
            # 普通文本：逐行添加，满了就 flush
            for line in part.split("\n"):
                line_tok = len(enc.encode(line + "\n"))
                if buf_tok + line_tok > chunk_tokens and buf:
                    flush()
                buf.append(line + "\n")
                buf_tok += line_tok

    if buf:
        result.append("".join(buf))

    return [c.replace("\x00", "").replace("\x02", "") for c in result if c.strip()]


def parse_and_chunk(html_path: Path, enc: tiktoken.Encoding, chunk_tokens: int = CHUNK_TOKENS, chunk_overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """
    BeautifulSoup 提取正文文本，regex 识别 Item 边界做 section-aware chunking。
    """
    from bs4 import BeautifulSoup

    filing_dir = html_path.parent
    meta = parse_filing_meta(filing_dir)

    try:
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "ix:header", "ix:hidden"]):
            tag.decompose()
        # 在转文本前标记 <table> 边界，使分块器可以识别表格原子区域
        for tbl in soup.find_all("table"):
            tbl.replace_with(_TBL + tbl.get_text(separator="\t") + _TBL)
        full_text = soup.get_text(separator="\n")
        full_text = full_text.replace("\xa0", " ")  # normalize non-breaking spaces
    except Exception as e:
        print(f"  [ERR] 解析失败 {html_path.parent.name}: {e}")
        return []

    # 按 Item 边界切分 section
    # 目录中每个 Item 都会出现一次，正文中再出现一次；只取最后一次作为真实边界
    item_pattern = re.compile(
        r"(?:^|\n)[ \t]*(Item\s+\d+[A-Za-z]?[.\s]+[A-Za-z][^\n]{3,80})",
        re.MULTILINE | re.IGNORECASE,
    )
    seen: dict[str, tuple[int, str]] = {}
    for m in item_pattern.finditer(full_text):
        title = " ".join(m.group(1).split())  # normalize internal whitespace/newlines
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
        # 用 word-boundary 匹配，防止 "item 1" 误匹配 "item 1a"
        matched_section = next(
            (v for k, v in SECTION_MAP.items()
             if re.match(rf"{re.escape(k)}[\s.]", section.lower())),
            section[:60],
        )
        for idx, chunk in enumerate(chunk_text_table_aware(text, enc, chunk_tokens, chunk_overlap)):
            chunks_out.append({
                "content": chunk,
                "doc_type": meta["doc_type"],
                "filing_date": meta["filing_date"],
                "period_end": meta["period_end"],
                "fiscal_year": meta["fiscal_year"],
                "fiscal_quarter": meta["fiscal_quarter"],
                "section": matched_section,
                "subsection": "",
                "company": COMPANY,
                "source_url": meta["source_url"],
                "chunk_index": idx,
                "token_count": len(enc.encode(chunk)),
            })

    if not boundaries or boundaries[0][0] > 0:
        # 文档头部（目录/封面）单独作为 Unknown section
        end = boundaries[0][0] if boundaries else len(full_text)
        emit("Unknown", full_text[:end])

    for i, (start, title) in enumerate(boundaries[:-1]):
        end = boundaries[i + 1][0]
        emit(title, full_text[start:end])

    return chunks_out


# ── 入库 ──────────────────────────────────────────────────

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


def ingest_filing(conn: psycopg.Connection, chunks: list[dict], embedder) -> int:
    """批量 embed + upsert 一份 filing 的所有 chunk。"""
    if not chunks:
        return 0

    texts = [c["content"] for c in chunks]
    try:
        vectors = embedder.encode(texts, batch_size=32)
    except Exception:
        # 整批失败时逐条重试，跳过触发内容过滤的 chunk
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


# ── 主流程 ────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-only", action="store_true", help="只下载，不解析入库")
    parser.add_argument("--ingest-only",   action="store_true", help="跳过下载，解析已有文件")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)

    # ── 步骤 1：下载 ──
    if not args.ingest_only:
        print("── 步骤 1/3：下载 SEC 文件 ──")
        download_filings()
    else:
        print("── 跳过下载，使用已有文件 ──")

    if args.download_only:
        print("[OK] 下载完成（--download-only 模式）")
        return

    # ── 步骤 2：扫描文件 ──
    files = iter_filing_files()
    if not files:
        sys.exit(f"[ERR] 未找到任何 HTML 文件，请先运行下载或检查路径：{RAW_DIR}")
    print(f"\n── 步骤 2/3：解析 {len(files)} 份文件 ──")

    enc = tiktoken.get_encoding("cl100k_base")
    chunk_tokens   = cfg.chunking.chunk_tokens
    chunk_overlap  = cfg.chunking.chunk_overlap

    # ── 步骤 3：embed + 入库 ──
    print("\n── 步骤 3/3：Embedding + 入库 ──")
    embedder = create_embedding(cfg)

    # 重新入库前清空旧数据，防止 section 字段等旧版残留
    with psycopg.connect(cfg.db.dsn) as conn:
        conn.execute("TRUNCATE TABLE sec_chunks")
        conn.commit()
    print("  [OK] sec_chunks 已清空，开始全量写入")

    total_chunks = 0
    with psycopg.connect(cfg.db.dsn) as conn:
        register_vector(conn)
        with tqdm(files, desc="filing", unit="file") as pbar:
            for html_path in pbar:
                label = f"{html_path.parent.parent.name}/{html_path.parent.name}"
                pbar.set_postfix(file=label[-30:])

                chunks = parse_and_chunk(html_path, enc, chunk_tokens, chunk_overlap)
                if not chunks:
                    continue

                n = ingest_filing(conn, chunks, embedder)
                total_chunks += n
                tqdm.write(f"  [OK] {label}  {n} chunks")

    print(f"\n[OK] ingest_sec 完成，共写入 {total_chunks} 个 chunk")


if __name__ == "__main__":
    main()
