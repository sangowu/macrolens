"""
A/B 版本对比：把两次 eval 结果 CSV 并排展示，输出分数差异。

用法：
    # 先跑旧版（用 git stash 恢复旧代码），结果存 v1
    uv run eval/run_eval.py --sets A B C --output eval/results_v1.csv

    # 恢复新版代码，结果存 v2
    uv run eval/run_eval.py --sets A B C --output eval/results_v2.csv

    # 对比
    uv run eval/compare_versions.py eval/results_v1.csv eval/results_v2.csv

    # 对比并生成图表（保存到 eval/comparison.png）
    uv run eval/compare_versions.py eval/results_v1.csv eval/results_v2.csv --plot
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


METRICS = ["faithfulness", "answer_relevancy", "context_precision", "context_recall", "ragas_score"]


def load_results(path: str) -> dict[str, dict]:
    """qid → row dict"""
    rows = {}
    for row in csv.DictReader(open(path, encoding="utf-8")):
        rows[row["qid"]] = row
    return rows


def safe_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _plot_comparison(
    v1_data: dict[str, list[float]],
    v2_data: dict[str, list[float]],
    v1_name: str,
    v2_name: str,
    out_path: str = "eval/comparison.png",
) -> None:
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("matplotlib not installed — skipping plot")
        return

    labels = ["Faithfulness", "Relevancy", "Precision", "Recall", "RAGAS"]
    v1_vals = [sum(v1_data[m]) / len(v1_data[m]) if v1_data[m] else 0 for m in METRICS]
    v2_vals = [sum(v2_data[m]) / len(v2_data[m]) if v2_data[m] else 0 for m in METRICS]

    x = np.arange(len(METRICS))
    w = 0.35
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(x - w / 2, v1_vals, w, label=v1_name, color="#6b7280")
    ax.bar(x + w / 2, v2_vals, w, label=v2_name, color="#2563eb")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylim(0, 1.15)
    ax.set_ylabel("Score")
    ax.set_title(f"Evaluation Metrics: {v1_name}  vs  {v2_name}")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    for i, (a, b) in enumerate(zip(v1_vals, v2_vals)):
        delta = b - a
        color = "#16a34a" if delta >= 0 else "#dc2626"
        ax.text(i + w / 2, b + 0.02, f"{delta:+.3f}", ha="center", fontsize=8, color=color, fontweight="bold")

    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    print(f"Plot saved → {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two eval result CSVs")
    parser.add_argument("v1", help="Baseline CSV path")
    parser.add_argument("v2", help="New version CSV path")
    parser.add_argument("--plot", action="store_true", help="Save bar chart to eval/comparison.png")
    args = parser.parse_args()

    v1_path, v2_path = args.v1, args.v2
    v1 = load_results(v1_path)
    v2 = load_results(v2_path)

    common_qids = sorted(set(v1) & set(v2))
    if not common_qids:
        print("No common question IDs found between the two files.")
        sys.exit(1)

    print(f"\n{'='*80}")
    print(f"  A/B Comparison: {Path(v1_path).name}  vs  {Path(v2_path).name}")
    print(f"  Common questions: {len(common_qids)}")
    print(f"{'='*80}\n")

    # ── 逐题对比 ──────────────────────────────────────────
    print(f"{'QID':<6} {'Set':<5} {'Metric':<20} {'v1':>7} {'v2':>7} {'Δ':>8}  {'Win':>5}")
    print("-" * 65)

    improvements = {m: [] for m in METRICS}
    regressions  = {m: [] for m in METRICS}

    for qid in common_qids:
        r1, r2 = v1[qid], v2[qid]
        set_name = r1.get("set", "?")

        for metric in METRICS:
            s1 = safe_float(r1.get(metric))
            s2 = safe_float(r2.get(metric))
            if s1 is None or s2 is None:
                continue
            delta = s2 - s1
            win = "v2 +" if delta > 0.01 else ("v1 +" if delta < -0.01 else "tie")
            print(f"{qid:<6} {set_name:<5} {metric:<20} {s1:>7.3f} {s2:>7.3f} {delta:>+8.3f}  {win:>5}")
            if delta > 0.01:
                improvements[metric].append(delta)
            elif delta < -0.01:
                regressions[metric].append(delta)

    # ── 汇总 ──────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("  Summary by metric")
    print(f"{'='*80}")
    print(f"{'Metric':<22} {'v1 avg':>8} {'v2 avg':>8} {'Δ avg':>8} {'Impr':>6} {'Regr':>6}")
    print("-" * 65)

    all_v1: dict[str, list[float]] = {m: [] for m in METRICS}
    all_v2: dict[str, list[float]] = {m: [] for m in METRICS}

    for qid in common_qids:
        r1, r2 = v1[qid], v2[qid]
        for metric in METRICS:
            s1 = safe_float(r1.get(metric))
            s2 = safe_float(r2.get(metric))
            if s1 is not None:
                all_v1[metric].append(s1)
            if s2 is not None:
                all_v2[metric].append(s2)

    for metric in METRICS:
        if not all_v1[metric] or not all_v2[metric]:
            continue
        avg1 = sum(all_v1[metric]) / len(all_v1[metric])
        avg2 = sum(all_v2[metric]) / len(all_v2[metric])
        delta = avg2 - avg1
        n_impr = len(improvements[metric])
        n_regr = len(regressions[metric])
        print(f"{metric:<22} {avg1:>8.3f} {avg2:>8.3f} {delta:>+8.3f} {n_impr:>6} {n_regr:>6}")

    # ── 延迟对比 ──────────────────────────────────────────
    lat1 = [safe_float(v1[q].get("latency_s")) for q in common_qids if safe_float(v1[q].get("latency_s"))]
    lat2 = [safe_float(v2[q].get("latency_s")) for q in common_qids if safe_float(v2[q].get("latency_s"))]
    if lat1 and lat2:
        print(f"\n  Latency: v1 avg={sum(lat1)/len(lat1):.1f}s  v2 avg={sum(lat2)/len(lat2):.1f}s  Δ={sum(lat2)/len(lat2)-sum(lat1)/len(lat1):+.1f}s")
        print(f"  (v2 adds one extra LLM call for evidence selection — expected latency increase)")

    if args.plot:
        _plot_comparison(all_v1, all_v2, Path(v1_path).stem, Path(v2_path).stem)


if __name__ == "__main__":
    main()
