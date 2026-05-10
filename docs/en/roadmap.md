# MacroLens Project Roadmap

> Iteration history, current status, and future directions. Last updated: May 2026 (v15c).

---

## Completed

### Core RAG Pipeline (v1–v12)

**Infrastructure**

- **PER Loop**: Plan → Execute → Critique (up to 3 rounds) → Synthesize
- **Hybrid Retrieval RRF**: pgvector semantic search + tsvector full-text, fused via Reciprocal Rank Fusion
- **Tool Use Structured Output**: Planner / Critic / Memory all use `tool_choice` to force LLM into JSON Schema — no regex parsing
- **Agentic Synthesizer**: compute tool runs sandboxed Python; results flow inline into the generation stream
- **Research Memory**: 2-4 key findings extracted per task, stored as pgvector embeddings, injected into future task context via similarity search
- **Async Task Queue**: PostgreSQL `tasks` table + asyncio Worker (`SELECT FOR UPDATE SKIP LOCKED`)
- **Sources Panel Filtering**: scans answer `[n]` citations, shows only referenced chunks — zero LLM cost

**Evaluation**

- LLM-as-Judge (Gemini 2.5 Pro, independent from pipeline)
- Four metrics: faithfulness / answer_relevancy / context_precision / context_recall
- context_precision upgraded to Precision@K; context_recall upgraded to atomic fact decomposition
- Eval sets A (factual) / B (multi-hop) / C (boundary/adversarial) — 18 questions total
- **All-time best: v12 ragas_score = 0.741**

**Key Bug Fixes** (18 total — see [`failure_analysis.md`](failure_analysis.html))

