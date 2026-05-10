# MacroLens System Flow — Step by Step

> Walking through every stage of the pipeline using "What was Google's advertising revenue CAGR from 2019 to 2023?" as the example query.

---

## Overview

```
User Input
   │
   ├─ Chat mode ──→ PER Loop directly (synchronous)
   └─ Task mode ──→ Insert into tasks table → background thread → UI polls
                          │
                    Memory retrieval
                          │
                    PER Loop (up to 3 iterations)
                     ├─ Planner   (LLM #1 · Tool Use → structured sub-queries)
                     ├─ Executor  (pure SQL + keyword fallback)
                     └─ Critic    (LLM #2)
                          │
                    Synthesizer — Agentic Loop (LLM #3)
                     └─ LLM generates text, calls compute tool for calculations
                          └─ Sandbox Python executes, result streams back inline
                          │
                    Citation validation [n]
                          │
                    Sources panel filtering (script, filters by [n] citations)
                          │
                    Output / Report writing / Memory extraction (Tool Use · LLM #4)
```

---

## Step 1: Input

The user types a question in the Gradio UI and submits.

**Chat mode** (`ui/app.py → run_query()`)
Executes the PER Loop synchronously, blocks until done, renders the result directly to the Chatbot component.

**Task mode** (`ui/app.py → submit_task()`)
Inserts a record into the `tasks` table (status=`pending`) and starts a background thread (`threading.Thread`). The UI uses `gr.Timer` to call `poll_task()` every 3 seconds, and renders the markdown report once the task is complete.

---

## Step 2: Memory Retrieval (Task mode only)

`agent/memory.py → retrieve()`

Before entering the PER Loop, performs a vector similarity search against `research_memory`:

```sql
SELECT memory_type, content, fiscal_year
FROM research_memory
ORDER BY embedding <=> $question_vec
LIMIT 3
```

If relevant prior findings are found, they are appended to the question and passed to the Planner:

```
Original question

Relevant prior findings:
- [finding] FEDFUNDS rose from 0.08% to 4.1% in 2022, a total of 402 bps.
- [finding] Google advertising revenue grew 7.2% YoY in 2022.
```

Chat mode relies on conversation history (short-term memory) for context. Task mode relies on the Memory database (long-term memory) for cross-session reuse.

---

## Step 3: Planner (LLM call #1 · Tool Use)

`agent/planner.py → plan()`

Sends the question to the LLM and forces structured sub-query output via **Tool Use**:

```python
tool_choice={"type": "tool", "name": "create_query_plan"}
```

The LLM must call the `create_query_plan` tool and fill in parameters validated against a JSON Schema. It cannot output free text. The result is used directly as a Python dict — no regex or `json.loads` needed.

```json
[
  {
    "query": "Google advertising revenue 2019 annual total",
    "sources": ["sec_chunks"],
    "filters": {"fiscal_year": 2019}
  },
  {
    "query": "Google advertising revenue 2023 annual total",
    "sources": ["sec_chunks"],
    "filters": {"fiscal_year": 2023}
  }
]
```

**On the 2nd iteration and beyond**, the prompt includes anti-repetition constraints:

```
Focus on what's still missing: {missing_hint}
Already searched (do NOT repeat): ["Google advertising revenue 2019...", ...]
```

---

## Step 4: Executor (Pure SQL, No LLM)

`agent/executor.py → execute()`

Routes each sub-query to a different retrieval path based on the `sources` field:

### SEC + Events: Dual-Path RRF

```sql
WITH semantic AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $vec) AS sem_rank
    FROM sec_chunks
    WHERE fiscal_year = $year
    LIMIT 20
),
lexical AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank(content_tsv, $query) DESC) AS lex_rank
    FROM sec_chunks
    WHERE content_tsv @@ websearch_to_tsquery($query)
    LIMIT 20
)
SELECT id, 1.0/(60+sem_rank) + 1.0/(60+lex_rank) AS rrf_score
FROM semantic FULL OUTER JOIN lexical USING (id)
ORDER BY rrf_score DESC LIMIT 12
```

### Macro Indicators: Exact SQL

```sql
SELECT mi.date, mi.value, m.name, m.unit
FROM macro_indicators mi
JOIN macro_series_meta m USING (series_id)
WHERE mi.series_id = ANY($series)
  AND mi.date BETWEEN $date_from AND $date_to
ORDER BY mi.date
```

### Price History: Stock Price + Valuation SQL

```sql
SELECT date, open, high, low, close, adj_close, volume, pe_ratio, ps_ratio
FROM price_history
WHERE ticker = ANY($tickers)
  AND date BETWEEN $date_from AND $date_to
ORDER BY ticker, date
```

Includes P/E and P/S valuation ratios (computed from yfinance data at ingest time). Supports cross-ticker queries against the MAG7 allowlist.

### Earnings History: Quarterly/Annual Financials SQL

```sql
SELECT period_end, fiscal_year, fiscal_quarter, period_type,
       revenue, net_income, eps_actual, eps_estimate, eps_surprise_pct,
       cloud_revenue, ads_revenue, operating_margin
FROM earnings_history
WHERE ticker = ANY($tickers)
  AND ($period_type IS NULL OR period_type = $period_type)
ORDER BY ticker, period_end
```

