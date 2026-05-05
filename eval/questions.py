"""
评估问题集。

Set A: 事实型（有明确数值/日期答案）
Set B: 多跳推理（需要跨数据源）
Set C: 边界/对抗（超范围、模糊、比较）
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Question:
    qid: str
    set_name: str          # "A" | "B" | "C"
    question: str
    ground_truth: str      # 参考答案（用于 recall 评估）
    key_facts: list[str] = field(default_factory=list)   # 必须出现在 context 中的关键事实


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
        ground_truth="The system only contains Google/Alphabet data, not Amazon data, so a direct comparison cannot be made.",
        key_facts=[],
    ),
]

ALL_QUESTIONS = SET_A + SET_B + SET_C


def get_set(name: str) -> list[Question]:
    return [q for q in ALL_QUESTIONS if q.set_name == name]
