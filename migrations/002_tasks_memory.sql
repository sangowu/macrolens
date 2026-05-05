-- MacroLens 第二次迁移
-- 新增：异步任务队列 + 研究记忆层

-- =========================================================
-- ① 任务队列
-- =========================================================
CREATE TABLE IF NOT EXISTS tasks (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    question     TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending',   -- pending/running/completed/failed
    report_md    TEXT,                                     -- 生成的 markdown 报告内容
    error_msg    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);

-- =========================================================
-- ② 研究记忆层
-- =========================================================
CREATE TABLE IF NOT EXISTS research_memory (
    id           SERIAL      PRIMARY KEY,
    task_id      UUID        REFERENCES tasks(id) ON DELETE CASCADE,
    memory_type  TEXT        NOT NULL,   -- finding | open_question
    content      TEXT        NOT NULL,
    embedding    vector(1024),
    ticker       TEXT        DEFAULT 'GOOGL',
    fiscal_year  SMALLINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_hnsw
    ON research_memory USING hnsw (embedding vector_cosine_ops)
    WITH (m = 8, ef_construction = 32);

CREATE INDEX IF NOT EXISTS idx_memory_type ON research_memory(memory_type);
