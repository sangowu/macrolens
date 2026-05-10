# MacroLens Project Roadmap

> Iteration history, current status, and future directions. Last updated: May 2026.

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

| Metric | v12 (best) | v14 (current) | Gap |
|--------|-----------|--------------|-----|
| faithfulness | 0.667 | **0.710** | **+0.043** ✅ |
| answer_relevancy | 0.972 | 0.952 | -0.020 |
| context_precision | 0.688 | 0.622 | -0.066 ⚠️ |
| context_recall | 0.651 | 0.490 | -0.161 ⚠️ |
| **ragas_score** | **0.741** | 0.694 | -0.047 |

**Main open issue**: context_recall significantly below v12 baseline.

Root cause: D03-type questions (macro-price correlation) — Planner does not always co-retrieve `price_history` + `macro_indicators`, leaving FEDFUNDS data absent from context so the Judge's atomic fact checks fail. See [`failure_analysis.md` Observation #19](failure_analysis.html).

**Pending merge**: PR #1 (`feature/macrolens-expansion` → `main`) — 109 unit tests passing.

---

## Near-term (This Month)

- [ ] **Fix D03 Planner routing**: force co-retrieval of `price_history` + `macro_indicators` for correlation questions
- [ ] **Run v15 eval** to verify recall recovers to >= 0.60
- [ ] **Merge PR #1** (`feature/macrolens-expansion` → `main`)
- [ ] **Complete MAG7 data ingestion**: META / AMZN / AAPL / NVDA / TSLA SEC files

---

## Mid-term (1–3 Months)

- [ ] **Restore context_recall to v12 baseline (0.651)**: refine Set D ground_truth + Planner routing
- [ ] **EPS estimate data quality**: yfinance historical estimates are incomplete — evaluate Alpha Vantage / Polygon for accurate analyst consensus data
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
| v15 | D03 Planner routing fix (planned) | Target >= 0.72 |