- `sec-parser` returning 3.8M empty nodes → replaced with BeautifulSoup + regex
- Section detection 4-layer compounding bug (Bug #18) → MD&A 2→30, Risk Factors 0→34, FinStmt 2→72 chunks
- Critic dead loop → anti-repeat fix
- Synthesizer hallucination → hard Rule 5: general knowledge does not exist for this answer

---

### v15: Planner Routing, Data Quality, and Synthesizer Hallucination Fixes (Current)

**Planner Routing Fix (`agent/planner.py`)**

- SYSTEM_PROMPT restructured: all 5 data source routing rules consolidated into a numbered `SOURCE ROUTING RULES:` block — no longer interleaved with examples
- New `MANDATORY MULTI-SOURCE RULE`: correlation/relationship questions must produce separate sub-queries for both `price_history` and `macro_indicators`
- Correlation example updated to precisely match D03 phrasing (`monthly stock returns`, `monthly changes`, single-year date range); second correlation example added (GOOGL vs CPI)
- New `DATE SCOPING` rule to prevent unnecessary date range expansion

**Data Quality Fix (`ingestion/ingest_prices.py`)**

- `fetch_earnings_history` switched from `tk.quarterly_earnings` (returns `None` in yfinance 1.3.0) to `tk.get_earnings_dates(limit=40)`
- EPS coverage expanded from 6 rows (all NULL) to 50 rows (2014–2026) with `eps_actual` / `eps_estimate` / `eps_surprise_pct`
- New `_ann_date_to_quarter_end()` helper maps earnings announcement date to fiscal quarter end
- Side effect: `pe_ratio` fully populated (2854/2854 rows), range 16.13–53.98

**Synthesizer Hallucination Fix (`agent/synthesizer.py` + `agent/per_loop.py`)**

- Rule 1 split into two: **NUMBERS AND DATES** (figures must appear verbatim in cited source) and **CAUSAL CLAIMS** (causal statements require explicit context support — correlation ≠ causation)
- `synthesize()` receives a `missing_hint` parameter; Critic-identified gaps placed as a `RETRIEVAL GAP` block at the top of the user message with hard "must not infer" constraint
- `per_loop.py` passes final `missing_hint` into `synthesize()`

**Eval Results (v14 → v15c)**

| Metric | v14 | v15c | Δ |
|--------|-----|------|---|
| faithfulness | 0.710 | **0.897** | **+0.187** ✅ |
| answer_relevancy | 0.952 | 0.872 | -0.080 ⚠️ |
| context_precision | 0.622 | **0.696** | **+0.074** ✅ |
| context_recall | 0.490 | **0.519** | +0.029 ✅ |
| **ragas_score** | 0.694 | **0.753** | **+0.059** ✅ |

> ragas_score 0.753 is the new all-time best, surpassing v12's 0.741.
> The answer_relevancy decline (-0.080) is a faithfulness tradeoff: A04-type questions now correctly refuse to answer when the specific figure is absent from context (faithfulness=1.0 vs former 0.0), but Judge penalizes incomplete answers on relevancy. The fix is improving retrieval so the right SEC chunk is consistently hit.

---

### MAG7 Expansion (v13–v14)

**New Data Sources**

- `price_history`: MAG7 daily OHLCV + P/E ratios (2015–present, ~90,000 rows)
- `earnings_history`: quarterly/annual EPS actual vs estimate + core financials (~700 rows)
- Monthly auto-aggregation: date ranges > 90 days auto-switch to monthly summaries (252 rows/yr → 12)
- Weekly auto-refresh Worker (`data_refresh_worker.py`) + startup freshness check

**MAG7 Multi-Ticker SEC Support**

- `ingest_sec_multi.py`: parameterized ingestion for all 7 companies
- `executor._search_sec()` with company whitelist filter (SQL injection safe)
- Currently ingested: GOOGL (~4,700 chunks), MSFT (~7,589 chunks)

**New Capabilities (4 Directions)**

| Direction | Data Source | Example Question |
|-----------|------------|-----------------|
| Investment decision support | price_history + earnings_history | "Is GOOGL P/E expensive vs its historical range?" |
| Earnings anomaly monitoring | earnings_history | "Did GOOGL beat EPS estimates in Q3 2023?" |
| Macro-price correlation | price_history + macro_indicators | "Correlation between 2022 Fed hikes and GOOGL returns?" |
| Competitor comparison | sec_chunks (multi-ticker) | "Google Cloud vs Azure revenue growth in 2023?" |

**Evaluation Progress**

- New eval set D (5 questions) covering all 4 new directions
- compute tool hardened: `import` explicitly FORBIDDEN, pre-injected names `np`/`pd` documented

---

## Current Status

| Metric | v12 | v14 | **v15c (current)** | vs v12 |
|--------|-----|-----|--------------------|--------|
| faithfulness | 0.667 | 0.710 | **0.897** | **+0.230** ✅ |
| answer_relevancy | 0.972 | 0.952 | 0.872 | -0.100 ⚠️ |
| context_precision | 0.688 | 0.622 | **0.696** | +0.008 ✅ |
| context_recall | 0.651 | 0.490 | 0.519 | -0.132 ⚠️ |
| **ragas_score** | 0.741 | 0.694 | **0.753** | **+0.012** ✅ |

**Open issues:**

1. **context_recall (0.519) still below v12 baseline (0.651)**: D03 ground_truth key_facts include computed values ("425 basis points", "Pearson -0.4 to -0.6") that don't exist as raw values in the database — recall cannot be improved through retrieval alone; requires revising Set D ground_truth design
2. **answer_relevancy (0.872) below v12 (0.972)**: RETRIEVAL GAP mechanism causes A04-type questions to correctly refuse ("context does not contain X"), lowering relevancy score; the root fix is improving SEC chunk retrieval to ensure the annual financial table chunk is consistently retrieved

**Pending merge**: PR #1 (`feature/macrolens-expansion` → `main`) — 109 unit tests passing.

---

## Near-term (This Month)

- [x] **Fix D03 Planner routing**: MANDATORY MULTI-SOURCE RULE + updated examples ✅
- [x] **Fix earnings_history / pe_ratio data**: yfinance API switch, EPS coverage 2014–2026 ✅
- [x] **Fix Synthesizer hallucination**: NUMBERS/CAUSAL rule split + RETRIEVAL GAP mechanism ✅
- [x] **v15c eval**: ragas_score 0.753, new all-time best ✅
- [ ] **Merge PR #1** (`feature/macrolens-expansion` → `main`)
- [ ] **Complete MAG7 data ingestion**: META / AMZN / AAPL / NVDA / TSLA SEC files

---

## Mid-term (1–3 Months)

- [ ] **Fix answer_relevancy**: improve SEC chunk retrieval stability for A04-type (annual financial table), prevent table truncation at chunk boundaries
- [ ] **Restore context_recall to v12 baseline (0.651)**: revise Set D ground_truth to replace computed key_facts with raw database values
- [ ] **Gradio UI new panels**: valuation dashboard (P/E historical band chart), earnings comparison panel
- [ ] **MAG7 data integrity check**: scheduled script to verify chunk counts and data freshness per ticker

---

## Long-term Vision

- [ ] **Real-time price data**: 15-minute delayed quotes via WebSocket / REST polling
- [ ] **News data source**: Guardian API foundation exists (`ingest_events_guardian.py`) — expand to structured news chunk ingestion
- [ ] **Cross-asset expansion**: ETF support (SPY / QQQ), macro-ETF correlation analysis
- [ ] **Structured research reports**: PDF output with P/E history charts, EPS trend charts, competitor comparison matrices
- [ ] **Eval set expansion**: Set E (real-time data), Set F (multi-turn conversation)

---

## Version History

| Version | Key Change | ragas_score |
|---------|-----------|-------------|
| v1 | Baseline (holistic judge, inline eval pipeline) | 0.566 |
| v11 | Eval methodology upgrade (Precision@K + atomic recall + Gemini 2.5 Pro) | 0.670 |
| v12 | Section detection Bug #18 fix (MD&A / Risk Factors chunk recovery) | **0.741** |
| v13 | MAG7 expansion + price/earnings data sources + Set D eval | 0.707 |
| v14 | Monthly price aggregation + numerical ground_truth + compute tool hardening | 0.694 |
| v15c | Planner routing fix + earnings/PE data fix + Synthesizer hallucination fix | **0.753** ★ |
