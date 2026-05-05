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
5. **评测集偏小**：18 个问题（SSH 错误丢失 2 题），统计显著性不足，需要扩展到 100+ 条

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

## 一分钟 Elevator Pitch

> 面试开场或 HR 初面用

"MacroLens 是一个专注于 GOOGL 财报和美国宏观经济的 RAG Agent。区别于普通 RAG 系统，它有三个设计亮点：第一，三路混合检索——SEC 财报用向量+全文 RRF fusion，宏观数据用精确 SQL，都在 PostgreSQL 里完成；第二，PER Loop 多轮充分性验证，Critic 判断 context 不够时驱动 Planner 生成不同维度的子查询，而不是重复搜索；第三，完整的评测体系，包含 chunk 策略消融实验和 RAGAS 端到端评估，有具体数字支撑每个设计决策。"
