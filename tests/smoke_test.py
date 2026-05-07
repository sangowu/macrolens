"""
冒烟测试：用真实 API 和数据库跑 2 个问题，验证新流程端到端不崩。
不做 RAGAS 评分，只检查：有答案、有引用、没报错。

运行：
    uv run tests/smoke_test.py
"""
from __future__ import annotations

import sys
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg
from pgvector.psycopg import register_vector

from agent.per_loop import run as per_loop_run
from models.config import load_config
from models.factory import create_embedding, create_llm_client

SMOKE_QUESTIONS = [
    "What was Google's total advertising revenue in fiscal year 2022?",       # 事实型，应有数字+引用
    "What was Google's advertising revenue CAGR from 2019 to 2023?",          # 需要 compute tool
]

CHECKS = {
    # 答案里应该有 [n] 引用
    "has_citation": lambda ans: bool(re.search(r"\[\d+\]", ans)),
    # 答案不为空
    "non_empty": lambda ans: len(ans.strip()) > 50,
    # 没有 <compute> 残留标签（旧版残留）
    "no_compute_tag": lambda ans: "<compute>" not in ans,
    # 没有 "[computation error" 字样
    "no_compute_error": lambda ans: "[computation error" not in ans,
}


def run_smoke() -> None:
    cfg = load_config("config.yaml")
    embedder = create_embedding(cfg)
    llm = create_llm_client(cfg)

    passed = 0
    failed = 0

    with psycopg.connect(cfg.db.dsn) as conn:
        register_vector(conn)

        for i, question in enumerate(SMOKE_QUESTIONS, 1):
            print(f"\n[{i}/{len(SMOKE_QUESTIONS)}] {question}")
            print("-" * 60)

            try:
                answer, context = per_loop_run(
                    question, cfg, conn, embedder, llm,
                    max_iter=2, verbose=True,
                )

                print(f"\nAnswer preview: {answer[:200].replace(chr(10), ' ')}...")
                print(f"Context items:  {len(context)}")

                all_passed = True
                for check_name, check_fn in CHECKS.items():
                    ok = check_fn(answer)
                    status = "PASS" if ok else "FAIL"
                    print(f"  {status}  {check_name}")
                    if not ok:
                        all_passed = False

                if all_passed:
                    passed += 1
                    print("→ PASSED")
                else:
                    failed += 1
                    print("→ FAILED")

            except Exception as e:
                import traceback
                failed += 1
                print(f"  ERROR: {type(e).__name__}: {e}")
                traceback.print_exc()
                print("→ FAILED (exception)")

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(SMOKE_QUESTIONS)}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_smoke()
