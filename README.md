# MacroLens

> A financial RAG agent for GOOGL SEC filings + US macroeconomic data, featuring hybrid retrieval, multi-turn sufficiency validation, and a full evaluation framework.

---

## Architecture

```
User Question
     │
     ▼
┌─────────────┐
│   Planner   │  LLM decomposes question into 1-4 structured sub-queries
│             │  (sources: sec_chunks / events / macro_indicators)
└──────┬──────┘
       │ sub-queries
       ▼
┌─────────────┐
│  Executor   │  Pure SQL — no LLM call
│             │
│  sec_chunks ├─ pgvector cosine ─┐
│             ├─ tsvector FTS    ─┼─ RRF fusion → top-k chunks
│             │                   │
│  events     ├─ pgvector cosine ─┤
│             ├─ tsvector FTS    ─┘
│             │
│  macro_indicators ── exact SQL (series_id + date range)
└──────┬──────┘
       │ context (deduplicated across iterations)
       ▼
┌─────────────┐
│   Critic    │  LLM judges sufficiency → (is_sufficient, missing_hint)
└──────┬──────┘
       │ insufficient? → back to Planner with missing_hint + already_searched
       │ sufficient or max_iter reached?
       ▼
┌─────────────┐
│ Synthesizer │  LLM generates answer with [n] citation notation
└─────────────┘
```

**PER Loop**: Plan → Execute → Critique → (refine up to 3×) → Synthesize

---

## Data Coverage

| Source | Content | Size |
|--------|---------|------|
| SEC EDGAR | GOOGL 10-K / 10-Q / 8-K (2019–2024) | ~4,700 chunks |
| FRED | 12 US macro series (GDP, CPI, FEDFUNDS, UNRATE, etc.) | ~5,000 data points |
| Events | 30 hand-curated key events (Fed policy, earnings, antitrust) | 30 records |

---

## Retrieval Design

### Hybrid Search (SEC + Events)

```sql
WITH semantic AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $vec) AS sem_rank
    FROM sec_chunks WHERE fiscal_year = $year
    LIMIT 20
),
lexical AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank(content_tsv, query) DESC) AS lex_rank
    FROM sec_chunks WHERE content_tsv @@ $query
    LIMIT 20
)
SELECT id, 1.0/(60+sem_rank) + 1.0/(60+lex_rank) AS rrf_score
FROM semantic FULL OUTER JOIN lexical USING (id)
ORDER BY rrf_score DESC LIMIT 12
```

- **Semantic**: pgvector cosine similarity (Qwen3-Embedding-0.6B, dim=1024)
- **Lexical**: PostgreSQL tsvector full-text search
- **Fusion**: Reciprocal Rank Fusion (k=60)

### Macro Indicators

Exact SQL query — no vector search needed for numerical time-series data.

---

## Evaluation Results

### Chunk Strategy Ablation (3 most recent 10-K filings, Set A questions)

| Strategy | Precision | Recall | Avg Tokens |
|----------|-----------|--------|------------|
| **Fixed 512/128** (selected) | **0.062** | 0.250 | 482 |
| Recursive | 0.016 | 0.250 | 516 |
| Semantic (threshold=0.75) | 0.000 | **0.375** | 198 |

Fixed sliding window selected: uniform chunk size → stable RRF ranking.

### RAGAS End-to-End Evaluation

| Metric | Set A (factual) | Set B (temporal) | Set C (analytical) |
|--------|----------------|-----------------|-------------------|
| RAGAS avg | 0.654 | 0.395 | 0.602 |
| Faithfulness | improved after prompt hardening | — | — |

### Embedding Model Comparison

| Model | Set A | Set B | Set C | Notes |
|-------|-------|-------|-------|-------|
| BGE-M3 (remote) | 0.669 | 0.420 | 0.667 | Best quality |
| Qwen3-Embedding-0.6B (online) | 0.654 | 0.395 | 0.602 | No SSH tunnel needed |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| LLM | Gemini / Claude (configurable) |
| Embedding | Qwen3-Embedding-0.6B (ModelScope) / BGE-M3 |
| Reranker | qwen3-rerank (DashScope) / BGE-Reranker-v2-m3 |
| Vector DB | PostgreSQL 17 + pgvector (HNSW index) |
| Full-text | PostgreSQL tsvector (GIN index) |
| Agent | Pure Python (no LangChain) |
| UI | Gradio |
| Evaluation | RAGAS + custom ablation framework |

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker (for PostgreSQL)
- API keys: Gemini, ModelScope, DashScope

