"""
评估问题集。

Set A: 事实型（有明确数值/日期答案）
Set B: 多跳推理（需要跨数据源）
Set C: 边界/对抗（超范围、模糊、比较）—— 域内但数据缺失/推测，管道内拒答
Set D: 方向覆盖（价格 / 财报异动 / 宏观-股价 / MAG7 对比）—— 手写 GT
Set E: 自动生成（真值表程序化出题 + 可判分数值 GT）—— 见 eval/generators/
Set F: 域外拒答（完全不属于金融领域，应被 Planner in_scope 前置拦截）

Set A-D 的 ground_truth 是手写散文，只被 context_recall 消费。
Set E 额外带 `expected`（数值 GT + tolerance）和 `source`（溯源），
可被 metrics.answer_correctness 自动判分。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DATASET_DIR = Path(__file__).parent / "datasets"


@dataclass(frozen=True)
class ExpectedValue:
    """一个可自动判分的数值真值。

    value/tolerance 用于 Python 侧确定性比对；
    description 是字段语义，喂给 answer_correctness 的抽取器，
    让它知道要从答案里找哪个数（而不是找"正确的"数）。
    """
    value: float
    tolerance: float
    description: str


@dataclass(frozen=True)
class GroundTruthSource:
    """GT 溯源：这个数字是从哪张表的哪几列来的。

    手写 GT 无法回答"你怎么知道这是对的"，这个字段就是答案。
    """
    table: str
    columns: list[str]
    ticker: str | None = None
    period_end: str | None = None       # ISO 日期，锚定时间的唯一可信列
    generator: str | None = None        # 生成器模板名，便于按模板切片分析
    note: str = ""


@dataclass
class Question:
    qid: str
    set_name: str          # "A" | "B" | "C" | "D" | "E" | "F"
    question: str
    ground_truth: str      # 参考答案（用于 recall 评估）
    key_facts: list[str] = field(default_factory=list)   # 必须出现在 context 中的关键事实
    # ── 以下仅自动生成题（Set E）填充 ────────────────────────
    expected: dict[str, ExpectedValue] | None = None     # 字段名 → 数值真值，供 answer_correctness
    source: GroundTruthSource | None = None              # GT 溯源


SET_A: list[Question] = [
    Question(
        qid="A01",
        set_name="A",
        question="What was Google's total advertising revenue in fiscal year 2022?",
        ground_truth="Google's total advertising revenue in 2022 was approximately $224.47 billion.",
        key_facts=["224", "advertising revenue", "2022"],
    ),
    Question(
        qid="A02",
        set_name="A",
        question="What was the Federal Funds Rate in December 2022?",
        ground_truth="The Federal Funds Rate was approximately 4.1% in December 2022.",
        key_facts=["4.1", "federal funds rate", "december 2022"],
    ),
    Question(
        qid="A03",
        set_name="A",
        question="When did the Federal Reserve first raise interest rates in 2022?",
        ground_truth="The Federal Reserve first raised interest rates in March 2022.",
        key_facts=["march 2022", "rate hike", "federal reserve"],
    ),
    Question(
        qid="A04",
        set_name="A",
        question="What was Google Cloud's revenue in fiscal year 2023?",
        ground_truth="Google Cloud revenue in 2023 was approximately $33.1 billion.",
        key_facts=["33", "google cloud", "2023"],
    ),
    Question(
        qid="A05",
        set_name="A",
        question="What was the US unemployment rate (UNRATE) in January 2023?",
        ground_truth="The US unemployment rate in January 2023 was approximately 3.4%.",
        key_facts=["3.4", "unemployment", "january 2023"],
    ),
    Question(
        qid="A06",
        set_name="A",
        question="What was Alphabet's net income for fiscal year 2021?",
        ground_truth="Alphabet's net income for fiscal year 2021 was approximately $76 billion.",
        key_facts=["76", "net income", "2021"],
    ),
    Question(
        qid="A07",
        set_name="A",
        question="What was the US CPI inflation rate in June 2022?",
        ground_truth="US CPI inflation peaked at approximately 9.1% year-over-year in June 2022.",
        key_facts=["9.1", "cpi", "june 2022"],
    ),
    Question(
        qid="A08",
        set_name="A",
        question="How many employees did Alphabet have at the end of 2022?",
        ground_truth="Alphabet had approximately 190,234 full-time employees at the end of 2022.",
        key_facts=["190", "employees", "2022"],
    ),
]

SET_B: list[Question] = [
    Question(
        qid="B01",
        set_name="B",
        question="How did Federal Reserve rate hikes in 2022 affect Google's advertising revenue growth?",
        ground_truth=(
            "Fed rate hikes in 2022 created macroeconomic uncertainty that pressured advertiser spending. "
            "Google's advertising revenue grew to $224.47B but faced headwinds including a 5% decline in "
            "cost-per-click in Q3 2022 and unfavorable foreign exchange impacts driven partly by dollar strength."
        ),
        key_facts=["rate hike", "advertising revenue", "cost-per-click", "2022"],
    ),
    Question(
        qid="B02",
        set_name="B",
        question="How did the COVID-19 pandemic affect Google's revenue in 2020 and what was the recovery trajectory?",
        ground_truth=(
            "COVID-19 impacted advertising spending in early 2020 but Google's revenues recovered strongly. "
            "Travel and retail advertising declined while e-commerce advertising grew."
        ),
        key_facts=["covid", "2020", "advertising", "recovery"],
    ),
    Question(
        qid="B03",
        set_name="B",
        question="What risks did Google identify related to AI competition and how did the industry landscape change from 2022 to 2024?",
        ground_truth=(
            "Google identified risks from AI competition including ChatGPT/OpenAI and Microsoft Bing AI. "
            "The company responded by launching Bard and Gemini while facing antitrust scrutiny."
        ),
        key_facts=["ai", "competition", "chatgpt", "openai", "microsoft", "gemini"],
    ),
    Question(
        qid="B04",
        set_name="B",
        question="How did macroeconomic conditions in 2023 compare to 2022 and what was the impact on Google's business?",
        ground_truth=(
            "2023 saw moderating inflation and stabilizing rates compared to 2022's aggressive tightening. "
            "Google's advertising revenue recovered with stronger growth in 2023."
        ),
        key_facts=["2023", "inflation", "advertising", "recovery", "federal reserve"],
    ),
    Question(
        qid="B05",
        set_name="B",
        question="What is the relationship between the Federal Funds Rate changes and Google's cost-per-click trends from 2021 to 2023?",
        ground_truth=(
            "As the Fed raised rates aggressively in 2022, macroeconomic pressure reduced advertiser budgets "
            "leading to declining cost-per-click metrics. CPC improved as conditions stabilized in 2023."
        ),
        key_facts=["federal funds rate", "cost-per-click", "2022", "2023"],
    ),
]

SET_C: list[Question] = [
    Question(
        qid="C01",
        set_name="C",
        question="What was Google's revenue in 2030?",
        ground_truth="This question cannot be answered as 2030 data is not available in the system.",
        key_facts=[],
    ),
    Question(
        qid="C02",
        set_name="C",
        question="Compare Google's advertising revenue growth rate versus US GDP growth rate from 2019 to 2023.",
        ground_truth=(
            "Google advertising revenue grew significantly faster than US GDP over 2019-2023, "
            "with advertising CAGR around 15-20% versus GDP nominal growth of 5-7% annually."
        ),
        key_facts=["advertising revenue", "gdp", "2019", "2023", "growth"],
    ),
    Question(
        qid="C03",
        set_name="C",
        question="What would happen to Google's stock price if the Fed cuts rates to zero?",
        ground_truth="This is a speculative forward-looking question that cannot be answered from historical filings.",
        key_facts=[],
    ),
    Question(
        qid="C04",
        set_name="C",
        question="Did Google mention climate change as a business risk?",
        ground_truth="Google/Alphabet's SEC filings mention environmental sustainability but climate change as a direct business risk varies by filing year.",
        key_facts=["climate", "environment", "risk"],
    ),
    Question(
        qid="C05",
        set_name="C",
        question="How does Google's revenue per employee compare to Amazon?",
        ground_truth=(
            "While the system supports MAG7 companies (GOOGL, MSFT, META, AMZN, AAPL, NVDA, TSLA), "
            "Amazon SEC data has not been ingested yet. A direct revenue-per-employee comparison "
            "cannot currently be made."
        ),
        key_facts=[],
    ),
]

# ── Set D: 新方向覆盖（价格数据 / 财报异动 / 宏观-股价 / MAG7 对比）────────

SET_D: list[Question] = [
    # D01 — 方向1：估值决策支持
    Question(
        qid="D01",
        set_name="D",
        question="Is GOOGL stock expensive compared to its historical P/E ratio range from 2019 to 2024?",
        ground_truth=(
            "The context contains monthly P/E ratio data for GOOGL from 2019 to 2024 in the price_history "
            "table (pe_ratio / avg_pe column). From this data, the historical minimum, maximum, and mean "
            "P/E can be calculated. The P/E trough occurred around 2022, and the peak around 2020-2021. "
            "Whether the current P/E is expensive or cheap relative to history can be assessed by computing "
            "the percentile of the current value within the historical range."
        ),
        key_facts=["P/E", "2022", "2021", "pe_ratio"],
    ),
    # D02 — 方向2：财报异动监控
    Question(
        qid="D02",
        set_name="D",
        question="Did GOOGL beat or miss EPS estimates in Q3 2023, and what was the surprise percentage?",
        ground_truth=(
            "GOOGL reported Q3 2023 EPS actual of $1.55, beating analyst estimates of $1.45, "
            "representing a positive earnings surprise of 7.05%. "
            "Note: Revenue data may not be available in the earnings_history table."
        ),
        key_facts=["Q3 2023", "1.55", "1.45", "7.05"],
    ),
    # D03 — 方向3：宏观-股价相关性分析
    Question(
        qid="D03",
        set_name="D",
        question="What was the correlation between Federal Funds Rate changes and GOOGL monthly stock returns in 2022?",
        ground_truth=(
            "The context provides the necessary raw data to compute the correlation: "
            "(1) Monthly Federal Funds Rate for 2022: starting at approximately 0.08% in January, "
            "rising to approximately 4.33% in December (series FEDFUNDS). "
            "(2) Monthly GOOGL closing prices for 2022. "
            "From these two series, the monthly rate changes and monthly stock returns can be calculated, "
            "and the Pearson correlation coefficient can be computed. "
            "The correlation is expected to be negative (rate hikes coincided with stock price declines)."
        ),
        key_facts=["0.08", "4.33", "2022", "FEDFUNDS"],
    ),
    # D04 — 方向3（延伸）：价格趋势分析
    Question(
        qid="D04",
        set_name="D",
        question="How did GOOGL stock price perform in 2022 compared to 2021, and what was the approximate percentage change?",
        ground_truth=(
            "The context contains monthly GOOGL closing prices for both 2021 and 2022 from price_history. "
            "In 2021 GOOGL prices ranged from approximately $86 (Jan) to $148 (Oct/Nov peak), "
            "and in 2022 from approximately $144 (Jan) to $88 (Dec), "
            "representing a decline of approximately 38-40% for 2022 vs a strong gain in 2021. "
            "Note: The causal explanation (Fed rate hikes, advertiser spending) is not in the "
            "price_history context and should not be asserted without SEC/events data."
        ),
        key_facts=["2022", "2021", "GOOGL"],
    ),
    # D05 — 方向4：MAG7 竞争对手对比（MSFT 数据已入库）
    Question(
        qid="D05",
        set_name="D",
        question="Compare Google Cloud and Microsoft Azure cloud revenue growth rates in fiscal year 2023. Which grew faster?",
        ground_truth=(
            "Google Cloud revenue for FY2023 was $33,088 million (approximately $33.1 billion), "
            "representing approximately 26-28% year-over-year growth. "
            "Microsoft's Intelligent Cloud segment for FY2023 was approximately $87.9 billion. "
            "Both segments showed similar growth rates in 2023. "
            "Note: Microsoft reports 'Intelligent Cloud' which includes Azure; "
            "the MSFT 10-K may not separately break out Azure revenue."
        ),
        key_facts=["Google Cloud", "33,088", "2023", "Microsoft", "revenue"],
    ),
]

# ── Set F: 域外拒答（应被 Planner 的 in_scope 前置拦截）──────────
#
# 与 Set C 的区别：Set C 是"域内但数据缺失/推测"（走完整管道后由
# Synthesizer 拒答）；Set F 是"完全不属于 MacroLens 领域"，应在
# Planner 阶段就以 in_scope=false 短路，根本不进入检索与合成。
#
# 期望行为：per_loop.run() 返回 reject_reason，context 为空（0 检索）。
# key_facts=[] —— 没有任何事实需要出现在 context 中。
#
# 反例护栏：C02（广告增速 vs GDP）、C05（GOOGL vs AMZN）看起来像域外，
# 实为域内跨源比较，必须留在 Set C，不得被误挡。跑 eval 时应确认
# Set C 分数不因本过滤下降。

SET_F: list[Question] = [
    Question(
        qid="F01",
        set_name="F",
        question="What's the weather in Dublin today?",
        ground_truth="Out of scope: MacroLens only covers MAG7 companies and US macroeconomics, not weather.",
        key_facts=[],
    ),
    Question(
        qid="F02",
        set_name="F",
        question="How do I write a for loop in Python?",
        ground_truth="Out of scope: this is a general programming question, unrelated to financial research.",
        key_facts=[],
    ),
    Question(
        qid="F03",
        set_name="F",
        question="Can you give me a recipe for a classic Italian carbonara?",
        ground_truth="Out of scope: cooking recipes are entirely outside MacroLens's financial domain.",
        key_facts=[],
    ),
    Question(
        qid="F04",
        set_name="F",
        question="What was Spotify's revenue last year?",
        ground_truth=(
            "Out of scope: Spotify is not one of the supported MAG7 tickers "
            "(GOOGL, MSFT, META, AMZN, AAPL, NVDA, TSLA) and has no ingested data."
        ),
        key_facts=[],
    ),
    Question(
        qid="F05",
        set_name="F",
        question="Who won the 2022 FIFA World Cup?",
        ground_truth="Out of scope: sports results are unrelated to MAG7 companies or US macroeconomics.",
        key_facts=[],
    ),
]

# ── Set E: 自动生成题（从真值表程序化生成）────────────────────
#
# 不在代码里手写，而是由 eval/generators/*.py 读数据库生成 JSONL，
# 产物 check 进 git。这样：
#   1. 评测集可复现、可 diff（v22 和 v23 跑的是不是同一批题？git 说了算）
#   2. GT 可 code review
#   3. 数据刷新后重跑生成器，git diff 直接暴露哪些真值变了
#   4. run_eval 不需要连数据库就能构建题目


def to_jsonl_dict(q: Question) -> dict:
    """Question → JSONL 行（供生成器写文件）。"""
    d: dict = {
        "qid": q.qid,
        "set_name": q.set_name,
        "question": q.question,
        "ground_truth": q.ground_truth,
        "key_facts": q.key_facts,
    }
    if q.expected:
        d["expected"] = {
            k: {"value": v.value, "tolerance": v.tolerance, "description": v.description}
            for k, v in q.expected.items()
        }
    if q.source:
        d["source"] = {
            "table": q.source.table,
            "columns": q.source.columns,
            "ticker": q.source.ticker,
            "period_end": q.source.period_end,
            "generator": q.source.generator,
            "note": q.source.note,
        }
    return d


def from_jsonl_dict(d: dict) -> Question:
    """JSONL 行 → Question。"""
    expected = None
    if d.get("expected"):
        expected = {k: ExpectedValue(**v) for k, v in d["expected"].items()}
    source = GroundTruthSource(**d["source"]) if d.get("source") else None
    return Question(
        qid=d["qid"],
        set_name=d["set_name"],
        question=d["question"],
        ground_truth=d["ground_truth"],
        key_facts=d.get("key_facts", []),
        expected=expected,
        source=source,
    )


def load_generated(pattern: str = "set_e_*.jsonl") -> list[Question]:
    """加载 eval/datasets/ 下所有生成的题集。文件不存在时返回空列表。"""
    questions: list[Question] = []
    if not DATASET_DIR.is_dir():
        return questions
    for path in sorted(DATASET_DIR.glob(pattern)):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    questions.append(from_jsonl_dict(json.loads(line)))
    return questions


SET_E: list[Question] = load_generated()

ALL_QUESTIONS = SET_A + SET_B + SET_C + SET_D + SET_F + SET_E


def get_set(name: str) -> list[Question]:
    return [q for q in ALL_QUESTIONS if q.set_name == name]
