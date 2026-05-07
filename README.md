# MacroLens

> A financial research agent for GOOGL SEC filings + US macroeconomic data, featuring hybrid retrieval, async task execution, code-verified computation, and cross-session research memory.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Chat Mode (synchronous)    Task Mode (async)           │
│  Gradio UI :7860            FastAPI :7878                │
└────────────┬────────────────────────┬────────────────────┘
             │                        │ POST /api/tasks
             │                   ┌────▼─────┐
             │                   │  tasks   │ PostgreSQL
             │                   │  table   │
             │                   └────┬─────┘
             │                        │ Worker polls
             └────────────┬───────────┘
                          ▼
             ┌────────────────────────┐
             │   Memory Retrieval     │ pgvector similarity
             │   research_memory      │ inject prior findings
             └────────────┬───────────┘
                          ▼
┌─────────────────────────────────────────────────────────┐
│                    PER Loop                             │
│                                                         │
│  Planner  →  Tool Use → structured sub-queries (JSON)   │
│              (anti-repeat: already_searched)            │
│      ↓                                                  │
│  Executor →  Pure SQL, no LLM call                      │
│   sec_chunks  ── pgvector cosine ──┐                    │
│              ── tsvector FTS    ───┼── RRF fusion        │
│   events      ── pgvector cosine ──┤                    │
│              ── tsvector FTS    ───┘                    │
│   macro_indicators ── exact SQL (series + date range)   │
│      ↓                                                  │
│  Critic   →  LLM judges sufficiency → refine up to 3×   │
│      ↓                                                  │
│  Synthesizer → Step 1: Tool Use selects evidence IDs    │
│              → Step 2: agentic loop writes answer       │
│                   LLM calls compute tool for math       │
│                   sandboxed Python executes inline      │
│              → Step 3: citation validation [n]          │
└────────────────────────┬────────────────────────────────┘
                         ↓
             ┌───────────────────────┐
             │   Report Writer       │ structured markdown
             │   Memory Extractor    │ Tool Use → findings → pgvector
             └───────────────────────┘
```

**PER Loop**: Plan → Execute → Critique → (refine up to 3×) → Synthesize

---

## Key Features

### Hybrid Retrieval (SEC + Events)

```sql
WITH semantic AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $vec) AS sem_rank
    FROM sec_chunks WHERE fiscal_year = $year LIMIT 20
),
lexical AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank(content_tsv, query) DESC) AS lex_rank
    FROM sec_chunks WHERE content_tsv @@ $query LIMIT 20
)
SELECT id, 1.0/(60+sem_rank) + 1.0/(60+lex_rank) AS rrf_score
FROM semantic FULL OUTER JOIN lexical USING (id)
ORDER BY rrf_score DESC LIMIT 12
```

### Synthesizer — Agentic Loop with Compute Tool

The Synthesizer uses an agentic loop so computation results flow directly into the generation stream — no regex parsing, no post-processing, no orphaned lines:

```
Agentic Answer Generation
  LLM reads full retrieved context, writes answer citing [n] sources
  When a derived metric is needed (CAGR, growth rate, basis points):
    → LLM calls the compute tool with self-contained Python
    → sandboxed Python executes (no import, 15s timeout)
    → result flows back inline — LLM continues writing naturally
  Loop ends at end_turn (no more tool calls needed)

Citation Validation
  All [n] references verified to exist in context
  Out-of-range citations logged as warnings

Sources Panel Filtering (script, zero LLM cost)
  Scans [n] citations in final answer → shows only referenced chunks
```

This replaces the prior `<compute>` regex approach — no tag parsing, no substitution pass, no risk of malformed extraction.

### Async Task Agent

```
POST /api/tasks {"question": "..."}  →  {"task_id": "uuid", "status": "pending"}

Background worker:
  1. Retrieve relevant memories (pgvector)
  2. Run PER Loop
  3. Write structured markdown report → tasks.report_md
  4. Extract key findings → research_memory

