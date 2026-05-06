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
                     ├─ Planner   (LLM #1)
                     ├─ Executor  (纯 SQL)
                     └─ Critic    (LLM #2)
                          │
                    Synthesizer   (LLM #3)
                          │
                    Code Executor (沙箱 Python)
                          │
                    孤立行清理
                          │
                    输出 / 报告写入 / Memory 提取
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

这让 Planner 在第一轮就能感知已有结论，避免重复检索。

---

## 第 3 步：Planner（LLM 调用 #1）

`agent/planner.py → plan()`

把问题发给 LLM（temperature=0），要求输出结构化子查询 JSON：

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

LLM 决定：拆成几个子查询、查哪个数据源（`sec_chunks` / `events` / `macro_indicators`）、加什么过滤条件（`fiscal_year`、`series`、`date_from/to`）。

**第 2 轮及之后**，prompt 附加 anti-repeat 约束：

```
Focus on what's still missing: {missing_hint}
Already searched (do NOT repeat): ["Google advertising revenue 2019...", ...]
```

防止 Planner 重复生成相同子查询。

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

- **向量路**：用 embedding 模型（Qwen3-Embedding-0.6B）把子查询文字编码成 1024 维向量，pgvector HNSW 索引做余弦相似度搜索
- **全文路**：PostgreSQL tsvector GIN 索引，`websearch_to_tsquery` 解析自然语言查询
- **RRF 融合**：两路各取 top-20，用排名倒数和合并，取 top-12

### Macro Indicators：精确 SQL

```sql
SELECT mi.date, mi.value, m.name, m.unit
FROM macro_indicators mi
JOIN macro_series_meta m USING (series_id)
WHERE mi.series_id = ANY($series)
  AND mi.date BETWEEN $date_from AND $date_to
ORDER BY mi.date
```

数值时间序列不做向量检索，直接精确匹配。

所有子查询的结果合并，去重（用 id / event_id / series_id+date 作为 key），写入 `all_context`。

---

## 第 5 步：Critic（LLM 调用 #2）

`agent/critic.py → critique()`

把原始问题 + 当前所有 context 发给 LLM，判断：

> 现有 context 能否充分回答这个问题？如果不能，缺什么？

返回 `(is_sufficient: bool, missing_hint: str)`。

- `is_sufficient=True` → 跳出循环，进入 Synthesizer
- `is_sufficient=False` → 把 `missing_hint` 和 `searched_queries` 一起带回第 3 步，Planner 生成新维度的子查询

最多循环 3 次。简单问题通常第 1 轮就充分，复杂跨数据源问题可能跑满 3 轮。

---

## 第 6 步：Synthesizer（LLM 调用 #3）

`agent/synthesizer.py → synthesize()`

把问题 + 编号后的 context 发给 LLM：

```
[1] SEC 10-K FY2019 | Business | 2019-12-31
    Google advertising revenues were $134,811 million...

[2] SEC 10-K FY2023 | Business | 2023-12-31
    Google advertising revenues were $237,855 million...

Question: What was Google's advertising revenue CAGR from 2019 to 2023?
```

LLM 识别到 CAGR 需要计算，在答案里嵌入 `<compute>` 块：

```
...With a 4-year period (2019 to 2023):

<compute>data={'s':134811,'e':237855}; result=(data['e']/data['s'])**(1/4)-1; print(f'{result*100:.1f}%')</compute>

Google's advertising revenue grew at a CAGR of 15.3% from 2019 to 2023.
```

**硬规则**：
1. 每个数字/日期/百分比必须有 `[n]` citation，找不到来源就不说
2. context 缺失时说 "The provided context does not contain [X]"
3. 不得用背景知识补全缺失 context

---

## 第 7 步：Code Executor

`agent/tools/code_executor.py → execute_python()`  
`agent/synthesizer.py → _resolve_compute_blocks()`

正则找到所有 `<compute>...</compute>` 标签，逐个执行：

```python
safe_globals = {
    "__builtins__": {白名单 builtins},  # 禁止 open/os/subprocess
    "pd": pandas, "np": numpy,
    "math": math, "statistics": statistics,
    "data": {},
}
exec(code, safe_globals)
# 捕获 stdout，替换掉标签
```

- 禁止 `import` 语句（`__import__` 不在白名单）
- `pd`、`np`、`math`、`statistics`、`datetime` 预注入，直接可用
- 15 秒超时（`threading.Timer`）
- 执行结果（print 输出）替换掉 `<compute>` 标签

执行后，文本变为：

```
...With a 4-year period (2019 to 2023):

15.3%          ← 标签被替换，但 LLM 把标签放在段落之间，形成孤立行

Google's advertising revenue grew at a CAGR of 15.3%...
```

---

## 第 8 步：孤立行清理

`agent/synthesizer.py → _remove_orphaned_results()`

LLM 习惯把 `<compute>` 放在段落之间（先列数据，再展示计算，再写结论），导致计算结果孤立成一整段。后续句子会重复同一个数字。

清理逻辑：逐行扫描，如果某行：
1. 内容恰好等于某个 compute 结果
2. 上一行是空行
3. 下一行是空行

则删掉这行和紧随其后的空行。

最终输出：

```
...With a 4-year period (2019 to 2023):

Google's advertising revenue grew at a CAGR of 15.3% from 2019 to 2023.
```

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
额外一次 LLM 调用，从问答对里提取 2-4 条关键 finding，embed 后存入 `research_memory`：

```json
[
  {"memory_type": "finding", "content": "Google advertising CAGR was 15.3% from 2019 to 2023.", "fiscal_year": null}
]
```

下次有相关问题时，这条记忆会被召回注入 Planner context。

---

## LLM 调用汇总

| 阶段 | 调用次数 | 模型 | temperature |
|------|----------|------|-------------|
| Planner | 最多 3 次 | Gemini / Claude | 0.0 |
| Critic | 最多 3 次 | Gemini / Claude | 0.0 |
| Synthesizer | 1 次 | Gemini / Claude | 0.0 |
| Memory 提取 | 1 次（Task 模式） | Gemini / Claude | 0.0 |
| **合计** | **3–7 次** | | |

Executor 和 Code Executor 不调 LLM，是纯 SQL + Python 计算。
