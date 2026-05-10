# MacroLens — Bug & Fix Log

> A record of bugs encountered during development, their root causes, and the fixes applied. Useful for explaining "how to debug a RAG system" in technical interviews.

---

## Bug #1: UnicodeEncodeError on Special Characters

**Symptom**
```
UnicodeEncodeError: 'gbk' codec can't encode character '✓' in position 0
```

**Root Cause**
Windows terminal defaults to GBK encoding. When Python's `print()` outputs UTF-8 special characters (✓/✗), a codec error is raised.

**Fix**
Replaced all `✓` → `[OK]` and `✗` → `[ERR]` in ingestion scripts. Added `encoding='utf-8'` to all file read/write operations.

**Lesson:** In cross-platform projects developed on Windows, avoid non-ASCII characters in all console output.

---

## Bug #2: sec-parser Returns 3,838,828 Empty Nodes

**Symptom**
Parsing the GOOGL 10-K HTML file with `sec-parser` returns 3.8 million nodes, all with empty content.

**Root Cause**
GOOGL's SEC filing format (with `ix:` XBRL namespace tags) is incompatible with `sec-parser`'s expected HTML structure, causing the parser to fail completely.

**Fix**
Switched to `BeautifulSoup` for raw HTML parsing, manually extracting `<p>` / `<table>` elements. Added a regex-based section boundary detector to split by `Item X. Title` patterns.

```python
ITEM_BOUNDARY = re.compile(
    r"(?:^|\n)[ \t]*(Item[\s\xa0]+\d+[A-Za-z]?[\.\s\xa0]+[A-Z][^\n]{3,80})",
    re.MULTILINE
)
```

**Lesson:** Don't blindly trust third-party parsing libraries — always have a fallback for non-standard formats.

---

## Bug #3: `doc_type='GOOGL'` and `fiscal_year=NULL`

**Symptom**
After SEC chunk ingestion, all records show `doc_type = 'GOOGL'` and `fiscal_year = NULL`.

**Root Cause (dual issue)**
1. `parse_filing_meta()` used the wrong path depth: `filing_dir.parent.parent.name` (= `GOOGL`) instead of `filing_dir.parent.name` (= `10-K`).
2. Code tried to read `filing-details.json`, which doesn't exist. The real metadata is in `full-submission.txt`.

**Fix**
```python
# Before
doc_type = filing_dir.parent.parent.name  # GOOGL

# After
doc_type = filing_dir.parent.name         # 10-K

# Metadata source changed to full-submission.txt, extracted with regex
FILED_DATE_RE  = re.compile(r"FILED AS OF DATE:\s+(\d{8})")
PERIOD_END_RE  = re.compile(r"PERIOD OF REPORT:\s+(\d{8})")
```

**Lesson:** Print a few sample rows before full ingestion to validate metadata — don't wait until after to discover issues.

---

## Bug #4: `period_end NOT NULL` Constraint Error

**Symptom**
During re-ingestion, some older filings fail with a NOT NULL constraint violation on `period_end`.

**Root Cause**
Early SEC filings sometimes omit the `PERIOD OF REPORT` field in `full-submission.txt`.

**Fix**
```sql
ALTER TABLE sec_chunks ALTER COLUMN period_end DROP NOT NULL;
```
Updated the migration script and changed the ingestion code to default missing `period_end` to `None` instead of raising an error.

---

## Bug #5: `content_tsv` Column Does Not Exist in Events Table

**Symptom**
`column "content_tsv" does not exist` when executing event retrieval SQL.

**Root Cause**
The events table's full-text index column is named `description_tsv`, but the executor hardcoded the wrong column name `content_tsv`.

**Fix**
Updated event retrieval SQL in `agent/executor.py`:
```sql
-- Before
content_tsv @@ websearch_to_tsquery(...)
-- After
description_tsv @@ websearch_to_tsquery(...)
```

**Lesson:** Always look up column names from the migration SQL files — don't rely on memory.

---

## Bug #6: `macro_series_meta` Field Name Error

