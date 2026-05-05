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
- 1.0: Answer directly and completely addresses the question
- 0.5: Answer is partially relevant or incomplete
- 0.0: Answer does not address the question

Respond with JSON only: {{"score": <float>, "reason": "<one sentence>"}}"""

_CONTEXT_PRECISION_PROMPT = """\
Given a question and a list of retrieved context chunks, evaluate what fraction of the chunks
are actually relevant and useful for answering the question.

Question: {question}

Retrieved context chunks:
{context_list}

Rate context precision from 0.0 to 1.0:
- 1.0: All retrieved chunks are relevant
- 0.5: About half the chunks are relevant
- 0.0: No chunks are relevant

Respond with JSON only: {{"score": <float>, "useful_count": <int>, "total_count": <int>, "reason": "<one sentence>"}}"""

_CONTEXT_RECALL_PROMPT = """\
Given a question, the ground truth answer, and retrieved context chunks,
evaluate whether the context contains the key information needed to answer the question.

Question: {question}
Ground truth: {ground_truth}

Retrieved context:
{context}

Rate context recall from 0.0 to 1.0:
- 1.0: Context contains all key facts needed to answer
- 0.5: Context contains some but not all key facts
- 0.0: Context is missing the key facts needed

Respond with JSON only: {{"score": <float>, "reason": "<one sentence>"}}"""


# ── 工具函数 ───────────────────────────────────────────────

def _format_context_flat(context: list[dict], max_chars: int = 3000) -> str:
    parts = []
    total = 0
    for i, item in enumerate(context, 1):
        src = item["source"]
        if src == "sec_chunks":
            text = f"[{i}][SEC {item.get('doc_type','')} FY{item.get('fiscal_year','')}] {item['content'][:300]}"
        elif src == "events":
            text = f"[{i}][Event {item.get('date','')}] {item.get('title','')}: {item.get('description','')[:200]}"
        else:
            text = f"[{i}][Macro {item.get('series_id','')} {item.get('date','')}] {item.get('value','')}"
        parts.append(text)
        total += len(text)
        if total > max_chars:
            break
    return "\n\n".join(parts)


def _format_context_list(context: list[dict], max_items: int = 15) -> str:
    lines = []
    for i, item in enumerate(context[:max_items], 1):
        src = item["source"]
        if src == "sec_chunks":
            lines.append(f"[{i}] SEC {item.get('doc_type','')} FY{item.get('fiscal_year','')} | {item['content'][:150]}")
        elif src == "events":
            lines.append(f"[{i}] Event | {item.get('title','')[:100]}")
        else:
            lines.append(f"[{i}] Macro {item.get('series_id','')} {item.get('date','')} = {item.get('value','')}")
    return "\n".join(lines)


def _call_judge(llm: LLMClient, prompt: str) -> dict:
    raw = llm.chat(
        system="You are a precise evaluation judge. Always respond with valid JSON only.",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=256,
        temperature=0.0,
    )
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
    return _call_judge(llm, prompt)


def context_recall(question: str, ground_truth: str, context: list[dict], llm: LLMClient) -> dict:
    prompt = _CONTEXT_RECALL_PROMPT.format(
        question=question,
        ground_truth=ground_truth,
        context=_format_context_flat(context),
    )
    return _call_judge(llm, prompt)


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
