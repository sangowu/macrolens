"""
评估指标。

LLM-as-Judge（分数由 LLM 给出，存在漂移，不可复现）：
- faithfulness:       答案中的声明是否都有 context 支撑？(0-1)
- answer_relevancy:   答案是否切题？(0-1)
- context_precision:  检索到的 context 中有多少比例真正有用？(0-1)
- context_recall:     关键事实是否被 context 覆盖？(0-1)

确定性判分（LLM 只做抽取，分数由 Python 算，同一答案跑十次分数相同）：
- answer_correctness: 答案说的数值和真值表对不对得上？(0-1)

前四个指标合起来也答不了"答案对不对"——一个忠实复述了错误 context 的
答案能拿满分 faithfulness。answer_correctness 是唯一消费数值 GT 的指标。
"""
from __future__ import annotations

import json
import re

from eval.questions import ExpectedValue
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

Important: Values derived by a calculation/compute tool from numbers that ARE present in the context (e.g., correlation coefficient, growth rate, CAGR computed from raw prices or revenues in context) count as supported, provided the underlying input data is present in the context.

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


# ── answer_correctness：LLM 只当 parser，判分在 Python ──────

_EXTRACTOR_SYSTEM = """\
You are a value extractor for an evaluation harness. You do NOT judge correctness.

Given a question and an answer produced by a RAG system, extract the numeric value that
the ANSWER STATES for each requested field.

Critical rules:
1. Extract what the answer ASSERTS, not what is actually true. If the answer is wrong,
   extract its wrong number. Never correct it. Never substitute a value you believe is right.
2. Match by semantic ROLE, not by position or proximity. If the answer says
   "EPS came in at 1.45 versus estimates of 1.55", then the reported/actual EPS is 1.45
   and the estimate is 1.55 — even if that ordering looks backwards to you.
3. Return the bare number as a string: strip currency symbols, commas, units and signs
   that are part of formatting. "$1.55" -> "1.55". "7.05%" -> "7.05". "1,234" -> "1234".
   Keep a genuine minus sign: "down 3.2%" stated as a negative -> "-3.2".
4. If the answer does not state a value for a field, return the literal string "null".
   Do NOT guess, do NOT compute it yourself, do NOT infer it from another field."""


def _build_extract_tool(expected: dict[str, ExpectedValue]) -> dict:
    """按 expected 的字段动态生成抽取 tool schema。

    字段类型用 string 而非 number：Gemini 的 Schema 不支持 ["number","null"]
    union，用 string + Python 侧解析既跨 provider 稳定，又把解析逻辑
    留在确定性代码里。
    """
    properties = {
        name: {
            "type": "string",
            "description": f"{ev.description}. Bare number as a string, or \"null\" if the answer does not state it.",
        }
        for name, ev in expected.items()
    }
    return {
        "name": "extract_values",
        "description": "Report the numeric values that the answer states for each field.",
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
        },
    }


_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_number(raw) -> float | None:
    """把抽取器返回的字符串解析成 float。无法解析或表示缺失时返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().strip('"').replace(",", "")
    if not text or text.lower() in {"null", "none", "n/a", "na", "not stated", "not provided"}:
        return None
    # 抽取器偶尔会留下 "$1.55" / "1.55 USD"，兜底取第一个数值
    match = _NUMBER_RE.search(text)
    return float(match.group(0)) if match else None


def answer_correctness(
    question: str,
    answer: str,
    expected: dict[str, ExpectedValue],
    llm: LLMClient,
) -> dict:
    """答案陈述的数值 vs 真值表，逐字段 tolerance 比对。

    LLM 只负责"答案里这个字段说的是哪个数"，打分完全在 Python：
    score = 命中字段数 / 总字段数。字段未提及 (None) 记为 miss。
    """
    tool = _build_extract_tool(expected)
    user_msg = f"Question: {question}\n\nAnswer to extract from:\n{answer}"

    extracted_raw = llm.chat_with_tools(
        system=_EXTRACTOR_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        tools=[tool],
        tool_choice={"type": "tool", "name": "extract_values"},
        max_tokens=512,
        temperature=0.0,
    )

    fields: dict[str, dict] = {}
    hits = 0
    for name, ev in expected.items():
        got = _parse_number(extracted_raw.get(name))
        if got is None:
            status, delta = "missing", None
        else:
            delta = abs(got - ev.value)
            status = "hit" if delta <= ev.tolerance else "wrong"
            if status == "hit":
                hits += 1
        fields[name] = {
            "expected": ev.value,
            "got": got,
            "tolerance": ev.tolerance,
            "delta": delta,
            "status": status,
        }

    score = hits / len(expected) if expected else 0.0
    misses = [f"{n}(exp={d['expected']}, got={d['got']})" for n, d in fields.items() if d["status"] != "hit"]
    reason = "all fields match" if not misses else "; ".join(misses)
    return {"score": score, "fields": fields, "reason": reason}


def evaluate_all(
    question: str,
    ground_truth: str,
    answer: str,
    context: list[dict],
    llm: LLMClient,
    expected: dict[str, ExpectedValue] | None = None,
) -> dict:
    """计算全部指标，返回汇总 dict。

    expected 为 None 时跳过 answer_correctness（Set A-D 手写题没有数值 GT）。
    ragas_score 只聚合四个 LLM-judge 指标，保持与历史版本口径可比；
    answer_correctness 单列，不混进去。
    """
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

    if expected:
        try:
            r = answer_correctness(question, answer, expected, llm)
            results["answer_correctness"] = r["score"]
            results["answer_correctness_reason"] = r["reason"]
            results["answer_correctness_fields"] = json.dumps(r["fields"])
        except Exception as e:
            results["answer_correctness"] = None
            results["answer_correctness_reason"] = str(e)
            results["answer_correctness_fields"] = ""
    else:
        results["answer_correctness"] = None
        results["answer_correctness_reason"] = ""
        results["answer_correctness_fields"] = ""

    return results
