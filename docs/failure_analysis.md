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

**待解决**：section 检测 bug（`\xa0` 问题）仍存在，重新 ingestion 时需修复正则。

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
