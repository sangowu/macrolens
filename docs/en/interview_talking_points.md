# MacroLens — Interview Talking Points

> Structure for each talking point: **Observation/Problem → My Decision → Supporting Data → Counter-argument**
>
> Interview principle: proactively naming limitations is a senior engineer signal — don't only talk about strengths.

---

## TP-1: Why pgvector Instead of Pinecone / ChromaDB?

**Decision:** The core challenge in financial RAG is not vector retrieval — it's coordinating three query types:
1. Vector similarity ("find semantically similar earnings report sections")
2. Exact time filtering ("only look at FY2022 data")
3. Precise numerical lookup ("what was FEDFUNDS on 2022-03-01")

pgvector lets all three happen within a single PostgreSQL transaction — no cross-system synchronization needed.

**Data:** `macro_indicators` exact queries run in <1ms. `sec_chunks` vector search + `fiscal_year` filter happen in the same SQL statement — no application-side joins.

**Counter-argument:** pgvector HNSW has lower recall than specialized vector databases at billion-scale. But GOOGL's 5-year earnings report chunks are well under 5,000 records — pgvector is completely sufficient, and introducing Pinecone would only add synchronization complexity.

---

## TP-2: Why PER Loop Instead of ReAct?

**Decision:** PER Loop (Plan→Execute→Critique→Synthesize) has a fixed structure with at most 7 LLM calls (3×Planner + 3×Critic + 1×Synthesizer). In financial Q&A testing, ReAct either stops too early (missing evidence) or too late (burning tokens) — because it lets the LLM decide when to stop.

**Data:** Simple questions take 3 LLM calls; complex ones take 7. Average latency: 8–15 seconds.

**Counter-argument:** PER Loop has limited expressiveness for open-domain questions. But financial Q&A is a closed domain with structured questions — a fixed Plan→Execute→Critique loop covers the space completely.

---

## TP-3: Why Fixed 512/128 Chunking Instead of Semantic Chunking?

**Decision:** Results from an ablation study (3 most recent 10-Ks, Set A question set):

| Strategy | Precision | Recall | Avg Tokens |
|----------|-----------|--------|------------|
| Fixed 512/128 | **0.062** | 0.250 | 482 |
| Recursive | 0.016 | 0.250 | 516 |
| Semantic (0.75) | 0.000 | **0.375** | 198 |

Semantic chunking has the highest recall, but Precision=0 — it produces 4× more chunks, the small vectors have unstable quality, and RRF rankings become noise. Fixed-size chunks are consistent, RRF scores are comparable, and Precision is highest.

**Counter-argument:** Semantic chunks are theoretically more semantically coherent. But the retrieval target is "a chunk that can answer the question," not "a chunk with coherent narrative" — these two semantic spaces don't align.

---

## TP-4: Embedding Model Selection

**Experimental comparison** (full RAGAS, three question sets):

| Model | Set A | Set B | Set C | Deployment |
|-------|-------|-------|-------|-----------|
| BGE-M3 (dim=1024) | 0.669 | 0.420 | 0.667 | AutoDL + SSH tunnel |
| Qwen3-Embedding-0.6B (dim=1024) | 0.654 | 0.395 | 0.602 | ModelScope API |

BGE-M3 performs better overall, especially on Set B (temporal reasoning) by +2.5 points. But Qwen3 doesn't require an SSH tunnel and is always available.

**Design highlight:** Via the `EmbeddingBackend` Protocol + Factory pattern, switching models requires only one line change in `config.yaml` — zero code changes. When dimensions match, the database doesn't need to be rebuilt. But different models have different vector spaces — you must re-ingest (a trap I fell into).

---

## TP-5: How Did You Solve the Synthesizer Hallucination Problem? (Three Iterations)

**Symptom:** Multiple questions had `faithfulness=0` in RAGAS evaluation. The Synthesizer was fabricating plausible numbers or causal inferences when context was insufficient.

**Round 1 Fix (v12 early): Soft constraint → hard rules**

Converted "do not fabricate" into three hard rules: every number must have a `[n]` citation / explicitly say "context does not contain X" when missing / background knowledge cannot supplement context. Result: ~+0.03 faithfulness improvement.