**Symptom**
Macro data retrieval fails with `column s.title does not exist` and `column s.units does not exist`.

**Root Cause**
The schema uses `name` and `unit` (singular), but the code used `title` and `units` (plural/alias).

**Fix**
Updated macro SQL in `agent/executor.py`:
```sql
-- Before
s.title, s.units
-- After
s.name, s.unit
```

---

## Bug #7: PER Loop Cannot Retrieve FY2022 SEC Data

**Symptom**
When asked about "Google 2022 advertising revenue," the PER Loop returns context with no FY2022 SEC chunks — the answer is hallucinated.

**Root Cause (triple compound)**
1. Section detection failure: after BeautifulSoup extraction, section headers with `\xa0` (non-breaking space) didn't match the regex — nearly all chunks were labeled `Business` or empty.
2. Executor contained a `section = ANY(...)` WHERE clause that filtered out `MD&A` — excluding almost everything.
3. `top_k=8` was too small; FY2022 nearest neighbors ranked 8+ and were cut off.

**Fix**
- Removed section filtering from the Executor SEC query — kept only `fiscal_year` filtering.
- Updated `config.yaml`: `top_k: 8 → 12`, `candidate_k: 15 → 20`.

**Follow-up:** `\xa0` was only the entry point — four compounding issues existed. See Bug #18.

---

## Bug #8: WinError 10061 — Connection Refused (SSH Tunnel Port Mismatch)

**Symptom**
```
WinError 10061: No connection could be made because the target machine actively refused it
```

**Root Cause**
`config.yaml` had `base_url: http://localhost:6006`, but the SSH tunnel was forwarding the remote service to local port `8000`.

**Fix**
Updated `config.yaml` to `base_url: http://localhost:8000`.

**Verification:** `netstat -an | findstr 8000` to confirm local port is listening.

---

## Bug #9: SSH Tunnel Password Authentication Not Supported

**Symptom**
The SSH tunnel auto-started by `factory.py` fails when using password authentication (AutoDL cloud server).

**Root Cause**
`SSHConfig` was missing an `ssh_port` field (AutoDL uses non-standard port 57671) and didn't support a `password_env` parameter.

**Fix**
Added new fields to `SSHConfig` in `models/config.py`, and updated tunnel setup logic in `factory.py`:
```python
class SSHConfig:
    ssh_port: int = 22              # new
    password_env: str | None = None  # new, reads from os.environ
```

---

## Bug #10: Chunk Ablation All Scores Are Zero

**Symptom**
Running `eval/chunk_ablation.py --files 3` completes with all three strategies scoring precision/recall = 0.

**Root Cause**
`iter_filing_files()` returns results in alphabetical order — taking the first 3 gives FY2015/2016/2017. But Set A questions ask about FY2021–2023, so the data is completely mismatched.

**Fix**
```python
# Before
files = [f for f in iter_filing_files() if "10-K" in str(f)][:args.files]

# After (take most recent N)
all_10k = [f for f in iter_filing_files() if "10-K" in str(f)]
files = sorted(all_10k, reverse=True)[:args.files]
```

---

## Bug #11: Synthesizer faithfulness=0 (Hallucination)

**Symptom**
Questions A01/A08/B04/B05/C02 score faithfulness=0. The Synthesizer generates plausible-sounding but unsourced numbers and dates when context is insufficient.

**Root Cause**
The SYSTEM_PROMPT only had a soft constraint: "Do not fabricate numbers or dates not present in the context." The LLM's tendency to generate complete-sounding answers overrides soft constraints.

**Fix**
Replaced the soft constraint with three hard rules in `agent/synthesizer.py`:
1. Every number/date/percentage must have a `[n]` citation — if no source exists, do not state it
2. If context is missing, explicitly say "The provided context does not contain [X]" — no inference, no estimation
3. Background knowledge may not supplement missing context

**Result:** All three question sets improved: Set A +0.031, Set B +0.024, Set C +0.028.

**Lesson:** Faithfulness constraints in RAG must be hard rules. "Every claim requires a citation" works best.

---

## Optimization #1: Critic Dead Loop — Same Missing Reason Across Iterations

