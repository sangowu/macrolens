"""
评估主入口：对 Set A/B/C 跑 PER Loop + 四项指标，结果写 CSV。

用法:
    uv run eval/run_eval.py --sets A B        # 只跑 Set A 和 B
    uv run eval/run_eval.py --sets A B C      # 全跑
    uv run eval/run_eval.py --sets A --max-iter 2
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s", stream=sys.stdout)
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

import psycopg

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.per_loop import run as per_loop_run
from eval.metrics import evaluate_all
from eval.questions import ALL_QUESTIONS, get_set
from models.config import load_config
from models.factory import create_embedding, create_judge_llm_client, create_llm_client


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sets", nargs="+", default=["A"], choices=["A", "B", "C"])
    parser.add_argument("--max-iter", type=int, default=3)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default="eval/results.csv")
    args = parser.parse_args()

    cfg = load_config(args.config)
    embedder = create_embedding(cfg)
    llm = create_llm_client(cfg)
    judge_llm = create_judge_llm_client(cfg)
    print(f"Pipeline LLM : {cfg.llm.model}")
    print(f"Judge LLM    : {cfg.judge.model if cfg.judge else cfg.llm.model}")

    questions = []
    for s in args.sets:
        questions.extend(get_set(s))

    print(f"Running evaluation on {len(questions)} questions (Sets: {args.sets})")

    fieldnames = [
        "qid", "set", "question",
        "faithfulness", "answer_relevancy", "context_precision", "context_recall", "ragas_score",
        "faithfulness_reason", "answer_relevancy_reason", "context_precision_reason", "context_recall_reason",
        "context_items", "latency_s", "answer_preview",
    ]

    output_path = Path(args.output)
    output_path.parent.mkdir(exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        with psycopg.connect(cfg.db.dsn) as conn:
            for q in questions:
                print(f"\n[{q.qid}] {q.question[:70]}...")
                t0 = time.time()

                try:
                    answer, context = per_loop_run(
                        q.question, cfg, conn, embedder, llm, max_iter=args.max_iter
                    )
                    latency = round(time.time() - t0, 1)

                    metrics = evaluate_all(q.question, q.ground_truth, answer, context, judge_llm)

                    row = {
                        "qid": q.qid,
                        "set": q.set_name,
                        "question": q.question,
                        "context_items": len(context),
                        "latency_s": latency,
                        "answer_preview": answer[:150].replace("\n", " "),
                        **metrics,
                    }
                    writer.writerow(row)
                    f.flush()

                    def _fmt(v) -> str:
                        return f"{v:.3f}" if v is not None else "None"
                    print(f"  RAGAS: {_fmt(metrics.get('ragas_score'))} | faithfulness={_fmt(metrics.get('faithfulness'))} | relevancy={_fmt(metrics.get('answer_relevancy'))} | precision={_fmt(metrics.get('context_precision'))} | recall={_fmt(metrics.get('context_recall'))} | {latency}s")

                except Exception as e:
                    print(f"  ERROR: {e}")
                    writer.writerow({"qid": q.qid, "set": q.set_name, "question": q.question, "ragas_score": None})
                    f.flush()

                time.sleep(1)  # 避免 API 速率限制

    print(f"\nResults saved to {output_path}")
    _print_summary(output_path)


def _print_summary(path: Path) -> None:
    import statistics
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    for set_name in ["A", "B", "C"]:
        set_rows = [r for r in rows if r["set"] == set_name]
        if not set_rows:
            continue
        scores = [float(r["ragas_score"]) for r in set_rows if r.get("ragas_score") and r["ragas_score"] != "None"]
        if scores:
            print(f"\nSet {set_name} ({len(scores)} questions):")
            print(f"  RAGAS avg:  {statistics.mean(scores):.3f}")
            print(f"  RAGAS min:  {min(scores):.3f}")
            print(f"  RAGAS max:  {max(scores):.3f}")


if __name__ == "__main__":
    main()
