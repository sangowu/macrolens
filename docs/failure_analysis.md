# MacroLens 错误与修复记录

> 记录开发过程中遇到的 Bug、根因分析和修复方案。供面试讲解"如何 debug RAG 系统"使用。

---

## Bug #1：UnicodeEncodeError 打印特殊字符

**现象**
```
UnicodeEncodeError: 'gbk' codec can't encode character '✓' in position 0
```

**根因**  
Windows 终端默认 GBK 编码，Python `print()` 输出 UTF-8 特殊字符（✓/✗）时报编码错误。

**修复**  
将所有 ingestion 脚本中的 `✓` → `[OK]`，`✗` → `[ERR]`。统一所有文件读写加 `encoding='utf-8'`。

**教训**：跨平台项目在 Windows 开发时，所有控制台输出避免使用非 ASCII 字符。

---

## Bug #2：sec-parser 返回 3,838,828 个空节点

**现象**  
用 `sec-parser` 解析 GOOGL 10-K HTML 文件，返回 380 万个节点，全部内容为空。

**根因**  
GOOGL 的 SEC 申报文件格式（带 `ix:` XBRL 命名空间标签）与 `sec-parser` 的预期 HTML 结构不兼容，导致解析完全失效。

**修复**  
完全替换解析方案：用 BeautifulSoup 提取纯文本（去掉 `script/style/ix:header` 标签），再用正则识别 `Item X. Title` 边界进行 section 切分。

```python
ITEM_BOUNDARY = re.compile(
    r"(?:^|\n)[ \t]*(Item[\s\xa0]+\d+[A-Za-z]?[\.\s\xa0]+[A-Z][^\n]{3,80})",
    re.MULTILINE
)
```

**教训**：不要盲目信任第三方解析库，对非标准格式要有降级方案。

---

## Bug #3：`doc_type='GOOGL'` 且 `fiscal_year=NULL`

**现象**  
SEC chunk 写入数据库后，查询发现 `doc_type` 全为 `'GOOGL'`，`fiscal_year` 全为 `NULL`。

**根因（双重问题）**  
1. `parse_filing_meta()` 用了错误的路径深度：`filing_dir.parent.parent.name`（= `GOOGL`），而应为 `filing_dir.parent.name`（= `10-K`）。  
2. 原先尝试读取 `filing-details.json`，但该文件实际不存在；正确的元数据在 `full-submission.txt` 中。

**修复**  
```python
# 修复前
doc_type = filing_dir.parent.parent.name  # GOOGL

# 修复后
doc_type = filing_dir.parent.name         # 10-K

# 元数据来源改为 full-submission.txt，用正则提取
FILED_DATE_RE  = re.compile(r"FILED AS OF DATE:\s+(\d{8})")
PERIOD_END_RE  = re.compile(r"PERIOD OF REPORT:\s+(\d{8})")
```

**教训**：存储前先打印几条样本验证元数据，不要等写完再查。

---

## Bug #4：`period_end NOT NULL` 约束报错

**现象**  
重新 ingestion 时，部分旧文件因无法解析 `PERIOD OF REPORT` 字段而导致 `NOT NULL` 约束违反报错。

**根因**  
`full-submission.txt` 格式在早期 filing 中有时缺少 `PERIOD OF REPORT` 字段。

**修复**  
```sql
ALTER TABLE sec_chunks ALTER COLUMN period_end DROP NOT NULL;
```
同步更新 migration 脚本，并在 ingestion 代码中将缺失的 period_end 默认设为 `None`（而非抛错）。

---

## Bug #5：events 表 `content_tsv` 列不存在

**现象**  
执行事件检索 SQL 时报 `column "content_tsv" does not exist`。

**根因**  
events 表的全文索引列实际命名为 `description_tsv`，executor 中 SQL 硬编码了错误列名 `content_tsv`。

**修复**  
`agent/executor.py` 中事件检索 SQL 改为 `description_tsv`：
```sql
-- 修复前
content_tsv @@ websearch_to_tsquery(...)
-- 修复后
description_tsv @@ websearch_to_tsquery(...)
```