**Symptom**
Logs show iterations 1 and 2 producing identical `missing` reasons. The second Planner generates sub-queries that heavily overlap with the first — only 4 new context items added.

**Root Cause**
`per_loop.py` only passed `missing_hint` to the second Planner prompt — it didn't include which queries had already been searched. The Planner had insufficient information to generate genuinely different retrieval strategies.

**Fix**
Appended the searched query list to every iteration's prompt:
```python
already = ", ".join(f'"{q}"' for q in searched_queries)
prompt = (
    f"{question}\n\n"
    f"Focus on what's still missing: {missing_hint}\n"
    f"Already searched (do NOT repeat these queries): [{already}]"
)
```

**Result:** Set B improved 0.366 → 0.395 (+0.029). The Planner now explores different dimensions each round; new context rose from 0 to 16–26 items.

---

## Optimization #2: Planner Section Filter Adjustment Experiment

**Symptom**
The Planner automatically adds `section: MD&A` filters to `sec_chunks` sub-queries, potentially excluding relevant content from other sections.

**Experiment Results**

| Approach | Set A | Set B | Set C | Conclusion |
|----------|-------|-------|-------|------------|
| Remove section filter entirely | 0.667 | 0.395 | 0.560 | Set C drops significantly |
| "Only filter if user explicitly mentions" | 0.654 | 0.395 | 0.602 | Set C partially recovers but unstable |

**Conclusion**
Section filtering helps for comprehensive analysis questions (Set C). Removing it entirely is harmful. Making it optional is the right direction, but the LLM's judgment on when to apply it is still unstable. Kept the "optional" strategy and accepted current performance — marginal returns are diminishing.

**Root cause:** Set C's drop is more attributable to Qwen3-Embedding-0.6B being weaker than BGE-M3, not the Planner itself.

---

## Bug #12: Gradio 6.x API Breaking Changes

**Symptom**
After upgrading to Gradio 6.14, the UI fails to start:
- `theme` parameter moved from `gr.Blocks()` to `launch()`
- `gr.Chatbot` no longer accepts `show_copy_button`, `bubble_full_width`, `type` parameters
- History format changed from `[[user, assistant]]` to `[{"role": "user", "content": ...}]`

**Fix**
- Moved `theme=gr.themes.Soft()` to the `demo.launch()` call
- Removed incompatible Chatbot parameters
- Updated history format to the messages dict format

**Lesson:** Major Gradio version upgrades (5→6) require checking the migration guide — don't assume backward compatibility.

---

## Bug #13: Eval Script Reimplemented PER Loop Without `already_searched`

**Symptom**
`run_eval.py` had an internal `_run_with_context_capture` function that independently reimplemented the Plan→Execute→Critique loop. Set B multi-hop question `context_recall` was low — unclear whether the pipeline or the eval was at fault.

**Root Cause**
`_run_with_context_capture`'s second-iteration prompt only appended `missing_hint` — it didn't include the `already_searched` list. The anti-repeat mechanism in `per_loop.py` was completely inactive during evaluation. The Planner repeated the first iteration's sub-queries; new context = 0, equivalent to running only 1 iteration.

**Fix**
Deleted the 32-line redundant function and replaced it with a direct call:
```python
answer, context = per_loop_run(q.question, cfg, conn, embedder, llm, max_iter=args.max_iter)
```

**Lesson:** Reimplementing the pipeline inside eval scripts is dangerous — any pipeline change requires a synchronous eval update, which is easily missed. Always test through the production code path.

---

## Bug #14: Judge Sees Only 5% of Full Context

**Symptom**
faithfulness and context_recall judges give unstable low scores even when answers are clearly correct.

**Root Cause**
`_format_context_flat` had a hard limit of `max_chars=3000`, truncating each SEC chunk to 300 characters. With 20–25 context items, 3000 characters covered less than 5% of the full content. Judges were making severely distorted judgments about whether "context supports the answer" because they couldn't see most of it.

