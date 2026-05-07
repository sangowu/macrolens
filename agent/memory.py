"""
Research Memory: 提取并检索跨任务的研究发现。

extract_and_store() — 任务完成后提取关键 finding，存入 research_memory
retrieve()          — 新任务开始前检索相关历史记忆，注入 context
"""
from __future__ import annotations

import psycopg
from pgvector.psycopg import register_vector

from models.llm.base import LLMClient

_EXTRACT_SYSTEM = """\
You are a research memory extractor. Given a question and its answer, extract 2-4 key findings worth remembering for future research sessions using the extract_findings tool.

Keep each content under 150 characters. fiscal_year is null if not year-specific."""

_EXTRACT_TOOL = {
    "name": "extract_findings",
    "description": "Extract key research findings from the question-answer pair.",
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "memory_type": {
                            "type": "string",
                            "enum": ["finding", "open_question"],
                            "description": "finding: concrete fact. open_question: couldn't be answered due to missing data.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The finding in under 150 characters.",
                        },
                        "fiscal_year": {
                            "type": ["integer", "null"],
                            "description": "Fiscal year if year-specific, otherwise null.",
                        },
                    },
                    "required": ["memory_type", "content", "fiscal_year"],
                },
            }
        },
        "required": ["findings"],
    },
}


def extract_and_store(
    task_id: str,
    question: str,
    answer: str,
    conn: psycopg.Connection,
    embedder,
    llm: LLMClient,
) -> int:
    """从问答对中提取关键发现并存入 research_memory，返回存储条数。"""
    prompt = f"Question: {question}\n\nAnswer: {answer[:2000]}"

    result = llm.chat_with_tools(
        system=_EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        tools=[_EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_findings"},
        max_tokens=512,
        temperature=0.0,
    )

    findings = result.get("findings", [])
    if not findings:
        return 0

    stored = 0
    with conn.cursor() as cur:
        for mem in findings:
            content = mem.get("content", "").strip()
            if not content:
                continue
            embedding = embedder.encode([content])[0]
            cur.execute(
                """
                INSERT INTO research_memory (task_id, memory_type, content, embedding, fiscal_year)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    task_id,
                    mem.get("memory_type", "finding"),
                    content,
                    embedding,
                    mem.get("fiscal_year"),
                ),
            )
            stored += 1
    conn.commit()
    return stored


def retrieve(
    question: str,
    conn: psycopg.Connection,
    embedder,
    top_k: int = 3,
) -> list[dict]:
    """检索与当前问题最相关的历史记忆，返回可注入 context 的 dict 列表。"""
    q_vec = embedder.encode([question])[0]

    register_vector(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT memory_type, content, fiscal_year, created_at,
                   embedding <=> %s::vector AS distance
            FROM research_memory
            ORDER BY distance
            LIMIT %s
            """,
            (q_vec, top_k),
        )
        rows = cur.fetchall()

    return [
        {
            "source": "memory",
            "memory_type": r[0],
            "content": r[1],
            "fiscal_year": r[2],
            "created_at": str(r[3])[:10],
            "distance": float(r[4]),
        }
        for r in rows
    ]
