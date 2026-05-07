# MacroLens 系统流程详解

> 以 "What was Google's advertising revenue CAGR from 2019 to 2023?" 为例，完整走一遍从输入到输出的每个环节。

---

## 概览

```
用户输入
   │
   ├─ Chat 模式 ──→ 直接进入 PER Loop（同步）
   └─ Task 模式 ──→ 写入 tasks 表 → 后台线程 → UI 轮询
                          │
                    Memory 检索
                          │
                    PER Loop（最多 3 轮）
                     ├─ Planner   (LLM #1 · Tool Use → 结构化子查询)
                     ├─ Executor  (纯 SQL + keyword fallback)
                     └─ Critic    (LLM #2)
                          │
                    Synthesizer — Agentic Loop (LLM #3)
                     └─ LLM 生成文字，遇到计算调用 compute tool
                          └─ 沙箱 Python 执行，结果直接流回生成流
                          │
                    引用验证 [n]
                          │
                    Sources 面板过滤（脚本，按 [n] 引用）
                          │
                    输出 / 报告写入 / Memory 提取 (Tool Use · LLM #4)
```

---

## 第 1 步：输入进来

用户在 Gradio UI 输入问题，点击发送。

**Chat 模式**（`ui/app.py → run_query()`）
同步执行 PER Loop，阻塞等待，结果直接渲染到 Chatbot。

**Task 模式**（`ui/app.py → submit_task()`）
向 `tasks` 表插入一条记录（status=`pending`），启动后台线程（`threading.Thread`）。UI 通过 `gr.Timer` 每 3 秒调用 `poll_task()` 查询状态，完成后渲染 markdown 报告。

---

## 第 2 步：Memory 检索（Task 模式）

`agent/memory.py → retrieve()`

在进入 PER Loop 之前，先对 `research_memory` 表做向量相似度检索：

```sql
SELECT memory_type, content, fiscal_year
FROM research_memory
ORDER BY embedding <=> $question_vec
LIMIT 3
```

如果找到相关历史发现，附加到问题末尾传给 Planner：

```
原始问题

Relevant prior findings:
- [finding] FEDFUNDS rose from 0.08% to 4.1% in 2022, a total of 402 bps.
- [finding] Google advertising revenue grew 7.2% YoY in 2022.
```

Chat 模式依赖对话历史（短期记忆）提供上下文，Task 模式依赖 Memory 数据库（长期记忆）跨会话复用。

---

## 第 3 步：Planner（LLM 调用 #1 · Tool Use）

`agent/planner.py → plan()`

把问题发给 LLM，通过 **Tool Use** 强制输出结构化子查询：

```python
tool_choice={"type": "tool", "name": "create_query_plan"}
```

LLM 必须调用 `create_query_plan` 工具，填写经 JSON Schema 校验的参数，不能输出任何自由文本。返回结果直接作为 Python dict 使用，无需正则或 `json.loads`。

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

**第 2 轮及之后**，prompt 附加 anti-repeat 约束：

```
Focus on what's still missing: {missing_hint}
Already searched (do NOT repeat): ["Google advertising revenue 2019...", ...]
```

---

## 第 4 步：Executor（纯 SQL，无 LLM）

`agent/executor.py → execute()`

对每个子查询，根据 `sources` 字段路由到不同检索路径：

### SEC + Events：双路 RRF

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

### Macro Indicators：精确 SQL

```sql
SELECT mi.date, mi.value, m.name, m.unit
FROM macro_indicators mi
JOIN macro_series_meta m USING (series_id)
WHERE mi.series_id = ANY($series)
  AND mi.date BETWEEN $date_from AND $date_to
ORDER BY mi.date
```

所有子查询的结果合并、去重，写入 `all_context`。

---

## 第 5 步：Critic（LLM 调用 #2 · Tool Use）

`agent/critic.py → critique()`

把原始问题 + 当前所有 context 发给 LLM，通过 **Tool Use** 强制返回结构化判断：

```python
tool_choice={"type": "tool", "name": "judge_sufficiency"}
# 返回: {"is_sufficient": true/false, "missing": "缺少什么"}
```

- `is_sufficient=True` → 跳出循环，进入 Synthesizer
- `is_sufficient=False` → 把 `missing_hint` 和 `searched_queries` 一起带回第 3 步

最多循环 3 次。与 Planner 一样，Tool Use 保证输出格式合法，不再依赖正则解析 JSON。

---

## 第 6 步：Synthesizer — Agentic Loop 写答案（LLM 调用 #3）

`agent/synthesizer.py → synthesize()`

