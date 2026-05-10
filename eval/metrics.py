"""
LLM-as-Judge 评估指标。

- faithfulness:       答案中的声明是否都有 context 支撑？(0-1)
- answer_relevancy:   答案是否切题？(0-1)
- context_precision:  检索到的 context 中有多少比例真正有用？(0-1)
- context_recall:     关键事实是否被 context 覆盖？(0-1)
"""
from __future__ import annotations

import json
import re

from models.llm.base import LLMClient

# ── Prompt 模板 ────────────────────────────────────────────

_FAITHFULNESS_PROMPT = """\
Given a question, an answer, and the retrieved context that was used to generate the answer,
evaluate whether every factual claim in the answer is supported by the context.

Question: {question}

Answer: {answer}

Context:
{context}

Rate faithfulness from 0.0 to 1.0:
- 1.0: All claims are directly supported by the context
- 0.5: Some claims are supported, some are not or are extrapolated
- 0.0: Claims contradict or are absent from the context

Respond with JSON only: {{"score": <float>, "reason": "<one sentence>"}}"""

_RELEVANCY_PROMPT = """\
Given a question and an answer, evaluate how well the answer addresses the question.

Question: {question}
Answer: {answer}

Rate answer relevancy from 0.0 to 1.0:
- 1.0: Answer directly and completely addresses the question. Also 1.0 if the question is speculative, out-of-scope, or unanswerable and the answer correctly says so.
- 0.5: Answer is partially relevant or incomplete
- 0.0: Answer does not address the question at all

Respond with JSON only: {{"score": <float>, "reason": "<one sentence>"}}"""

_CONTEXT_PRECISION_PROMPT = """\
Given a question and a list of retrieved context chunks (in retrieval order), judge whether
each chunk is relevant and useful for answering the question.

Question: {question}

Retrieved context chunks (in order):
{context_list}

For each chunk output true (relevant) or false (not relevant), in the same order.

Respond with JSON only:
{{
  "relevance": [true, false, ...],
  "reason": "<one sentence>"
}}"""

_CONTEXT_RECALL_PROMPT = """\
You are evaluating whether retrieved context contains the information needed to answer a question.

Question: {question}
Ground truth: {ground_truth}

Retrieved context:
{context}

Instructions:
1. List every distinct atomic fact in the ground truth (one per line, keep them short).
2. For each fact, write true if the context explicitly supports it, false if not.
3. Compute score = (number of true) / (total facts).

Respond with JSON only:
{{
  "atomic_facts": ["<fact1>", "<fact2>", ...],
  "supported": [true, false, ...],
  "score": <float 0.0-1.0>,
  "reason": "<one sentence>"
}}"""


# ── 工具函数 ───────────────────────────────────────────────

def _format_context_flat(context: list[dict], max_chars: int = 10000) -> str:
    parts = []
    total = 0
    for i, item in enumerate(context, 1):
        src = item["source"]
        if src == "sec_chunks":
            text = f"[{i}][SEC {item.get('doc_type','')} FY{item.get('fiscal_year','')}] {item['content'][:600]}"
        elif src == "events":
            text = f"[{i}][Event {item.get('date','')}] {item.get('title','')}: {item.get('description','')[:400]}"
        elif src == "price_history":
            if item.get("_granularity") == "monthly":
                pe = f" avg_P/E={item['avg_pe']:.1f}" if item.get("avg_pe") else ""
                text = f"[{i}][Price {item.get('ticker','')} {item.get('date','')} monthly] close={item.get('close','')} avg={item.get('avg_close','')}{pe}"
            else:
                pe = f" P/E={item['pe_ratio']:.1f}" if item.get("pe_ratio") else ""
                text = f"[{i}][Price {item.get('ticker','')} {item.get('date','')}] close={item.get('close','')}{pe}"
        elif src == "earnings_history":
            text = (
                f"[{i}][Earnings {item.get('ticker','')} FY{item.get('fiscal_year','')}Q{item.get('fiscal_quarter','')}]"
                f" EPS={item.get('eps_actual','N/A')} est={item.get('eps_estimate','N/A')}"
                f" surprise={item.get('eps_surprise_pct','N/A')}%"
            )
        else:
            title = item.get('title') or item.get('series_id', '')
            text = f"[{i}][Macro {title} ({item.get('series_id','')}) {item.get('date','')}] {item.get('value','')} {item.get('units','')}"
        parts.append(text)
        total += len(text)
        if total > max_chars:
            break
    return "\n\n".join(parts)