**教训**：column 名称应从 migration SQL 文件统一查阅，不要靠记忆。

---

## Bug #6：`macro_series_meta` 字段名错误

**现象**  
宏观数据检索 SQL 执行报错 `column s.title does not exist`、`column s.units does not exist`。

**根因**  
schema 中字段实际为 `name` 和 `unit`（单数），代码里写的是 `title` 和 `units`（复数/别名）。

**修复**  
`agent/executor.py` 中宏观 SQL：
```sql
-- 修复前
s.title, s.units
-- 修复后
s.name, s.unit
```

---

## Bug #7：PER Loop 检索不到 FY2022 SEC 数据

**现象**  
问 "Google 2022 advertising revenue" 时，PER Loop 返回的 context 完全不含 2022 年 SEC chunk，答案靠幻觉生成。

**根因（三重叠加）**  
1. Section 检测失效：BeautifulSoup 提取文本后，section 标签因 `\xa0`（non-breaking space）导致正则未匹配，几乎所有 chunk 都被标为 `Business` 或空。  
2. Executor 含 section 过滤（`section = ANY(...)` WHERE 子句），`MD&A` 过滤条件将所有 chunk 排除。  
3. `top_k=8` 太小，FY2022 的向量最近邻排在第 8+ 位，截断后丢失。

**修复**  
- 移除 executor SEC 查询中的 section 过滤，只保留 `fiscal_year` 过滤。  
- `config.yaml` 中 `top_k: 8 → 12`，`candidate_k: 15 → 20`。

**后续修复**（Bug #18）：`\xa0` 只是入口，实际存在四个叠加问题——见 Bug #18。

---

## Bug #8：WinError 10061 — 连接被拒绝（SSH 隧道端口错误）

**现象**  
```
WinError 10061: No connection could be made because the target machine actively refused it
```

**根因**  
`config.yaml` 中 `base_url: http://localhost:6006`，但 SSH 隧道实际将远程服务转发到本地 `8000` 端口。

**修复**  
`config.yaml` 改为 `base_url: http://localhost:8000`。

**验证方式**：`netstat -an | findstr 8000` 确认本地端口监听状态。

---

## Bug #9：SSH 隧道密码认证不支持

**现象**  
factory.py 自动启动 SSH 隧道时只支持 key_file，使用密码认证（AutoDL 云服务器）时失败。

**根因**  
`SSHConfig` 没有 `ssh_port` 字段（AutoDL 使用非标准端口 57671），且不支持 `password_env` 参数。

**修复**  
`models/config.py` 中 `SSHConfig` 新增字段，`factory.py` 中隧道建立逻辑同步更新：
```python
class SSHConfig:
    ssh_port: int = 22          # 新增
    password_env: str | None = None  # 新增，从 os.environ 读取
```

---

## Bug #10：Chunk Ablation 全部得分 0

**现象**  
`eval/chunk_ablation.py --files 3` 运行完毕，三种策略 precision/recall 全为 0。

**根因**  
`iter_filing_files()` 返回按文件名字母序排列的结果，取前 3 个是 FY2015/2016/2017；而 Set A 问题问的是 FY2021-2023，数据完全错位。

**修复**  
```python
# 修复前
files = [f for f in iter_filing_files() if "10-K" in str(f)][:args.files]

# 修复后（取最新 N 份）
all_10k = [f for f in iter_filing_files() if "10-K" in str(f)]
files = sorted(all_10k, reverse=True)[:args.files]
```

---

## Bug #11：Synthesizer faithfulness=0（幻觉生成）

**现象**  
RAGAS 评估中 A01/A08/B04/B05/C02 等题目 faithfulness=0，Synthesizer 在 context 不足时生成听起来合理但无来源支撑的数字和日期。

**根因**  
SYSTEM_PROMPT 只有软约束 `"Do not fabricate numbers or dates not present in the context"`，LLM 的内在倾向是生成完整答案，软约束不足以阻止幻觉。

