-- =============================================================================
-- schema_v2.sql  ——  schema v2 统一建表脚本（6 张表）
-- -----------------------------------------------------------------------------
-- 说明：
--   * SQLite 方言；所有语句幂等（IF NOT EXISTS），可重复执行。
--   * 外键策略：全部为「应用层收口」的弱外键，但在此显式声明 FOREIGN KEY
--     并统一 ON DELETE 语义，删除一致性交给 PRAGMA foreign_keys = ON 时的引擎
--     兜底（见 db.py 的连接初始化 get_conn()）。
--   * 时间字段统一 TEXT + datetime('now','localtime')，与旧库保持兼容。
--   * 去重统一策略：content_hash / input_hash / url + 部分唯一索引。
--   * 本脚本只负责「建表 + 索引」，不插入任何业务数据；
--     老库补列（ALTER TABLE ADD COLUMN）由 db.py 的 _MIGRATION_COLUMNS 负责。
--
-- 6 张表：
--   1. competitions  竞赛信息主表
--   2. awards        获奖作品（核心表，向量化的主料）
--   3. search_leads  搜索线索（自动更新入口）
--   4. crawl_jobs    任务日志（可观测）
--   5. insights      生成的分析结论（带缓存，防重复烧钱）
--   6. ideas         灵感清单
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. competitions —— 竞赛信息主表
-- 时间模型：月份(1-12)整数 + 跨年标记，不含具体日期/年份。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS competitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    category        TEXT,
    reg_start_month INTEGER CHECK (reg_start_month IS NULL OR reg_start_month BETWEEN 1 AND 12),
    reg_end_month   INTEGER CHECK (reg_end_month   IS NULL OR reg_end_month   BETWEEN 1 AND 12),
    reg_cross_year  INTEGER NOT NULL DEFAULT 0 CHECK (reg_cross_year IN (0, 1)),
    official_url    TEXT,
    level           TEXT    NOT NULL DEFAULT 'national'
                            CHECK (level IN ('national', 'provincial')),
    level_label     TEXT,
    sort_order      INTEGER,
    intro           TEXT,
    summary         TEXT,
    tech_stack      TEXT    NOT NULL DEFAULT '[]',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_competitions_category
    ON competitions (category);
CREATE INDEX IF NOT EXISTS idx_competitions_reg_month
    ON competitions (reg_start_month, reg_end_month);
CREATE INDEX IF NOT EXISTS idx_competitions_level
    ON competitions (level);


-- -----------------------------------------------------------------------------
-- 2. awards —— 获奖作品（核心表）
-- embedding_state = 'stale' 是「SQLite 写完 -> 待同步 Chroma」的脏标记。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS awards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    competition_id  INTEGER REFERENCES competitions (id) ON DELETE SET NULL,
    year            INTEGER,
    category        TEXT,
    title           TEXT    NOT NULL,
    team            TEXT,
    advisor         TEXT,
    award_level     TEXT,
    summary         TEXT,
    tech_stack      TEXT    NOT NULL DEFAULT '[]',
    highlights      TEXT,
    detail_md       TEXT,
    source_url      TEXT,
    source_type     TEXT    NOT NULL DEFAULT 'manual',
    raw_html_path   TEXT,
    confidence      REAL    NOT NULL DEFAULT 1.0,
    review_status   TEXT    NOT NULL DEFAULT 'pending'
                            CHECK (review_status IN ('pending', 'approved', 'rejected')),
    embedding_state TEXT    NOT NULL DEFAULT 'none'
                            CHECK (embedding_state IN ('none', 'ok', 'stale')),
    content_hash    TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_awards_year_cat
    ON awards (year, category);
CREATE INDEX IF NOT EXISTS idx_awards_comp
    ON awards (competition_id);
CREATE INDEX IF NOT EXISTS idx_awards_review
    ON awards (review_status);
CREATE INDEX IF NOT EXISTS idx_awards_embed
    ON awards (embedding_state);
-- 部分唯一索引：可空但唯一，实现幂等去重
CREATE UNIQUE INDEX IF NOT EXISTS ux_awards_hash
    ON awards (content_hash) WHERE content_hash IS NOT NULL;


-- -----------------------------------------------------------------------------
-- 3. search_leads —— 搜索线索（自动更新入口）
-- 流转：new -> accepted / ignored / crawled
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS search_leads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    query          TEXT    NOT NULL,
    title          TEXT,
    url            TEXT    NOT NULL,
    snippet        TEXT,
    provider       TEXT,
    keyword_hit    TEXT,
    status         TEXT    NOT NULL DEFAULT 'new'
                           CHECK (status IN ('new', 'accepted', 'ignored', 'crawled')),
    competition_id INTEGER REFERENCES competitions (id) ON DELETE SET NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_url
    ON search_leads (url);
CREATE INDEX IF NOT EXISTS idx_leads_status
    ON search_leads (status);


-- -----------------------------------------------------------------------------
-- 4. crawl_jobs —— 任务日志（可观测）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,
    target     TEXT,
    status     TEXT    NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'ok', 'failed')),
    items_in   INTEGER NOT NULL DEFAULT 0,
    items_out  INTEGER NOT NULL DEFAULT 0,
    cost_token INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    started_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    ended_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_kind
    ON crawl_jobs (kind);


-- -----------------------------------------------------------------------------
-- 5. insights —— 生成的分析结论（带缓存，防重复烧钱）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS insights (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL CHECK (kind IN ('experience', 'trend', 'gap')),
    scope        TEXT,
    competition_id INTEGER REFERENCES competitions (id) ON DELETE SET NULL,
    input_hash   TEXT,
    content_md   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_insights_scope
    ON insights (scope);
-- 部分唯一索引：同一输入指纹只生成一次，命中即复用缓存
CREATE UNIQUE INDEX IF NOT EXISTS ux_insights_hash
    ON insights (input_hash) WHERE input_hash IS NOT NULL;


-- -----------------------------------------------------------------------------
-- 6. ideas —— 灵感清单
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ideas (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    competition_id INTEGER REFERENCES competitions (id) ON DELETE SET NULL,
    title          TEXT    NOT NULL,
    pitch          TEXT,
    detail_md      TEXT,
    novelty        TEXT,
    risk           TEXT,
    ref_awards     TEXT,
    status         TEXT    NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft', 'starred', 'archived')),
    difficulty     TEXT,
    metrics        TEXT,
    content_hash   TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_ideas_comp
    ON ideas (competition_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ideas_hash
    ON ideas (content_hash) WHERE content_hash IS NOT NULL;
