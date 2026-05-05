-- MacroLens 初始化迁移
-- PostgreSQL 17 + pgvector 0.8.x
-- 向量维度：1024（适配 BGE-M3 / BGE-large-en-v1.5 / Qwen3-Embedding-0.6B）
-- 切换 BGE-small-en-v1.5（dim=384）时需重建 embedding 列和相关索引

CREATE EXTENSION IF NOT EXISTS vector;

-- =========================================================
-- ① SEC 财报 chunk（核心向量表）
-- =========================================================
CREATE TABLE IF NOT EXISTS sec_chunks (
    id              BIGSERIAL       PRIMARY KEY,
    doc_type        TEXT            NOT NULL,           -- 10-K / 10-Q / 8-K
    filing_date     DATE,                               -- 申报日
    period_end      DATE,                               -- 财报期末（≠ filing_date，按此过滤；旧格式文件可能为 NULL）
    fiscal_year     SMALLINT,
    fiscal_quarter  SMALLINT,
    section         TEXT,                               -- MD&A / Risk Factors / Financial Statements / Business
    subsection      TEXT,
    company         TEXT            NOT NULL DEFAULT 'GOOGL',
    source_url      TEXT,
    chunk_index     INTEGER,
    token_count     INTEGER,
    content         TEXT            NOT NULL,
    content_tsv     TSVECTOR GENERATED ALWAYS AS
                    (to_tsvector('english', content)) STORED,
    embedding       vector(1024),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chunks_period
    ON sec_chunks(period_end);

CREATE INDEX IF NOT EXISTS idx_chunks_company_period
    ON sec_chunks(company, period_end);

CREATE INDEX IF NOT EXISTS idx_chunks_section
    ON sec_chunks(section);

CREATE INDEX IF NOT EXISTS idx_chunks_tsv
    ON sec_chunks USING GIN(content_tsv);

-- HNSW 索引在 bulk insert 完成后再建（见注释）
-- m=16, ef_construction=64 是平衡召回率与内存的常用起点
-- 建议：先跑 ingestion 把全量数据写入，再执行下面这条
CREATE INDEX IF NOT EXISTS idx_chunks_hnsw
    ON sec_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- =========================================================
-- ② 事件表（结构化 + 向量 + 全文）
-- =========================================================
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT            PRIMARY KEY,
    date            DATE            NOT NULL,
    category        TEXT,           -- company_action / fed_policy / macro_shock / industry
    entity          TEXT,
    severity        SMALLINT,       -- 1-5
    title           TEXT,
    description     TEXT,
    source_url      TEXT,
    meta            JSONB           NOT NULL DEFAULT '{}',
    description_tsv TSVECTOR GENERATED ALWAYS AS
                    (to_tsvector('english', coalesce(description, ''))) STORED,
    embedding       vector(1024),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_date
    ON events(date);

CREATE INDEX IF NOT EXISTS idx_events_category
    ON events(category);

CREATE INDEX IF NOT EXISTS idx_events_entity
    ON events(entity);

CREATE INDEX IF NOT EXISTS idx_events_tsv
    ON events USING GIN(description_tsv);

CREATE INDEX IF NOT EXISTS idx_events_hnsw
    ON events USING hnsw (embedding vector_cosine_ops)
    WITH (m = 8, ef_construction = 32);  -- 仅 30 条，参数保守即可

-- =========================================================
-- ③ FRED 指标元数据
-- 只有 12 行，不建 HNSW，顺序扫描更快
-- =========================================================
CREATE TABLE IF NOT EXISTS macro_series_meta (
    series_id       TEXT            PRIMARY KEY,
    name            TEXT            NOT NULL,
    description     TEXT,
    unit            TEXT,
    frequency       TEXT,           -- Monthly / Quarterly / Annual / Weekly / Daily
    embedding       vector(1024),   -- 用于 "通胀指标" → CPIAUCSL/PCEPI 语义映射
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
);

-- =========================================================
-- ④ FRED 数值时间序列（精确查询，不进向量）
-- =========================================================
CREATE TABLE IF NOT EXISTS macro_indicators (
    series_id       TEXT            NOT NULL,
    date            DATE            NOT NULL,
    value           NUMERIC,
    ingested_at     TIMESTAMPTZ     NOT NULL DEFAULT now(),  -- 记录 ingestion 时间戳（FRED 有 revision）
    PRIMARY KEY (series_id, date)
);

CREATE INDEX IF NOT EXISTS idx_macro_date
    ON macro_indicators(date);

CREATE INDEX IF NOT EXISTS idx_macro_series_date
    ON macro_indicators(series_id, date DESC);  -- 按 series 查最新值时用得上

-- =========================================================
-- 初始化 FRED 指标元数据（12 个 series 的描述）
-- embedding 在 ingest_fred.py 运行后填充
-- =========================================================
INSERT INTO macro_series_meta (series_id, name, description, unit, frequency) VALUES
    ('GDP',      'Gross Domestic Product',                     'Nominal GDP, seasonally adjusted annual rate',         'Billions of Dollars',  'Quarterly'),
    ('GDPC1',    'Real GDP',                                   'Real GDP, chained 2017 dollars, SAAR',                 'Billions of Dollars',  'Quarterly'),
    ('CPIAUCSL', 'Consumer Price Index',                       'CPI for all urban consumers, all items, SA',           'Index 1982-84=100',    'Monthly'),
    ('PCEPI',    'PCE Price Index',                            'Personal consumption expenditures price index, SA',    'Index 2017=100',       'Monthly'),
    ('UNRATE',   'Unemployment Rate',                          'Civilian unemployment rate, seasonally adjusted',      'Percent',              'Monthly'),
    ('PAYEMS',   'Nonfarm Payrolls',                           'Total nonfarm employees, seasonally adjusted',         'Thousands of Persons', 'Monthly'),
    ('FEDFUNDS', 'Federal Funds Rate',                         'Effective federal funds rate, monthly average',        'Percent',              'Monthly'),
    ('DGS10',    '10-Year Treasury Rate',                      'Market yield on 10-year US Treasury, daily',          'Percent',              'Daily'),
    ('M2SL',     'M2 Money Stock',                             'M2 money supply, seasonally adjusted',                 'Billions of Dollars',  'Monthly'),
    ('VIXCLS',   'CBOE Volatility Index (VIX)',               'Market expectation of 30-day volatility',              'Index',                'Daily'),
    ('UMCSENT',  'University of Michigan Consumer Sentiment',  'Index of consumer sentiment',                          'Index 1966 Q1=100',    'Monthly'),
    ('USREC',    'NBER Recession Indicator',                   'Binary: 1 during recession, 0 otherwise',             'Binary',               'Monthly')
ON CONFLICT (series_id) DO NOTHING;