**修复**  
将软约束改为三条硬规则（`agent/synthesizer.py`）：
1. 每个数字/日期/百分比必须有 `[n]` 引用，找不到来源就不说
2. context 缺失时明确说 "The provided context does not contain [X]"，不推断不估算
3. 新增身份约束：background knowledge 不得用于补全缺失 context

**效果**：RAGAS 三组均有提升：Set A +0.031、Set B +0.024、Set C +0.028

**教训**：RAG 系统的 faithfulness 约束必须是硬规则，配合 "每个 claim 都要引用" 的要求效果最佳。

---

## 优化 #1：Critic 两轮输出相同 missing（死循环问题）

**现象**  
从日志观察到，第 1 轮和第 2 轮 Critic 给出完全相同的 missing 原因，第 2 轮 Planner 生成的子查询与第 1 轮高度重叠，只新增了 4 条 context。

**根因**  
`per_loop.py` 传给第 2 轮 Planner 的 prompt 只包含 missing_hint，没有告知已经搜过哪些 queries，Planner 没有足够信息生成真正不同的检索策略。

**修复**  
在每轮 prompt 中附加已检索的子查询列表：
```python
already = ", ".join(f'"{q}"' for q in searched_queries)
prompt = (
    f"{question}\n\n"
    f"Focus on what's still missing: {missing_hint}\n"
    f"Already searched (do NOT repeat these queries): [{already}]"
)
```

**效果**：Set B 从 0.366 → 0.395（+0.029），Planner 每轮探索不同维度，new context 从 0 提升到 16-26 条。

---

## 优化 #2：Planner section filter 调整实验

**现象**  
Planner 对 sec_chunks 子查询自动加了 `section: MD&A` 过滤，但部分相关内容分布在其他章节，可能被过滤掉。

**实验过程与结论**  

| 方案 | Set A | Set B | Set C | 结论 |
|------|-------|-------|-------|------|
| 完全移除 section filter | 0.667 | 0.395 | 0.560 | Set C 大幅下降 |
| 改为"仅用户明确提及时才加" | 0.654 | 0.395 | 0.602 | Set C 部分恢复但不稳定 |

**结论**  
section filter 对综合分析类问题（Set C）有帮助，完全移除反而有害。改为可选是正确方向，但 LLM 判断何时该加 section 仍不稳定。最终保留"可选"策略，接受现状，边际收益已递减。

**根本原因**：Set C 下降更多来自 Qwen3-Embedding-0.6B 本身比 BGE-M3 弱，非 Planner 问题。

---

## Bug #12：Gradio 6.x API Breaking Changes


**现象**  
升级到 Gradio 6.14 后启动 UI 报错：
- `theme` 参数从 `gr.Blocks()` 移到 `launch()`
- `gr.Chatbot` 不支持 `show_copy_button`、`bubble_full_width`、`type` 参数
- history 格式从 `[[user, assistant]]` 改为 `[{"role": "user", "content": ...}]`

**修复**  
- `theme=gr.themes.Soft()` 移至 `demo.launch()` 调用
- 删除 Chatbot 中不兼容的参数
- history 格式改为 messages dict 格式

**教训**：Gradio 版本跨越大版本（5→6）时需查阅 migration guide，不要假设 API 向后兼容。

---

## Bug #13：Eval 重写了 PER Loop 但漏掉 `already_searched`

**现象**  
`run_eval.py` 里存在一个 `_run_with_context_capture` 内部函数，独立实现了 Plan→Execute→Critique 循环。Set B 多跳题的 context_recall 偏低，难以判断是 pipeline 问题还是 eval 问题。

**根因**  
`_run_with_context_capture` 的第 2 轮 prompt 只拼了 `missing_hint`，没有附加 `already_searched` 列表。`per_loop.py` 里的防重机制在 eval 过程中完全没有生效。Planner 在第 2 轮重复了第 1 轮的子查询，new context=0，等价于只跑了 1 轮。

