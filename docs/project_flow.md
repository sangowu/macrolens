# MacroLens 项目架构总览

> MacroLens 是一个面向金融研究的 RAG Agent，专注于 MAG7 科技公司财报与美国宏观经济数据的问答与分析。

---

## 系统定位

| 维度 | 说明 |
|------|------|
| **核心目标** | 对金融问题给出有来源、可验证的精确答案 |
| **数据范围** | MAG7 七家公司（GOOGL / MSFT / META / AMZN / AAPL / NVDA / TSLA）SEC 财报 + FRED 宏观指标 + 股价历史 + 季度财报 + 手工标注事件 |
| **关键差异** | 答案中每个数字有双重保障：citation `[n]` + 可执行 Python 代码 |
| **架构特点** | 固定 4 步 PER Loop，不用 LangChain，每个组件独立可测 |

---

## 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                       用户界面层                              │
│              Gradio UI  ·  FastAPI  ·  Gradio               │
│                 Chat 模式         Task 模式                   │
└───────────────────┬─────────────────┬───────────────────────┘
                    │                 │
                    │         ┌───────▼──────────────┐
                    │         │  PostgreSQL tasks 表  │
                    │         │  Worker 轮询执行       │
                    │         └───────┬──────────────┘
                    │                 │
                    ▼                 ▼
┌─────────────────────────────────────────────────────────────┐
│                      PER Loop 核心                           │
│                                                             │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐             │
│   │ Planner  │───▶│ Executor │───▶│  Critic  │             │
│   │  LLM #1  │    │ 纯 SQL   │    │  LLM #2  │             │
│   │ Tool Use │    │ 无 LLM   │    │ Tool Use │             │
│   └──────────┘    └──────────┘    └──────┬───┘             │
│        ▲                                  │                 │
│        └──── missing_hint + searched ─────┘  (最多 3 轮)    │
│                                           │ sufficient=true │
│                                           ▼                 │
│                              ┌────────────────────┐         │
│                              │    Synthesizer      │         │
│                              │    LLM #3 Agentic  │         │
│                              │  └─ compute tool   │         │
│                              │     沙箱 Python     │         │
│                              └────────────────────┘         │
└─────────────────────────────────────────────────────────────┘
                    │
          ┌─────────┴─────────┐
          │                   │
    Chat 输出             Task 输出
   答案 + Sources       markdown 报告
                              │
                     Research Memory
                     (pgvector 存储)
                     下次任务自动检索
```

---

## 五大数据源

| 数据源 | 内容 | 检索方式 |
|--------|------|---------|
| `sec_chunks` | MAG7 公司 10-K / 10-Q / 8-K（2019–2024） | pgvector 语义 + tsvector 全文 → RRF 融合 |
| `events` | 30 条手工标注事件（Fed 政策、财报、反垄断） | 同上，查 events 表 |
| `macro_indicators` | 12 个 FRED 宏观序列（GDP、CPI、FEDFUNDS 等） | 精确 SQL，按 series_id + 日期范围 |
| `price_history` | MAG7 日线 OHLCV + P/E、P/S 估值比率 | 精确 SQL，按 ticker + 日期范围 |
| `earnings_history` | MAG7 季度/年度 EPS、收入、利润率（含超预期指标） | 精确 SQL，按 ticker + 会计期间 |

---

## 数据流详解

### 输入阶段

- **Chat 模式**：同步执行 PER Loop，结果直接渲染
- **Task 模式**：写入 `tasks` 表 → Worker 轮询 → 支持后台长任务

Task 模式在进入 PER Loop 前，先从 `research_memory` 检索相关历史发现注入 Planner context。

### PER Loop（核心管道）

**Planner** → 把问题拆解为 1–4 条结构化子查询，通过 Tool Use 强制输出 JSON Schema，无需正则解析。

**Executor** → 纯 SQL，无 LLM 调用，按子查询的 `sources` 字段路由到五个数据源。

**Critic** → 判断 context 是否充分。不充分时把 `missing_hint` + `searched_queries` 反馈给 Planner，最多 3 轮。

**Synthesizer** → Agentic Loop 写答案，遇到计算自动调用 compute tool，沙箱 Python 执行后结果直接内联。

### 输出阶段

- **Citation 验证**：扫描 `[n]`，范围检查
- **Sources 过滤**：只展示答案中实际引用的 chunk（脚本，零 LLM）
- **Task 模式**：写 markdown 报告 + 提取 2–4 条 finding 存入 Research Memory

---

## 技术栈

```
LLM           Gemini（主）/ Anthropic Claude（备）
Embedding     Qwen3-Embedding-0.6B dim=1024 (ModelScope) / BGE-M3（备）
Reranker      Qwen3-Rerank (DashScope) / BGE-Reranker-v2-m3（备）
数据库        PostgreSQL 17 + pgvector（HNSW 索引，port 5433）
框架          FastAPI · Gradio · uv
评测          自定义 LLM-as-Judge（Precision@K + 原子 recall）
```

---

## 关键设计决策

### 为什么不用 LangChain？

纯 Python 编排，每个组件独立可测。`planner.py` / `executor.py` / `critic.py` 各自可单独 debug，LangChain 的抽象层会遮住这些路径。

### 为什么用 pgvector 而不是 Pinecone？

金融 RAG 需要三种查询在同一事务里完成：向量相似度 + 精确时间过滤 + 数值精确查询。pgvector 让这三件事在一条 SQL 里完成，不需要跨系统同步。

### 为什么用固定 PER Loop 而不是 ReAct？

ReAct 让 LLM 自己决定何时停，金融 Q&A 实测中 ReAct 要么过早停（漏 evidence）要么过晚停（烧钱）。PER Loop 固定 4 步、最多 7 次 LLM 调用，可预测、可测试。

### 为什么计算用 agentic loop 而不是标签？

旧版 `<compute>` 标签需要正则提取 + 事后替换，产生孤立行、格式异常。Agentic loop 让 compute tool 结果直接内联到生成流，无后处理。

---

## 核心文件索引

```
agent/
  per_loop.py           PER Loop 入口，固定四步编排
  planner.py            问题 → 结构化子查询 (Tool Use)
  executor.py           子查询 → SQL 检索，无 LLM（路由 5 个数据源）
  critic.py             context 充分性判断 (Tool Use)
  synthesizer.py        答案生成，Agentic Loop + compute
  memory.py             Research Memory 存取
  report_writer.py      Task 模式结构化 markdown 报告

