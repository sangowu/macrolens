-- Migration 005: 数据刷新日志表
-- 记录每次 data_refresh_worker 的执行结果，便于排查失败

CREATE TABLE IF NOT EXISTS data_refresh_log (
    id           SERIAL PRIMARY KEY,
    run_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_type    TEXT        NOT NULL,   -- 'price_history' | 'earnings_history'
    ticker       TEXT        NOT NULL,
    rows_added   INTEGER,
    rows_updated INTEGER,
    status       TEXT        NOT NULL,   -- 'success' | 'failed' | 'skipped'
    error_msg    TEXT
);

CREATE INDEX IF NOT EXISTS idx_refresh_log_run_at
    ON data_refresh_log(run_at DESC);

CREATE INDEX IF NOT EXISTS idx_refresh_log_ticker_type
    ON data_refresh_log(ticker, data_type);
