-- =============================================================================
-- schema_v2.sql  ——  schema v2 统一建表脚本（11 张表）
-- -----------------------------------------------------------------------------
-- 说明：
--   * SQLite 方言；所有语句幂等（IF NOT EXISTS），可重复执行。
--   * 外键策略：全部为「应用层收口」的弱外键，但在此显式声明 FOREIGN KEY
--     并统一 ON DELETE 语义，删除一致性交给 PRAGMA foreign_keys = ON 时的引擎
--     兜底（见 db.py 的连接初始化 get_conn()）。
--   * 时间字段统一 TEXT + datetime('now','localtime')，与旧库保持兼容。
--   * 去重统一策略：content_hash / input_hash / url + 部分唯一索引。
--   * 本脚本只负责「建表 + 索引」，不插入任何业务数据；
--     老库补列（ALTER TABLE ADD COLUMN）由 db.py 的 _MIGRATION_COLUMNS
--     （competitions）与 _AWARD_MIGRATION_COLUMNS（awards）负责。
--   * awards 表列定义已与线上老库对齐（28 列）：线上老库比早期 schema 多出
--     competition_raw / level / session / award_grade / track / difficulty /
--     metrics 这些历史列，这里全部纳入建表语句，保证「全新部署」与
--     「存量老库迁移后」得到完全相同的表结构。
--
-- 11 张表：
--   1. competitions      竞赛信息主表
--   2. awards            获奖作品（核心表，向量化的主料）
--   3. search_leads      搜索线索（自动更新入口）
--   4. crawl_jobs        任务日志（可观测）
--   5. insights          生成的分析结论（带缓存，防重复烧钱）
--   6. ideas             灵感清单
--   7. users             用户账号（鉴权地基）
--   8. user_preparations 用户备赛清单
--   9. ai_usage          AI 每日配额计数
--  10. ai_analysis_cache AI 分析结果全局缓存（单作品解析 / 赛事共性分析）
--  11. feedbacks         用户反馈（只写入，无查询接口）
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
    -- 封面图（只存 URL，不下载图片）
    image_url       TEXT,
    -- AI 提取的 2 字赛事简称（无图卡片的 Logo 兜底；真实封面永远优先）
    short_name      TEXT,
    -- 比赛时间（月份 1-12；与报名时间字段保持同一粒度）
    contest_start_month INTEGER CHECK (contest_start_month IS NULL OR contest_start_month BETWEEN 1 AND 12),
    contest_end_month   INTEGER CHECK (contest_end_month   IS NULL OR contest_end_month   BETWEEN 1 AND 12),
    -- 采集溯源（占位赛事后续由人工/采集器补全）
    discover_source TEXT,
    last_checked_at TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    level           TEXT    NOT NULL DEFAULT 'national'
                            CHECK (level IN ('national', 'provincial')),
    level_label     TEXT,
    sort_order      INTEGER,
    intro           TEXT,
    summary         TEXT,
    tech_stack      TEXT    NOT NULL DEFAULT '[]',
    -- 详情补充（2026-11-08）：由赛氪详情 API 清洗出的真实文本，
    -- 只在详情接口返回；无法抽取时保持 NULL，不编造。
    participants    TEXT,
    content_scope   TEXT,
    exam_schedule   TEXT,
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
--
-- 共 28 列，列名与「顺序」均与线上存量老库对齐，两边可逐列 diff。
-- 其中 competition_raw / level / session / award_grade / track / difficulty /
-- metrics 是线上老库的历史列（早期 schema 未声明），competitions.py 的 Tab2
-- 正是读取 track / session / award_grade / level，缺列会导致字段全空。
-- 老库若缺 advisor / award_level / detail_md，由 db.py 的
-- _AWARD_MIGRATION_COLUMNS 用 ALTER TABLE ADD COLUMN 幂等补齐。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS awards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    competition_id  INTEGER REFERENCES competitions (id) ON DELETE SET NULL,
    -- competition_raw: 原始赛事名（抓取落库时未归并到 competitions 的文本）
    competition_raw TEXT,
    category        TEXT,
    level           TEXT,
    year            INTEGER,
    -- session: 赛段（国赛 / 省赛 …）
    session         TEXT,
    title           TEXT    NOT NULL,
    team            TEXT,
    -- award_grade: 获奖等级（一等奖 / 二等奖 …）
    award_grade     TEXT,
    -- track: 赛道 / 组别
    track           TEXT,
    summary         TEXT,
    tech_stack      TEXT    NOT NULL DEFAULT '[]',
    highlights      TEXT,
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
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    -- difficulty / metrics: 老库历史列（作品难度、量化指标，可为空）
    difficulty      TEXT,
    metrics         TEXT,
    -- advisor / award_level / detail_md: 老库可能缺失，由迁移脚本幂等补列
    advisor         TEXT,
    award_level     TEXT,
    detail_md       TEXT
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

-- -----------------------------------------------------------------------------
-- 7. users —— 用户账号（鉴权地基）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username
    ON users (username);

-- -----------------------------------------------------------------------------
-- 8. user_preparations -- user prep list ("my preparation")
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_preparations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    competition_id INTEGER NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (user_id)        REFERENCES users (id)        ON DELETE CASCADE,
    FOREIGN KEY (competition_id) REFERENCES competitions (id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_user_prep
    ON user_preparations (user_id, competition_id);
CREATE INDEX IF NOT EXISTS idx_user_prep_comp
    ON user_preparations (competition_id);

-- -----------------------------------------------------------------------------
-- 9. ai_usage -- per-user daily AI quota counter
--    One row per (user_id, usage_date). Used/limit compared atomically.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_usage (
    user_id    INTEGER NOT NULL,
    usage_date TEXT    NOT NULL,              -- 'YYYY-MM-DD' (local date)
    used       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (user_id, usage_date),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_date
    ON ai_usage (usage_date);

-- -----------------------------------------------------------------------------
-- 10. ai_analysis_cache -- global cache for AI analysis results
--    Keyed by (target_type, target_id):
--      target_type = 'work'        -> target_id = awards.id
--      target_type = 'competition' -> target_id = competitions.id
--    input_hash 是「实际送入大模型的 prompt」的 SHA-256：命中缓存后仍需比对它，
--    作品/赛事内容一旦变化，指纹随之变化，旧结果自动失效并重新生成。
--    唯一索引保证「一个作品/赛事只保留一条最新结果」。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_analysis_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT    NOT NULL CHECK (target_type IN ('work', 'competition')),
    target_id   INTEGER NOT NULL,
    input_hash  TEXT    NOT NULL,
    content_md  TEXT    NOT NULL,
    model       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_analysis_cache_target
    ON ai_analysis_cache (target_type, target_id);

-- -----------------------------------------------------------------------------
-- 11. feedbacks -- user feedback (write-only; no query API by design)
--    user_id 允许为空，并随账号删除置空（ON DELETE SET NULL），
--    这样账号注销后反馈内容仍然保留，便于事后排查。
--    接口层强制登录，user_id 由 Token 解析结果写入，客户端无法伪造。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feedbacks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users (id) ON DELETE SET NULL,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_feedbacks_created
    ON feedbacks (created_at);
CREATE INDEX IF NOT EXISTS idx_feedbacks_user
    ON feedbacks (user_id);
