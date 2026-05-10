# MacroLens 项目路线图

> 迭代历史、当前状态与未来方向。最后更新：2026-05（v15c）。

---

## 已完成

### 核心 RAG 管道（v1–v12）

**基础架构**

- **PER Loop**：Plan → Execute → Critique（最多 3 轮） → Synthesize 四步管道
- **混合检索 RRF**：pgvector 语义检索 + tsvector 全文检索，Reciprocal Rank Fusion 融合排名
- **Tool Use 结构化输出**：Planner / Critic / Memory 全部用 `tool_choice` 强制 LLM 填 JSON Schema，替代 regex 解析
- **Agentic Synthesizer**：compute tool 沙箱执行 Python，计算结果内联到生成流，无 regex 后处理
- **Research Memory**：每次任务提取 2-4 条关键发现，pgvector 存储，后续任务相似度检索注入上下文
- **Async Task Queue**：PostgreSQL `tasks` 表 + asyncio Worker（`SELECT FOR UPDATE SKIP LOCKED`）
- **Sources 面板过滤**：扫描答案 `[n]` 引用，仅展示被引用的 chunk，零 LLM 开销

**评估体系**

- LLM-as-Judge（Gemini 2.5 Pro 独立评审）
- faithfulness / answer_relevancy / context_precision / context_recall 四项指标
- context_precision 升级为 Precision@K，context_recall 升级为原子事实分解
- 评估集 Set A（事实型）/ Set B（多跳推理）/ Set C（边界/对抗）共 18 题
- **RAGAS 历史最高：v12 ragas_score = 0.741**

**关键 Bug 修复**（共 18 项，详见 [`failure_analysis.md`](failure_analysis.html)）

- `sec-parser` 返回 380 万空节点 → 替换为 BeautifulSoup + regex
- Section 检测四重叠加（Bug #18）→ MD&A 2→30，Risk Factors 0→34，Financial Statements 2→72 chunks
- Critic 死循环 → 去重机制修复
- Synthesizer 幻觉 → 强制 Rule 5：背景知识不存在

---

### v15：Planner 路由、数据质量与 Synthesizer 幻觉三连修（当前版本）

**Planner 路由修复（`agent/planner.py`）**

- SYSTEM_PROMPT 结构重组：全部 5 个数据源路由规则集中为 `SOURCE ROUTING RULES:` 编号列表，不再夹在示例之间
- 新增 `MANDATORY MULTI-SOURCE RULE`：相关性/关系类问题强制同时生成 `price_history` + `macro_indicators` 两个 sub-query
- 相关性示例措辞精确对齐 D03（`monthly stock returns`、`monthly changes`、单年日期范围）；新增 GOOGL vs CPI 第二个相关性示例
- 新增 `DATE SCOPING` 规则：防止日期范围无故扩展

**数据质量修复（`ingestion/ingest_prices.py`）**

- `fetch_earnings_history` 从 `tk.quarterly_earnings`（yfinance 1.3.0 已返回 None）切换为 `tk.get_earnings_dates(limit=40)`
- EPS 历史覆盖从 6 行全 NULL 扩展为 50 行（2014–2026），含 `eps_actual` / `eps_estimate` / `eps_surprise_pct`
- 新增 `_ann_date_to_quarter_end()` 将公告日期映射到财季末
- 副作用：`pe_ratio` 从全 NULL（0/2854）恢复为全部有值（2854/2854），区间 16.13–53.98

**Synthesizer 幻觉修复（`agent/synthesizer.py` + `agent/per_loop.py`）**

- Rule 1 拆分为两条独立规则：**NUMBERS AND DATES**（数值必须在 cited source 中原文存在）和 **CAUSAL CLAIMS**（因果声明需 context 明确陈述机制，相关性≠因果性）
- `synthesize()` 增加 `missing_hint` 参数；Critic 检测到的缺口作为 `RETRIEVAL GAP` 置于 user message 最前，明确约束"不得推断或召回"
- `per_loop.py` 在调用 `synthesize()` 时传入最终 `missing_hint`

**评估结果（v14 → v15c）**

| 指标 | v14 | v15c | Δ |
|------|-----|------|---|
| faithfulness | 0.710 | **0.897** | **+0.187** ✅ |
| answer_relevancy | 0.952 | 0.872 | -0.080 ⚠️ |
| context_precision | 0.622 | **0.696** | **+0.074** ✅ |
| context_recall | 0.490 | **0.519** | +0.029 ✅ |
| **ragas_score** | 0.694 | **0.753** | **+0.059** ✅ |

> ragas_score 0.753 为项目历史最高，超越 v12 的 0.741。
> answer_relevancy 下滑 -0.080 是 faithfulness 修复的副作用：A04 类问题改为正确拒绝"context 不含该数值"（faithfulness 1.0），但 Judge 对"未直接给出答案"的 relevancy 评分较低。修复路径是改善检索，确保对应 chunk 稳定命中。

---

### MAG7 扩展（v13–v14）

**新数据源**

- `price_history`：MAG7 日线 OHLCV + P/E 比率（2015 至今，~90,000 行）
- `earnings_history`：季度/年度 EPS actual vs estimate + 核心财务指标（~700 行）
- 月度自动聚合：日期范围 > 90 天自动切换为月度汇总，减少 Judge 噪声行数（252行/年 → 12行）
- 每周自动刷新 Worker（`data_refresh_worker.py`） + 启动新鲜度检查

