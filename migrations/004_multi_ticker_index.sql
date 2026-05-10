-- Migration 004: 多 Ticker SEC 检索优化索引
-- sec_chunks.company 列已存在，补充复合索引提升多公司查询性能

CREATE INDEX IF NOT EXISTS idx_chunks_company_fy
    ON sec_chunks(company, fiscal_year, section);