models/
  llm/base.py           LLMClient Protocol
  llm/anthropic_client.py
  llm/gemini_client.py
  embedding/            BGE-M3 / Qwen3 / remote / online
  reranker/             BGE-Reranker / Qwen3-Rerank / remote
  factory.py            按 config.yaml 实例化

api/tasks.py            FastAPI 任务队列
worker/
  task_worker.py        后台 Worker（SELECT FOR UPDATE SKIP LOCKED）
  data_refresh_worker.py  自动增量刷新 price_history / earnings_history

ingestion/
  ingest_sec.py         单 ticker SEC 入库（GOOGL）
  ingest_sec_multi.py   多 ticker SEC 入库（MAG7，支持 --tickers 参数）
  ingest_fred.py        FRED 宏观指标入库
  ingest_events.py      手工事件入库
  ingest_prices.py      股价历史 + 季度财报入库（yfinance）
  chunkers.py           Fixed / Recursive / Semantic 切块策略

migrations/
  001_init.sql          核心 schema（sec_chunks, events, macro_indicators）
  002_tasks_memory.sql  任务队列 + research_memory
  003_price_earnings.sql  price_history + earnings_history
  004_multi_ticker_index.sql  多 ticker 查询性能索引
  005_refresh_log.sql   数据刷新日志

eval/
  run_eval.py           LLM-as-Judge 评测（Set A/B/C/D）
  compare_versions.py   版本对比
  chunk_ablation.py     切块策略消融实验
  questions.py          问题集定义
  metrics.py            Precision@K + 原子 recall

ui/app.py               Gradio UI
config.yaml             唯一配置入口
```

---

## 评测结果

### 全量对比（含 Set D，v12 → v13）

| 指标 | v1 基准 | v12 | v13（当前） |
|------|---------|-----|------------|
| context_precision | 0.174 | 0.627 | 0.571 |
| context_recall | 0.471 | 0.657 | 0.590 |
| faithfulness | 0.618 | 0.534 | **0.713** |
| answer_relevancy | 1.000 | 0.951 | 0.930 |
| **ragas_score** | **0.566** | 0.698 | **0.707** |

> v13 faithfulness 大幅提升（+0.179）：price_history / earnings_history 是结构化数值表，每个数字有明确来源，幻觉率显著降低。precision/recall 小幅下降是**评估方法与日线时序数据不完全匹配**所致（见观察 #19），非检索质量变差。

### 问题集

| 集合 | 题数 | 类型 |
|------|------|------|
| Set A | 8 | 事实型：单跳精确查询 |
| Set B | 5 | 多跳推理：跨数据源因果 |
| Set C | 5 | 边界/对抗：超范围、拒绝、模糊 |
| Set D | 5 | 新数据源：价格、财报、相关性 |