LLM 拿到**全量 context**，进入 agentic loop 写答案：

```
LLM 开始生成文字
   │
   ├─ 不需要计算 → 继续写，直到 end_turn
   │
   └─ 需要计算（CAGR、增长率、基点等）
          ↓
       调用 compute tool，传入 Python 代码
          ↓
       沙箱执行，结果发回 LLM
          ↓
       LLM 把结果内联到句子里，继续生成
          ↓
       直到 end_turn
```

**为什么用 agentic loop 而不是 `<compute>` 标签**：
- 无正则解析，无事后替换，无孤立行清理
- 计算结果直接流入生成流，答案天然完整
- 沙箱保证计算精度，LLM 不做算术

沙箱约束：白名单 builtins，预注入 `pd`/`np`/`math`/`statistics`/`datetime`，禁止 `import`，15 秒超时。

**硬规则（System Prompt）**：
1. 每个数字/日期/百分比必须有 `[n]` citation
2. context 缺失时说 "The provided context does not contain [X]"
3. 不得用背景知识补全缺失 context
4. 派生指标必须通过 compute tool 计算

---

## 第 7 步：引用验证

`agent/synthesizer.py → _validate_citations()`

扫描答案中所有 `[n]`，验证 n 是否在 context 的范围内：

```python
citations = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
for n in citations:
    if n < 1 or n > len(selected_context):
        logger.warning("[%d] out of range", n)
```

超出范围的引用记录为 warning，不中断答案输出（可扩展为拒绝重新生成）。

---

## 第 8 步：Sources 面板过滤

`ui/app.py → _build_sources_md()`

纯脚本处理，零 LLM 调用。扫描答案里所有 `[n]` 引用，只展示被实际引用的 chunk，过滤未被使用的检索结果：

```python
cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
items = [(i, item) for i, item in enumerate(context, 1) if i in cited]
```

12 条检索结果里通常只有 2–4 条被引用，Sources 面板只展示这几条，用户可以直接溯源，不需要翻阅无关内容。

---

## 第 9 步：输出

### Chat 模式

- 答案渲染到 Chatbot
- Sources 面板：每条 context 的来源、日期、内容预览
- Stats 面板：迭代次数 / context 条数 / 估算 token 数 / 总耗时

### Task 模式

`agent/report_writer.py → write_report()`
格式化结构化 markdown 报告（Answer + Evidence 分 SEC/Events/Macro 三节），写入 `tasks.report_md`，status 改为 `completed`。

`agent/memory.py → extract_and_store()`
通过 **Tool Use** 从问答对里提取 2–4 条关键 finding，embed 后存入 `research_memory`：

```json
[
  {"memory_type": "finding", "content": "Google advertising CAGR was 15.3% from 2019 to 2023.", "fiscal_year": null}
]
```

与 Planner 一样，`tool_choice` 强制返回合法 JSON，无需正则解析。

---

## LLM 调用汇总

| 阶段 | 调用次数 | 方式 | temperature |
|------|----------|------|-------------|
| Planner | 最多 3 次 | Tool Use（结构化输出） | 0.0 |
| Critic | 最多 3 次 | Tool Use（结构化输出） | 0.0 |
| Synthesizer（写答案） | 1 次（含多轮 compute tool call） | Agentic loop | 0.0 |
| Memory 提取 | 1 次（Task 模式） | Tool Use（结构化输出） | 0.0 |
| **合计** | **3–8 次** | | |

Executor（SQL）、Code Executor（Python 沙箱）、Sources 面板过滤均不调 LLM。

Executor（SQL）和 Code Executor（Python 沙箱）不调 LLM。

---

## 与旧版的主要差异

| 模块 | 旧版 | 新版 |
|------|------|------|
| Planner 输出解析 | 正则 + `json.loads` | Tool Use，直接取 dict |
| Planner filters schema | 无约束，macro series 经常漏填 | 明确定义各字段 + keyword fallback |
| Critic 输出解析 | 正则 + `json.loads` | Tool Use，直接取 dict |
| 计算触发 | `<compute>` 标签 + 正则提取 + 事后替换 | compute tool agentic loop，结果直接内联 |
| 孤立行清理 | `_remove_orphaned_results()` | 不再需要 |
| Memory 提取 | 正则 + `json.loads` | Tool Use，直接取 dict |
| 引用验证 | 无 | `_validate_citations()` 兜底检查 |
| Sources 面板 | 展示全量检索结果 | 脚本按 `[n]` 过滤，只展示被引用的 |
| Macro series 格式 | 只接受列表，格式错误静默返回空 | 自动转列表 + keyword fallback 兜底 |
