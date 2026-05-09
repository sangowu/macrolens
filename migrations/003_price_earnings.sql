-- Migration 003: 新增股价历史表和财报历史表
-- 供方向1（估值决策）、方向2（财报异动）、方向3（宏观-股价相关性）共用
-- 两张表均走精确 SQL 查询，不需要向量索引

-- =========================================================
-- ① 股价历史（日线 OHLCV + 衍生估值字段）
-- =========================================================
CREATE TABLE IF NOT EXISTS price_history (
    ticker          TEXT        NOT NULL,
    date            DATE        NOT NULL,
    open            NUMERIC,
    high            NUMERIC,
    low             NUMERIC,
    close           NUMERIC     NOT NULL,
    adj_close       NUMERIC,
    volume          BIGINT,
    -- 衍生估值字段：入库时由 TTM EPS / Revenue 计算，无对应季报时为 NULL
    pe_ratio        NUMERIC,
    ps_ratio        NUMERIC,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_price_ticker_date
    ON price_history(ticker, date DESC);

CREATE INDEX IF NOT EXISTS idx_price_date
    ON price_history(date);

-- =========================================================
-- ② 季度/年度财报历史（EPS + 核心财务指标）
-- =========================================================
CREATE TABLE IF NOT EXISTS earnings_history (
    ticker              TEXT        NOT NULL,
    period_end          DATE        NOT NULL,
    fiscal_year         SMALLINT    NOT NULL,
    fiscal_quarter      SMALLINT,                   -- 1-4；年报为 NULL
    period_type         TEXT        NOT NULL,        -- 'quarterly' | 'annual'
    -- 损益表
    revenue             NUMERIC,                    -- 单位：千美元
    net_income          NUMERIC,
    operating_income    NUMERIC,
    -- EPS
    eps_actual          NUMERIC,
    eps_estimate        NUMERIC,                    -- 分析师预期，yfinance 覆盖不全，允许 NULL
    eps_surprise        NUMERIC,                    -- eps_actual - eps_estimate
    eps_surprise_pct    NUMERIC,                    -- surprise %，允许 NULL
    -- GOOGL 专项分部数据
    cloud_revenue       NUMERIC,
    ads_revenue         NUMERIC,
    -- 利润率
    gross_profit        NUMERIC,
    gross_margin        NUMERIC,                    -- 0.0-1.0
    operating_margin    NUMERIC,
    source              TEXT        DEFAULT 'yfinance',
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, period_end, period_type)
);

CREATE INDEX IF NOT EXISTS idx_earnings_ticker_period
    ON earnings_history(ticker, period_end DESC);

CREATE INDEX IF NOT EXISTS idx_earnings_fiscal_year
    ON earnings_history(ticker, fiscal_year, fiscal_quarter);