**修复**  
删除 32 行冗余函数，直接调用 `per_loop.run()`：
```python
answer, context = per_loop_run(q.question, cfg, conn, embedder, llm, max_iter=args.max_iter)
```

**教训**：eval 脚本里"重实现"pipeline 是危险的，任何 pipeline 的改动都需要同步更新 eval，极易遗漏。直接调用被测函数才能保证评估的是真实行为。

---

## Bug #14：Judge 只能看到全量 context 的 5%

**现象**  
faithfulness 和 context_recall Judge 给出不稳定的低分，即使答案明显正确。

**根因**  
`_format_context_flat` 的硬上限是 `max_chars=3000`，每条 SEC chunk 截断到 300 字符。一次评估有 20–25 条 context，3000 字符只覆盖了全量内容的不到 5%。Judge 因为看不完整内容，对"context 是否支撑答案"的判断严重失真。

**修复**  
- `max_chars` 3000 → 10000，每条 SEC chunk 截断 300 → 600 字符
- `_format_context_list` 的 `max_items` 15 → 25，每条 150 → 300 字符

**教训**：LLM-as-Judge 的上下文窗口限制是隐性的评估偏差来源。Judge 看不到的内容等于不存在，必须确保 Judge 有足够的信息做判断。

---

## Bug #15：Gemini 2.5 Pro `resp.text` 返回 None 导致评估 CSV 出现重复行

**现象**  
评估 CSV 里每道题出现两行，大多数指标为空；`ragas_score` 有时显示为 `1.0`（实际只有 `answer_relevancy` 成功）。

**根因（三层叠加）**  
1. Gemini 2.5 Pro 是思考模型（thinking model），某些 prompt 下 `resp.text` 返回 `None`
2. `GeminiClient.chat()` 直接 `resp.text.strip()` → `AttributeError: 'NoneType'`
3. `evaluate_all` 内部捕获了这个异常，把指标设为 `None`，但 `ragas_score` 也变成 `None`
4. `run_eval.py` 中 `print(f"RAGAS: {score:.3f}")` 对 `None` 格式化 → `TypeError`
5. 外层 `try/except` 捕获 `TypeError`，写入第二条空行

**修复**  
```python
# gemini_client.py
return (resp.text or "").strip()   # 防御 None

# metrics.py
if not raw:
    raise ValueError("Judge returned empty response")  # 早退，明确报错

# run_eval.py
def _fmt(v): return f"{v:.3f}" if v is not None else "None"  # 安全格式化
```

**教训**：思考模型的响应格式与普通模型不同，接入新模型时必须验证 `resp.text` 的边界行为。连锁异常（A 崩溃 → B 捕获 → C 写错数据）需要在每一层都加防御。

---

## Bug #16：`answer_relevancy` Judge 将正确的"无法回答"判为 0.0

**现象**  
C03（"如果 Fed 降息到零，Google 股价会怎样"）在评估中 `answer_relevancy=0.0`。系统给出了"无法从历史财报回答投机性问题"的正确拒答，但 Judge 认为"答案没有回答问题"。

**根因**  
`_RELEVANCY_PROMPT` 对 `1.0` 的定义只有"直接完整地回答问题"，没有覆盖"问题本身不可回答"的场景。Judge 把正确的拒答当成了不相关。

**修复**  
在 prompt 的 1.0 描述中补充：
```
- 1.0: ... Also 1.0 if the question is speculative, out-of-scope, or unanswerable
       and the answer correctly says so.
```

**效果**：C03 answer_relevancy 0.0 → 1.0

**教训**：评估边界案例（Set C 的对抗/超范围题）与主流题目有不同的"正确答案"定义，Judge prompt 必须显式覆盖这些场景，否则指标会系统性低估系统的正确行为。

---

## Bug #17：Synthesizer 用背景知识填补缺失 context（faithfulness 低）

**现象**  
B02（COVID-19 对 Google 营收的影响）评估 faithfulness=0.2，Judge 指出答案包含"American Rescue Plan"、"shift to less commercial topics"等内容，但这些信息不在任何 context chunk 里。