GET /api/tasks/{id}  →  {"status": "completed", "report_md": "..."}
```

### Research Memory

After each task, an LLM call extracts 2-4 key findings and stores them as vector embeddings. Future tasks retrieve relevant prior findings via similarity search and inject them into the planning context — giving the agent continuity across sessions.

---

## Data Coverage

| Source | Content | Size |
|--------|---------|------|
| SEC EDGAR | GOOGL 10-K / 10-Q / 8-K (2019–2024) | ~4,700 chunks |
| FRED | 12 US macro series (GDP, CPI, FEDFUNDS, UNRATE, etc.) | ~5,000 data points |
| Events | 30 hand-curated key events (Fed policy, earnings, antitrust) | 30 records |

---

## Evaluation Results

### Chunk Strategy Ablation (3 most recent 10-K filings, Set A questions)

| Strategy | Precision | Recall | Avg Tokens |
|----------|-----------|--------|------------|
| **Fixed 512/128** (selected) | **0.062** | 0.250 | 482 |
| Recursive | 0.016 | 0.250 | 516 |
| Semantic (threshold=0.75) | 0.000 | **0.375** | 198 |

### RAGAS End-to-End Evaluation

| Model | Set A (factual) | Set B (temporal) | Set C (analytical) |
|-------|----------------|-----------------|-------------------|
| BGE-M3 (remote) | 0.669 | 0.420 | 0.667 |
| Qwen3-Embedding-0.6B (online) | 0.654 | 0.395 | 0.602 |

Faithfulness improved +0.024–0.031 after hardening Synthesizer prompt to mandatory citation rules.  
Set B improved +0.029 after adding `already_searched` anti-repeat to Planner.

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
| Task Queue | PostgreSQL + asyncio worker (SELECT FOR UPDATE SKIP LOCKED) |
| API | FastAPI |
| UI | Gradio |
| Evaluation | RAGAS + custom ablation framework |

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker (for PostgreSQL)
- API keys: Gemini, ModelScope, DashScope, FRED

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

# 4. Initialize DB
uv run python -c "
import psycopg
conn = psycopg.connect('postgresql://macrolens:macrolens@localhost:5433/macrolens')
conn.autocommit = True
for f in ['migrations/001_init.sql', 'migrations/002_tasks_memory.sql']:
    conn.cursor().execute(open(f).read())
print('DB ready')
"

# 5. Ingest data
uv run ingestion/ingest_sec.py --ingest-only
uv run ingestion/ingest_fred.py
uv run ingestion/ingest_events.py

# 6. Launch (three terminals)
uv run ui/app.py                                    # Gradio UI  :7860
uv run uvicorn api.tasks:app --port 7878            # Task API   :7878
uv run worker/task_worker.py --verbose              # Worker
```

### CLI

```bash
uv run agent/per_loop.py "How did Fed rate hikes in 2022 affect Google's advertising revenue?"
uv run agent/per_loop.py --max-iter 3 --verbose "What are Google's main risk factors in 2023?"
```

### Evaluation

```bash
uv run eval/run_eval.py --sets A B C
uv run eval/chunk_ablation.py --files 3
```

---

## Project Structure

