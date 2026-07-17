"""EPS 评测题生成器：从 earnings_history 程序化生成题目 + 可判分数值 GT。

用法:
    uv run eval/generators/eps.py --audit                    # 只探测数据可得性，不出题
    uv run eval/generators/eps.py                            # 生成到 eval/datasets/set_e_eps.jsonl
    uv run eval/generators/eps.py --per-template 12
    uv run eval/generators/eps.py --tickers GOOGL META

设计要点:
  - 只覆盖日历财年公司（见 eval/gt_sources.py 的财年陷阱说明）
  - 时间锚定用 period_end（不会说谎的列），措辞同时给 "Q3 2023" 和
    "quarter ending 2023-09-30"，避免裸 FY 口径歧义
  - 抽样是确定性的（按 ticker 分层 + 时间均匀取点），无随机种子，
    同样的数据永远生成同样的题
  - ground_truth 散文也是从数据生成的 —— 不再手抄
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
load_dotenv(Path(__file__).parent.parent.parent / ".env", encoding="utf-8")

import json

import psycopg

from eval.gt_sources import CALENDAR_FY_TICKERS, audit_eps_coverage, fetch_eps_quarters
from eval.questions import DATASET_DIR, ExpectedValue, GroundTruthSource, Question, to_jsonl_dict
from models.config import load_config

# ── tolerance 口径 ────────────────────────────────────────
# 不同字段的容差不该一样，取决于"专家会不会认为这个答案错了"：
TOL_EPS = 0.01      # 直接查表值，美分级精度，答案没理由偏离
TOL_SURPRISE_PCT = 0.1     # 也是表里现成的列（设计文档 §6.1 的 0.1 口径）
TOL_GROWTH_PCT = 1.0       # 派生值：46.23% 答成 "46%" 是正确的答法，不该判错

# 强制纳入的行：GOOGL 2023 Q3 就是手写题 D02 的同一道题。
# 保留它才能在同一份 CSV 里直接对照"手写 GT vs 自动 GT"。
PINNED = {("GOOGL", date(2023, 9, 30))}


def _q_label(row: dict) -> str:
    """时间措辞：季度标签 + period_end 锚定。

    对日历财年公司 "Q3 2023" 是准确的；附上 period_end 消除 FY 口径歧义。
    """
    pe = row["period_end"]
    return f"Q{row['fiscal_quarter']} {row['fiscal_year']} (the quarter ending {pe.isoformat()})"


def _src(row: dict, columns: list[str], generator: str, note: str = "") -> GroundTruthSource:
    return GroundTruthSource(
        table="earnings_history",
        columns=columns,
        ticker=row["ticker"],
        period_end=row["period_end"].isoformat(),
        generator=generator,
        note=note,
    )


# ── 模板 1: EPS 直查 ──────────────────────────────────────

def gen_lookup(row: dict, seq: int) -> Question:
    eps = row["eps_actual"]
    return Question(
        qid=f"E-LOOK-{seq:03d}",
        set_name="E",
        question=f"What was {row['ticker']}'s reported earnings per share (EPS) for {_q_label(row)}?",
        ground_truth=(
            f"{row['ticker']} reported an actual EPS of ${eps} for "
            f"Q{row['fiscal_quarter']} {row['fiscal_year']} "
            f"(quarter ending {row['period_end'].isoformat()})."
        ),
        key_facts=[row["ticker"], str(eps), row["period_end"].isoformat()],
        expected={
            "reported_eps": ExpectedValue(
                value=eps,
                tolerance=TOL_EPS,
                description=f"The actual/reported EPS in USD that the answer states for {row['ticker']} in that quarter",
            ),
        },
        source=_src(row, ["eps_actual"], "lookup"),
    )


# ── 模板 2: beat/miss + surprise%（D02 同型）────────────────

def gen_surprise(row: dict, seq: int) -> Question:
    actual, est, pct = row["eps_actual"], row["eps_estimate"], row["eps_surprise_pct"]
    verdict = "beating" if actual > est else ("missing" if actual < est else "matching")
    direction = "positive" if pct > 0 else ("negative" if pct < 0 else "flat")
    return Question(
        qid=f"E-SURP-{seq:03d}",
        set_name="E",
        question=(
            f"Did {row['ticker']} beat or miss analyst EPS estimates in {_q_label(row)}, "
            f"and what was the surprise percentage?"
        ),
        ground_truth=(
            f"{row['ticker']} reported an actual EPS of ${actual} for "
            f"Q{row['fiscal_quarter']} {row['fiscal_year']} "
            f"(quarter ending {row['period_end'].isoformat()}), {verdict} the analyst estimate "
            f"of ${est}, a {direction} earnings surprise of {pct}%."
        ),
        key_facts=[row["ticker"], str(actual), str(est), str(pct)],
        expected={
            "reported_eps": ExpectedValue(
                value=actual, tolerance=TOL_EPS,
                description="The actual/reported EPS in USD that the answer states",
            ),
            "estimated_eps": ExpectedValue(
                value=est, tolerance=TOL_EPS,
                description="The analyst consensus/estimated EPS in USD that the answer states",
            ),
            "surprise_pct": ExpectedValue(
                value=pct, tolerance=TOL_SURPRISE_PCT,
                description="The earnings surprise percentage that the answer states (negative if a miss)",
            ),
        },
        source=_src(row, ["eps_actual", "eps_estimate", "eps_surprise_pct"], "surprise"),
    )


# ── 模板 3: EPS 同比（派生计算）────────────────────────────

def gen_yoy(row: dict, prior: dict, seq: int) -> Question:
    cur, ago = row["eps_actual"], prior["eps_actual"]
    growth = round((cur - ago) / abs(ago) * 100, 2)
    verb = "grew" if growth > 0 else ("declined" if growth < 0 else "was flat")
    return Question(
        qid=f"E-YOY-{seq:03d}",
        set_name="E",
        question=(
            f"How did {row['ticker']}'s EPS in {_q_label(row)} compare to the same quarter "
            f"a year earlier, and what was the year-over-year percentage change?"
        ),
        ground_truth=(
            f"{row['ticker']}'s EPS {verb} from ${ago} in Q{prior['fiscal_quarter']} "
            f"{prior['fiscal_year']} (quarter ending {prior['period_end'].isoformat()}) "
            f"to ${cur} in Q{row['fiscal_quarter']} {row['fiscal_year']} "
            f"(quarter ending {row['period_end'].isoformat()}), "
            f"a year-over-year change of {growth}%."
        ),
        key_facts=[row["ticker"], str(cur), str(ago), str(growth)],
        expected={
            "current_eps": ExpectedValue(
                value=cur, tolerance=TOL_EPS,
                description=f"The EPS in USD that the answer states for the later quarter ({row['period_end'].isoformat()})",
            ),
            "year_ago_eps": ExpectedValue(
                value=ago, tolerance=TOL_EPS,
                description=f"The EPS in USD that the answer states for the year-earlier quarter ({prior['period_end'].isoformat()})",
            ),
            "yoy_growth_pct": ExpectedValue(
                value=growth, tolerance=TOL_GROWTH_PCT,
                description="The year-over-year EPS percentage change that the answer states (negative if a decline)",
            ),
        },
        source=_src(
            row, ["eps_actual"], "yoy",
            note=f"派生值 yoy_growth_pct 由 {prior['period_end'].isoformat()} 与 {row['period_end'].isoformat()} 两行计算",
        ),
    )


# ── 确定性抽样 ────────────────────────────────────────────

def _stratified_pick(rows: list[dict], n: int, pinned: set = frozenset()) -> list[dict]:
    """按 ticker 分层 + 时间均匀取点。确定性：同样的输入永远同样的输出。

    不用 random.sample 是因为评测集必须可复现且 diff 可读——
    随机抽样一旦换种子，整个题集就变了，跨版本没法比。
    """
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    picked: list[dict] = []
    forced = [r for r in rows if (r["ticker"], r["period_end"]) in pinned]
    picked.extend(forced)

    tickers = sorted(by_ticker)
    remaining = max(n - len(picked), 0)
    per_ticker = max(remaining // len(tickers), 1) if tickers else 0

    for t in tickers:
        pool = [r for r in by_ticker[t] if r not in picked]
        if not pool or per_ticker == 0:
            continue
        if len(pool) <= per_ticker:
            picked.extend(pool)
            continue
        # 在时间轴上均匀取点，保证跨年份覆盖
        step = (len(pool) - 1) / (per_ticker - 1) if per_ticker > 1 else 0
        idx = sorted({round(i * step) for i in range(per_ticker)})
        picked.extend(pool[i] for i in idx)

    picked.sort(key=lambda r: (r["ticker"], r["period_end"]))
    return picked[:n] if len(picked) > n else picked


def _print_audit(conn: psycopg.Connection) -> None:
    print(f"\n{'ticker':<8}{'quarters':>10}{'actual':>9}{'estimate':>10}{'surprise':>10}  range")
    print("-" * 72)
    for r in audit_eps_coverage(conn):
        flag = "" if r["ticker"] in CALENDAR_FY_TICKERS else "  ← 财年错配，不出题"
        print(
            f"{r['ticker']:<8}{r['quarters']:>10}{r['has_actual']:>9}"
            f"{r['has_estimate']:>10}{r['has_surprise']:>10}  "
            f"{r['earliest']} → {r['latest']}{flag}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", action="store_true", help="只探测数据可得性，不生成题目")
    parser.add_argument("--tickers", nargs="+", default=list(CALENDAR_FY_TICKERS))
    parser.add_argument("--per-template", type=int, default=8, help="每个模板生成多少题")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default=str(DATASET_DIR / "set_e_eps.jsonl"))
    args = parser.parse_args()

    cfg = load_config(args.config)

    with psycopg.connect(cfg.db.dsn) as conn:
        _print_audit(conn)
        if args.audit:
            return

        tickers = tuple(args.tickers)
        all_rows = fetch_eps_quarters(conn, tickers=tickers)
        est_rows = fetch_eps_quarters(conn, tickers=tickers, require_estimate=True)

    print(f"可用行: eps_actual={len(all_rows)}  含 estimate={len(est_rows)}")

    questions: list[Question] = []

    # 模板 1: 直查
    for i, row in enumerate(_stratified_pick(all_rows, args.per_template), 1):
        questions.append(gen_lookup(row, i))

    # 模板 2: beat/miss（需要 estimate）
    for i, row in enumerate(_stratified_pick(est_rows, args.per_template, pinned=PINNED), 1):
        questions.append(gen_surprise(row, i))

    # 模板 3: 同比（需要去年同期行存在）
    by_key = {(r["ticker"], r["fiscal_year"], r["fiscal_quarter"]): r for r in all_rows}
    yoy_pairs = [
        (r, by_key[(r["ticker"], r["fiscal_year"] - 1, r["fiscal_quarter"])])
        for r in all_rows
        if (r["ticker"], r["fiscal_year"] - 1, r["fiscal_quarter"]) in by_key
    ]
    yoy_rows = _stratified_pick([r for r, _ in yoy_pairs], args.per_template)
    prior_of = {(r["ticker"], r["period_end"]): p for r, p in yoy_pairs}
    for i, row in enumerate(yoy_rows, 1):
        questions.append(gen_yoy(row, prior_of[(row["ticker"], row["period_end"])], i))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for q in questions:
            f.write(json.dumps(to_jsonl_dict(q), ensure_ascii=False) + "\n")

    n_fields = sum(len(q.expected) for q in questions if q.expected)
    print(f"\n生成 {len(questions)} 道题 / {n_fields} 个可判分数值字段 → {out}")
    for tmpl in ("LOOK", "SURP", "YOY"):
        n = sum(1 for q in questions if f"-{tmpl}-" in q.qid)
        print(f"  {tmpl:<6} {n:>3} 题")
    print("\n下一步: git diff eval/datasets/ 检查 GT，然后")
    print("  uv run eval/run_eval.py --sets E --output eval/results_v22.csv")


if __name__ == "__main__":
    main()