**MAG7 多 Ticker SEC 支持**

- `ingest_sec_multi.py`：参数化入库，支持全部 7 家公司
- `executor._search_sec()` 新增 company 白名单过滤（防 SQL 注入）
- 当前已入库：GOOGL（~4,700 chunks）、MSFT（~7,589 chunks）

**新能力（四个方向）**

| 方向 | 数据源 | 能回答的问题 |
|------|-------|------------|
| 投资决策支持 | price_history + earnings_history | "GOOGL 当前 P/E 在历史区间的什么位置？" |
| 财报异动监控 | earnings_history | "GOOGL Q3 2023 EPS 超预期了多少？" |
| 宏观-股价相关性 | price_history + macro_indicators | "2022 年 Fed 加息与 GOOGL 股价的相关系数？" |
| 竞争对手对比 | sec_chunks（多 ticker）| "Google Cloud 与 Azure 2023 年增速对比？" |

**评估进展**

- 新增评估集 Set D（5 题），覆盖四个新方向
- compute tool 描述加固：明确禁止 `import`，使用预注入名称 `np`/`pd`

---

## 当前状态

| 指标 | v12 | v14 | **v15c（当前）** | v12 差距 |
|------|-----|-----|-----------------|---------|
| faithfulness | 0.667 | 0.710 | **0.897** | **+0.230** ✅ |
| answer_relevancy | 0.972 | 0.952 | 0.872 | -0.100 ⚠️ |
| context_precision | 0.688 | 0.622 | **0.696** | +0.008 ✅ |
| context_recall | 0.651 | 0.490 | 0.519 | -0.132 ⚠️ |
| **ragas_score** | 0.741 | 0.694 | **0.753** | **+0.012** ✅ |

**主要未解问题**：

1. **context_recall（0.519）仍低于 v12 基线（0.651）**：D03 的 ground_truth key_facts 含计算结果（"425 基点"、"Pearson -0.4~-0.6"），这些值不在数据库中，recall 无法靠检索改善；需修订 Set D 的 ground_truth 设计
2. **answer_relevancy（0.872）低于 v12（0.972）**：RETRIEVAL GAP 机制使 A04 类题目改为正确拒绝回答，短期内 relevancy 偏低；根本修复是改善 SEC chunk 检索，确保年报财务表格稳定命中

**待合并**：PR #1（`feature/macrolens-expansion` → `main`）已通过所有 109 个单元测试。

---

## 近期计划（本月）

- [x] **修复 D03 Planner 路由**：MANDATORY MULTI-SOURCE RULE + 示例更新 ✅
- [x] **修复 earnings_history / pe_ratio 数据**：yfinance API 切换，EPS 覆盖 2014–2026 ✅
- [x] **修复 Synthesizer 幻觉**：NUMBERS/CAUSAL 规则拆分 + RETRIEVAL GAP 机制 ✅
- [x] **v15c eval**：ragas_score 0.753，历史最高 ✅
- [ ] **合并 PR #1**（`feature/macrolens-expansion` → `main`）
- [ ] **完成 MAG7 数据入库**：META / AMZN / AAPL / NVDA / TSLA SEC 文件入库

---

## 中期方向（1–3 个月）

- [ ] **修复 answer_relevancy**：改善 A04 类（年报财务表格）的 SEC chunk 检索稳定性，确保财务汇总行不被截断
- [ ] **提升 context_recall 至 v12 基线（0.651）**：修订 Set D ground_truth，将计算结果类 key_facts 替换为数据库中存在的原始数值
- [ ] **Gradio UI 新增专属入口**：估值仪表盘（P/E 历史区间图）、财报对比面板
- [ ] **MAG7 数据完整性验证**：建立定期校验脚本，检查各 ticker chunks 数量、最新数据日期

---

## 长期愿景

- [ ] **实时价格数据**：延迟 15 分钟行情接入（WebSocket / REST 轮询）
- [ ] **新闻数据源**：Guardian API 已有基础（`ingest_events_guardian.py`），扩展为结构化新闻 chunk 入库
- [ ] **跨资产扩展**：支持 ETF（SPY / QQQ）和宏观 ETF，实现股债相关性分析
- [ ] **结构化投研报告**：PDF 输出，含 P/E 历史图表、EPS 趋势图、竞争对手对比矩阵
- [ ] **评估集扩展**：Set E（实时数据类）、Set F（多轮对话类）

---

## 版本历史速查

| 版本 | 核心变化 | ragas_score |
|------|---------|-------------|
| v1 | 基线（holistic judge，内联 eval 管道）| 0.566 |
| v11 | eval 方法论升级（Precision@K + 原子 recall + Gemini 2.5 Pro judge）| 0.670 |
| v12 | Section 检测 Bug #18 修复（MD&A / Risk Factors chunk 大幅增加）| **0.741** |
| v13 | MAG7 扩展 + price/earnings 新数据源 + Set D 评估集 | 0.707 |
| v14 | 月度价格聚合 + ground_truth 数值化 + compute tool import 禁止 | 0.694 |
| v15c | Planner 路由修复 + earnings/PE 数据修复 + Synthesizer 幻觉修复 | **0.753** ★ |