Includes EPS beat/miss metrics (`eps_surprise_pct`) and GOOGL-specific segment data (cloud_revenue / ads_revenue).

All sub-query results are merged and deduplicated into `all_context`.

---

## Step 5: Critic (LLM call #2 · Tool Use)

`agent/critic.py → critique()`

Sends the original question + all current context to the LLM. Forces a structured judgment via **Tool Use**:

```python
tool_choice={"type": "tool", "name": "judge_sufficiency"}
# Returns: {"is_sufficient": true/false, "missing": "what is missing"}
```

- `is_sufficient=True` → exit loop, proceed to Synthesizer
- `is_sufficient=False` → pass `missing_hint` + `searched_queries` back to Step 3

Maximum 3 iterations. Like the Planner, Tool Use guarantees valid output format — no regex JSON parsing.

---

## Step 6: Synthesizer — Agentic Loop (LLM call #3)

`agent/synthesizer.py → synthesize()`

The LLM receives the **full context** and enters an agentic loop to write the answer:

```
LLM begins generating text
   │
   ├─ No computation needed → continue writing until end_turn
   │
   └─ Computation needed (CAGR, growth rate, basis points, etc.)
          ↓
       calls compute tool with Python code
          ↓
       sandbox executes, result returned to LLM
          ↓
       LLM inlines the result into the sentence, continues generating
          ↓
       until end_turn
```

**Why agentic loop instead of `<compute>` tags:**
- No regex parsing, no post-substitution, no orphaned line cleanup
- Computed results flow directly into the generation stream — the answer is naturally complete
- Sandbox guarantees numerical accuracy; LLM does not do arithmetic

Sandbox constraints: whitelisted builtins, pre-injected `pd`/`np`/`math`/`statistics`/`datetime`, no `import` allowed, 15-second timeout.

**Hard rules (System Prompt):**
1. Every number/date/percentage must have a `[n]` citation
2. If context is missing, say "The provided context does not contain [X]"
3. Do not use background knowledge to fill in missing context
4. All derived metrics must be computed via the compute tool
5. General knowledge does not exist for this query — if it's not in the context, it doesn't exist

---

## Step 7: Citation Validation

`agent/synthesizer.py → _validate_citations()`

Scans the answer for all `[n]` references and validates they are in range:

```python
citations = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
for n in citations:
    if n < 1 or n > len(selected_context):
        logger.warning("[%d] out of range", n)
```

Out-of-range citations are logged as warnings without interrupting output (can be extended to trigger regeneration).

---

## Step 8: Sources Panel Filtering

`ui/app.py → _build_sources_md()`

Pure script processing, zero LLM calls. Scans all `[n]` citations in the answer and shows only the chunks that were actually cited, filtering out unused retrieval results:

```python
cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
items = [(i, item) for i, item in enumerate(context, 1) if i in cited]
```

Of the 12 retrieved results, typically only 2–4 are cited. The Sources panel shows only those, letting the user trace directly back to the source material.

---

## Step 9: Output

### Chat Mode

- Answer rendered in Chatbot
- Sources panel: source, date, and content preview for each cited chunk
- Stats panel: iteration count / context chunk count / estimated token count / total latency

### Task Mode

`agent/report_writer.py → write_report()`
Formats a structured markdown report (Answer + Evidence split into SEC/Events/Macro sections), writes to `tasks.report_md`, sets status to `completed`.

`agent/memory.py → extract_and_store()`
Extracts 2–4 key findings from the Q&A pair via **Tool Use**, embeds and stores them in `research_memory`:

```json
[
  {"memory_type": "finding", "content": "Google advertising CAGR was 15.3% from 2019 to 2023.", "fiscal_year": null}
]
```

Like the Planner, `tool_choice` forces valid JSON output — no regex parsing.

---

## LLM Call Summary

| Stage | Calls | Method | Temperature |
|-------|-------|--------|-------------|
| Planner | up to 3 | Tool Use (structured output) | 0.0 |
| Critic | up to 3 | Tool Use (structured output) | 0.0 |
| Synthesizer (answer) | 1 (+ multiple compute tool calls) | Agentic loop | 0.0 |
| Memory extraction | 1 (Task mode) | Tool Use (structured output) | 0.0 |
| **Total** | **3–8** | | |

Executor (SQL) and Code Executor (Python sandbox) make no LLM calls.

---

## Key Changes from Previous Version

| Module | Before | After |
|--------|--------|-------|
| Planner output parsing | Regex + `json.loads` | Tool Use, direct dict access |
| Planner filters schema | Unconstrained, macro series often missed | Explicit field definitions + keyword fallback |
| Critic output parsing | Regex + `json.loads` | Tool Use, direct dict access |
| Computation trigger | `<compute>` tags + regex + post-substitution | compute tool agentic loop, inlined result |
| Orphaned line cleanup | `_remove_orphaned_results()` | No longer needed |
| Memory extraction | Regex + `json.loads` | Tool Use, direct dict access |
| Citation validation | None | `_validate_citations()` safety check |
| Sources panel | Shows all retrieved results | Script filters by `[n]`, shows only cited |
| Macro series format | Lists only, silent empty return on error | Auto-convert + keyword fallback |
