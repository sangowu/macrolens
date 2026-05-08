# MacroLens 面试 Talking Points

> 每个 talking point 结构：**现象/问题 → 我的决策 → 数据支撑 → 反方观点**
>
> 面试原则：主动提局限是 senior 信号，不要只讲优点。

---

## TP-1：为什么用 pgvector 而不是 Pinecone / ChromaDB？

**决策**：金融 RAG 的核心难点不是向量检索，是三种查询的协调：
1. 向量相似度（"找语义相近的财报段落"）
2. 精确时间过滤（"只看 FY2022 的数据"）
3. 数值精确查询（"FEDFUNDS 在 2022-03-01 是多少"）

pgvector 让这三件事在同一个 PostgreSQL 事务里完成，不需要跨系统同步。

**数据**：macro_indicators 精确查询 <1ms；sec_chunks 向量检索 + fiscal_year 过滤在同一条 SQL 里，不用 application-side join。

**反方**：pgvector HNSW 在亿级向量下召回率不如专用向量库。但 GOOGL 5 年财报 chunk 量级在 5000 条以内，pgvector 完全够用，引入 Pinecone 反而增加同步复杂度。

---

## TP-2：为什么用 PER Loop 而不是 ReAct？

**决策**：PER Loop（Plan→Execute→Critique→Synthesize）结构固定，最多 7 次 LLM 调用（3×Planner + 3×Critic + 1×Synthesizer）。ReAct 让 LLM 自己决定何时停，金融 Q&A 实测中 ReAct 要么过早停（漏 evidence）要么过晚停（烧钱）。

**数据**：简单问题 3 次 LLM 调用，复杂问题 7 次；平均耗时 8-15s。

**反方**：PER Loop 对开放域问题表达力不够。但金融 Q&A 是封闭域，问题结构清晰，固定的 Plan→Execute→Critique 足以覆盖。

---

## TP-3：为什么选 Fixed 512/128 而不是 Semantic Chunking？

**决策**：三种策略的消融实验结果（3 份最新 10-K，Set A 问题集）：

| 策略 | Precision | Recall | Avg Tokens |
|------|-----------|--------|------------|
| Fixed 512/128 | **0.062** | 0.250 | 482 |
| Recursive | 0.016 | 0.250 | 516 |
| Semantic (0.75) | 0.000 | **0.375** | 198 |

Semantic 召回最高，但 Precision=0——chunk 数量是 Fixed 的 4 倍，小 chunk 的向量质量不稳定，RRF 排名结果是噪声。Fixed 大小一致，RRF 得分可比较，Precision 最高。

**反方**：Semantic chunk 理论上语义更内聚。但实际检索目标是"能回答问题的 chunk"，不是"叙述连贯的 chunk"，两者语义空间不对齐。

---

## TP-4：Embedding 模型选型与对比

**实验对比**（完整 RAGAS，三组问题集）：

| 模型 | Set A | Set B | Set C | 部署方式 |
|------|-------|-------|-------|---------|
| BGE-M3 (dim=1024) | 0.669 | 0.420 | 0.667 | AutoDL + SSH tunnel |
| Qwen3-Embedding-0.6B (dim=1024) | 0.654 | 0.395 | 0.602 | ModelScope API |

BGE-M3 综合更优，尤其 Set B（时间推理）高 2.5 个点。但 Qwen3 不依赖 SSH 隧道，随时可用。

**设计亮点**：通过 `EmbeddingBackend` Protocol + Factory 模式，切换模型只需改 `config.yaml` 一行，代码零改动。维度相同时不需要重建数据库——但模型不同时向量空间不同，必须重新 ingest（这是个坑，我踩过）。

---

## TP-5：如何解决 Synthesizer 幻觉问题？

**现象**：RAGAS 评估中多个问题 faithfulness=0，Synthesizer 在 context 不足时编造合理的数字。

**根因**：软约束 "Do not fabricate numbers" 打不过 LLM 生成完整答案的内在倾向。

**修复**：把软约束改成三条硬规则：
1. 每个数字/日期/百分比必须有 `[n]` 引用，找不到来源就不说
2. context 缺失时明确说 "The provided context does not contain [X]"
3. 新增身份约束：background knowledge 不得补全缺失 context

**效果**：Set A +0.031、Set B +0.024、Set C +0.028。答案末尾出现 "注：提供的上下文中未包含..." 这类诚实声明，faithfulness 明显提升。

---

## TP-6：多轮检索的 Critic 死循环问题

**现象**：日志显示 Critic 连续三轮给出完全相同的 missing 原因，Planner 第二轮重复了第一轮的子查询，new context=0。