**根因**  
SYSTEM_PROMPT 有三条硬规则，但都指向"找不到来源就不写"。LLM 生成完整叙述的内在倾向强于软性约束，在问题需要宏观背景时会无意识地引入背景知识，特别是叙事性强的历史事件（COVID 影响、经济危机）。

**修复**  
新增 Rule 5，从根本上切断 LLM 引用通用知识的动机：
```
5. Your general knowledge about world events, economics, or companies does NOT exist
   for the purpose of this answer. If it is not in the retrieved context, it did not happen.
```

**效果**：B02 faithfulness 0.20 → 0.30（部分改善）。

**残余问题**：B01/B02 这类因果分析题（"加息如何影响广告收入"）在 SEC 财报中找不到直接的因果陈述，只有孤立的数字。Rule 5 可以阻止 Synthesizer 补充背景叙述，但无法提供财报中本不存在的因果分析。这是数据源的结构性局限，根本解决方案是引入分析师报告。

**教训**：对于叙事性强的事件（COVID、经济危机），LLM 极难严格区分"来自 context"和"来自训练数据"的信息。Synthesizer 的 faithfulness 对于此类题目存在系统性天花板。

---

## Bug #18：Section 检测四重叠加问题（ingest_sec.py）

**现象**  
FY2022 10-K 的 `sec_chunks` 中，MD&A 只有 2–5 条，Risk Factors 为 0 条，所有 chunk 几乎全部被归入 `Business`。

**根因（四重叠加，每项独立都会导致错误）**

1. **`\xa0` 未 normalize**：`soup.get_text()` 把 HTML `&nbsp;` 保留为 `\xa0`（non-breaking space）。虽然正则包含 `\xa0`，但 HTML 里有时是多个 `\xa0` 混合普通空格，导致部分标题不匹配。修复：`full_text.replace("\xa0", " ")`。

2. **无 `re.IGNORECASE`**：10-K 正文标题是全大写（`ITEM 7.\nMANAGEMENT'S DISCUSSION...`），目录是混合大小写（`Item 7. Management's...`）。正则无 `re.IGNORECASE` 时只匹配目录，正文标题完全被忽略。修复：加 `re.IGNORECASE`。

3. **目录边界覆盖正文边界**：`findall` 按位置顺序返回，目录出现在前（位置 ~6000–7500）；用 list 保留所有匹配时，每个 Item 在 TOC 和正文各有一次，两次边界都被记录。结果是 TOC 里 `Item 7` 到 `Item 7A` 之间只有一个页码（2 个 chunk），正文里的真实 MD&A 内容被错误地归入前一个 section。修复：用 `seen` dict 对每个 item 编号只保留最后一次出现位置（正文 > 目录）。

4. **`SECTION_MAP` startswith 误匹配**：`"item 1a...".startswith("item 1")` 为 True，导致所有 Item 1A（Risk Factors）内容被错误归入 Item 1（Business）。修复：改用 `re.match(rf"{re.escape(k)}[\s.]", section.lower())` 加 word-boundary。

5. **旧数据残留**：`ON CONFLICT DO NOTHING` 无唯一约束，等价于总是插入，re-ingest 时旧版 section 名称的 chunk 累积在库中。修复：re-ingest 前 `TRUNCATE TABLE sec_chunks`。

**修复效果（chunk 分布）**

| Section | 修复前 | 修复后 |
|---------|-------|-------|
| Business | ~535 | 12 |
| Risk Factors | 0 | 34 |
| MD&A | 2–5 | 30 |
| Financial Statements | 2–3 | 72 |

**修复效果（RAGAS 指标，v11 → v12）**

| 指标 | 修复前（v11） | 修复后（v12） | Δ |
|------|------------|------------|---|
| faithfulness | 0.544 | **0.667** | **+0.123** |
| answer_relevancy | 0.944 | 0.972 | +0.028 |
| context_precision | 0.603 | 0.688 | +0.085 |
| context_recall | 0.587 | 0.651 | +0.064 |
| **ragas_score** | 0.670 | **0.741** | **+0.071** |