**Fix**
- `max_chars` 3000 → 10000, each SEC chunk truncated 300 → 600 characters
- `_format_context_list` `max_items` 15 → 25, each item 150 → 300 characters

**Lesson:** LLM-as-Judge context window limits are a hidden source of evaluation bias. What the judge can't see doesn't exist — ensure the judge has sufficient information to evaluate accurately.

---

## Bug #15: Gemini 2.5 Pro `resp.text` Returns None — Duplicate CSV Rows

**Symptom**
The evaluation CSV has two rows per question, most metrics are empty. `ragas_score` sometimes shows `1.0` (only `answer_relevancy` succeeded).

**Root Cause (three-layer cascade)**
1. Gemini 2.5 Pro is a thinking model — `resp.text` returns `None` for certain prompts
2. `GeminiClient.chat()` called `resp.text.strip()` directly → `AttributeError: 'NoneType'`
3. `evaluate_all` caught the exception, set metrics to `None`, but `ragas_score` also became `None`
4. `run_eval.py` formatted with `f"RAGAS: {score:.3f}"` on `None` → `TypeError`
5. Outer `try/except` caught the `TypeError` and wrote a second empty row

**Fix**
```python
# gemini_client.py
return (resp.text or "").strip()   # guard against None

# metrics.py
if not raw:
    raise ValueError("Judge returned empty response")  # fail fast, clear error

# run_eval.py
def _fmt(v): return f"{v:.3f}" if v is not None else "None"  # safe formatting
```

**Lesson:** Thinking model response formats differ from standard models — validate `resp.text` edge cases when integrating a new model. Cascading exceptions (A crashes → B catches → C writes bad data) require defensive handling at every layer.

---

## Bug #16: `answer_relevancy` Judge Penalizes Correct Refusals (0.0)

**Symptom**
C03 ("If the Fed cuts rates to zero, what would happen to Google's stock price?") receives `answer_relevancy=0.0`. The system correctly refuses to answer a speculative question, but the judge says "the answer doesn't answer the question."

**Root Cause**
`_RELEVANCY_PROMPT` defined a score of 1.0 only as "directly and completely answers the question" — with no provision for appropriate refusals of unanswerable questions.

**Fix**
Added to the 1.0 definition in the prompt:
```
- 1.0: ... Also 1.0 if the question is speculative, out-of-scope, or unanswerable
       and the answer correctly says so.
```

**Result:** C03 answer_relevancy 0.0 → 1.0.

**Lesson:** Adversarial/boundary evaluation cases (Set C) have different definitions of "correct answer" than mainstream questions. Judge prompts must explicitly cover these scenarios — otherwise metrics will systematically underestimate correct behavior.

---

## Bug #17: Synthesizer Uses Background Knowledge to Fill Missing Context

**Symptom**
B02 (COVID-19's impact on Google's 2020 revenue) scores faithfulness=0.2. The judge flags that the answer contains "American Rescue Plan," "shift to less commercial topics," etc. — content not present in any retrieved context chunk.

**Root Cause**
The SYSTEM_PROMPT had three hard rules, but all focused on "don't write without a source." The LLM's tendency to produce complete narrative overrides soft constraints, especially for historically significant events (COVID impact, economic crises) where training data is rich.

**Fix**
Added Rule 5 to fundamentally cut the LLM's motivation to use general knowledge:
```
5. Your general knowledge about world events, economics, or companies does NOT exist
   for the purpose of this answer. If it is not in the retrieved context, it did not happen.
```

**Result:** B02 faithfulness 0.20 → 0.30 (partial improvement).

**Residual issue:** Causal questions like B01/B02 ("how did rate hikes affect ad revenue") cannot be directly answered from SEC filings — only isolated raw numbers exist. Rule 5 prevents narrative supplementation but cannot supply causal analysis that doesn't exist in the source data. The fundamental fix is adding analyst reports as a data source.

**Lesson:** For narrative-heavy events (COVID, economic crises), LLMs struggle to reliably distinguish "from context" vs "from training data." Synthesizer faithfulness has a systematic ceiling for this question type.

---

## Bug #18: Section Detection — Four Compounding Issues