**根因**：第二轮 Planner 只知道"缺什么"，不知道"已经搜过什么"，无法生成真正不同的检索策略。

**修复**：在每轮 prompt 中附加 `already_searched` 列表：
```
Focus on what's still missing: {missing_hint}
Already searched (do NOT repeat): ["Federal Reserve rate hikes...", "Google advertising revenue..."]
```

**效果**：第二轮 Planner 开始探索新维度（macro_shock、GDP、RSAFS），new context 从 0 提升到 16-26 条，Set B +0.029。

**注**：Critic 三轮 missing 原因仍然相同——这是数据层面的天花板，SEC 财报本来就没有"加息→广告收入"的直接因果分析，这类内容只在分析师报告里，属于预期行为。

---

## TP-7：为什么不用 LangChain？

**决策**：纯 Python 编排，每个组件独立可测。

- `planner.py`：可以单独测试 prompt 输出的 JSON 格式
- `executor.py`：可以单独测试 RRF SQL 的召回结果
- `critic.py`：可以单独测试充分性判断的准确率

LangChain 的抽象层会把这些调试路径全部遮住。

**反方**：如果未来要接更多工具（web search、code interpreter），LangChain/LangGraph 的生态会节省时间。但 MVP 阶段过度框架化是负担，不是优势。

---

## TP-8：系统的局限是什么？

> 主动说局限是 senior 信号，面试官会主动追问，不如提前准备好。

1. **单 ticker**：只覆盖 GOOGL，跨公司对比需要扩展 ingestion（schema 已支持 `company` 字段）
2. **事件库手工维护**：30 条手工标注，规模化需要自动事件抽取（NER + 新闻爬虫）
3. **因果分析缺失**：SEC 财报不包含"加息→广告收入下降"的直接因果链，这类分析需要分析师报告（付费数据源）
4. **latency 较高**：8-15s/query，主要来自 3 次 LLM 调用 + 2 次 embedding API。生产环境需要 streaming + async
5. **评测集偏小**：18 个问题，统计显著性不足，需要扩展到 100+ 条

---

## TP-9：如何扩展到生产规模？

**存储**：pgvector HNSW 支持百万级向量；分区表按 fiscal_year 分区可进一步提速

**latency**：
- Planner/Critic 换成更快的小模型（Haiku/Flash）
- Synthesizer 保留大模型
- Streaming 输出减少感知延迟

**数据更新**：FRED 数据每月更新，SEC 每季度出新申报，`ON CONFLICT DO UPDATE` 保证幂等性，可以直接跑增量 ingest

**多公司**：schema 已有 `company` 字段，ingestion 加新 CIK 即可；评测集需要对应扩充

---

## TP-10：为什么用 Code Executor 而不是让 LLM 直接计算？

**决策**：LLM 做多步数值计算是幻觉的高发区。Synthesizer 之前 faithfulness=0 的案例中，有一类就是 LLM 从 context 拿到正确的数字，但在脑子里算错了增长率。

**实现**：Synthesizer 用 `chat_agentic()` 多轮循环。LLM 写答案途中遇到派生计算直接调用 `compute` tool，沙箱 Python 执行后结果内联返回，LLM 继续生成——无需标签解析，无需二次替换。

```
LLM: "Revenue grew "
→ calls compute(code="print(f'{(224.5/182.5-1)*100:.1f}%')")
← sandbox returns: "23.0%"
LLM: "Revenue grew 23.0% YoY [1]"
```

**数据**：每个派生数字有两层保障——来源 citation `[n]` + 可执行代码。计算结果可独立验证，不依赖 LLM 推理。

**沙箱设计**：白名单 builtins，预注入 `pd/np/math/statistics`，禁止 `import`，15s 超时。

**早期设计是 `<compute>` 标签**：Synthesizer 生成标签，post-process 时 regex 提取代码执行、结果替换标签。被废弃原因：LLM 有时把标签放在段落之间，执行结果变成孤立行，正则提取也容易受 LLM 输出格式影响失效。agentic loop 消除了后处理这一环节。

---

## TP-11：评估方法论：为什么不用 RAGAS 库，以及踩了哪些坑？

**决策**：自定义 LLM-as-Judge，不用官方 `ragas` 库。

原因是**接口不兼容**：`ragas` 要求 LangChain `BaseLLM` 接口和 `context: list[str]` 格式，本项目用自定义 `LLMClient` Protocol，context 是 `list[dict]`（含 source、fiscal_year、series_id 等字段）。适配的代价高于自己实现，而且自定义实现可以把这些结构信息直接暴露给 Judge。