def _format_context_list(context: list[dict], max_items: int = 25) -> str:
    lines = []
    for i, item in enumerate(context[:max_items], 1):
        src = item["source"]
        if src == "sec_chunks":
            lines.append(f"[{i}] SEC {item.get('doc_type','')} FY{item.get('fiscal_year','')} | {item['content'][:300]}")
        elif src == "events":
            lines.append(f"[{i}] Event | {item.get('title','')[:100]}")
        elif src == "price_history":
            if item.get("_granularity") == "monthly":
                pe = f" avg_P/E={item['avg_pe']:.1f}" if item.get("avg_pe") else ""
                lines.append(f"[{i}] Price {item.get('ticker','')} {item.get('date','')} (monthly) close={item.get('close','')} avg={item.get('avg_close','')}{pe}")
            else:
                pe = f" P/E={item['pe_ratio']:.1f}" if item.get("pe_ratio") else ""
                lines.append(f"[{i}] Price {item.get('ticker','')} {item.get('date','')} close={item.get('close','')}{pe}")
        elif src == "earnings_history":
            lines.append(
                f"[{i}] Earnings {item.get('ticker','')} FY{item.get('fiscal_year','')}Q{item.get('fiscal_quarter','')} "
                f"EPS={item.get('eps_actual','N/A')} est={item.get('eps_estimate','N/A')} "
                f"surprise={item.get('eps_surprise_pct','N/A')}%"
            )
        else:
            title = item.get('title') or item.get('series_id', '')
            lines.append(f"[{i}] Macro {title} ({item.get('series_id','')}) {item.get('date','')} = {item.get('value','')} {item.get('units','')}")
    return "\n".join(lines)


def _call_judge(llm: LLMClient, prompt: str) -> dict:
    raw = llm.chat(
        system="You are a precise evaluation judge. Always respond with valid JSON only.",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
        temperature=0.0,
    )
    if not raw:
        raise ValueError("Judge returned empty response")
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        raw = match.group(0)
    return json.loads(raw)


# ── 公开指标函数 ───────────────────────────────────────────

def faithfulness(question: str, answer: str, context: list[dict], llm: LLMClient) -> dict:
    prompt = _FAITHFULNESS_PROMPT.format(
        question=question,
        answer=answer,
        context=_format_context_flat(context),
    )
    return _call_judge(llm, prompt)


def answer_relevancy(question: str, answer: str, llm: LLMClient) -> dict:
    prompt = _RELEVANCY_PROMPT.format(question=question, answer=answer)
    return _call_judge(llm, prompt)


def context_precision(question: str, context: list[dict], llm: LLMClient) -> dict:
    prompt = _CONTEXT_PRECISION_PROMPT.format(
        question=question,
        context_list=_format_context_list(context),
    )
    r = _call_judge(llm, prompt)
    relevance: list[bool] = r.get("relevance", [])
    if not relevance:
        return {"score": 0.0, "useful_count": 0, "total_count": 0, "reason": r.get("reason", "")}

    # Precision@K: Σ(P@k × rel_k) / Σ(rel_k)
    numerator = 0.0
    denominator = 0.0
    for k, rel in enumerate(relevance, 1):
        if rel:
            precision_at_k = sum(1 for v in relevance[:k] if v) / k
            numerator += precision_at_k
            denominator += 1
    score = numerator / denominator if denominator > 0 else 0.0
    return {
        "score": score,
        "useful_count": int(denominator),
        "total_count": len(relevance),
        "reason": r.get("reason", ""),
    }


def context_recall(question: str, ground_truth: str, context: list[dict], llm: LLMClient) -> dict:
    prompt = _CONTEXT_RECALL_PROMPT.format(
        question=question,
        ground_truth=ground_truth,
        context=_format_context_flat(context),
    )
    r = _call_judge(llm, prompt)
    # LLM 直接在 JSON 里算好 score；若 LLM 漏算则用 supported/atomic_facts 自行计算
    if "score" not in r:
        facts = r.get("atomic_facts", [])
        supported = r.get("supported", [])
        total = len(facts)
        r["score"] = sum(1 for v in supported if v) / total if total > 0 else 0.0
    return r


def evaluate_all(
    question: str,
    ground_truth: str,
    answer: str,
    context: list[dict],
    llm: LLMClient,
) -> dict:
    """一次性计算全部四个指标，返回汇总 dict。"""
    results = {}
    for name, fn, kwargs in [
        ("faithfulness",      faithfulness,      {"question": question, "answer": answer, "context": context, "llm": llm}),
        ("answer_relevancy",  answer_relevancy,  {"question": question, "answer": answer, "llm": llm}),
        ("context_precision", context_precision, {"question": question, "context": context, "llm": llm}),
        ("context_recall",    context_recall,    {"question": question, "ground_truth": ground_truth, "context": context, "llm": llm}),
    ]:
        try:
            r = fn(**kwargs)
            results[name] = r.get("score", 0.0)
            results[f"{name}_reason"] = r.get("reason", "")
        except Exception as e:
            results[name] = None
            results[f"{name}_reason"] = str(e)

    valid = [v for v in [results.get(k) for k in ["faithfulness","answer_relevancy","context_precision","context_recall"]] if v is not None]
    results["ragas_score"] = sum(valid) / len(valid) if valid else None
    return results