**Symptom**
FY2022 10-K `sec_chunks` shows only 2–5 MD&A entries, 0 Risk Factors entries, and nearly all chunks labeled `Business`.

**Root Cause (four independent, compounding issues)**

1. **`\xa0` not normalized:** `soup.get_text()` preserves HTML `&nbsp;` as `\xa0`. The regex includes `\xa0` but HTML sometimes has multiple `\xa0` mixed with regular spaces, causing some headers to not match. **Fix:** `full_text.replace("\xa0", " ")`.

2. **No `re.IGNORECASE`:** 10-K body headers are ALL CAPS (`ITEM 7.\nMANAGEMENT'S DISCUSSION...`), while the table of contents uses mixed case (`Item 7. Management's...`). Without `re.IGNORECASE`, only TOC entries matched — all body headers were missed. **Fix:** Added `re.IGNORECASE`.

3. **TOC boundaries overwriting body boundaries:** `findall` returns matches in position order. TOC appears first (positions ~6000–7500). With a list preserving all matches, each Item appears once in TOC and once in the body — both positions recorded. Result: the TOC's `Item 7` to `Item 7A` span is only 2 chunks, and the actual MD&A body content is incorrectly attributed to the previous section. **Fix:** Used a `seen` dict to keep only the **last** occurrence of each item number (body > TOC).

4. **`SECTION_MAP` startswith mismatches:** `"item 1a...".startswith("item 1")` is True, causing all Item 1A (Risk Factors) content to be misattributed to Item 1 (Business). **Fix:** Changed to `re.match(rf"{re.escape(k)}[\s.]", section.lower())` with word boundary.

5. **Stale data accumulation:** `ON CONFLICT DO NOTHING` without a unique constraint is equivalent to always inserting. Old section-name chunks accumulate during re-ingestion. **Fix:** `TRUNCATE TABLE sec_chunks` before re-ingestion.

**Fix Results (chunk distribution)**

| Section | Before | After |
|---------|--------|-------|
| Business | ~535 | 12 |
| Risk Factors | 0 | 34 |
| MD&A | 2–5 | 30 |
| Financial Statements | 2–3 | 72 |

**Fix Results (RAGAS metrics, v11 → v12)**

| Metric | Before (v11) | After (v12) | Δ |
|--------|-------------|------------|---|
| faithfulness | 0.544 | **0.667** | **+0.123** |
| answer_relevancy | 0.944 | 0.972 | +0.028 |
| context_precision | 0.603 | 0.688 | +0.085 |
| context_recall | 0.587 | 0.651 | +0.064 |
| **ragas_score** | 0.670 | **0.741** | **+0.071** |

`faithfulness` improvement was largest (+0.123): before the fix, MD&A only had 2 chunks. The Synthesizer was retrieving mostly table-of-contents and boilerplate text, making it difficult to find real data for faithfulness verification. After the fix, 30 substantive MD&A chunks entered the retrieval pool.

**Lesson:** HTML documents (SEC 10-K) have two structural layers — table of contents + body. Text-based regex section detection easily matches both. Always prefer the **last** occurrence (body) over the first (TOC). Validate section distribution by querying the database immediately after ingestion — don't wait for anomalous eval scores to surface the issue.

---

## Observation #19: v13 New Data Sources Cause context_precision / context_recall Decline

**Version:** v12 → v13 (added price_history / earnings_history / MAG7 support)

**Symptom (v12 → v13 full comparison, including Set D)**

| Metric | v12 | v13 | Δ |
|--------|-----|-----|---|
| faithfulness | 0.534 | **0.713** | **+0.179** ✅ |
| answer_relevancy | 0.951 | 0.930 | -0.021 ⚠️ |
| context_precision | 0.627 | 0.571 | -0.056 ⚠️ |
| context_recall | 0.657 | 0.590 | -0.067 ⚠️ |
| **ragas_score** | 0.698 | **0.707** | +0.009 ✅ |

**Root Cause Analysis**

