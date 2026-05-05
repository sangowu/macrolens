"""
MacroLens Task API

启动方式:
    uv run uvicorn api.tasks:app --reload --port 8080

端点:
    POST /api/tasks          提交分析任务
    GET  /api/tasks/{id}     查询任务状态/结果
    GET  /api/tasks          列出最近任务
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import psycopg

from models.config import load_config

_cfg = load_config("config.yaml")
_conn: psycopg.Connection | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _conn
    _conn = psycopg.connect(_cfg.db.dsn)
    yield
    if _conn:
        _conn.close()


app = FastAPI(title="MacroLens Task API", lifespan=lifespan)


class TaskRequest(BaseModel):
    question: str


class TaskResponse(BaseModel):
    task_id: str
    status: str
    question: str | None = None
    report_md: str | None = None
    error_msg: str | None = None
    created_at: str | None = None
    completed_at: str | None = None


@app.post("/api/tasks", response_model=TaskResponse, status_code=201)
def create_task(req: TaskRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question cannot be empty")

    with _conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tasks (question) VALUES (%s) RETURNING id, status, created_at",
            (req.question.strip(),),
        )
        row = cur.fetchone()
    _conn.commit()

    return TaskResponse(
        task_id=str(row[0]),
        status=row[1],
        question=req.question.strip(),
        created_at=str(row[2]),
    )


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str):
    with _conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, question, report_md, error_msg, created_at, completed_at FROM tasks WHERE id = %s",
            (task_id,),
        )
        row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="task not found")

    return TaskResponse(
        task_id=str(row[0]),
        status=row[1],
        question=row[2],
        report_md=row[3],
        error_msg=row[4],
        created_at=str(row[5]) if row[5] else None,
        completed_at=str(row[6]) if row[6] else None,
    )


@app.get("/api/tasks", response_model=list[TaskResponse])
def list_tasks(limit: int = 20):
    with _conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, question, report_md, error_msg, created_at, completed_at
            FROM tasks ORDER BY created_at DESC LIMIT %s
            """,
            (min(limit, 100),),
        )
        rows = cur.fetchall()

    return [
        TaskResponse(
            task_id=str(r[0]),
            status=r[1],
            question=r[2],
            report_md=r[3],
            error_msg=r[4],
            created_at=str(r[5]) if r[5] else None,
            completed_at=str(r[6]) if r[6] else None,
        )
        for r in rows
    ]
