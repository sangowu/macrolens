# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**所有回复必须使用中文。**

---

## Common Commands

```bash
# 启动三个服务（分别开终端）
uv run ui/app.py                              # Gradio UI  :7860
uv run uvicorn api.tasks:app --port 7878      # Task API   :7878
uv run worker/task_worker.py --verbose        # 后台任务 Worker

# CLI 直接问答
uv run agent/per_loop.py "问题"
uv run agent/per_loop.py --max-iter 3 --verbose "问题"

# 数据入库
uv run ingestion/ingest_sec.py --ingest-only
uv run ingestion/ingest_fred.py
uv run ingestion/ingest_events.py

# 测试
uv run pytest tests/test_new_components.py -v    # 单元测试（无需真实 API）
uv run pytest tests/smoke_test.py -v             # 集成冒烟测试（需 DB + API key）

# 评估
uv run eval/run_eval.py --sets A B C --output eval/results_v10.csv
uv run eval/compare_versions.py eval/results_v1.csv eval/results_v10.csv
uv run eval/chunk_ablation.py --files 3
```

---

## Architecture

### PER Loop（核心管道）

`agent/per_loop.py::run()` 是所有问答的入口，固定四步：

```
Plan  →  Execute  →  Critique  →  (最多 3 轮)  →  Synthesize
```

每轮都把 `missing_hint` 和 `searched_queries` 带回 Planner，避免重复检索。

### 四个 Agent 组件

| 文件 | 职责 | LLM 调用方式 |
|------|------|-------------|
| `agent/planner.py` | 问题 → 1-4 条结构化子查询 | `chat_with_tools` (tool_choice 强制) |
| `agent/executor.py` | 子查询 → 数据库检索，纯 SQL 无 LLM | — |
| `agent/critic.py` | 判断 context 是否充分 | `chat_with_tools` (tool_choice 强制) |
| `agent/synthesizer.py` | 生成带引用的答案，计算用 compute tool | `chat_agentic` (多轮 agentic loop) |

### LLM 接口层

所有 LLM 调用经 `models/llm/base.py::LLMClient` Protocol 统一，三个方法：
- `chat()` — 普通文本生成
- `chat_with_tools()` — 单次强制 tool call，返回 tool input dict（用于 Planner/Critic/Memory）
- `chat_agentic()` — 多轮 agentic loop，LLM 可反复调用 tool 直到 end_turn（用于 Synthesizer）

实现：`models/llm/anthropic_client.py` 和 `models/llm/gemini_client.py`，通过 `models/factory.py::create_llm_client()` 按 `config.yaml` 实例化。

### 数据源路由（Executor）

`agent/executor.py` 按子查询的 `sources` 字段路由：

- **`sec_chunks`** — pgvector 语义 + tsvector 全文 → RRF 融合
- **`events`** — 同上，但查 events 表
- **`macro_indicators`** — 精确 SQL，按 series_id + 日期范围。Planner 漏填 series 时，`_infer_series()` 从查询文本关键词自动推断

### 配置

唯一配置入口：`config.yaml`。`models/config.py::load_config()` 读取，传给 `models/factory.py` 工厂方法。

切换 LLM：修改 `config.yaml` 的 `llm.provider`（`gemini`/`anthropic`）和 `llm.model`。  
切换 Embedding：修改 `embedding.backend`（`online`/`local_bge`/`local_qwen`/`remote`）。

**注意**：Gemini pro 系列默认启用 AFC（Automatic Function Calling），会破坏 `chat_agentic` 的手动 tool 执行循环。当前稳定配置是 `gemini-3.1-flash-lite-preview`。

### Task 模式（异步）

`api/tasks.py`（FastAPI） → PostgreSQL `tasks` 表 → `worker/task_worker.py`（`SELECT FOR UPDATE SKIP LOCKED` 轮询）。

Task 完成后：`agent/report_writer.py` 写 markdown 报告，`agent/memory.py` 提取 2-4 条 finding 存入 `research_memory` 表供后续任务检索。

### Sources 面板过滤

`ui/app.py::_build_sources_md(context, answer)` 扫描答案中所有 `[n]` 引用，只展示被实际引用的 chunk，零 LLM 开销。

---

## Key Design Constraints

- **所有结构化输出用 Tool Use**，不用 regex + json.loads。Planner、Critic、Memory 都通过 `tool_choice` 强制 LLM 填 JSON Schema。
- **Synthesizer 的 compute 通过 agentic loop**，不用 `<compute>` 标签。计算结果直接内联到生成流。
- **沙箱约束**：`agent/tools/code_executor.py` 白名单 builtins，预注入 `pd/np/math/statistics/datetime`，禁止 `import`，15 秒超时。
- **`vector_dim: 1024`** 必须与 DDL 和 embedding 模型一致，改了要重建索引。
- **DB 端口 5433**（非默认 5432），PostgreSQL 17 + pgvector，HNSW 索引。