**faithfulness large improvement (+0.179):** price_history / earnings_history are structured numerical tables. When the LLM writes an answer, every number has a clear context source — the judge can verify claims one by one. Hallucination rate drops significantly. This is a direct benefit of the new data sources.

**context_precision decline (-0.056):** `_search_price_history()` returns daily-granularity data (one row per day) — a single query can pull 200–300 price records. When the judge evaluates Precision@K, it marks individual rows like "2022-03-15 close $2,850" as low-relevance (a single row can't independently answer "what is the correlation?"). This inflates the count of "irrelevant chunks," dragging down precision. The root cause is **retrieval granularity vs. judge evaluation granularity mismatch**.

**context_recall decline (-0.067):** Set D ground truths are primarily methodology-descriptive ("should compute P/E percentile and give an interval description"). When the judge decomposes atomic facts, the resulting claims tend to be "needs compute tool" rather than specific numbers — and a row of price data can't directly cover these claims. Root cause: **ground_truth design is descriptive rather than numerical**.

**answer_relevancy slight decline (-0.021):** Set D valuation/correlation question answers are structurally longer (contain compute code and historical interval descriptions). The judge's assessment of whether the answer is "directly on-topic" is slightly conservative.

**Improvement Directions**

1. **Precision fix (code-level):** Add monthly/quarterly aggregation to `_search_price_history()` — compress daily data into time-period summaries before sending to context. Estimated precision recovery to 0.62+.

```python
# Current: one row per day → 200 rows
# Improved: monthly aggregate → 20 rows, each with open/close/pe_ratio monthly averages
```

2. **Recall fix (evaluation-level):** Rewrite Set D ground truths as numerical (e.g., "Q3 2023 EPS actual=1.55, est≈1.45, surprise≈+6.9%") so the judge's atomic fact decomposition can directly compare against context values.

3. **Overall assessment:** faithfulness +0.179 is a substantive improvement (new data sources reduce hallucination). The precision/recall decline is primarily **evaluation method mismatch with the new data type**, not degraded retrieval quality. Priority: fix #1 (aggregation), then re-run v13 eval.

**Lesson:** When introducing structured time-series data (daily prices), evaluation metric adaptation must be considered simultaneously. Precision@K assumes each context item is an independent text chunk — it doesn't apply well to "one row = one time point" time-series data. When adding new data sources, review the `format_context` function in the eval script and consider aggregation before sending to the judge.

---

## Observation #20: yfinance 1.3.0 API Change Causes Empty earnings_history and NULL pe_ratio

**Version:** v13–v14 (data ingestion phase)

**Symptom**

- `earnings_history` table had only 6 rows (2024–2026), all `eps_actual` / `eps_estimate` NULL
- `price_history` 2,854 rows, `pe_ratio` all NULL (0/2,854 non-null)
- D01 (P/E historical valuation) and D02 (Q3 2023 EPS beat/miss) had context_items=0 or recall=0 in eval

**Root Cause**

`ingest_prices.py::fetch_earnings_history` used `tk.quarterly_earnings`, which was deprecated and now returns `None` in yfinance 1.3.0. The column name checks (`"Reported EPS"`, `"EPS Estimate"`) never execute, so all EPS fields remain NULL.

`compute_pe_ps_ratios` depends on a 4-quarter rolling TTM from `eps_actual` — with all NULL values, P/E can never be computed.

**Fix** (`ingestion/ingest_prices.py`)

- Replaced `tk.quarterly_earnings` with `tk.get_earnings_dates(limit=40)`
- New API columns: `['EPS Estimate', 'Reported EPS', 'Surprise(%)']`, index = earnings announcement date
- New `_ann_date_to_quarter_end()` maps announcement date to fiscal quarter end (Jan–Mar → prev-year Q4, Apr–Jun → Q1, Jul–Sep → Q2, Oct–Dec → Q3)
- After fix: 50 rows (2014–2026), pe_ratio fully populated (range 16.13–53.98)
- Q3 2023: eps_actual=1.55, eps_estimate=1.45, surprise=7.05% (exact match to D02 ground truth)

**Lesson:** yfinance attributes frequently return `None` or change column names silently on major version bumps — no exception raised. Ingestion scripts need to verify non-null rates on key fields after every yfinance upgrade, not just row counts.

---

## Observation #21: Synthesizer Infers Causation from Correlation in Large Context (B01-type Hallucination)

**Version:** v14 → v15

**Symptom**

B01 question: "How did Federal Reserve rate hikes in 2022 affect Google's advertising revenue growth?" Context had 49 items (monthly FEDFUNDS data + multiple SEC ad revenue chunks). faithfulness=0.0.

Judge finding: The answer's core claim (rate hikes → advertisers cut budgets → Google ad revenue declined) cited real context items [39][40], but neither chunk explicitly states this causal mechanism. The Synthesizer recognized temporal correlation in the data (rates rose in 2022 + ad revenue declined in 2022) and filled in the causal chain from training knowledge — then attached a real context citation number as cover.

**Root Cause**

Original Rule 1 required "causal claims must be cited" but didn't distinguish between "cited source explicitly states the causal mechanism" and "cited source contains temporally correlated data." The LLM used training knowledge to complete the causal chain, then found a real context citation to attach to it.

**Fix** (`agent/synthesizer.py`)

Split Rule 1 into two independent rules:

- **NUMBERS AND DATES**: figures must appear verbatim in the cited source — no background knowledge recall
- **CAUSAL CLAIMS** (new): causal statements require a context source that explicitly states the mechanism; correlated data ≠ causation; if no explicit statement exists, must write: "The provided context does not establish a direct causal link between X and Y."

After fix, B01 answer correctly states: "The provided context does not establish a direct causal link between Fed rate hikes and Google's advertising revenue." faithfulness: 0.0 → 1.0.

**Lesson:** The hardest hallucination to catch in RAG is not fabricated text — it's training-knowledge causal reasoning dressed up with real citation numbers. Explicit separation of "numerical citation rules" from "causal mechanism citation rules" is required.

---

## Observation #22: Critic missing_hint Not Passed to Synthesizer — Known Gaps Still Get Hallucinated

**Version:** v14 → v15

**Symptom**

A04 question: "What was Google Cloud's revenue in fiscal year 2023?" The Critic reported in every iteration: "context does not contain the total annual Google Cloud revenue figure." Despite this, the Synthesizer output "$33,088 million [1]" with a citation (faithfulness=0.0).

**Root Cause**

`per_loop.py` did not pass `missing_hint` to `synthesize()`:

```python
# Before fix
answer = synthesize(question, all_context, llm, max_tokens=cfg.llm.max_tokens)
```

The Synthesizer never sees the Critic's diagnosis. Since "$33,088M" is high-confidence training-prior knowledge, it gets emitted without constraint. Even with Rule 1 ("figures must appear verbatim in cited source"), the LLM "believes" chunk [1] contains the number and the rule fails to block it.

**Fix** (two changes)

1. `synthesizer.py`: Add `missing_hint` parameter. Format it as a `RETRIEVAL GAP` block **placed before the context** in the user message:
   ```
   RETRIEVAL GAP (read before answering): After exhaustive retrieval, the following was confirmed NOT present in the context: {missing_hint}. You MUST NOT state these figures or facts. ...
   ```

2. `per_loop.py`: Pass final `missing_hint` into `synthesize()`:
   ```python
   answer = synthesize(question, all_context, llm, max_tokens=cfg.llm.max_tokens, missing_hint=missing_hint)
   ```

**Key design decision:** Place `RETRIEVAL GAP` *before* the context, not after. Testing showed that placing it at the end caused the LLM to ignore the constraint (strong training prior overrides trailing instructions). Placing it first makes it effective.

After fix, A04 (when the right chunk is absent): "The provided context does not contain the total revenue figure for Google Cloud for the full fiscal year 2023." faithfulness=1.0.

**Lesson:** The Critic is the component that best understands context gaps within the PER Loop — but if that diagnosis is never surfaced to the Synthesizer, the detection is wasted. The Critic → Synthesizer information path must be explicit in the architecture, not left for the LLM to infer from context completeness alone.
