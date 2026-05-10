# MacroLens — Project Architecture Overview

> MacroLens is a financial research RAG Agent focused on Q&A and analysis of MAG7 tech company earnings reports and US macroeconomic data.

---

## What It Does

| Dimension | Description |
|-----------|-------------|
| **Core goal** | Answer financial questions with source-cited, verifiable answers |
| **Data scope** | MAG7 (GOOGL / MSFT / META / AMZN / AAPL / NVDA / TSLA) SEC filings + FRED macro + price history + quarterly earnings + hand-labeled events |
| **Key differentiator** | Every number has dual verification: citation `[n]` + executable Python code |
| **Architecture** | Fixed 4-step PER Loop, no LangChain, each component independently testable |

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         UI Layer                             │
│              Gradio UI  ·  FastAPI  ·  Gradio               │
│                 Chat Mode          Task Mode                 │
└───────────────────┬─────────────────┬───────────────────────┘
                    │                 │
                    │         ┌───────▼──────────────┐
                    │         │  PostgreSQL tasks     │
                    │         │  Worker polls & runs  │
                    │         └───────┬──────────────┘
                    │                 │
                    ▼                 ▼
┌─────────────────────────────────────────────────────────────┐
│                      PER Loop Core                           │
│                                                             │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐             │
│   │ Planner  │───▶│ Executor │───▶│  Critic  │             │
│   │  LLM #1  │    │ Pure SQL │    │  LLM #2  │             │
│   │ Tool Use │    │ No LLM   │    │ Tool Use │             │
│   └──────────┘    └──────────┘    └──────┬───┘             │
│        ▲                                  │                 │
│        └──── missing_hint + searched ─────┘  (up to 3 iter)│
│                                           │ sufficient=true │
│                                           ▼                 │
│                              ┌────────────────────┐         │
│                              │    Synthesizer      │         │
│                              │    LLM #3 Agentic  │         │
│                              │  └─ compute tool   │         │
│                              │     Sandbox Python  │         │
│                              └────────────────────┘         │
└─────────────────────────────────────────────────────────────┘
                    │
          ┌─────────┴─────────┐
          │                   │
      Chat Output         Task Output
    Answer + Sources     Markdown report
                              │
                     Research Memory
                     (pgvector store)
                     auto-retrieved next run
```

---

## Five Data Sources

| Source | Content | Retrieval Method |
|--------|---------|-----------------|
| `sec_chunks` | MAG7 10-K / 10-Q / 8-K (2019–2024) | pgvector vector + tsvector full-text → RRF fusion |
| `events` | 30 hand-labeled events (Fed policy, earnings, antitrust) | Same approach, events table |
| `macro_indicators` | 12 FRED macro series (GDP, CPI, FEDFUNDS, etc.) | Exact SQL by series_id + date range |
| `price_history` | MAG7 daily OHLCV + P/E, P/S valuation ratios | Exact SQL by ticker + date range |
| `earnings_history` | MAG7 quarterly/annual EPS, revenue, margins (+ beat/miss metrics) | Exact SQL by ticker + fiscal period |

---

## Data Flow

### Input Stage

- **Chat mode**: synchronous PER Loop execution, result rendered immediately
- **Task mode**: writes to `tasks` table → Worker polls → supports long-running background tasks

Task mode retrieves relevant prior findings from `research_memory` before entering the PER Loop and injects them into Planner context.

### PER Loop (Core Pipeline)

**Planner** → decomposes the question into 1–4 structured sub-queries, using Tool Use to enforce JSON Schema output — no regex parsing needed.

**Executor** → pure SQL, no LLM calls, routes by the sub-query's `sources` field across five data sources.

**Critic** → determines if context is sufficient. If not, feeds `missing_hint` + `searched_queries` back to Planner. Up to 3 iterations.

**Synthesizer** → Agentic Loop for answer generation. When computation is needed, it calls the compute tool; sandbox Python executes and the result is inlined directly into the generation stream.

### Output Stage

- **Citation validation**: scans `[n]` markers, range-checks each
- **Sources filtering**: shows only chunks actually cited in the answer (script-only, zero LLM cost)
- **Task mode**: writes markdown report + extracts 2–4 key findings into Research Memory

---

## Tech Stack

```
LLM           Gemini (primary) / Anthropic Claude (fallback)
Embedding     Qwen3-Embedding-0.6B dim=1024 (ModelScope) / BGE-M3 (fallback)
Reranker      Qwen3-Rerank (DashScope) / BGE-Reranker-v2-m3 (fallback)
Database      PostgreSQL 17 + pgvector (HNSW index, port 5433)
Framework     FastAPI · Gradio · uv
Evaluation    Custom LLM-as-Judge (Precision@K + atomic recall)
```

---

## Key Design Decisions

### Why not LangChain?

Pure Python orchestration — each component is independently testable. `planner.py` / `executor.py` / `critic.py` can each be debugged in isolation. LangChain's abstraction layer hides those debugging paths.

### Why pgvector instead of Pinecone?

Financial RAG requires three query types in the same transaction: vector similarity + exact time filtering + precise numerical lookup. pgvector handles all three in a single SQL query, eliminating cross-system synchronization.

### Why fixed PER Loop instead of ReAct?

ReAct lets the LLM decide when to stop. In financial Q&A testing, ReAct either stops too early (missing evidence) or too late (burning tokens). PER Loop is fixed at 4 steps with a maximum of 7 LLM calls — predictable and testable.

### Why agentic loop instead of `<compute>` tags?

The old tag-based approach required regex extraction + post-processing substitution, causing orphaned lines and formatting issues. The agentic loop inlines compute tool results directly into the generation stream — no post-processing needed.

---

## Key File Index

```
agent/
  per_loop.py           PER Loop entry point, fixed four-step orchestration
  planner.py            Question → structured sub-queries (Tool Use)
  executor.py           Sub-queries → SQL retrieval, no LLM (routes 5 sources)
  critic.py             Context sufficiency judgment (Tool Use)
  synthesizer.py        Answer generation, Agentic Loop + compute
  memory.py             Research Memory read/write
  report_writer.py      Task mode structured markdown report