`faithfulness` 提升最显著（+0.123）：修复前 MD&A 只有 2 个 chunk，Synthesizer 检索到的主要是目录和样板段落，难以找到 faithfulness 验证所需的真实数据。修复后 30 个实质性 MD&A chunk 进入检索池，Judge 可以直接核对答案声明。

**教训**：HTML 文档（SEC 10-K）有目录 + 正文两个结构层次，基于文本的正则 section 检测容易同时匹配两者。应优先取最后出现位置（正文），而非第一次（目录）。section 字段校验应在 ingest 后立即查询 DB 分布，而不是等到 eval 分数异常时才发现。

---

## 观察 #19：v13 新数据源导致 context_precision / context_recall 下降

**版本**：v12 → v13（新增 price_history / earnings_history / MAG7 支持）

**现象（v12 → v13 全量对比，含 Set D）**

| 指标 | v12 | v13 | Δ |
|------|-----|-----|---|
| faithfulness | 0.534 | **0.713** | **+0.179** ✅ |
| answer_relevancy | 0.951 | 0.930 | -0.021 ⚠️ |
| context_precision | 0.627 | 0.571 | -0.056 ⚠️ |
| context_recall | 0.657 | 0.590 | -0.067 ⚠️ |
| **ragas_score** | 0.698 | **0.707** | +0.009 ✅ |

**根因分析**

**faithfulness 大幅提升（+0.179）**：price_history / earnings_history 是结构化数值表，LLM 生成答案时每个数字都有明确的 context 来源，Judge 核对时容易逐一验证，幻觉率显著降低。这是新数据源带来的直接收益。

**context_precision 下降（-0.056）的根因**：`_search_price_history()` 返回日线粒度数据（每天一条），一个问题可能拉入 200-300 行价格记录。Judge 评估 Precision@K 时，把"2022-03-15 收盘价 \$2,850"这类单行视为低相关（它单独看确实不能回答"相关系数是多少"），导致大量行被判为不相关 chunk，拉低 precision 分数。本质是**检索粒度与 Judge 评估粒度不匹配**。

**context_recall 下降（-0.067）的根因**：Set D 的 ground_truth 以方法论描述为主（"应计算 P/E 百分位并给出区间描述"），Judge 做原子事实分解时拆出的 claim 往往是"需要调用 compute tool"而非具体数值，context 中的价格行无法直接覆盖这类 claim，导致 recall 被低估。本质是 **ground_truth 设计偏描述性而非数值性**。

**answer_relevancy 轻微下降（-0.021）**：Set D 的估值/相关性问题答案结构更长（含 compute 代码和历史区间描述），Judge 对答案是否"直接切题"的判断略偏保守。

**改进方向**

1. **Precision 修复（代码层）**：在 `_search_price_history()` 加月度/季度聚合，将日线数据压缩为时间段汇总后送入 context，减少低信息密度的行数。预计 precision 可恢复至 0.62+。

```python
# 当前：每天一行 → 200 行
# 改进：按月聚合 → 20 行，每行含 open/close/pe_ratio 月均值
```

2. **Recall 修复（评估层）**：将 Set D 的 ground_truth 改为数值型（如"Q3 2023 EPS actual=1.55，est≈1.45，surprise≈+6.9%"），使 Judge 的原子事实分解能与 context 中的数值直接比对。

3. **综合判断**：faithfulness +0.179 是实质性改善（新数据源解决了幻觉问题），precision/recall 的下降主要是**评估方法与新数据类型不完全匹配**导致，而非检索质量真的变差。优先修复 #1（聚合），再重跑 v13 eval 验证。

**教训**：引入结构化时序数据（日线价格）时，评估指标的适配需要同步考虑。Precision@K 假设每个 context 条目是一个独立的文本 chunk，对"一行=一个时间点"的时序数据不适用。应在引入新数据源时同步审查 eval 的 format_context 函数，考虑聚合后再送 Judge。

---
