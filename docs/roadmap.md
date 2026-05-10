# MacroLens 项目路线图

> 迭代历史、当前状态与未来方向。最后更新：2026-05。

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

| 指标 | v12（历史最高）| v14（当前）| 差距 |
|------|-------------|-----------|------|
| faithfulness | 0.667 | **0.710** | **+0.043** ✅ |
| answer_relevancy | 0.972 | 0.952 | -0.020 |
| context_precision | 0.688 | 0.622 | -0.066 ⚠️ |
| context_recall | 0.651 | 0.490 | -0.161 ⚠️ |
| **ragas_score** | **0.741** | 0.694 | -0.047 |

**主要未解问题**：context_recall 显著低于 v12 基线。

根因：D03 类问题（宏观-股价相关性）的 Planner 未强制同时检索 `price_history` + `macro_indicators`，context 缺少 FEDFUNDS 数据，Judge 原子核对失败。详见 [`failure_analysis.md` 观察 #19](failure_analysis.html)。

**待合并**：PR #1（`feature/macrolens-expansion` → `main`）已通过所有 109 个单元测试。

---

## 近期计划（本月）

- [ ] **修复 D03 Planner 路由**：相关性问题强制同时检索 `price_history` + `macro_indicators`
- [ ] **跑 v15 eval**，验证 recall 回升至 >= 0.60
- [ ] **合并 PR #1**（`feature/macrolens-expansion` → `main`）
- [ ] **完成 MAG7 数据入库**：META / AMZN / AAPL / NVDA / TSLA SEC 文件入库

---

## 中期方向（1–3 个月）

- [ ] **提升 context_recall 至 v12 基线（0.651）**：优化 Set D ground_truth + Planner 路由
- [ ] **EPS estimate 数据质量**：yfinance 历史 estimate 覆盖不完整，考虑接入 Alpha Vantage / Polygon 提供更准确的分析师共识数据
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
| v15 | D03 Planner 路由修复（计划中）| 目标 >= 0.72 |