models/
  llm/base.py           LLMClient Protocol
  llm/anthropic_client.py
  llm/gemini_client.py
  embedding/            BGE-M3 / Qwen3 / remote / online
  reranker/             BGE-Reranker / Qwen3-Rerank / remote
  factory.py            Factory from config.yaml

api/tasks.py            FastAPI task queue
worker/
  task_worker.py        Background Worker (SELECT FOR UPDATE SKIP LOCKED)
  data_refresh_worker.py  Auto incremental refresh for price_history / earnings_history

ingestion/
  ingest_sec.py         Single-ticker SEC ingestion (GOOGL)
  ingest_sec_multi.py   Multi-ticker SEC ingestion (MAG7, --tickers flag)
  ingest_fred.py        FRED macro indicator ingestion
  ingest_events.py      Manual event ingestion
  ingest_prices.py      Price history + quarterly earnings ingestion (yfinance)
  chunkers.py           Fixed / Recursive / Semantic chunking strategies

migrations/
  001_init.sql          Core schema (sec_chunks, events, macro_indicators)
  002_tasks_memory.sql  Task queue + research_memory
  003_price_earnings.sql  price_history + earnings_history
  004_multi_ticker_index.sql  Multi-ticker query performance indexes
  005_refresh_log.sql   Data refresh log

eval/
  run_eval.py           LLM-as-Judge evaluation (Sets A/B/C/D)
  compare_versions.py   Version comparison
  chunk_ablation.py     Chunking strategy ablation
  questions.py          Question set definitions
  metrics.py            Precision@K + atomic recall

ui/app.py               Gradio UI
config.yaml             Single configuration entry point
```

---

## Evaluation Results

### Full Comparison (including Set D, v12 → v13)

| Metric | v1 Baseline | v12 | v13 (current) |
|--------|------------|-----|---------------|
| context_precision | 0.174 | 0.627 | 0.571 |
| context_recall | 0.471 | 0.657 | 0.590 |
| faithfulness | 0.618 | 0.534 | **0.713** |
| answer_relevancy | 1.000 | 0.951 | 0.930 |
| **ragas_score** | **0.566** | 0.698 | **0.707** |

> v13 faithfulness improved significantly (+0.179): price_history / earnings_history are structured numerical tables — every number has a clear source, drastically reducing hallucination. The slight precision/recall decline is due to **evaluation method mismatch with daily time-series data** (see Observation #19), not degraded retrieval quality.

### Question Sets

| Set | Count | Type |
|-----|-------|------|
| Set A | 8 | Factual: single-hop exact queries |
| Set B | 5 | Multi-hop reasoning: cross-source causal |
| Set C | 5 | Boundary/adversarial: out-of-scope, refusals, ambiguous |
| Set D | 5 | New data sources: price, earnings, correlation |