**实现的四个指标**：
- `faithfulness`：答案里的每个声明是否有 context 来源支撑
- `answer_relevancy`：答案是否切题
- `context_precision`：Precision@K——按检索顺序对每个 chunk 判断相关性，计算排名加权精度
- `context_recall`：原子事实分解——把 ground truth 拆成独立声明，逐一检查 context 是否覆盖

**踩的坑，面试时主动说**：

*坑 1：eval 里重写了 per_loop，但漏了 `already_searched`*
`run_eval.py` 里有一个 `_run_with_context_capture` 函数独立实现了 PER Loop，但 Planner 的第二轮 prompt 缺少"已搜过的查询"列表。结果 eval 跑出来的多跳问题（Set B）分数偏低，是 pipeline 的问题还是 eval 的问题都难判断。修复：直接调 `per_loop.run()`，删掉 32 行冗余代码。

*坑 2：Judge 看到的 context 只有 3000 字符*
原始截断是 3000 chars/15 chunks，覆盖了全量 context 的不到 5%。faithfulness 和 recall Judge 因为看不到完整内容，给出了不准确的低分。修复：扩大到 10000 chars/25 chunks。

*坑 3：弱模型（Flash Lite）做 Judge 导致 recall=0*
macro 数据的 context 显示的是 `UNRATE: 3.4`，ground truth 写的是 "US unemployment rate was 3.4%"。Flash Lite 不能可靠地把 `UNRATE` 映射到"unemployment rate"，对明显正确的答案给出 recall=0。修复：context 格式补全完整 title（`Unemployment Rate (UNRATE)`）+ Judge 换成 Gemini 2.5 Pro。

*坑 4：Gemini 2.5 Pro `resp.text` 返回 None*
思考模型（thinking model）在某些 prompt 下 `resp.text` 是 None，直接 `.strip()` 报 AttributeError。下游 `ragas_score` 因为 None 导致 f-string 格式化崩溃，触发 except 分支写入第二行，CSV 里每题出现重复行。两处修复：`(resp.text or "").strip()` + print 格式化用安全函数。

**最终效果**（v1 → v10）：

| 指标 | v1 | v10 | Δ |
|------|----|----|---|
| context_precision | 0.174 | 0.579 | +0.405 |
| context_recall | 0.471 | 0.648 | +0.178 |
| ragas_score | 0.566 | 0.685 | +0.119 |

---

## TP-12：Async Task Agent + Research Memory

**决策**：聊天模式是无状态的 Q&A；Task 模式引入了两个不同的能力——

**异步任务**：用户提交问题 → PostgreSQL tasks 表 → Worker 后台执行 PER Loop → 生成结构化 markdown 报告。`SELECT FOR UPDATE SKIP LOCKED` 支持多 worker 并发不重复。

**Research Memory**：任务完成后，LLM 提取 2-4 条关键 finding 存入 `research_memory`（pgvector embedding）。下次任务开始时，similarity search 召回相关历史发现，注入到 Planner context。

**效果**：
- 第一次问"2022 FEDFUNDS 变化"→ 存储 finding："FEDFUNDS rose from 0.08% to 4.1%，402 bps"
- 第二次问"加息对 Google 广告的影响"→ 自动注入上一条记忆，Planner 有额外上下文

**反方**：没有用 LangGraph / Mem0 等框架，纯手写任务队列和 memory 层。原因：PER Loop 是有界的 4 步流程，不需要复杂图结构；memory 是领域定制的结构化存储，通用框架反而控制力弱。自己搭更容易在面试里解释每一行在做什么。

---

## 一分钟 Elevator Pitch

> 面试开场或 HR 初面用

"MacroLens 是一个专注于 GOOGL 财报和美国宏观经济的研究 Agent，有别于普通 RAG 聊天机器人。核心差异有三点：第一，答案里的每个数字都有两层验证——SEC 文档的引用标注，加上可执行的 Python 代码；增长率、CAGR、基点变化都是代码算出来的，不是 LLM 推理的。第二，异步任务模式——用户提交分析任务，Agent 在后台自主执行，生成结构化 markdown 报告，而不是即时聊天回复。第三，跨会话研究记忆——每次任务完成后提取关键发现存入向量数据库，下次任务自动召回相关历史，Agent 有认知连续性。整个系统纯 Python，没有 LangChain，每个组件独立可测，有自定义 LLM-as-Judge 评测框架支撑设计决策，ragas_score 从 0.566 迭代到 0.685。"