```
macrolens/
├── agent/
│   ├── planner.py         # LLM decomposes question → sub-queries
│   ├── executor.py        # Hybrid retrieval (RRF SQL)
│   ├── critic.py          # Sufficiency judge
│   ├── synthesizer.py     # Citation-grounded answer + <compute> blocks
│   ├── per_loop.py        # PER Loop orchestration
│   ├── report_writer.py   # Formats markdown research report
│   ├── memory.py          # Research memory: extract findings + retrieve
│   └── tools/
│       └── code_executor.py  # Sandboxed Python execution
├── api/
│   └── tasks.py           # FastAPI: POST/GET /api/tasks
├── worker/
│   └── task_worker.py     # Async polling worker
├── ingestion/
│   ├── ingest_sec.py      # SEC EDGAR → sec_chunks
│   ├── ingest_fred.py     # FRED API → macro_indicators
│   ├── ingest_events.py   # events.json → events
│   └── chunkers.py        # Fixed / Recursive / Semantic chunkers
├── models/
│   ├── base.py            # EmbeddingBackend / RerankerBackend Protocol
│   ├── factory.py         # Backend factory (local / remote / online)
│   ├── embedding/         # local_bge, local_qwen, remote, online
│   └── reranker/          # local, remote, online (DashScope / Cohere)
├── eval/
│   ├── run_eval.py        # RAGAS evaluation runner
│   ├── chunk_ablation.py
│   ├── questions.py       # Evaluation question sets (A/B/C)
│   └── metrics.py         # context_precision / context_recall
├── ui/
│   └── app.py             # Gradio UI (Chat tab + Analysis Task tab)
├── migrations/
│   ├── 001_init.sql       # Core schema (sec_chunks, events, macro_indicators)
│   └── 002_tasks_memory.sql  # tasks + research_memory tables
├── data/
│   └── events.json        # Hand-curated event timeline
├── docs/
│   ├── failure_analysis.md
│   └── interview_talking_points.md
├── cloud_server/
│   └── server.py          # FastAPI inference server (remote embedding)
└── config.yaml            # Single source of truth for all configuration
```

---

## Key Design Decisions

**Why PostgreSQL over Pinecone/Chroma?**
Financial RAG requires time filtering + exact numerical queries + vector search in the same transaction. pgvector enables all three without data synchronization complexity.

**Why PER Loop over ReAct?**
Financial Q&A is a closed domain. PER Loop's fixed structure (3 LLM calls minimum) is more predictable and cheaper than ReAct's open-ended tool use.

**Why Tool Use for structured output instead of regex?**
Planner and Memory Extractor previously parsed LLM output with `re` + `json.loads`, which fails silently when the LLM adds surrounding text or produces malformed JSON. Tool Use with `tool_choice` forces the LLM to fill a validated schema — format errors are impossible.

**Why send full context to Synthesizer instead of pre-filtering?**
Financial report chunks are highly uniform — all contain dense numbers and financial terminology. A separate filtering step forces the LLM to judge relevance before seeing the answer, which is harder than finding the answer directly. The Synthesizer's LLM naturally ignores irrelevant chunks while reading and only cites what it uses. Post-hoc filtering of the Sources panel by `[n]` citations achieves clean presentation at zero extra cost.

**Why compute via Tool Use instead of `<compute>` tags?**
Inline tag embedding requires regex extraction and a second parse pass, and produces orphaned result lines when the LLM places the tag between paragraphs. Tool Use integrates computation into the generation stream: the LLM calls the tool mid-sentence, receives the result, and continues writing — no post-processing needed.

**Why Code Executor instead of LLM arithmetic?**
LLM arithmetic on multi-year financial data is a hallucination risk. The Code Executor moves computation into deterministic Python — every derived number in the answer is verifiable by the code shown.

**Why custom task queue over LangGraph?**
The PER Loop is a bounded 4-step pipeline, not a complex graph. A PostgreSQL task table + asyncio worker is simpler, fully observable, and consistent with the "independently testable components" philosophy.

**Why no LangChain?**
Every component is independently testable. The retrieval SQL, Planner prompt, Critic logic, and Code Executor can each be evaluated in isolation.

**Why Fixed chunking over Semantic?**
Ablation results: Fixed achieves higher precision (0.062 vs 0.000) with uniform chunk sizes that produce stable RRF rankings.

---

## Failure Analysis

12 documented bugs and optimizations in [`docs/failure_analysis.md`](docs/failure_analysis.md), including:

- `sec-parser` returning 3.8M empty nodes → replaced with BeautifulSoup + regex
- Synthesizer hallucination (faithfulness=0) → hardened to mandatory `[n]` citation rules
- Planner repeating identical sub-queries → added `already_searched` list to prompt
- Chunk ablation scoring 0 due to year mismatch → reversed sort to select most recent filings
- Critic dead loop → anti-repeat fixed Set B +0.029
- `<compute>` output appearing as isolated line → replaced `<compute>` tag + regex with compute Tool Use agentic loop; orphaned-line cleanup no longer needed

---

## Author

**Sango Wu** | AI Engineer Portfolio Project | 2026
