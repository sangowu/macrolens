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
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

import psycopg

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.per_loop import run as per_loop_run
from eval.metrics import evaluate_all
from eval.questions import ALL_QUESTIONS, get_set
from models.config import load_config
from models.factory import create_embedding, create_llm_client


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

                all_context: list[dict] = []

                def _run_with_context_capture(question, cfg, conn, embedder, llm, max_iter):
                    """Wrapper to capture context from per_loop."""
                    from agent.critic import critique
                    from agent.executor import execute
                    from agent.planner import plan
                    from agent.synthesizer import synthesize

                    context: list[dict] = []
                    history: list[dict] = []
                    missing_hint = ""

                    for iteration in range(1, max_iter + 1):
                        prompt = question if iteration == 1 else f"{question}\n\nFocus on: {missing_hint}"
                        sub_queries = plan(prompt, llm, history=history if iteration > 1 else None)
                        new_items = execute(sub_queries, conn, embedder, cfg.llm)
                        seen = {(c.get("id") or c.get("event_id") or c.get("date","") + c.get("series_id","")) for c in context}
                        for c in new_items:
                            key = c.get("id") or c.get("event_id") or c.get("date","") + c.get("series_id","")
                            if key not in seen:
                                context.append(c)
                                seen.add(key)
                        is_sufficient, missing_hint = critique(question, context, llm)
                        if is_sufficient or iteration == max_iter:
                            break
                        history.append({"role": "assistant", "content": missing_hint})

                    answer = synthesize(question, context, llm, max_tokens=cfg.llm.max_tokens)
                    return answer, context

                try:
                    answer, context = _run_with_context_capture(
                        q.question, cfg, conn, embedder, llm, args.max_iter
                    )
                    latency = round(time.time() - t0, 1)

                    metrics = evaluate_all(q.question, q.ground_truth, answer, context, llm)

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

                    score = metrics.get("ragas_score")
                    print(f"  RAGAS: {score:.3f} | faithfulness={metrics.get('faithfulness'):.2f} | relevancy={metrics.get('answer_relevancy'):.2f} | precision={metrics.get('context_precision'):.2f} | recall={metrics.get('context_recall'):.2f} | {latency}s")

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
