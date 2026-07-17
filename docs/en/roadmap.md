# MacroLens Project Roadmap

> Iteration history, current status, and future directions. Last updated: May 2026 (v21).

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

## Current Status (v21)

| Metric | v12 | v15c | **v21 (current)** | vs v15c |
|--------|-----|------|-------------------|---------|
| faithfulness | 0.667 | 0.891 | 0.717 | -0.174 ⚠️ |
| answer_relevancy | 0.972 | 0.870 | **0.957** | **+0.087** ✅ |
| context_precision | 0.688 | 0.691 | 0.667 | -0.024 → |
| context_recall | 0.651 | 0.512 | **0.549** | **+0.037** ✅ |
| **ragas_score** | 0.741 | **0.753** | 0.725 | -0.028 → |

**Key changes since v15c (performance optimization phase):**

- **Set D ground_truth revision** (v17b): key_facts replaced with raw DB values — D01/D03/D04 recall improved significantly
- **earnings_history dedup fix** (v17b): per_loop.py dedup key adds period_end + fiscal_quarter — D02 recall 0→1.0
- **EPS precision fix** (v17e): synthesizer.py eps_surprise_pct format +.1f → +.2f — D02 faithfulness 0.5→1.0
- **Reranker integration** (v18+): BGE-reranker-v2-m3 via Docker (`cloud_server/`); candidate_k=50 candidates → cross-encoder reranking → top_k=12; auto-fallback to RRF on API failure
- **Smart Critic window** (v21): structured data (macro/price/earnings) shown in full, sec_chunks/events capped at 40 — eliminates false-missing reports on large contexts
- **RETRIEVAL GAP restored** (v21): re-enabled missing_hint injection after Critic window fix; faithfulness 0.687 → 0.717

**Remaining ceilings (not solvable by retrieval):**

1. **A01/A04 faithfulness instability**: Annual ad revenue and Google Cloud annual total are in 10-K financial tables but not consistently retrieved in top-k; Synthesizer occasionally fills from background knowledge
2. **B03 data gap**: 2022 SEC filings predate ChatGPT/OpenAI's rise to prominence; events table lacks AI competition entries
3. **RAGAS faithfulness metric sensitivity**: a single hallucinated claim per answer subtracts 0.5; evaluation noise ≈ ±0.03

---

## Near-term (This Month)

- [x] **Fix D03 Planner routing**: MANDATORY MULTI-SOURCE RULE + updated examples ✅
- [x] **Fix earnings_history / pe_ratio data**: yfinance API switch, EPS coverage 2014–2026 ✅
- [x] **Fix Synthesizer hallucination**: NUMBERS/CAUSAL rule split ✅
- [x] **v15c eval**: ragas_score 0.753, all-time best ✅
- [x] **Set D ground_truth revision**: raw DB key_facts, D01/D03/D04 recall improved ✅
- [x] **earnings dedup fix**: period_end + fiscal_quarter dedup key, D02 recall 0→1.0 ✅
- [x] **Reranker integration**: BGE-reranker-v2-m3 Docker service, candidate_k=50 ✅
- [x] **Smart Critic window + RETRIEVAL GAP restored** ✅
- [x] **Performance optimization phase complete**: v21 ragas_score=0.725 ✅

---

## Next Phase: Feature Expansion

- [ ] **Gradio UI new panels**: valuation dashboard (P/E historical band chart), earnings comparison panel
- [ ] **Complete MAG7 data ingestion**: META / AMZN / AAPL / NVDA / TSLA SEC files

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
| v17b | Set D ground_truth revision + earnings dedup fix + EPS precision fix | 0.747 |
| v19 | Reranker integration (Docker BGE-reranker-v2-m3), candidate_k=20 | 0.699 |
| v20 | candidate_k 20→50, larger reranker candidate pool | 0.711 |
| v21 | Smart Critic window + RETRIEVAL GAP restored, performance phase final | 0.725 |