### Setup

```bash
# 1. Clone and install
git clone https://github.com/sangowu/macrolens
cd macrolens
uv sync

# 2. Start PostgreSQL
docker run -d --name macrolens-pg \
  -e POSTGRES_USER=macrolens \
  -e POSTGRES_PASSWORD=macrolens \
  -e POSTGRES_DB=macrolens \
  -p 5433:5432 pgvector/pgvector:pg17

# 3. Configure
cp .env.example .env   # fill in API keys
# edit config.yaml if needed

# 4. Initialize DB
uv run python -c "
import psycopg
conn = psycopg.connect('postgresql://macrolens:macrolens@localhost:5433/macrolens')
conn.autocommit = True
conn.cursor().execute(open('migrations/001_init.sql').read())
"

# 5. Ingest data
uv run ingestion/ingest_sec.py --ingest-only
uv run ingestion/ingest_fred.py
uv run ingestion/ingest_events.py

# 6. Launch UI
uv run ui/app.py
# Open http://localhost:7860
```

### CLI Usage

```bash
uv run agent/per_loop.py "How did Fed rate hikes in 2022 affect Google's advertising revenue?"
uv run agent/per_loop.py --max-iter 3 --verbose "What are Google's main risk factors in 2023?"
```

### Run Evaluation

```bash
uv run eval/run_eval.py --sets A B C
uv run eval/chunk_ablation.py --files 3
```

---

## Project Structure

```
macrolens/
├── agent/
│   ├── planner.py       # LLM decomposes question → sub-queries
│   ├── executor.py      # Hybrid retrieval (RRF SQL)
│   ├── critic.py        # Sufficiency judge
│   ├── synthesizer.py   # Citation-grounded answer generation
│   └── per_loop.py      # PER Loop orchestration
├── ingestion/
│   ├── ingest_sec.py    # SEC EDGAR → sec_chunks
│   ├── ingest_fred.py   # FRED API → macro_indicators
│   ├── ingest_events.py # events.json → events
│   └── chunkers.py      # Fixed / Recursive / Semantic chunkers
├── models/
│   ├── base.py          # EmbeddingBackend / RerankerBackend Protocol
│   ├── factory.py       # Backend factory (local / remote / online)
│   ├── embedding/       # local_bge, local_qwen, remote, online
│   └── reranker/        # local, remote, online (Cohere / DashScope)
├── eval/
│   ├── run_eval.py      # RAGAS evaluation runner
│   ├── chunk_ablation.py
│   ├── questions.py     # Evaluation question sets (A/B/C)
│   └── metrics.py       # context_precision / context_recall
├── ui/
│   └── app.py           # Gradio UI
├── migrations/
│   └── 001_init.sql     # PostgreSQL schema
├── data/
│   └── events.json      # Hand-curated event timeline
├── docs/
│   ├── failure_analysis.md   # 12 bugs + optimizations with root causes
│   └── interview_talking_points.md
├── cloud_server/
│   └── server.py        # FastAPI inference server (for remote backend)
└── config.yaml          # Single source of truth for all configuration
```

---

## Key Design Decisions

**Why PostgreSQL over Pinecone/Chroma?**
Financial RAG requires time filtering + exact numerical queries + vector search in the same transaction. pgvector enables all three without data synchronization complexity.

**Why PER Loop over ReAct?**
Financial Q&A is a closed domain. PER Loop's fixed structure (3 LLM calls minimum) is more predictable and cheaper than ReAct's open-ended tool use (10+ calls).

**Why no LangChain?**
Every component is independently testable. The retrieval SQL, the Planner prompt, and the Critic logic can each be evaluated in isolation — LangChain abstractions would obscure this.

**Why Fixed chunking over Semantic?**
Ablation results: Fixed achieves higher precision (0.062 vs 0.000) with uniform chunk sizes that produce stable RRF rankings. Semantic chunking's 4× chunk count inflates retrieval noise.

---

## Failure Analysis

12 documented bugs and optimizations in [`docs/failure_analysis.md`](docs/failure_analysis.md), including:

- `sec-parser` returning 3.8M empty nodes → replaced with BeautifulSoup + regex
- Synthesizer hallucination (faithfulness=0) → hardened to mandatory [n] citation rules
- Planner repeating identical sub-queries across iterations → added `already_searched` list to prompt
- Chunk ablation scoring 0 due to year mismatch → reversed sort order to select most recent filings

---

## Author

**Sango Wu** | AI Engineer Portfolio Project | 2026