**Round 2 Fix (v15 Observation #21): Causal hallucination**

B01 faithfulness=0 root cause wasn't outright fabrication — the Synthesizer used training-knowledge causal inference and then attached real context citation numbers as cover. "FEDFUNDS rose AND ad revenue declined" → LLM internally concluded causation → found real [39][40] in context to cite.

Split Rule 1 into two separate rules: **NUMBERS AND DATES** (figures must appear verbatim in the cited source) and **CAUSAL CLAIMS** (causal statements require the context to explicitly state the mechanism — correlation ≠ causation; if no explicit statement exists, must write "context does not establish a direct causal link").

**Round 3 Fix (v15 Observation #22): Critic's gap diagnosis never reached Synthesizer**

A04's Critic reported "context does not contain Google Cloud annual revenue" across all three iterations. The Synthesizer still output $33,088M. Root cause: `per_loop.py` didn't pass `missing_hint` to `synthesize()` — the Synthesizer was completely unaware of the Critic's diagnosis.

Fix: format Critic's `missing_hint` as a `RETRIEVAL GAP` block placed **at the very top of the user message** (before the context). Testing showed placing it at the end was ineffective — high-confidence training priors override trailing instructions. Placing it first works.

**Final result (v14 → v15c):** faithfulness 0.710 → 0.897 (+0.187), new all-time high.

**Likely follow-up question:** "Why does placement matter?" → LLM attention decays over long context. A trailing instruction has lower priority than a high-confidence training prior. An explicit constraint at the top of the message fires before the generation begins.

---

## TP-6: Multi-Turn Retrieval — The Critic Dead Loop Problem

**Symptom:** Logs showed the Critic giving the same missing reason three consecutive turns. The Planner's second iteration repeated the first iteration's sub-queries exactly. New context = 0.

**Root Cause:** In the second iteration, the Planner only knows "what is missing" — not "what has already been searched." Without that, it cannot generate genuinely different retrieval strategies.

**Fix:** Added an `already_searched` list to every iteration's prompt:
```
Focus on what's still missing: {missing_hint}
Already searched (do NOT repeat): ["Federal Reserve rate hikes...", "Google advertising revenue..."]
```

**Result:** The second-iteration Planner began exploring new dimensions (macro_shock, GDP, RSAFS). New context rose from 0 to 16–26 chunks. Set B +0.029.

**Note:** The Critic still gives the same missing reason across all three rounds — this is a data-layer ceiling. SEC filings genuinely do not contain direct causal analysis linking interest rates to ad revenue. That type of content exists only in analyst reports, and this is expected behavior.

---

## TP-7: Why Not LangChain?

**Decision:** Pure Python orchestration, with each component independently testable.

- `planner.py`: test prompt output JSON format in isolation
- `executor.py`: test RRF SQL recall results in isolation
- `critic.py`: test sufficiency judgment accuracy in isolation

LangChain's abstraction layer hides all of these debugging paths.

**Counter-argument:** If more tools are needed in the future (web search, code interpreter), LangChain/LangGraph's ecosystem would save time. But over-framing at the MVP stage is a burden, not an advantage.

---

## TP-8: What Are the System's Limitations?

> Proactively naming limitations is a senior signal — interviewers will follow up, so better to have the answers prepared.

1. **No causal analysis:** SEC filings don't contain direct causal chains like "rate hike → ad revenue decline." This is the data ceiling for B01-type questions — not fixable in code. Analyst reports (paid data sources) would be needed.
2. **context_recall still below v12 baseline (0.519 vs 0.651):** D03 ground_truth key_facts include computed values ("425 basis points", Pearson coefficient) that don't exist as raw values in the database — recall cannot improve through retrieval alone; requires revising eval set design.
3. **answer_relevancy decline (0.872 vs 0.972):** The RETRIEVAL GAP mechanism causes the system to answer "context does not contain X" when retrieval fails, which the Judge penalizes on relevancy. The root fix is improving SEC chunk retrieval stability.
4. **Manual event library:** 30 hand-labeled entries. Scaling requires automated event extraction (NER + news crawler).
5. **High latency:** 8–15s/query, mainly from 3 LLM calls + 2 embedding API calls. Production would need streaming + async.
6. **Small evaluation set:** 23 questions (Set A/B/C/D). Statistical significance is insufficient — needs 100+ examples.

---

## TP-9: How Would You Scale to Production?

**Storage:** pgvector HNSW supports millions of vectors. Partitioned tables by `fiscal_year` can further improve performance.

**Latency:**
- Swap Planner/Critic for faster, smaller models (Haiku/Flash)
- Keep Synthesizer on a large model
- Streaming output reduces perceived latency

**Data updates:** FRED data updates monthly, SEC filings quarterly. `ON CONFLICT DO UPDATE` ensures idempotency — incremental ingest runs are safe to schedule.

**Multi-company:** The schema already has a `company` field. Adding a new CIK to ingestion is all that's needed — the evaluation set would need to expand accordingly.

---

## TP-10: Why Code Executor Instead of Letting the LLM Calculate?

**Decision:** Multi-step numerical computation is one of the highest-risk hallucination scenarios for LLMs. Before adding the compute tool, one category of faithfulness=0 cases involved the Synthesizer correctly extracting numbers from context but computing the growth rate incorrectly "in its head."

**Implementation:** The Synthesizer uses `chat_agentic()` in a multi-turn loop. While writing an answer, when derived computation is needed, it calls the `compute` tool directly. The sandbox executes Python and returns the result inline — no tag parsing, no second-pass substitution.

```
LLM: "Revenue grew "
→ calls compute(code="print(f'{(224.5/182.5-1)*100:.1f}%')")
← sandbox returns: "23.0%"
LLM: "Revenue grew 23.0% YoY [1]"
```

**Data:** Every derived number has two layers of verification — source citation `[n]` + executable code. Results can be independently validated, without relying on LLM reasoning.

**Sandbox design:** Whitelisted builtins, pre-injected `pd/np/math/statistics`, no `import` allowed, 15-second timeout.

**Why the old `<compute>` tag approach failed:** The Synthesizer would sometimes place tags between paragraphs, making the execution result an isolated line. Regex extraction was also fragile against LLM output formatting variations. The agentic loop eliminates post-processing entirely.

---

## TP-11: Evaluation Methodology — Why Not Use the RAGAS Library?

**Decision:** Custom LLM-as-Judge, not the official `ragas` library.

**Why:** Interface incompatibility. `ragas` requires a LangChain `BaseLLM` interface and `context: list[str]` format. This project uses a custom `LLMClient` Protocol with `context: list[dict]` (containing source, fiscal_year, series_id, etc.). The adaptation cost exceeded the cost of implementing it directly — and the custom implementation can expose these structural fields directly to the Judge.

**Four metrics implemented:**
- `faithfulness`: Does every claim in the answer have context support?
- `answer_relevancy`: Does the answer actually address the question?
- `context_precision`: Precision@K — judges each chunk by retrieval rank, computes rank-weighted precision
- `context_recall`: Atomic fact decomposition — breaks ground truth into independent claims, checks if each is covered by context

**Bugs encountered (mention proactively in interviews):**

*Bug 1: Eval script reimplemented PER Loop without `already_searched`*
`run_eval.py` had a `_run_with_context_capture` function that independently reimplemented the PER Loop but was missing the "already searched queries" list in the second-iteration Planner prompt. Multi-hop question (Set B) scores were low, and it was unclear whether the pipeline or eval was the culprit. Fix: replaced 32 lines of redundant code with a direct call to `per_loop.run()`.

*Bug 2: Judge received only 3,000 characters of context*
The original truncation was 3,000 chars / 15 chunks — less than 5% of the full context. The faithfulness and recall judges were giving inaccurate low scores because they couldn't see the complete context. Fix: expanded to 10,000 chars / 25 chunks.

*Bug 3: Weak model (Flash Lite) as Judge produced recall=0*
Macro context displayed `UNRATE: 3.4`. The ground truth said "US unemployment rate was 3.4%." Flash Lite couldn't reliably map `UNRATE` to "unemployment rate," giving recall=0 on clearly correct answers. Fix: enriched context format to include the full series title (`Unemployment Rate (UNRATE)`) and switched the Judge to Gemini 2.5 Pro.

*Bug 4: Gemini 2.5 Pro `resp.text` returned None*
The thinking model returns `resp.text = None` for certain prompts. The `.strip()` call raised an AttributeError. Downstream, the f-string in `ragas_score` also crashed, triggering the except branch and writing a duplicate row — one question, two CSV rows. Two fixes: `(resp.text or "").strip()` + safe formatting function.

*Bug 5: Relevancy judge penalized correct refusals (C03)*
C03 asked "If the Fed cuts rates to zero, what would happen to Google's stock price?" — unanswerable from historical filings. The system correctly refused. The judge gave 0.0 because "the answer doesn't answer the question." Root cause: the 1.0 definition in the prompt only described "directly and completely answers the question" — no provision for appropriate refusals. Fix: added "correctly stating a question is unanswerable also scores 1.0." C03 relevancy 0.0 → 1.0.

*Bug 6: Synthesizer used background knowledge to fill missing context (B01/B02 low faithfulness)*
B02 asked about COVID-19's impact on Google's 2020 revenue. Despite the "do not use background knowledge" constraint, the Synthesizer still output "American Rescue Plan," "shift to less commercial topics," etc. — content not in the context. Root cause: soft constraints lose against the LLM's tendency to generate complete narrative. Fix: added Rule 5 — "Your general knowledge does not exist for this query." B02 faithfulness improved partially (0.20 → 0.30).

**B01/B02 faithfulness data ceiling:** Causal questions ("how did rate hikes affect ad revenue") cannot be directly answered from SEC filings — only raw numbers exist. The Synthesizer must either infer causality (penalized by a strict judge) or refuse to infer (answer quality degrades). This is a structural limitation of the data source, not a fixable code issue. Adding analyst reports as a data source would be the real solution.

**Final results (v1 → v12 → v13 → v15c):**

| Metric | v1 | v12 | v13 | **v15c** | v1→v15c Δ |
|--------|----|----|-----|---------|---------|
| context_precision | 0.174 | 0.627 | 0.571 | **0.696** | +0.522 |
| context_recall | 0.471 | 0.657 | 0.590 | 0.519 | +0.048 |
| faithfulness | 0.618 | 0.534 | 0.713 | **0.897** | **+0.279** |
| answer_relevancy | 1.000 | 0.951 | 0.930 | 0.872 | -0.128 |
| ragas_score | 0.566 | 0.698 | 0.707 | **0.753** | **+0.187** |

v15c faithfulness all-time high (0.897): three rounds of hallucination fixes — hard rules + causal claim separation + Critic→Synthesizer information flow. ragas_score 0.753 surpasses the previous v12 record of 0.741.

**Two new eval bugs in v15 (mention proactively):**

*Bug 7: yfinance 1.3.0 silent API change caused empty earnings data*
`tk.quarterly_earnings` returns `None` in yfinance 1.3.0 with no exception raised — all ingested eps_actual/eps_estimate/pe_ratio values were NULL. D01 (P/E valuation) and D02 (EPS beat/miss) scored zero in eval. Detected by: directly querying `SELECT COUNT(pe_ratio) FROM price_history` → returned 0. Fix: switched to `tk.get_earnings_dates(limit=40)` — EPS coverage expanded from 6 NULL rows to 50 rows (2014–2026), pe_ratio fully populated. **Lesson:** After any third-party API upgrade, verify non-null rates on critical fields — don't just check row counts.

*Bug 8: Critic missing_hint was treated as a logging artifact, not architecture data*
The Critic accurately identified "context missing X" on every iteration, but `per_loop.py` only passed `missing_hint` to the next Planner iteration for query refinement — never to the final Synthesizer. The Synthesizer had no awareness of the Critic's diagnosis and filled the gap with training priors. Detected by: comparing Critic output against Synthesizer citations in verbose mode. This is not an LLM problem — it's an architectural data-flow design flaw. The Critic→Synthesizer path must be explicit.

---

## TP-12: Async Task Agent + Research Memory

**Decision:** Chat mode is stateless Q&A. Task mode adds two distinct capabilities:

**Async tasks:** User submits a question → PostgreSQL tasks table → Worker executes PER Loop in background → generates a structured markdown report. `SELECT FOR UPDATE SKIP LOCKED` supports multiple concurrent workers without duplication.

**Research Memory:** After a task completes, the LLM extracts 2–4 key findings and stores them in `research_memory` (pgvector embedding). At the start of the next task, similarity search retrieves relevant historical findings and injects them into Planner context.

**Results:**
- First query "2022 FEDFUNDS changes" → stored finding: "FEDFUNDS rose from 0.08% to 4.1%, +402 bps"
- Second query "impact of rate hikes on Google ad revenue" → previous finding automatically injected into context

**Counter-argument:** Not using LangGraph / Mem0 or other frameworks — pure handwritten task queue and memory layer. Reason: the PER Loop is a bounded 4-step flow and doesn't need a complex graph structure. The memory layer is domain-specific structured storage where a general-purpose framework would reduce control. Building it from scratch makes it easier to explain every line of code in an interview.

---

## One-Minute Elevator Pitch

> For opening a technical interview or HR screen

"MacroLens is a financial research agent covering all MAG7 tech companies — distinct from a typical RAG chatbot in three ways. First, every number in an answer has two layers of verification: a source citation traceable back to SEC filings, price history, or earnings tables, plus executable Python code — growth rates, CAGRs, correlation coefficients are computed by code, not inferred by the LLM. Second, async task mode — users submit analysis tasks and the agent executes autonomously in the background to produce a structured markdown report, rather than an instant chat reply. Third, cross-session research memory — each completed task extracts key findings into a vector database, and future tasks automatically retrieve relevant history; the agent has cognitive continuity. The system covers five data sources: SEC filings, FRED macro indicators, daily price history, quarterly EPS with estimates, and hand-labeled events. It's pure Python with no LangChain, every component independently testable, with a custom LLM-as-Judge evaluation framework backing every design decision. ragas_score improved from 0.566 to 0.753 across 15 evaluation versions, with faithfulness reaching 0.897 — a new all-time high."
