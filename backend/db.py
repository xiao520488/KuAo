# -*- coding: utf-8 -*-
"""
比赛信息管理系统 - 数据访问层 (db.py)

设计要点（v2）：
- 连接统一走上下文管理器 `with get_conn() as c:`：
  * 正常结束自动 commit；异常自动 rollback；finally 一定 close。
  * 连接被显式封禁「隐式提交过久」，避免忘记 close 导致 SQLite 锁泄漏。
- 建表由 schema_v2.sql 统一负责（6 张表 + 外键 + 索引），幂等可重复执行。
- 迁移补列 `_MIGRATION_COLUMNS` 保留（老库 CREATE TABLE IF NOT EXISTS 不会加列）。
- 回填 `_backfill_detail_fields` 保留，一次性安全补齐 summary / tech_stack。
- 对外函数签名与返回结构保持不变，避免上层调用方受影响的「不坏」前提。

【v2 上下文管理器改造说明】
- 全模块唯一一处 `sqlite3.connect` 位于 `get_conn()`，其余所有业务函数
  一律通过 `with get_conn() as conn:` 获取连接，禁止裸连接。
- `get_conn()` 现支持可选 `path` 参数（默认 DB_PATH），便于测试指定临时库；
  不传时行为与旧版完全一致，保证向后兼容。
- 连接内统一开启 `PRAGMA foreign_keys = ON` 与 `busy_timeout`，
  降低并发写时的 "database is locked" 概率。

【v3 新增字段】
- competitions.full_process_guide (TEXT)：参赛全过程攻略（Markdown），
  由离线脚本 generate_guides.py 调用大模型生成后回写。

【v4 新增字段】
- competitions.short_name (TEXT)：AI 提取的 2 字赛事简称，仅用于无图卡片
  / 图片加载失败时的 Logo 式兜底；真实封面图永远优先。
  由 generate_short_names.py 批量补全，review_candidates.py 审核入库时自动生成。
"""

from typing import Optional
from contextlib import contextmanager
import difflib
import json
import logging
import os
import re
import sqlite3
import unicodedata
from datetime import datetime
from urllib.parse import urlparse

import image_guard

logger = logging.getLogger(__name__)

# 数据库文件路径（与 db.py 同目录）
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "competitions.db")
# 建表脚本路径（v2：6 张表统一脚本）
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_v2.sql")


# 计算机类类别关键词（命中则视为计算机类，排在最前）
_COMPUTER_KEYWORDS = ("计算机", "软件", "程序设计", "算法", "人工智能", "AI",
                      "大数据", "信息安全", "网络安全", "服务外包", "电子信息",
                      "物联网", "嵌入式", "ICT")

# 创新创业类关键词：即使与计算机类有交叉（如"创新创业/软件服务"），
# 也整体排在纯计算机类之后。
_INNOVATION_KEYWORDS = ("创新创业", "创客", "创业")


def _build_expr(keywords):
    """把关键词列表拼成 SQL 表达式：CASE WHEN category LIKE '%x%' OR ... THEN 1 ELSE 0 END"""
    parts = []
    for kw in keywords:
        safe = kw.replace("'", "''")
        parts.append(f"category LIKE '%{safe}%'")
    return " OR ".join(parts)


_COMPUTER_EXPR = _build_expr(_COMPUTER_KEYWORDS)
_INNOVATION_EXPR = _build_expr(_INNOVATION_KEYWORDS)

# 排序子句（四级）：
#  1) 赛道分组 group_rank：0=计算机类 1=创新创业类 2=其它
#  2) is_national DESC —— 国家级优先于省级
#  3) sort_order ASC   —— 手动权重，NULL 排最后
#  4) id ASC
_ORDER_BY_CLAUSE = f"""
ORDER BY
    CASE
        WHEN ({_COMPUTER_EXPR}) AND NOT ({_INNOVATION_EXPR}) THEN 0
        WHEN ({_INNOVATION_EXPR}) THEN 1
        ELSE 2
    END ASC,
    CASE WHEN level = 'national' THEN 0 ELSE 1 END ASC,
    CASE WHEN sort_order IS NULL THEN 1 ELSE 0 END ASC,
    sort_order ASC,
    id ASC
"""

# ---------------------------------------------------------------------------
# 智能排序（报名紧迫度）——列表与首页热门共用
# ---------------------------------------------------------------------------
# 「今天」固定按北京时间切日：
#   SQLite 的 date('now') 取的是 UTC，UTC 部署的服务器上会把截止日整体平移 8 小时，
#   出现「凌晨 0-8 点还把昨天截止的比赛算成报名中」的错位；
#   统一用 date('now','+8 hours')，无论服务器时区设成什么，判断口径都一致。
_TODAY_EXPR = "date('now','+8 hours')"


def _is_date_sql(col: str) -> str:
    """生成「该列是合法的 YYYY-MM-DD」判断。

    三重校验，任一不过就当「无日期」：
      1. 非空；
      2. 形状是 YYYY-MM-DD（GLOB，先便宜地挡掉 '2026-11' 这类月度值）；
      3. `date(col) = col` —— date() 对 2026-13-40 / 2026-1-9 返回 NULL，
         对 2026-02-30 会归一化成 2026-03-02（不等于原值），
         因此这一条同时干掉「越界月日」和「非规范写法」。
    """
    return (f"({col} IS NOT NULL AND {col} GLOB "
            "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
            f"AND date({col}) = {col})")


_CUR_MONTH_EXPR = f"CAST(strftime('%m', {_TODAY_EXPR}) AS INTEGER)"


def _month_window_sql() -> str:
    """生成「当前月份落在 [reg_start_month, reg_end_month] 内」判断。

    口径与 Python 侧 `_month_in_range()` **逐字对齐**（同一份跨年规则）：
      * reg_cross_year=0 且 start <= end：month BETWEEN start AND end；
      * 其余（跨年 / 区间不规范 start > end）：month >= start OR month <= end。
    月度值越界（如 0、13）一律视为不可判定，返回 FALSE。
    """
    return (
        "("
        "reg_start_month IS NOT NULL AND reg_end_month IS NOT NULL "
        "AND reg_start_month BETWEEN 1 AND 12 "
        "AND reg_end_month BETWEEN 1 AND 12 "
        "AND (CASE "
        "WHEN COALESCE(reg_cross_year, 0) = 0 "
        "AND reg_start_month <= reg_end_month "
        f"THEN ({_CUR_MONTH_EXPR} BETWEEN reg_start_month AND reg_end_month) "
        f"ELSE ({_CUR_MONTH_EXPR} >= reg_start_month "
        f"OR {_CUR_MONTH_EXPR} <= reg_end_month) "
        "END)"
        ")"
    )


# 是否拥有「可用的精确日期」（决定走日期判定还是月份兜底）
_HAS_DATE_SQL = (f"({_is_date_sql('reg_start_date')} "
                 f"OR {_is_date_sql('reg_end_date')})")

# 月份兜底：完全没有精确日期，但 *_month 区间覆盖当前月 —— 也算「正在报名」。
# 只在没有精确日期时才启用，避免月份区间盖过更准的日期判定。
_MONTH_FALLBACK_SQL = f"(NOT {_HAS_DATE_SQL} AND {_month_window_sql()})"


# 报名阶段：
#   0 = 正在报名（今天落在 [reg_start_date, reg_end_date] 内，含首尾；
#               只有截止日、没有开始日的也算开放中；
#               无精确日期但 *_month 覆盖本月 -> 同样归入本阶段）
#   1 = 尚未开始（今天 < reg_start_date）
#   2 = 已截止  （今天 > reg_end_date）
#   3 = 无日期  （日期与月份都不可用，沉底，绝不参与前排）
# 注意顺序即优先级：「过期」先判，避免 reg_start_date 脏值把已截止的捞回前排。
_PHASE_EXPR = f"""
    CASE
        WHEN {_is_date_sql('reg_end_date')} AND {_TODAY_EXPR} > reg_end_date THEN 2
        WHEN {_is_date_sql('reg_start_date')} AND {_TODAY_EXPR} < reg_start_date THEN 1
        WHEN {_HAS_DATE_SQL} THEN 0
        WHEN {_MONTH_FALLBACK_SQL} THEN 0
        ELSE 3
    END
"""

# 「数据精度」：0 = 有精确日期，1 = 仅月份兜底。
# 同属「正在报名」时，日期确定的永远排在只有月份的之前（不让学生看到
# 一条只有月份的比赛抢在「明天截止」的上面）。
_PREC_EXPR = f"""
    CASE
        WHEN {_HAS_DATE_SQL} THEN 0
        WHEN {_MONTH_FALLBACK_SQL} THEN 1
        ELSE 2
    END
"""

# 排序：阶段优先 → 组内精度（有日期 > 仅月份）→ 组内紧迫度 → id 兜底
#   * 报名中：reg_end_date 升序（越早截止越靠前）；缺截止日的排在组尾（9999-12-31）
#   * 仅月份兜底：按「还有几个月截止」升序，用 (end_month - 当前月 + 12) % 12 计算，
#     跨年区间（如 11 月开报、次年 3 月截止）也能算出正确的剩余月数
#   * 未开始：reg_start_date 升序（最近要开报的在前）
#   * 已截止：reg_end_date 倒序（刚结束的在上，最老的沉到最后）
#   * 无日期：按比赛开始日兜底，仍无则按 id，整体压在最后
_ORDER_BY_SMART = f"""
ORDER BY
    _phase ASC,
    CASE WHEN _phase IN (0, 1) THEN _prec END ASC,
    CASE _phase WHEN 0 THEN COALESCE(reg_end_date, '9999-12-31') END ASC,
    CASE WHEN _phase = 0 AND _prec = 1
         THEN ((reg_end_month - {_CUR_MONTH_EXPR} + 12) % 12) END ASC,
    CASE _phase WHEN 1 THEN COALESCE(reg_start_date, '9999-12-31') END ASC,
    CASE _phase WHEN 2 THEN COALESCE(reg_end_date, reg_start_date) END DESC,
    CASE _phase WHEN 3 THEN COALESCE(contest_start_date, '9999-12-31') END ASC,
    id ASC
"""


def _strip_phase(row) -> dict:
    """去掉排序用的临时列 _phase/_prec，避免泄漏到 API 响应。"""
    d = dict(row)
    d.pop("_phase", None)
    d.pop("_prec", None)
    return d


_LEVEL_LABELS = {
    "national": "国家级",
    "provincial": "省级",
}


def level_label_of(row: dict) -> str:
    """根据 level 推导中文级别文案；已显式指定 level_label 时优先返回它。"""
    label = (row.get("level_label") or "").strip()
    if label:
        return label
    return _LEVEL_LABELS.get((row.get("level") or "national").strip(), "国家级")


def _enrich(row: dict) -> dict:
    """补全 level_label，保证对外返回的行始终带中文级别文案。"""
    row = dict(row)
    row["level"] = row.get("level") or "national"
    row["level_label"] = level_label_of(row)
    # full_process_guide 兜底为空串，避免前端拿到 None 需额外判空
    if row.get("full_process_guide") is None:
        row["full_process_guide"] = ""
    # 详情补充三字段统一兜底为空串，前端按「空 -> 空壳」渲染
    for key in _DETAIL_TEXT_FIELDS:
        if key in row and row.get(key) is None:
            row[key] = ""
    return row


# ============================================================
# 连接管理（全部走上下文管理器）
# ============================================================

@contextmanager
def get_conn(path: Optional[str] = None):
    """
    数据库连接上下文管理器（全模块唯一连接入口）。

    用法::

        with get_conn() as c:
            c.execute(...)          # 正常退出自动 commit
        # 异常退出自动 rollback，且无论如何都会 close()

    参数:
        path: 可选的数据库文件路径，默认使用模块级 DB_PATH。
              便于测试时指向临时库，不改变生产默认行为。

    特性：
    - row_factory = sqlite3.Row，便于按列名取值
    - 开启外键约束（PRAGMA foreign_keys = ON），配合 schema_v2.sql 的 REFERENCES
    - 设置 busy_timeout，缓解并发写时的 database is locked
    - 正常结束 commit；异常 rollback 并重新抛出；finally 一定 close
    """
    db_path = path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # 先设等待时间，再切 WAL：避免切换日志模式时因短暂锁竞争直接失败。
    # WAL：读写不互相阻塞，显著降低并发写入时的 database is locked 概率。
    # busy_timeout：写锁竞争时最多等待 10s，而不是立刻报错。
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
# 初始化 / 迁移
# ============================================================

# 索引脚本（须在建表 + 迁移补列之后执行；老库缺列时建索引会失败，故单独执行）
INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_competitions_category
    ON competitions (category);
CREATE INDEX IF NOT EXISTS idx_competitions_reg_month
    ON competitions (reg_start_month, reg_end_month);
CREATE INDEX IF NOT EXISTS idx_competitions_level
    ON competitions (level);
CREATE INDEX IF NOT EXISTS idx_insights_comp
    ON insights (competition_id);
CREATE INDEX IF NOT EXISTS idx_ideas_comp
    ON ideas (competition_id);
"""

# 新赛事候选表（2026-11-08 定，见 CRAWLER_HANDOVER.md 第九节 9.4）。
# 所有「新赛事发现」先落本表，人工审核后才 INSERT competitions；
# 发现脚本默认 dry-run，不允许自动写主表。
CANDIDATES_DDL = """
CREATE TABLE IF NOT EXISTS competition_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT    NOT NULL,       -- saikr | edu-roster | edu-notice
    source_key          TEXT    NOT NULL,       -- saikr:59755 / edu-roster:<name-hash>
    source_url          TEXT,
    raw_name            TEXT    NOT NULL,
    norm_name           TEXT    NOT NULL,
    official_url        TEXT,
    image_url           TEXT,
    level_label         TEXT,
    category_hint       TEXT,
    reg_start_month     INTEGER,
    reg_end_month       INTEGER,
    reg_cross_year      INTEGER DEFAULT 0,
    contest_start_month INTEGER,
    contest_end_month   INTEGER,
    -- 精确日期（2026-11-09 双轨制）：'YYYY-MM-DD' 或 'MM-DD'；原文只有月份时留 NULL
    reg_start_date      TEXT,
    reg_end_date        TEXT,
    contest_start_date  TEXT,
    contest_end_date    TEXT,
    raw_json            TEXT,
    status              TEXT    NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/duplicate
    competition_id      INTEGER,                -- 审核通过后回填 competitions.id
    created_at          TEXT    DEFAULT (datetime('now', 'localtime')),
    reviewed_at         TEXT,
    UNIQUE (source, source_key)
);
CREATE INDEX IF NOT EXISTS idx_candidates_status
    ON competition_candidates (status, id);
CREATE INDEX IF NOT EXISTS idx_candidates_norm_name
    ON competition_candidates (norm_name);
CREATE INDEX IF NOT EXISTS idx_candidates_source
    ON competition_candidates (source);
"""

# 候选表补列：精确日期（2026-11-09 双轨制）。老库的候选表由 CANDIDATES_DDL
# 的 CREATE TABLE IF NOT EXISTS 建出，已存在的表不会被改写，故需单独 ALTER 补列。
_CANDIDATE_MIGRATION_COLUMNS = {
    "reg_start_date": "TEXT",
    "reg_end_date": "TEXT",
    "contest_start_date": "TEXT",
    "contest_end_date": "TEXT",
}

# AI 生成任务表（2026-11-08 定）。
# 攻略 / 灵感的异步生成都先落本表：唯一部分索引保证「同一赛事同一类任务同时只有 1 个
# 活跃任务」（原子抢占），同时充当成本审计与失败冷却的依据。
AI_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS ai_jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT    NOT NULL CHECK (kind IN ('guide', 'inspiration')),
    competition_id INTEGER NOT NULL REFERENCES competitions (id) ON DELETE CASCADE,
    trigger        TEXT    NOT NULL DEFAULT 'manual'
                           CHECK (trigger IN ('on_demand', 'daily', 'manual')),
    status         TEXT    NOT NULL DEFAULT 'queued'
                           CHECK (status IN ('queued', 'running', 'done', 'failed', 'skipped')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    tokens         INTEGER,
    requested_by   INTEGER,                 -- 触发用户 id（匿名/定时为空）
    request_ip     TEXT,                    -- 匿名防刷预留（当前接口强制登录）
    error          TEXT,                    -- 失败原因（截断 500 字，不回传前端）
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    started_at     TEXT,
    finished_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_jobs_active
    ON ai_jobs (kind, competition_id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS idx_ai_jobs_created
    ON ai_jobs (kind, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_comp
    ON ai_jobs (competition_id, kind, id);
"""

# ai_jobs.kind 白名单
AI_JOB_KINDS = ("guide", "inspiration")
AI_JOB_ACTIVE_STATUSES = ("queued", "running")

SUMMARY_MAX_LEN = 60

# 迁移新增列：列名 -> DDL 片段（ALTER TABLE ADD COLUMN 用）
_MIGRATION_COLUMNS = {
    "level": "TEXT NOT NULL DEFAULT 'national'",
    "level_label": "TEXT",
    "sort_order": "INTEGER",
    "intro": "TEXT",
    "summary": "TEXT",
    "tech_stack": "TEXT NOT NULL DEFAULT '[]'",
    # 参赛全过程攻略（Markdown），由 generate_guides.py 离线生成回写
    "full_process_guide": "TEXT",
    # 赛事封面图 URL（只存链接，不落本地图片）
    "image_url": "TEXT",
    # AI 提取的 2 字赛事简称（无图卡片兜底；真实封面永远优先）
    "short_name": "TEXT",
    # 比赛时间月份（与报名月份同粒度）
    "contest_start_month": "INTEGER",
    "contest_end_month": "INTEGER",
    # 精确日期（2026-11-09 双轨制）：与 *_month 并存，只增不删。
    # 'YYYY-MM-DD' 或 'MM-DD'；原文只给月份时保持 NULL，前端降级回月份展示。
    "reg_start_date": "TEXT",
    "reg_end_date": "TEXT",
    "contest_start_date": "TEXT",
    "contest_end_date": "TEXT",
    # 采集溯源 / 上下架
    "discover_source": "TEXT",
    "last_checked_at": "TEXT",
    "is_active": "INTEGER NOT NULL DEFAULT 1",
    # 攻略生成溯源（2026-11-08）：用于前端状态、失效判定与批量重刷
    "guide_generated_at": "TEXT",
    "guide_model": "TEXT",
    "guide_prompt_ver": "TEXT",
    "guide_input_hash": "TEXT",
    # 详情补充（2026-11-08）：由 enrich_saikr_details.py 从赛氪详情 API 清洗后写入
    "participants": "TEXT",
    "content_scope": "TEXT",
    "exam_schedule": "TEXT",
    # 阶段级时间表（2026-11-13）：JSON 数组
    # [{"stage":"初赛","start_date":"2026-12-01","end_date":"2026-12-20","note":""}]
    # 由 stage_schedule.py 护栏后写入，前端详情页按时间轴渲染。
    "stage_schedule": "TEXT",
    # 是否有「项目作品」可供 AI 提炼（2026-11-14）
    # 0 = 纯算法/知识类赛事（蓝桥杯、ACM-ICPC、码蹄杯、百度之星…）：
    #     历年公示的只有获奖名单，没有可提炼的项目作品，基于名单做
    #     「选题灵感 / 技术趋势」没有备赛价值，反而会诱导用户看名单。
    #     这类赛事只保留「历年获奖名单」证明含金量；灵感入口在前端隐藏，
    #     后端直接 400，绝不调用大模型（省掉一次调用成本）。
    "has_project_awards": "INTEGER NOT NULL DEFAULT 1",
}

# 详情补充文本字段（只由详情接口返回，列表接口不返回）
_DETAIL_TEXT_FIELDS = ("participants", "content_scope", "exam_schedule",
                       "stage_schedule")

# awards 表补列：列名 -> DDL 片段（ALTER TABLE ADD COLUMN 用）
#
# 线上老库与 schema_v2.sql 的 awards 定义存在双向差异，这里按「并集」补齐：
#   * 下面第一组：schema_v2.sql 有、线上老库没有（补上才算对齐 schema）
#   * 下面第二组：线上老库有、schema_v2.sql 没有（全新部署时补上，
#     否则 competitions.py 读取的 track / session / award_grade / level
#     会全部取不到值，前端 Tab2 的赛道/赛段/奖项等级会空）
#
# 全部为可空 TEXT，满足 SQLite 的 ALTER TABLE ADD COLUMN 限制
# （不能带 UNIQUE / PRIMARY KEY，NOT NULL 必须带默认值）。
_AWARD_MIGRATION_COLUMNS = {
    # —— schema_v2.sql 定义、线上老库缺失 ——
    "advisor": "TEXT",
    "award_level": "TEXT",
    "detail_md": "TEXT",
    # —— 线上老库存在、schema_v2.sql 未声明的历史列 ——
    "competition_raw": "TEXT",
    "level": "TEXT",
    "session": "TEXT",
    "award_grade": "TEXT",
    "track": "TEXT",
    "difficulty": "TEXT",
    "metrics": "TEXT",
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """
    幂等补列：对老库执行 ALTER TABLE ADD COLUMN。
    列已存在时 SQLite 抛 duplicate column name，捕获忽略。
    """
    for col, ddl in _MIGRATION_COLUMNS.items():
        try:
            conn.execute(f"ALTER TABLE competitions ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise


def _migrate_award_columns(conn: sqlite3.Connection) -> list:
    """幂等补齐 awards 表缺失列；返回本次实际新增的列名列表。

    只做 ALTER TABLE ADD COLUMN：不 DROP、不 DELETE、不 UPDATE 任何业务行，
    重复执行安全（已有列直接跳过）。
    """
    existing = _award_columns(conn)
    added = []
    for col, ddl in _AWARD_MIGRATION_COLUMNS.items():
        if col in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE awards ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError as e:
            # 并发下可能被别的进程抢先加列，按幂等处理
            if "duplicate column name" not in str(e).lower():
                raise
            continue
        added.append(col)
    return added


def migrate_award_columns() -> list:
    """对外入口：单独执行 awards 补列迁移（供迁移脚本调用）。

    返回本次新增的列名列表；已对齐的库返回空列表。
    """
    with get_conn() as conn:
        return _migrate_award_columns(conn)


def missing_award_columns() -> list:
    """返回当前库 awards 表缺失（待迁移）的列名，用于迁移前体检。"""
    with get_conn() as conn:
        existing = _award_columns(conn)
    return [col for col in _AWARD_MIGRATION_COLUMNS if col not in existing]


# insights 表补列：老库的 insights 没有 competition_id，导致无法按赛事隔离。
_INSIGHT_MIGRATION_COLUMNS = {
    "competition_id": "INTEGER",
}


def _migrate_insight_columns(conn: sqlite3.Connection) -> list:
    """幂等补齐 insights.competition_id，返回本次新增列。

    历史 insights 是按类别聚合的全局数据，补列后为 NULL；
    读取接口必须按 competition_id 过滤，因此这些旧数据不会混入具体赛事。
    """
    try:
        existing = {
            r["name"] for r in conn.execute("PRAGMA table_info(insights)").fetchall()
        }
    except sqlite3.OperationalError:
        return []
    if not existing:
        return []
    added = []
    for col, ddl in _INSIGHT_MIGRATION_COLUMNS.items():
        if col in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE insights ADD COLUMN {col} {ddl}")
            added.append(col)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    return added


# users 表补列：is_admin 标记管理员（默认 0，老库由迁移自动补上）
_USER_MIGRATION_COLUMNS = {
    "is_admin": "INTEGER NOT NULL DEFAULT 0",
}

# feedbacks 表补列：is_read 供管理后台标记已读
_FEEDBACK_MIGRATION_COLUMNS = {
    "is_read": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate_simple_columns(conn: sqlite3.Connection, table: str,
                            columns: dict) -> list:
    """幂等补列通用实现；返回本次实际新增的列名列表。"""
    try:
        existing = {
            r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
    except sqlite3.OperationalError:
        return []
    added = []
    for col, ddl in columns.items():
        if col in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            added.append(col)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    return added


def migrate_user_columns() -> list:
    """对外入口：为 users 补 is_admin 列（迁移脚本 / 测试可用）。"""
    with get_conn() as conn:
        return _migrate_simple_columns(conn, "users", _USER_MIGRATION_COLUMNS)


def _backfill_detail_fields(conn: sqlite3.Connection) -> None:
    """
    一次性安全回填：
    - summary 为空且有 intro -> 取 intro 前 SUMMARY_MAX_LEN 字
    - tech_stack 为空 / 'null' -> 填 '[]'
    """
    try:
        conn.execute(
            "UPDATE competitions "
            "SET summary = substr(intro, 1, ?) "
            "WHERE (summary IS NULL OR summary = '') "
            "AND intro IS NOT NULL AND intro <> ''",
            (SUMMARY_MAX_LEN,),
        )
        conn.execute(
            "UPDATE competitions SET tech_stack = '[]' "
            "WHERE tech_stack IS NULL OR TRIM(tech_stack) = '' "
            "OR LOWER(TRIM(tech_stack)) = 'null'"
        )
    except sqlite3.OperationalError:
        # 老库尚未补列等极端情况，交由后续初始化再处理
        pass


def init_db(db_path: Optional[str] = None) -> None:
    """
    初始化数据库：执行 schema_v2.sql 建表（IF NOT EXISTS，可重复调用），
    再做补列迁移（competitions + awards）、候选表建表、回填与索引创建。
    不会插入任何业务数据。

    参数:
        db_path: 可选目标库路径，默认生产库 DB_PATH；测试脚本可传 test_crawl.db。
    """
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema_sql = f.read()
    with get_conn(db_path) as conn:
        conn.executescript(schema_sql)
        conn.executescript(CANDIDATES_DDL)
        conn.executescript(AI_JOBS_DDL)
        _migrate_columns(conn)
        _migrate_award_columns(conn)
        _migrate_insight_columns(conn)
        _migrate_simple_columns(conn, "users", _USER_MIGRATION_COLUMNS)
        _migrate_simple_columns(conn, "feedbacks", _FEEDBACK_MIGRATION_COLUMNS)
        _migrate_simple_columns(conn, "competition_candidates",
                                _CANDIDATE_MIGRATION_COLUMNS)
        _ensure_ai_analysis_cache(conn)
        conn.executescript(INDEXES_SQL)
        _backfill_detail_fields(conn)


# ============================================================
# 一、基础增删改（维护比赛库）
# ============================================================

def add_competition(data: dict) -> int:
    """
    新增一条比赛。

    参数:
        data: dict，需含以下键（created_at 由数据库自动生成，可不传）：
            name (str, 必填)
            category (str)
            reg_start_month (int, 1-12)
            reg_end_month (int, 1-12)
            reg_cross_year (int, 0/1)
            official_url (str)
    返回:
        新插入记录的 id (int)
    """
    level = data.get("level") or "national"
    if level not in ("national", "provincial"):
        level = "national"
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO competitions
               (name, category, reg_start_month, reg_end_month, reg_cross_year,
                official_url, level, level_label, sort_order, full_process_guide,
                reg_start_date, reg_end_date, contest_start_date, contest_end_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("name"),
                data.get("category"),
                data.get("reg_start_month"),
                data.get("reg_end_month"),
                (1 if data.get("reg_cross_year") else 0),
                data.get("official_url"),
                level,
                data.get("level_label"),
                data.get("sort_order"),
                data.get("full_process_guide"),
                _date_or_none(data.get("reg_start_date")),
                _date_or_none(data.get("reg_end_date")),
                _date_or_none(data.get("contest_start_date")),
                _date_or_none(data.get("contest_end_date")),
            ),
        )
        return cur.lastrowid


def update_competition(cid: int, data: dict, *,
                       db_path: Optional[str] = None) -> None:
    """
    按 id 更新比赛信息。仅更新 data 中出现的字段。

    参数:
        db_path: 可选目标库；批量脚本 / 审核补全可指向测试库，
                 默认沿用模块级 DB_PATH（生产库）。
    """
    allowed = ("name", "category", "reg_start_month", "reg_end_month",
               "reg_cross_year", "official_url", "level", "level_label",
               "sort_order", "intro", "summary", "tech_stack",
               "full_process_guide", "image_url", "short_name",
               "contest_start_month", "contest_end_month", "discover_source",
               "last_checked_at", "is_active", "participants", "content_scope",
               "exam_schedule", "stage_schedule",
               "reg_start_date", "reg_end_date",
               "contest_start_date", "contest_end_date",
               "has_project_awards")
    fields, values = [], []
    for key in allowed:
        if key in data:
            val = data[key]
            if key == "reg_cross_year":
                val = 1 if val else 0
            if key == "has_project_awards":
                # 0/1 落库，字符串 'false' 之类一律按 False 处理
                val = 1 if val in (1, True, "1", "true", "True") else 0
            if key == "level" and val not in ("national", "provincial"):
                val = "national"
            if key == "image_url":
                # 二维码/logo/banner/非 http(s) 一律置 NULL，由前端走简称兜底
                val = _clean_image_url(val)
            if key in _DATE_FIELDS:
                # 只接受能被证实的年月日；解析不出就写 NULL（不猜日期）
                val = _date_or_none(val)
            fields.append(f"{key} = ?")
            values.append(val)
    if not fields:
        return
    values.append(cid)
    with get_conn(db_path) as conn:
        conn.execute(
            f"UPDATE competitions SET {', '.join(fields)} WHERE id = ?", values
        )


def update_full_process_guide(cid: int, guide: str) -> None:
    """仅更新某场比赛的参赛全过程攻略（供 generate_guides.py 回写使用）。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE competitions SET full_process_guide = ? WHERE id = ?",
            (guide, cid),
        )


# 2 字赛事简称（纯汉字；不含数字/字母/标点）
_SHORT_NAME_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2}")


def save_short_name(cid: int, short_name: Optional[str],
                    db_path: Optional[str] = None) -> Optional[str]:
    """写入 AI 提取的 2 字赛事简称（供批量脚本 / 审核入库调用）。

    参数:
        cid: 比赛 id。
        short_name: 2 个纯汉字；空值表示清空（回退前端首字兜底）。
        db_path: 可选目标库，默认生产库；测试脚本可传 test_crawl.db。

    返回:
        实际写入的简称（清空时为 None）。

    异常:
        ValueError: 非 2 个纯汉字，或比赛 id 不存在。
    """
    value = unicodedata.normalize("NFKC", str(short_name or "")).strip()
    if value:
        if not _SHORT_NAME_RE.fullmatch(value):
            raise ValueError("short_name 必须是 2 个纯汉字：%r" % short_name)
    else:
        value = None

    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE competitions SET short_name = ? WHERE id = ?",
            (value, int(cid)),
        )
        if cur.rowcount != 1:
            raise ValueError("竞赛 ID=%s 不存在，简称未写入" % cid)
    return value


def fill_competition_detail_text(cid: int, *, participants=None,
                                 content_scope=None, exam_schedule=None,
                                 only_empty: bool = True,
                                 db_path: Optional[str] = None) -> dict:
    """写入赛氪详情补充文本（participants / content_scope / exam_schedule）。

    参数:
        cid: 比赛 id。
        participants / content_scope / exam_schedule: 待写入文本；
            None 或空白串表示该字段本次不写。
        only_empty: 默认 True，只填 NULL / 空串，已有值一律保留（不覆盖）。
        db_path: 可选目标库，默认生产库；测试脚本可传 test_crawl.db。

    返回:
        {字段名: 实际写入的文本}；已有值/空值被跳过时不包含该键。
    """
    updates = {}
    for key, val in (("participants", participants),
                     ("content_scope", content_scope),
                     ("exam_schedule", exam_schedule)):
        if val is None:
            continue
        text = str(val).strip()
        if not text:
            continue
        updates[key] = text
    if not updates:
        return {}

    written = {}
    with get_conn(db_path) as conn:
        for key, text in updates.items():
            sql = f"UPDATE competitions SET {key} = ? WHERE id = ?"
            if only_empty:
                sql += f" AND ({key} IS NULL OR TRIM({key}) = '')"
            cur = conn.execute(sql, (text, cid))
            if cur.rowcount == 1:
                written[key] = text
    return written


def save_generated_guide(cid: int, guide: str, *, model: Optional[str] = None,
                         prompt_ver: Optional[str] = None,
                         input_hash: Optional[str] = None,
                         db_path: Optional[str] = None) -> None:
    """写入 AI 生成的攻略，并同步更新溯源字段（guide_generated_at 等）。

    只有 `ai_content.generate_guide_for()` 与运维脚本会调用；写之前由调用方
    负责「已有攻略不覆盖」的判断，本函数只做单条 UPDATE。
    """
    cid = int(cid)
    if not (guide or "").strip():
        raise ValueError("攻略内容为空，拒绝写入")
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE competitions SET full_process_guide = ?, "
            "guide_generated_at = datetime('now', 'localtime'), "
            "guide_model = ?, guide_prompt_ver = ?, guide_input_hash = ? "
            "WHERE id = ?",
            (guide, model, prompt_ver, input_hash, cid),
        )
        if cur.rowcount != 1:
            raise ValueError("竞赛 ID=%s 不存在，攻略未写入" % cid)


def delete_competition(cid: int) -> None:
    """按 id 删除比赛。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM competitions WHERE id = ?", (cid,))


# 删除赛事时必须一并清理的子表（都以 competition_id 关联，不清理会产生数据孤儿）。
# 与 cleanup_empty_shells.py 口径一致：新增关联表时两处都要补。
COMPETITION_CHILD_TABLES = (
    "awards",            # 获奖作品
    "ideas",             # 备赛灵感 / 计划
    "insights",          # AI 洞察
    "user_preparations", # 用户备赛收藏
    "ai_jobs",           # AI 任务队列
    "ai_analysis_cache", # AI 分析缓存
    "ai_usage",          # AI 用量统计
)


def _table_has_column(conn, table: str, column: str) -> bool:
    """表不存在或没有该列时返回 False（老库缺表不报错）。"""
    try:
        return any(row[1] == column for row in conn.execute("PRAGMA table_info(%s)" % table))
    except sqlite3.Error:
        return False


def delete_competition_cascade(cid: int) -> Optional[dict]:
    """删除比赛并级联清理全部子表数据（单事务，失败整体回滚）。

    返回 {"name": 名称, "deleted": {表名: 删除条数}, "candidates_unlinked": n}；
    比赛不存在时返回 None，由路由层转 404。
    """
    cid = int(cid)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
        if row is None:
            return None
        name = row["name"]

        deleted = {}
        for table in COMPETITION_CHILD_TABLES:
            if _table_has_column(conn, table, "competition_id"):
                cur = conn.execute(
                    "DELETE FROM %s WHERE competition_id = ?" % table, (cid,)
                )
                if cur.rowcount:
                    deleted[table] = cur.rowcount

        # 候选表保留审核痕迹，只解绑外键，避免指向已删赛事
        unlinked = 0
        if _table_has_column(conn, "competition_candidates", "competition_id"):
            cur = conn.execute(
                "UPDATE competition_candidates SET competition_id = NULL "
                "WHERE competition_id = ?", (cid,)
            )
            unlinked = cur.rowcount

        conn.execute("DELETE FROM competitions WHERE id = ?", (cid,))
    return {"name": name, "deleted": deleted, "candidates_unlinked": unlinked}


def list_competitions_for_admin(limit: int = 20, offset: int = 0,
                                keyword: Optional[str] = None) -> dict:
    """管理后台「赛事管理」列表：轻量字段 + 分页，按 id 倒序（新入库在前）。

    返回 {"total": 总数, "items": [...]}；items 只带后台展示必需的列，
    不返回 longtext（intro / guide），避免列表响应过大。
    """
    where, params = "", []
    if keyword:
        where = " WHERE name LIKE ?"
        params.append("%%%s%%" % keyword)
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM competitions" + where, params
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT id, name, short_name, level, level_label, category, "
            "       discover_source, official_url, image_url, is_active, created_at, "
            "       (SELECT COUNT(*) FROM awards a "
            "         WHERE a.competition_id = competitions.id) AS awards_count "
            "FROM competitions" + where +
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)],
        ).fetchall()
    return {"total": total, "items": [dict(row) for row in rows]}


def get_competition(cid: int) -> Optional[dict]:
    """按 id 获取单条比赛，不存在返回 None。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
    return _enrich(row) if row else None


# ============================================================
# 二、查询 / 列表
# ============================================================

def list_competitions(category: Optional[str] = None,
                      month: Optional[int] = None,
                      keyword: Optional[str] = None,
                      limit: Optional[int] = None,
                      include_inactive: bool = False) -> list:
    """
    条件查询比赛列表（智能排序）。

    排序口径（2026-11-09 重构，全局统一）：
      1. 正在报名（今天在 [reg_start_date, reg_end_date] 内）—— reg_end_date 升序
         其中「只有月份、且 *_month 覆盖本月」的也算报名中，但整组排在
         所有带精确日期的之后（不猜具体日期，也不抢前排）
      2. 尚未开始（今天 < reg_start_date）—— reg_start_date 升序
      3. 已截止（今天 > reg_end_date）—— reg_end_date 倒序
      4. 无日期（日期与月份都不可用）—— 沉底

    全部由 SQL 基于服务器当前时间计算，不落地任何「状态」字段：
    过了截止日期，下一次刷新自动归入「已截止」；新审核通过的比赛按日期自动插队。

    参数:
        category: 类别模糊匹配
        month: 报名月覆盖该月份（考虑跨年区间），Python 侧二次过滤
        keyword: 名称模糊匹配
        limit: 只返回前 N 条（None = 全部）。**必须在 month 过滤之后才截断**：
               若把 LIMIT 写进 SQL，就是「先截断再过滤」，传了 limit 反而
               可能返回比 limit 更少的行，也会漏掉排在后面的当月赛事。
        include_inactive: True 时连下架（is_active=0）的赛事一起返回；
               仅供维护脚本（如 classify_competitions.py）批量打标记使用，
               对外接口永远只返回上架赛事。
    """
    where = ("WHERE 1 = 1" if include_inactive
             else "WHERE COALESCE(is_active, 1) = 1")
    params = []
    if category:
        where += " AND category LIKE ?"
        params.append(f"%{category}%")
    if keyword:
        where += " AND name LIKE ?"
        params.append(f"%{keyword}%")

    sql = (f"SELECT * FROM (SELECT *, {_PHASE_EXPR} AS _phase, "
           f"{_PREC_EXPR} AS _prec FROM competitions {where}) "
           f"{_ORDER_BY_SMART}")
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    result = [_enrich(_strip_phase(r)) for r in rows]
    if month:
        result = [r for r in result
                  if _month_in_range(month, r.get("reg_start_month"),
                                     r.get("reg_end_month"),
                                     r.get("reg_cross_year"))]
    if limit is not None:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 0
        if n >= 0:
            result = result[:n]
    return result


def list_hot_competitions(limit: int = 8) -> list:
    """首页「热门竞赛」（冷启动口径：没有用户浏览数据时使用）。

    定义：级别为国家级 + 当前正在报名或尚未开始（已截止、无日期的直接排除），
    排序沿用列表同一套紧迫度（越早截止越靠前，其次按临近开报时间），取前 N 条。
    无精确日期但 *_month 覆盖本月的也计入，排在所有带日期的之后。
    全部由 SQL 依据服务器当前时间计算，不缓存、不落库，刷新即生效。

    参数:
        limit: 返回条数上限（自动收敛到 1~50，默认 8）
    返回:
        list[dict]，已按紧迫度排序、已补 level_label

    注意：**实际条数允许少于 limit**。筛选口径是「当下真的能报」，赛季淡季
    只有 3 条就返回 3 条；禁止用已截止赛事补齐凑数（2026-11-12 产品决定，
    见 CRAWLER_HANDOVER.md §11.2），首页留白优于让用户误以为赛事过期。
    """
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 8
    n = max(1, min(50, n))

    sql = (f"SELECT * FROM (SELECT *, {_PHASE_EXPR} AS _phase, "
           f"{_PREC_EXPR} AS _prec FROM competitions "
           "WHERE COALESCE(is_active, 1) = 1 "
           "AND COALESCE(NULLIF(TRIM(level), ''), 'national') = 'national') "
           f"WHERE _phase IN (0, 1) {_ORDER_BY_SMART} LIMIT ?")
    with get_conn() as conn:
        rows = conn.execute(sql, (n,)).fetchall()
    return [_enrich(_strip_phase(r)) for r in rows]


def get_categories() -> list:
    """返回去重后的类别列表（非空）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM competitions "
            "WHERE COALESCE(is_active, 1) = 1 "
            "AND category IS NOT NULL AND TRIM(category) <> '' "
            "ORDER BY category"
        ).fetchall()
    return [r["category"] for r in rows]


def get_registering_by_month(month: int) -> list:
    """返回指定月份正在报名的比赛列表。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM competitions" + _ORDER_BY_CLAUSE).fetchall()
    return [r for r in (_enrich(x) for x in rows)
            if _month_in_range(month, r.get("reg_start_month"),
                               r.get("reg_end_month"),
                               r.get("reg_cross_year"))]


def get_month_timeline(month: int) -> list:
    """返回全年 12 个月的时间线视图（每个月报名中的比赛）。"""
    timeline = []
    for m in range(1, 13):
        timeline.append({"month": m, "competitions": get_registering_by_month(m)})
    return timeline


def _month_in_range(month: int, start: Optional[int], end: Optional[int],
                    cross_year) -> bool:
    """
    判断 month 是否落在 [start, end] 报名区间内。
    cross_year=1 表示区间跨年（如 start=11, end=3）。
    """
    if month is None or start is None or end is None:
        return False
    try:
        month, start, end = int(month), int(start), int(end)
    except (TypeError, ValueError):
        return False
    if start <= end and not cross_year:
        return start <= month <= end
    # 跨年或不规范区间：命中「start..12」或「1..end」
    return month >= start or month <= end


# ============================================================
# 三、获奖作品（awards）查询
# ============================================================

def _fetch_source_url_map(conn, title_keys) -> dict:
    """批量取「标题 -> 非空 source_url」，用于跨赛事同标题的链接兜底。

    title_keys 中同时包含原始标题与归一化标题（去空白/标点、小写），
    返回的 dict 同时以两种键索引，便于调用方直接命中。
    仅返回非空 URL；同标题多条有链接时取 id 最小的一条（最早已收录）。
    任何异常都静默降级为空 dict，不影响主查询。
    """
    if not title_keys:
        return {}

    raw_titles = [t for t in title_keys if isinstance(t, str) and t]
    if not raw_titles:
        return {}

    result = {}
    try:
        rows = conn.execute(
            "SELECT id, title, source_url FROM awards "
            "WHERE source_url IS NOT NULL AND TRIM(source_url) <> '' "
            "ORDER BY id ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}

    for r in rows:
        title = r["title"]
        url = (r["source_url"] or "").strip()
        if not title or not url:
            continue
        for key in (title, _norm_title(title)):
            if key and key not in result:
                result[key] = url
    return result


def _norm_title(title: Optional[str]) -> str:
    """标题归一化：去空白与常见标点、转小写，用于跨赛事同标题匹配。"""
    if not title:
        return ""
    t = re.sub(r"[\s\u3000]+", "", str(title))
    t = re.sub(r"[，。、；：！？,.;:!?（）()\[\]【】《》\"'“”‘’\-—_/\\|]+", "", t)
    return t.lower()


def _award_row_to_dict(row: sqlite3.Row, url_map: Optional[dict] = None) -> dict:
    """把 awards 行转成对外 dict：解析 tech_stack，补 origin_url 兜底。"""
    d = dict(row)
    raw_tech = d.get("tech_stack")
    tech = []
    if raw_tech:
        try:
            import json
            parsed = json.loads(raw_tech)
            if isinstance(parsed, list):
                tech = [str(x) for x in parsed if str(x).strip()]
            elif isinstance(parsed, str) and parsed.strip():
                tech = [parsed.strip()]
        except (ValueError, TypeError):
            tech = [s.strip() for s in str(raw_tech).split(",") if s.strip()]
    d["tech_stack"] = tech

    origin = (d.get("source_url") or "").strip()
    if not origin and url_map:
        title = d.get("title")
        origin = (url_map.get(title or "")
                  or url_map.get(_norm_title(title)) or "")
    d["origin_url"] = origin or None
    return d


def list_awards_by_competition(competition_id: int, limit: Optional[int] = None) -> list:
    """按竞赛 id 查询获奖作品列表（按年份倒序、id 升序）。

    过滤 review_status='rejected' 的人工拒绝脏数据（与路由文档约定一致）。
    """
    sql = ("SELECT * FROM awards WHERE competition_id = ? "
           "AND COALESCE(review_status, 'pending') <> 'rejected' "
           "ORDER BY CASE WHEN year IS NULL THEN 1 ELSE 0 END ASC, "
           "year DESC, id ASC")
    params = [competition_id]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        url_map = _fetch_source_url_map(
            conn, [r["title"] for r in rows if r["title"]]
        )
    return [_award_row_to_dict(r, url_map) for r in rows]


def get_award_by_id(award_id: int) -> Optional[dict]:
    """按 id 获取单条获奖作品，不存在返回 None。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM awards WHERE id = ?", (award_id,)
        ).fetchone()
        if not row:
            return None
        url_map = _fetch_source_url_map(conn, [row["title"]] if row["title"] else [])
    return _award_row_to_dict(row, url_map)


def awards_fingerprint(competition_id: int) -> str:
    """该赛事获奖数据指纹：条数/最大 id/最后更新时间/标题总长。

    用于判断「底层数据是否变过」：没变就复用缓存，绝不重复调用大模型。
    单条聚合查询，走 competition_id 索引，1.3 万条耗时 < 5ms。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(id), 0) AS mx, "
            "COALESCE(MAX(updated_at), '') AS ut, "
            "COALESCE(SUM(LENGTH(COALESCE(title, ''))), 0) AS tl "
            "FROM awards WHERE competition_id = ?",
            (int(competition_id),),
        ).fetchone()
    if not row:
        return "n=0|mx=0|ut=|tl=0"
    return f"n={row['c']}|mx={row['mx']}|ut={row['ut']}|tl={row['tl']}"


def count_awards_by_competition(competition_id: int) -> int:
    """统计某竞赛下的获奖作品数量。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM awards WHERE competition_id = ?",
            (competition_id,),
        ).fetchone()
    return int(row["c"]) if row else 0


# ============================================================
# 四、参赛全过程攻略（full_process_guide）
# ============================================================

def list_competitions_for_guide(only_missing: bool = False) -> list:
    """返回用于生成攻略的比赛列表（id / name / category / official_url 等）。

    参数:
        only_missing: 为 True 时只返回 full_process_guide 为空或未填的比赛，
                      便于脚本断点续跑、避免重复消耗大模型额度。
    """
    sql = ("SELECT id, name, category, level, level_label, official_url, "
           "intro, summary, full_process_guide, guide_generated_at, "
           "guide_model, guide_prompt_ver FROM competitions")
    if only_missing:
        sql += (" WHERE full_process_guide IS NULL "
                "OR TRIM(full_process_guide) = ''")
    sql += " ORDER BY id ASC"
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [_enrich(r) for r in rows]


def list_competitions_for_short_names(only_missing: bool = True,
                                      db_path: Optional[str] = None) -> list:
    """返回用于 AI 提取 2 字简称的比赛列表（id / name / category / short_name）。

    参数:
        only_missing: 为 True 时只返回 short_name 为空/未填的比赛，
                      便于断点续跑、避免重复消耗大模型额度。
        db_path: 可选目标库；未迁移的老库即使没有 short_name 列也能只读查询
                 （该列按 NULL 处理），方便 --dry-run 保持零副作用。

    返回:
        list[dict]，按 id 升序。
    """
    with get_conn(db_path) as conn:
        try:
            columns = {
                r["name"] for r in conn.execute(
                    "PRAGMA table_info(competitions)"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            return []
        if not columns:
            return []

        has_short_name = "short_name" in columns
        short_expr = "short_name" if has_short_name else "NULL AS short_name"
        sql = "SELECT id, name, category, %s FROM competitions" % short_expr
        if only_missing and has_short_name:
            sql += " WHERE short_name IS NULL OR TRIM(short_name) = ''"
        sql += " ORDER BY id ASC"
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_full_process_guide(cid: int) -> Optional[str]:
    """读取某场比赛的参赛全过程攻略；比赛不存在返回 None。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT full_process_guide FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
    if row is None:
        return None
    return row["full_process_guide"] or ""


# ============================================================
# 五、兼容旧接口（保留占位，避免上层 import 失败）
# ============================================================

def now_str() -> str:
    """返回当前本地时间字符串（YYYY-MM-DD HH:MM:SS）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_user_by_id(user_id):
    """根据 user_id 查询 users 表，返回单条记录（fetchone）。"""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(username):
    """根据 username 查询 users 表，返回单条记录（fetchone）。"""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def create_user(username: str, password_hash: str) -> int:
    """
    新建用户，返回新用户 id。

    参数:
        username: 用户名（调用方已归一化，唯一索引兜底）
        password_hash: 已哈希的密码（bcrypt），严禁传明文
    返回:
        新插入记录的 id (int)
    异常:
        用户名重复时抛 sqlite3.IntegrityError（由上层转 409）
    """
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hash),
        )
        return cur.lastrowid


# ============================================================
# 六、ai_usage：AI 每日配额（原子扣减）
# ============================================================
# 说明：本段曾因 db.py 被整文件覆盖而丢失，导致 security.require_ai_quota /
# current_ai_quota 内的 `from db import consume_ai_quota` 直接 ImportError，
# 所有 AI 接口 500。此处按原实现恢复。
#
# 表结构见 schema_v2.sql 第 9 节：ai_usage(user_id, usage_date, used, updated_at)，
# 主键 (user_id, usage_date)，即「每人每天一行」。

def _today_str() -> str:
    """本地日期字符串 'YYYY-MM-DD'；配额在本地零点自然翻滚。"""
    return datetime.now().strftime("%Y-%m-%d")


def consume_ai_quota(user_id: int, limit: int) -> dict:
    """原子扣减一次今日 AI 调用额度。

    并发语义
    --------
    整个扣减是**单条带条件的 UPDATE**：

        UPDATE ai_usage SET used = used + 1
        WHERE user_id = ? AND usage_date = ? AND used < ?

    SQLite 的写操作串行化，两个并发请求不可能同时通过 `used < limit` 判断，
    因此不会把 used 顶穿上限。用 `cur.rowcount` 判定是否抢到配额
    （1 = 抢到，0 = 已用尽），避免「先读后写」的竞态。

    当天首次调用走带守卫的 INSERT，直接以 used=1 建行。

    返回
    ----
    dict：allowed (bool), used (int), limit (int), remaining (int)
    limit <= 0 表示不限次数（仅开发环境），此时 remaining = -1。
    被拒绝时 used 等于已用尽的值（== limit），remaining = 0。
    """
    limit = int(limit)
    uid = int(user_id)
    today = _today_str()

    # limit <= 0 表示不限次数（开发环境逃生口）
    if limit <= 0:
        return {"allowed": True, "used": 0, "limit": 0, "remaining": -1}

    with get_conn() as conn:
        # 1) 快路径：当天已有行 -> 带守卫自增
        cur = conn.execute(
            "UPDATE ai_usage SET used = used + 1, "
            "updated_at = datetime('now', 'localtime') "
            "WHERE user_id = ? AND usage_date = ? AND used < ?",
            (uid, today, limit),
        )
        if cur.rowcount == 1:
            row = conn.execute(
                "SELECT used FROM ai_usage WHERE user_id = ? AND usage_date = ?",
                (uid, today),
            ).fetchone()
            used = int(row["used"])
            return {
                "allowed": True,
                "used": used,
                "limit": limit,
                "remaining": max(limit - used, 0),
            }

        # 2) 没有行 or 配额已用尽 -> 查一次区分
        row = conn.execute(
            "SELECT used FROM ai_usage WHERE user_id = ? AND usage_date = ?",
            (uid, today),
        ).fetchone()

        if row is not None:
            # 行存在但守卫失败 => 已用尽
            used = int(row["used"])
            return {"allowed": False, "used": used, "limit": limit, "remaining": 0}

        # 3) 当天第一次调用：建行且 used=1。
        #    INSERT OR IGNORE：若并发请求先建了行，rowcount == 0，
        #    回落到带守卫的 UPDATE。
        cur = conn.execute(
            "INSERT OR IGNORE INTO ai_usage (user_id, usage_date, used) "
            "VALUES (?, ?, 1)",
            (uid, today),
        )
        if cur.rowcount == 1:
            return {"allowed": True, "used": 1, "limit": limit,
                    "remaining": max(limit - 1, 0)}

        # 抢插入失败 -> 重试一次带守卫的自增
        cur = conn.execute(
            "UPDATE ai_usage SET used = used + 1, "
            "updated_at = datetime('now', 'localtime') "
            "WHERE user_id = ? AND usage_date = ? AND used < ?",
            (uid, today, limit),
        )
        row = conn.execute(
            "SELECT used FROM ai_usage WHERE user_id = ? AND usage_date = ?",
            (uid, today),
        ).fetchone()
        used = int(row["used"]) if row else limit
        allowed = cur.rowcount == 1
        return {
            "allowed": allowed,
            "used": used,
            "limit": limit,
            "remaining": max(limit - used, 0) if allowed else 0,
        }


def get_ai_quota(user_id: int, limit: int) -> dict:
    """只读查询今日用量，不扣减（供 /api/ai/quota 展示用）。

    返回 dict：used (int), limit (int), remaining (int)
    remaining = -1 表示不限次数（limit <= 0），由上层翻译成 None。
    """
    limit = int(limit)
    with get_conn() as conn:
        try:
            row = conn.execute(
                "SELECT used FROM ai_usage WHERE user_id = ? AND usage_date = ?",
                (int(user_id), _today_str()),
            ).fetchone()
        except sqlite3.OperationalError:
            # 老库尚未建 ai_usage 表，降级为「未使用」
            return {"used": 0, "limit": limit, "remaining": max(limit, 0)}
    used = int(row["used"]) if row else 0
    if limit <= 0:
        return {"used": used, "limit": 0, "remaining": -1}
    return {"used": used, "limit": limit, "remaining": max(limit - used, 0)}


# ============================================================
# 七、user_preparations：用户备赛清单（需登录）
# ============================================================
# 表见 schema_v2.sql 第 8 节，唯一索引 ux_user_prep(user_id, competition_id)
# 保证同一竞赛重复加入是幂等的。

def add_preparation(user_id: int, competition_id: int) -> dict:
    """把某竞赛加入用户的备赛清单（幂等）。

    返回 {"id": 清单行 id, "created": 是否本次新建}
    竞赛不存在时抛 ValueError（由路由转 404）。
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM competitions WHERE id = ?", (int(competition_id),)
        ).fetchone()
        if row is None:
            raise ValueError("competition not found")

        cur = conn.execute(
            "INSERT OR IGNORE INTO user_preparations (user_id, competition_id) "
            "VALUES (?, ?)",
            (int(user_id), int(competition_id)),
        )
        created = cur.rowcount > 0
        got = conn.execute(
            "SELECT id FROM user_preparations WHERE user_id = ? AND competition_id = ?",
            (int(user_id), int(competition_id)),
        ).fetchone()
    return {"id": got["id"] if got else None, "created": created}


def remove_preparation(user_id: int, competition_id: int) -> bool:
    """从备赛清单移除某竞赛；有行被删除返回 True。"""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM user_preparations WHERE user_id = ? AND competition_id = ?",
            (int(user_id), int(competition_id)),
        )
        return cur.rowcount > 0


# 备赛清单需要、但属于「后续迁移新增」的 competitions 列。
# 老库缺列时用 NULL 占位，保证 JOIN 查询不整体失败。
_PREP_EXTRA_COLUMNS = (
    ("contest_start_month", "c.contest_start_month"),
    ("contest_end_month", "c.contest_end_month"),
    ("reg_start_date", "c.reg_start_date"),
    ("reg_end_date", "c.reg_end_date"),
    ("contest_start_date", "c.contest_start_date"),
    ("contest_end_date", "c.contest_end_date"),
    ("image_url", "c.image_url"),
    ("short_name", "c.short_name"),
)


def list_preparations(user_id: int) -> list:
    """列出用户备赛清单（JOIN 竞赛信息），最新加入的在前。

    返回 list[dict]，键：prep_id / competition_id / added_at /
    name / category / level / level_label / reg_start_month / reg_end_month /
    reg_cross_year / contest_start_month / contest_end_month / image_url /
    short_name / official_url / summary。

    注意：image_url / short_name / contest_*_month 供前端卡片封面与「比赛时间」使用，
    只读查询，不涉及任何写操作。老库尚未迁移这些列时自动降级为 NULL，
    避免因缺列把备赛清单静默清空。
    """
    with get_conn() as conn:
        try:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(competitions)")}
        except sqlite3.OperationalError:
            return []

        extra = "".join(
            ", {expr} AS {name}".format(
                expr=expr if name in existing else "NULL", name=name
            )
            for name, expr in _PREP_EXTRA_COLUMNS
        )

        try:
            rows = conn.execute(
                f"""
                SELECT p.id              AS prep_id,
                       p.competition_id  AS competition_id,
                       p.created_at      AS added_at,
                       c.name            AS name,
                       c.category        AS category,
                       c.level           AS level,
                       c.level_label     AS level_label,
                       c.reg_start_month AS reg_start_month,
                       c.reg_end_month   AS reg_end_month,
                       c.reg_cross_year  AS reg_cross_year{extra},
                       c.official_url    AS official_url,
                       c.summary         AS summary
                FROM user_preparations p
                JOIN competitions c ON c.id = p.competition_id
                WHERE p.user_id = ?
                ORDER BY p.created_at DESC, p.id DESC
                """,
                (int(user_id),),
            ).fetchall()
        except sqlite3.OperationalError:
            # 老库尚未建 user_preparations 表 -> 视为空清单
            return []
    return [dict(r) for r in rows]


def is_prepared(user_id: int, competition_id: int) -> bool:
    """该竞赛是否已在用户备赛清单中。"""
    with get_conn() as conn:
        try:
            row = conn.execute(
                "SELECT 1 FROM user_preparations WHERE user_id = ? AND competition_id = ?",
                (int(user_id), int(competition_id)),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
    return row is not None


# ============================================================
# 八、users：账号补充 CRUD
# ============================================================
# 已有的读/建：create_user / get_user_by_id / get_user_by_username。
# 这里补齐改、删、列表，避免上层只能手写 SQL。

def exists_user(username: str) -> bool:
    """用户名是否已存在。"""
    return get_user_by_username(username) is not None


def update_user_password(user_id: int, password_hash: str) -> bool:
    """更新密码哈希（调用方负责传入已 bcrypt 的哈希）。返回是否有行被更新。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, int(user_id)),
        )
        return cur.rowcount > 0


def delete_user(user_id: int) -> bool:
    """删除用户；user_preparations / ai_usage 由外键 ON DELETE CASCADE 级联清理。"""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
        return cur.rowcount > 0


def list_users(limit: int = 100, offset: int = 0) -> list:
    """列出用户（不含 password_hash，避免误回显）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at FROM users "
            "ORDER BY id ASC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        ).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# 九、awards：获奖作品补充 CRUD
# ============================================================
# 读：list_awards_by_competition / get_award_by_id / count_awards_by_competition
# 这里补齐增、改、删。写入后置 embedding_state='stale'，作为
# 「SQLite 已更新、待同步 Chroma」的脏标记（与 schema 设计一致）。

# competition_raw / award_grade 是抓取落库需要的字段：
#   - competition_raw 保存未归并到 competitions 的原始赛事名；
#   - award_grade 是前端 Award 模型与检索读取的获奖等级，抓取时与 award_level 双写。
_AWARD_WRITABLE_FIELDS = (
    "competition_id", "competition_raw", "year", "category", "title", "team",
    "advisor", "award_level", "award_grade", "summary", "tech_stack",
    "highlights", "detail_md", "source_url", "source_type", "raw_html_path",
    "confidence", "review_status", "embedding_state", "content_hash",
)

# 这些字段变化会影响向量语义，需要把 embedding_state 标脏
_AWARD_EMBEDDING_FIELDS = (
    "title", "summary", "highlights", "detail_md", "tech_stack",
    "category", "award_level", "award_grade", "competition_raw",
    "year", "team", "advisor",
)


def _encode_tech_stack(value) -> str:
    """tech_stack 入库编码：list -> JSON 字符串；空值 -> '[]'。"""
    if value is None:
        return "[]"
    if isinstance(value, str):
        return value.strip() or "[]"
    try:
        import json
        return json.dumps(list(value), ensure_ascii=False)
    except (TypeError, ValueError):
        return "[]"


def _award_columns(conn) -> set:
    """当前 awards 表的真实列名集合。

    线上库是「老库」（含 session / award_grade / track / difficulty / metrics
    等历史列），而 schema_v2.sql 是新建库脚本（含 advisor / award_level /
    detail_md）。两边列并不完全一致，写入前按真实列裁剪，避免
    "table awards has no column named ..." 的 OperationalError。
    """
    try:
        return {r["name"] for r in conn.execute("PRAGMA table_info(awards)").fetchall()}
    except sqlite3.OperationalError:
        return set()


def create_award(data: dict) -> int:
    """新增一条获奖作品，返回新记录 id。

    data 键取自 _AWARD_WRITABLE_FIELDS（不属于当前表结构的键会被自动忽略）；
    tech_stack 允许传 list。title 必填（表约束 NOT NULL）。
    content_hash 若与已有记录冲突，会抛 sqlite3.IntegrityError（唯一索引兜底）。
    """
    if not isinstance(data, dict):
        raise TypeError("data must be a dict")

    planned = []
    for key in _AWARD_WRITABLE_FIELDS:
        if key not in data:
            continue
        val = _encode_tech_stack(data[key]) if key == "tech_stack" else data[key]
        planned.append((key, val))

    with get_conn() as conn:
        existing = _award_columns(conn)
        columns, values = [], []
        for key, val in planned:
            if key in existing:
                columns.append(key)
                values.append(val)

        if "title" not in columns:
            raise ValueError("title is required")

        placeholders = ", ".join("?" * len(columns))
        cur = conn.execute(
            f"INSERT INTO awards ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        return cur.lastrowid


def update_award(award_id: int, data: dict) -> bool:
    """按 id 部分更新获奖作品。仅更新 data 中出现的字段。

    语义字段变化时自动把 embedding_state 置为 'stale'，
    并刷新 updated_at；返回是否有行被更新。
    """
    if not isinstance(data, dict):
        raise TypeError("data must be a dict")

    fields, values = [], []
    touched_content = False
    with get_conn() as conn:
        existing = _award_columns(conn)
        for key in _AWARD_WRITABLE_FIELDS:
            if key not in data or key not in existing:
                continue
            val = data[key]
            if key == "tech_stack":
                val = _encode_tech_stack(val)
            if key in _AWARD_EMBEDDING_FIELDS:
                touched_content = True
            fields.append(f"{key} = ?")
            values.append(val)

        if not fields:
            return False

        if "updated_at" in existing:
            fields.append("updated_at = datetime('now', 'localtime')")
        if (touched_content and "embedding_state" not in data
                and "embedding_state" in existing):
            fields.append("embedding_state = 'stale'")

        values.append(int(award_id))
        cur = conn.execute(
            f"UPDATE awards SET {', '.join(fields)} WHERE id = ?", values
        )
        return cur.rowcount > 0


def delete_award(award_id: int) -> bool:
    """按 id 删除获奖作品；有行被删除返回 True。"""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM awards WHERE id = ?", (int(award_id),))
        return cur.rowcount > 0


# ============================================================
# 十、ai_analysis_cache：AI 分析结果全局缓存
# ============================================================
# 目的：单作品深度解析 / 赛事共性分析的结果按「作品 / 赛事」全局落库，
# 同一个目标再次请求时直接复用，不再调用大模型（省时省钱，响应恒定）。
#
# 表见 schema_v2.sql 第 10 节。缓存有效性由 input_hash 把关：
#   * 读出时比对当前 prompt 的 SHA-256 与库中值；
#   * 不一致说明作品/赛事数据已更新，视为未命中，重新生成并覆盖旧行。
# 因此「数据更新后仍返回旧分析」不会发生。
#
# 老库（competitions.db）不会自动重跑 schema_v2.sql，故这里对
# "no such table" 做一次建表重试，保证首次调用即可自愈。

AI_ANALYSIS_TARGET_TYPES = ("work", "competition", "inspiration")

# 灵感缓存：同一赛事只要生成过，后续请求直接读此标记，命中则绝不再调用大模型。
# input_hash 由「获奖数据指纹 + prompt 版本 + 模型」组成，只有底层数据变化才失效。
_INSPIRATION_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS ai_analysis_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT    NOT NULL CHECK (target_type IN ('work', 'competition', 'inspiration')),
    target_id   INTEGER NOT NULL,
    input_hash  TEXT    NOT NULL,
    content_md  TEXT    NOT NULL,
    model       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_analysis_cache_target
    ON ai_analysis_cache (target_type, target_id);
"""

_AI_ANALYSIS_CACHE_DDL = _INSPIRATION_CACHE_DDL


def _ensure_ai_analysis_cache(conn: sqlite3.Connection) -> None:
    """幂等建表：创建 ai_analysis_cache 及其唯一索引。

    老库的 CHECK 约束只允许 work/competition，新增 inspiration 时无法插入。
    该表是纯缓存（内容可随时重建），因此旧表 CHECK 不含 inspiration 时
    直接重建：建新表 -> 迁移旧行 -> 删旧表 -> 改名。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_analysis_cache'"
    ).fetchone()
    old_sql = (row[0] or "") if row else ""
    if old_sql and "'inspiration'" not in old_sql:
        try:
            conn.execute("ALTER TABLE ai_analysis_cache RENAME TO ai_analysis_cache_old")
            conn.executescript(_AI_ANALYSIS_CACHE_DDL)
            conn.execute(
                "INSERT INTO ai_analysis_cache "
                "(id, target_type, target_id, input_hash, content_md, model, created_at, updated_at) "
                "SELECT id, target_type, target_id, input_hash, content_md, model, created_at, updated_at "
                "FROM ai_analysis_cache_old"
            )
            conn.execute("DROP TABLE ai_analysis_cache_old")
            logger.info("ai_analysis_cache CHECK 已升级，新增 inspiration 缓存类型")
            return
        except sqlite3.Error as exc:  # 迁移失败则保持旧表，灵感缓存降级为无缓存
            logger.warning("ai_analysis_cache 升级失败（降级为无缓存）: %r", exc)
            try:
                conn.execute("DROP TABLE IF EXISTS ai_analysis_cache_old")
            except sqlite3.Error:
                pass
    conn.executescript(_AI_ANALYSIS_CACHE_DDL)


def ensure_ai_analysis_cache() -> None:
    """对外入口：确保缓存表存在（供启动脚本/运维手动调用）。

    AI 路由本身会在读写时自动建表，无需手动执行；这里提供显式入口，
    便于部署后体检或预热。失败只记日志，不阻断主流程。
    """
    try:
        with get_conn() as conn:
            _ensure_ai_analysis_cache(conn)
    except sqlite3.Error as exc:
        logger.warning("创建 ai_analysis_cache 表失败: %r", exc)


def _check_target_type(target_type: str) -> str:
    """校验缓存目标类型，非法值直接抛 ValueError（属于调用方 bug）。"""
    t = str(target_type).strip().lower()
    if t not in AI_ANALYSIS_TARGET_TYPES:
        raise ValueError(
            f"target_type must be one of {AI_ANALYSIS_TARGET_TYPES}, got {target_type!r}"
        )
    return t


def get_ai_analysis(target_type: str, target_id: int,
                    input_hash: Optional[str] = None) -> Optional[dict]:
    """读取某作品/某赛事的已缓存分析结果。

    参数:
        target_type: 'work' 或 'competition'
        target_id:   对应 awards.id 或 competitions.id
        input_hash:  当前 prompt 的指纹。传入时只有指纹一致才返回缓存，
                     不一致（内容已更新）返回 None，由调用方重新生成。

    返回:
        dict（id / target_type / target_id / input_hash / content_md /
        model / created_at / updated_at），未命中或读库异常返回 None。
        缓存只是加速手段，任何异常都降级为「无缓存」，不影响接口可用性。
    """
    t = _check_target_type(target_type)
    sql = (
        "SELECT id, target_type, target_id, input_hash, content_md, model, "
        "created_at, updated_at FROM ai_analysis_cache "
        "WHERE target_type = ? AND target_id = ?"
    )
    params = (t, int(target_id))

    try:
        with get_conn() as conn:
            try:
                row = conn.execute(sql, params).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                _ensure_ai_analysis_cache(conn)
                row = conn.execute(sql, params).fetchone()
    except sqlite3.Error as exc:
        logger.warning("读取 AI 分析缓存失败（降级为无缓存）: %r", exc)
        return None

    if row is None:
        return None
    cached = dict(row)
    if input_hash is not None and cached.get("input_hash") != input_hash:
        return None
    return cached


def save_ai_analysis(target_type: str, target_id: int, input_hash: str,
                     content_md: str, model: Optional[str] = None) -> bool:
    """写入/覆盖某作品或某赛事的分析结果（按 target 唯一）。

    UPSERT 语义：同一 target 已存在时更新 input_hash / content_md / model
    与 updated_at，不新增多行。空内容不写（返回 False），避免污染缓存。

    返回:
        True=已落库；False=内容为空或写库异常（已记日志）。
        写缓存失败绝不影响接口正常返回分析结果。
    """
    t = _check_target_type(target_type)
    text = (content_md or "").strip()
    if not text:
        return False

    sql = (
        "INSERT INTO ai_analysis_cache "
        "(target_type, target_id, input_hash, content_md, model) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(target_type, target_id) DO UPDATE SET "
        "input_hash = excluded.input_hash, "
        "content_md = excluded.content_md, "
        "model = excluded.model, "
        "updated_at = datetime('now', 'localtime')"
    )
    params = (t, int(target_id), input_hash, text, model)

    try:
        with get_conn() as conn:
            try:
                conn.execute(sql, params)
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                _ensure_ai_analysis_cache(conn)
                conn.execute(sql, params)
    except sqlite3.Error as exc:
        logger.warning("写入 AI 分析缓存失败（忽略）: %r", exc)
        return False
    return True


def delete_ai_analysis(target_type: str, target_id: int) -> bool:
    """删除某作品/某赛事的缓存分析结果；有行被删除返回 True。

    供数据修正或需要强制重新生成时使用；接口自身不主动调用。
    """
    t = _check_target_type(target_type)
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM ai_analysis_cache WHERE target_type = ? AND target_id = ?",
                (t, int(target_id)),
            )
            return cur.rowcount > 0
    except sqlite3.Error as exc:
        logger.warning("删除 AI 分析缓存失败: %r", exc)
        return False


# ============================================================
# 十一、insights / ideas：灵感工作台（按赛事强隔离）
# ============================================================
# 读取接口必须带 competition_id，禁止返回跨赛事混合数据；
# 写入由离线脚本 analyzer.py 完成，接口本身只读。

INSIGHT_KINDS = ("experience", "trend", "gap")


def _check_competition_id(competition_id) -> int:
    """灵感相关接口的统一入口校验：competition_id 必须为正整数。"""
    if competition_id is None:
        raise ValueError("competition_id 必填，禁止跨赛事混合读取灵感数据")
    try:
        cid = int(competition_id)
    except (TypeError, ValueError):
        raise ValueError(f"competition_id 必须是整数，收到: {competition_id!r}")
    if cid <= 0:
        raise ValueError(f"competition_id 必须大于 0，收到: {cid}")
    return cid


def _json_list(value) -> list:
    """把 evidence / ref_awards 统一解析为 list；脏数据返回空列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            import json
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def list_insights_by_competition(competition_id: int,
                                 kind: Optional[str] = None) -> list:
    """按赛事读取 insights（严格过滤，competition_id 必填）。"""
    cid = _check_competition_id(competition_id)
    sql = "SELECT * FROM insights WHERE competition_id = ?"
    params: list = [cid]
    if kind:
        if kind not in INSIGHT_KINDS:
            raise ValueError(f"kind 必须是 {INSIGHT_KINDS} 之一，收到: {kind!r}")
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY CASE kind WHEN 'experience' THEN 0 WHEN 'trend' THEN 1 " \
           "WHEN 'gap' THEN 2 ELSE 3 END, id DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def save_insight(kind: str, scope: str, content_md: str, *,
                 competition_id: int, evidence: Optional[list] = None,
                 model: Optional[str] = None,
                 input_hash: Optional[str] = None) -> int:
    """写入一条赛事洞察。

    同一赛事同一 kind 只保留最新一条（先删后插），避免页面重复渲染旧结论。
    """
    if kind not in INSIGHT_KINDS:
        raise ValueError(f"kind 必须是 {INSIGHT_KINDS} 之一，收到: {kind!r}")
    cid = _check_competition_id(competition_id)
    text = (content_md or "").strip()
    if not text:
        raise ValueError("content_md 不能为空")
    evidence_json = None
    if evidence is not None:
        import json
        evidence_json = json.dumps(list(evidence), ensure_ascii=False)
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM insights WHERE competition_id = ? AND kind = ?",
            (cid, kind),
        )
        cur = conn.execute(
            "INSERT INTO insights "
            "(kind, scope, competition_id, input_hash, content_md, evidence, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, scope or "", cid, input_hash, text, evidence_json, model),
        )
        return int(cur.lastrowid)


def delete_insights_by_competition(competition_id: int) -> int:
    """删除某赛事的全部 insights，返回删除行数（--force 重生成用）。"""
    cid = _check_competition_id(competition_id)
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM insights WHERE competition_id = ?", (cid,)
        )
        return int(cur.rowcount)


def list_ideas_by_competition(competition_id: int) -> list:
    """按赛事读取 ideas（严格过滤，competition_id 必填）。"""
    cid = _check_competition_id(competition_id)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ideas WHERE competition_id = ? ORDER BY id ASC",
            (cid,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_idea(title: str, *, competition_id: int,
              pitch: Optional[str] = None, detail_md: Optional[str] = None,
              novelty: Optional[str] = None, risk: Optional[str] = None,
              ref_awards: Optional[list] = None, status: str = "draft",
              difficulty: Optional[str] = None, metrics: Optional[str] = None,
              content_hash: Optional[str] = None) -> tuple:
    """写入一条赛事 idea，返回 (id, action)。

    content_hash 默认用「赛事 ID + 标题」生成，保证不同赛事同名 idea 不会被误去重。
    """
    cid = _check_competition_id(competition_id)
    text = (title or "").strip()
    if not text:
        raise ValueError("idea.title 不能为空")
    if status not in ("draft", "starred", "archived"):
        status = "draft"
    if not content_hash:
        import hashlib
        content_hash = hashlib.sha1(f"{cid}|{text}".encode("utf-8")).hexdigest()
    ref_json = None
    if ref_awards is not None:
        import json
        ref_json = json.dumps(list(ref_awards), ensure_ascii=False)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO ideas "
            "(competition_id, title, pitch, detail_md, novelty, risk, ref_awards, "
            " status, difficulty, metrics, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, text, pitch, detail_md, novelty, risk, ref_json,
             status, difficulty, metrics, content_hash),
        )
        if cur.rowcount > 0:
            return int(cur.lastrowid), "inserted"
        row = conn.execute(
            "SELECT id FROM ideas WHERE content_hash = ?", (content_hash,)
        ).fetchone()
    return (int(row["id"]) if row else None), "skipped"


def delete_ideas_by_competition(competition_id: int) -> int:
    """删除某赛事的全部 ideas，返回删除行数（--force 重生成用）。"""
    cid = _check_competition_id(competition_id)
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM ideas WHERE competition_id = ?", (cid,))
        return int(cur.rowcount)


# ============================================================
# 十二、feedbacks：用户反馈（只写入，按需求不提供查询接口）
# ============================================================
# 表见 schema_v2.sql 第 11 节。
# 接口层强制登录，这里再做一次内容兜底校验；user_id 允许为空，
# 但正常调用始终由 /api/feedback 从 Token 解析后传入。
#
# 老库在进程未重启时可能还没执行到 schema_v2.sql 的建表，
# 故对 "no such table" 做一次建表重试，保证首次提交即可成功。

_FEEDBACKS_DDL = """
CREATE TABLE IF NOT EXISTS feedbacks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users (id) ON DELETE SET NULL,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    is_read    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feedbacks_created
    ON feedbacks (created_at);
CREATE INDEX IF NOT EXISTS idx_feedbacks_user
    ON feedbacks (user_id);
"""


def _ensure_feedbacks(conn: sqlite3.Connection) -> None:
    """幂等建表：创建 feedbacks 表及其索引。"""
    conn.executescript(_FEEDBACKS_DDL)


def create_feedback(content: str, user_id: Optional[int] = None) -> int:
    """写入一条用户反馈，返回新记录 id。

    参数:
        content: 反馈正文；首尾空白会被去掉，去空白后为空则抛 ValueError。
        user_id: 提交人 id（可空）。由路由层从 Token 解析后传入，避免伪造。

    异常:
        ValueError: 内容为空（由路由转 422）。
        sqlite3.Error: 写库失败（由路由转 500）。
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("反馈内容不能为空")

    sql = "INSERT INTO feedbacks (user_id, content) VALUES (?, ?)"
    params = (user_id, text)

    with get_conn() as conn:
        try:
            cur = conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            _ensure_feedbacks(conn)
            cur = conn.execute(sql, params)
        return int(cur.lastrowid)


def list_feedbacks(limit: int = 200, offset: int = 0,
                   db_path: Optional[str] = None) -> list:
    """管理后台用：按时间倒序读取所有用户反馈（含用户名、已读标记）。"""
    limit = max(1, min(int(limit or 200), 500))
    offset = max(0, int(offset or 0))
    sql = (
        "SELECT f.id, f.user_id, f.content, f.created_at, f.is_read, "
        "       u.username "
        "FROM feedbacks f LEFT JOIN users u ON u.id = f.user_id "
        "ORDER BY f.id DESC LIMIT ? OFFSET ?"
    )
    with get_conn(db_path) as conn:
        try:
            rows = conn.execute(sql, (limit, offset)).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            _ensure_feedbacks(conn)
            rows = []
    return [dict(row) for row in rows]


def mark_feedback_read(feedback_id: int, is_read: int = 1,
                       db_path: Optional[str] = None) -> bool:
    """把单条反馈标记为已读（/ 未读）；返回是否命中记录。"""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE feedbacks SET is_read = ? WHERE id = ?",
            (1 if is_read else 0, feedback_id),
        )
        return cur.rowcount > 0


def count_unread_feedbacks(db_path: Optional[str] = None) -> int:
    """未读反馈数（管理后台侧边栏角标用）。"""
    with get_conn(db_path) as conn:
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM feedbacks WHERE is_read = 0"
            ).fetchone()[0])
        except sqlite3.OperationalError:
            return 0


def set_user_admin(user_id: int, is_admin: bool = True,
                   db_path: Optional[str] = None) -> bool:
    """设置 / 取消管理员标记；返回是否命中用户。"""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE users SET is_admin = ? WHERE id = ?",
            (1 if is_admin else 0, user_id),
        )
        return cur.rowcount > 0


def set_user_admin_by_username(username: str, is_admin: bool = True,
                               db_path: Optional[str] = None) -> bool:
    """按用户名（小写归一化后）设置管理员标记。"""
    name = (username or "").strip().lower()
    if not name:
        return False
    user = get_user_by_username(name)
    if user is None:
        return False
    return set_user_admin(int(user["id"]), is_admin, db_path=db_path)


# ============================================================
# 十二、新赛事候选表（competition_candidates）
# ============================================================
# 流程：发现脚本 -> competition_candidates(pending) -> 人工审核 ->
#       审核通过才 INSERT competitions（本模块不自动写主表）。
# 去重口径见 CRAWLER_HANDOVER.md 9.4：
#   * 归一化名完全相同 -> 重复；
#   * 相似度 >= 0.9 且域名相同（或任一方无官网）-> 重复；
#   * 名称里的地域/赛区标记不同（全国/省/赛区/选拔赛…）-> 不自动合并。

CANDIDATE_STATUSES = ("pending", "approved", "rejected", "duplicate")
NAME_SIMILARITY_THRESHOLD = 0.90

_CANDIDATE_WRITABLE_FIELDS = (
    "source", "source_key", "source_url", "raw_name", "norm_name",
    "official_url", "image_url", "level_label", "category_hint",
    "reg_start_month", "reg_end_month", "reg_cross_year",
    "contest_start_month", "contest_end_month", "raw_json",
    "reg_start_date", "reg_end_date", "contest_start_date", "contest_end_date",
    "status", "competition_id",
)

_MONTH_FIELDS = (
    "reg_start_month", "reg_end_month",
    "contest_start_month", "contest_end_month",
)

# 地域 / 赛区 / 选拔层级标记：这些词不一致时，不因名称相似而合并
# （避免把「挑战杯」与「挑战杯省赛」、「世界技能大赛」与「…中国选拔赛」合并）。
_REGION_TOKENS = (
    "国际", "世界", "全国", "中国", "国家级", "省级", "省赛", "国赛",
    "区域", "赛区", "华东", "华北", "华南", "华中", "西南", "西北", "东北",
    "选拔赛", "分赛区", "总决赛",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def candidates_table_exists(db_path: Optional[str] = None) -> bool:
    """候选表是否已建（dry-run 时用于跳过建表，保持只读）。"""
    with get_conn(db_path) as conn:
        return _table_exists(conn, "competition_candidates")


def ensure_candidates_table(db_path: Optional[str] = None) -> bool:
    """幂等创建候选表；返回 True 表示本次新建。

    只建候选表，不跑 schema_v2.sql / 补列 / 回填，避免为了候选表
    在生产库上触发无关写操作。
    """
    with get_conn(db_path) as conn:
        existed = _table_exists(conn, "competition_candidates")
        if not existed:
            conn.executescript(CANDIDATES_DDL)
        # 老候选表补精确日期列（幂等；纯 ADD COLUMN，不动任何业务行）
        _migrate_simple_columns(conn, "competition_candidates",
                                _CANDIDATE_MIGRATION_COLUMNS)
    return not existed


def _month_or_none(value):
    """1-12 的整数月份；其余（0 / None / '-0001' / 越界）一律 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        month = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return month if 1 <= month <= 12 else None


_DATE_FIELDS = (
    "reg_start_date", "reg_end_date",
    "contest_start_date", "contest_end_date",
)

# 精确日期允许 2 种形态：'YYYY-MM-DD'（带年份）或 'MM-DD'（原文只有月日）。
_RE_DATE_YMD = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RE_DATE_MD = re.compile(r"^(\d{2})-(\d{2})$")
_RE_DATE_TEXT = re.compile(r"(?:(\d{4})\s*[年./-]\s*)?(\d{1,2})\s*[月./-]\s*(\d{1,2})")


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _date_or_none(value) -> Optional[str]:
    """精确日期归一化：接受 '2026-04-15' / '2026/4/15' / '4月15日' / '04-15'。

    返回 'YYYY-MM-DD'（原文带年份）或 'MM-DD'（原文只有月日）；
    解析不出「年-月-日」或日期不合法（如 2 月 30 日、2026-04 只有月）一律 None——
    **宁缺毋滥，绝不替模型补日**。
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "nan", "-"):
        return None

    match = _RE_DATE_YMD.match(text)
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= _days_in_month(year, month):
            return "%04d-%02d-%02d" % (year, month, day)
        return None

    match = _RE_DATE_MD.match(text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12 and 1 <= day <= _days_in_month(2000, month):
            return "%02d-%02d" % (month, day)
        return None

    # 中文/混合写法：2026年4月15日、2026.4.15、4月15日
    match = _RE_DATE_TEXT.search(text)
    if match:
        year_text, month, day = match.group(1), int(match.group(2)), int(match.group(3))
        if not 1 <= month <= 12:
            return None
        if year_text:
            year = int(year_text)
            if 1 <= day <= _days_in_month(year, month):
                return "%04d-%02d-%02d" % (year, month, day)
            return None
        if 1 <= day <= _days_in_month(2000, month):
            return "%02d-%02d" % (month, day)
    return None


def _clean_url(url) -> Optional[str]:
    """只接受 http(s) 绝对地址；其余返回 None。"""
    if not isinstance(url, str):
        return None
    text = url.strip()
    if text.startswith("//"):
        text = "https:" + text
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return text


def _clean_image_url(url) -> Optional[str]:
    """清洗封面图 URL：绝对地址 + image_guard 黑名单（qr/logo/banner...）。

    所有写 image_url 的入口（采集、候选提拔、批量补全）都会经过这里，
    即使调用方绕过爬虫闸门，也不会把二维码/站点 logo 写进正式表。
    """
    cleaned = _clean_url(url)
    if cleaned and image_guard.is_bad_image_url(cleaned):
        logger.warning("封面图 URL 命中黑名单，拒绝入库：%s", cleaned)
        return None
    return cleaned


def normalize_competition_name(name: str) -> str:
    """赛事名归一化（去序号/年份/届次/标点/空白/大小写），用于去重。

    保留「全国/省赛/赛区/选拔赛」等地域信息，交给 _region_signature 判断，
    避免把省级赛和全国主赛错误合并。
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name)).strip()
    # 去掉营销标签：【最后14天】【5届积累】[推荐] 等（不是赛事正式名称的一部分）
    text = re.sub(r"【[^】]*】", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = text.strip()
    text = re.sub(r"^\s*\d{1,3}\s*[、.．)）]\s*", "", text)
    text = re.sub(r"^(?:19|20)\d{2}\s*年?\s*", "", text)
    text = re.sub(r"[（(]\s*(?:19|20)\d{2}\s*年?\s*[）)]", "", text)
    text = re.sub(
        r"[（(][^）)]{0,20}第[一二三四五六七八九十百零\d]+届[^）)]{0,20}[）)]",
        "", text,
    )
    text = re.sub(r"第[一二三四五六七八九十百零\d]+届", "", text)
    text = text.lower()
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
    return text


def _url_host(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _same_site(url_a: Optional[str], url_b: Optional[str]) -> bool:
    """同域或互为子域；任一为空时返回 False（交给调用方决定是否放宽）。"""
    host_a, host_b = _url_host(url_a), _url_host(url_b)
    if not host_a or not host_b:
        return False
    return (
        host_a == host_b
        or host_a.endswith("." + host_b)
        or host_b.endswith("." + host_a)
    )


def _region_signature(norm_name: str) -> tuple:
    return tuple(sorted(token for token in _REGION_TOKENS if token in norm_name))


def _name_similarity(norm_a: str, norm_b: str) -> float:
    if not norm_a or not norm_b:
        return 0.0
    return difflib.SequenceMatcher(None, norm_a, norm_b).ratio()


def find_duplicate_candidate(norm_name: str,
                             official_url: Optional[str] = None,
                             db_path: Optional[str] = None,
                             threshold: float = NAME_SIMILARITY_THRESHOLD,
                             include_candidates: bool = True,
                             exclude_candidate_id: Optional[int] = None
                             ) -> Optional[dict]:
    """在候选表 + 主表里找同名/高度相似的赛事。

    include_candidates=False 时只查主表（审核入库前用，避免命中候选自身）。
    返回 None 表示可插入；否则返回:
        {where: 'competitions'|'candidates', id, name, similarity, reason, source}
    """
    if not norm_name:
        return None

    with get_conn(db_path) as conn:
        comp_rows = []
        try:
            comp_rows = conn.execute(
                "SELECT id, name, official_url FROM competitions"
            ).fetchall()
        except sqlite3.OperationalError:
            pass

        cand_rows = []
        if include_candidates and _table_exists(conn, "competition_candidates"):
            cand_rows = conn.execute(
                "SELECT id, source, raw_name, norm_name, official_url "
                "FROM competition_candidates WHERE status <> 'rejected'"
            ).fetchall()

    pool = []
    for row in comp_rows:
        pool.append({
            "where": "competitions",
            "id": row["id"],
            "name": row["name"],
            "norm": normalize_competition_name(row["name"]),
            "url": row["official_url"],
            "source": "competitions",
        })
    for row in cand_rows:
        if (exclude_candidate_id is not None
                and row["id"] == exclude_candidate_id):
            continue
        pool.append({
            "where": "candidates",
            "id": row["id"],
            "name": row["raw_name"],
            "norm": row["norm_name"] or normalize_competition_name(row["raw_name"]),
            "url": row["official_url"],
            "source": row["source"],
        })

    # 1) 归一化名完全相同：直接判重（主表优先）
    for item in pool:
        if item["norm"] and item["norm"] == norm_name:
            return {
                "where": item["where"], "id": item["id"], "name": item["name"],
                "similarity": 1.0, "reason": "归一化名相同",
                "source": item["source"],
            }

    # 2) 相似度高 + 地域标记一致 + 官网同域（或任一方无官网）才判重
    signature = _region_signature(norm_name)
    best = None
    for item in pool:
        item_norm = item["norm"]
        if not item_norm:
            continue
        if _region_signature(item_norm) != signature:
            continue
        score = _name_similarity(norm_name, item_norm)
        if score < threshold:
            continue
        if official_url and item["url"] and not _same_site(official_url, item["url"]):
            continue
        if best is None or score > best["similarity"]:
            best = {
                "where": item["where"], "id": item["id"], "name": item["name"],
                "similarity": round(score, 4), "reason": "名称高度相似",
                "source": item["source"],
            }
    return best


def add_candidate(data: dict, db_path: Optional[str] = None,
                  check_duplicate: bool = True) -> dict:
    """插入一条候选赛事（默认先去重）。

    返回:
        {"status": "inserted", "id": 1, "norm_name": "..."}
        {"status": "duplicate", "reason": "...", "existing": {...}}
    """
    raw_name = str(data.get("raw_name") or data.get("name") or "").strip()
    if not raw_name:
        raise ValueError("raw_name 不能为空")

    source = str(data.get("source") or "").strip()
    source_key = str(data.get("source_key") or "").strip()
    if not source or not source_key:
        raise ValueError("source / source_key 必填")

    norm_name = str(data.get("norm_name") or "").strip()
    if not norm_name:
        norm_name = normalize_competition_name(raw_name)

    official_url = _clean_url(data.get("official_url"))
    if check_duplicate:
        duplicate = find_duplicate_candidate(
            norm_name, official_url=official_url, db_path=db_path
        )
        if duplicate:
            return {
                "status": "duplicate",
                "reason": duplicate["reason"],
                "existing": duplicate,
                "norm_name": norm_name,
            }

    row = {
        "source": source,
        "source_key": source_key,
        "source_url": _clean_url(data.get("source_url")),
        "raw_name": raw_name,
        "norm_name": norm_name,
        "official_url": official_url,
        "image_url": _clean_image_url(data.get("image_url")),
        "level_label": data.get("level_label") or None,
        "category_hint": data.get("category_hint") or None,
        "reg_cross_year": 1 if str(data.get("reg_cross_year") or 0).strip() in
                               ("1", "true", "True") else 0,
        "raw_json": data.get("raw_json"),
        "status": str(data.get("status") or "pending").strip(),
        "competition_id": data.get("competition_id"),
    }
    if row["status"] not in CANDIDATE_STATUSES:
        raise ValueError("非法候选状态：%s" % row["status"])
    for field in _MONTH_FIELDS:
        row[field] = _month_or_none(data.get(field))
    for field in _DATE_FIELDS:
        row[field] = _date_or_none(data.get(field))

    if row["raw_json"] is not None and not isinstance(row["raw_json"], str):
        row["raw_json"] = json.dumps(row["raw_json"], ensure_ascii=False)

    cols = [field for field in _CANDIDATE_WRITABLE_FIELDS if field in row]
    placeholders = ", ".join("?" for _ in cols)
    sql = "INSERT INTO competition_candidates (%s) VALUES (%s)" % (
        ", ".join(cols), placeholders
    )

    try:
        with get_conn(db_path) as conn:
            if not _table_exists(conn, "competition_candidates"):
                conn.executescript(CANDIDATES_DDL)
            cur = conn.execute(sql, [row[col] for col in cols])
            new_id = int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return {
            "status": "duplicate",
            "reason": "source_key 已存在",
            "existing": {"where": "candidates", "source": source},
            "norm_name": norm_name,
        }

    return {"status": "inserted", "id": new_id, "norm_name": norm_name}


_CANDIDATE_SELECT_COLUMNS = (
    "id, source, source_key, source_url, raw_name, norm_name, official_url, "
    "image_url, level_label, category_hint, reg_start_month, reg_end_month, "
    "reg_cross_year, contest_start_month, contest_end_month, "
    "reg_start_date, reg_end_date, contest_start_date, contest_end_date, "
    "status, competition_id, created_at, reviewed_at"
)


def query_candidates(status: Optional[str] = None, source: Optional[str] = None,
                     limit: int = 100, offset: int = 0,
                     db_path: Optional[str] = None) -> list:
    """按状态/来源查询候选（不含 raw_json，避免刷屏）。"""
    if not candidates_table_exists(db_path):
        return []
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if source:
        where.append("source = ?")
        params.append(source)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    sql = ("SELECT %s FROM competition_candidates%s "
           "ORDER BY id DESC LIMIT ? OFFSET ?") % (_CANDIDATE_SELECT_COLUMNS, clause)
    with get_conn(db_path) as conn:
        rows = conn.execute(sql, params + [limit, offset]).fetchall()
    return [dict(row) for row in rows]


def get_candidate(candidate_id: int,
                  db_path: Optional[str] = None) -> Optional[dict]:
    """读取单条候选（含 raw_json），审核/排查用。"""
    if not candidates_table_exists(db_path):
        return None
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM competition_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
    return dict(row) if row else None


def count_candidates(status: Optional[str] = None,
                     db_path: Optional[str] = None) -> int:
    if not candidates_table_exists(db_path):
        return 0
    sql = "SELECT COUNT(*) FROM competition_candidates"
    params = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    with get_conn(db_path) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def candidate_stats(db_path: Optional[str] = None) -> dict:
    """候选表总览：总数 / 待审 / 按状态 / 按来源。"""
    stats = {
        "total": 0, "pending": 0,
        "by_status": {}, "by_source": {}, "pending_by_source": {},
    }
    if not candidates_table_exists(db_path):
        return stats
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT source, status, COUNT(*) AS n "
            "FROM competition_candidates GROUP BY source, status"
        ).fetchall()
    for row in rows:
        count = int(row["n"])
        status, source = row["status"], row["source"]
        stats["total"] += count
        stats["by_status"][status] = stats["by_status"].get(status, 0) + count
        stats["by_source"][source] = stats["by_source"].get(source, 0) + count
        if status == "pending":
            stats["pending"] += count
            stats["pending_by_source"][source] = (
                stats["pending_by_source"].get(source, 0) + count
            )
    return stats


def update_candidate_status(candidate_id: int, status: str,
                            competition_id: Optional[int] = None,
                            db_path: Optional[str] = None) -> bool:
    """更新审核状态（approve/reject 入口用）；返回是否命中记录。"""
    if status not in CANDIDATE_STATUSES:
        raise ValueError("非法候选状态：%s" % status)
    if not candidates_table_exists(db_path):
        return False
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE competition_candidates "
            "SET status = ?, competition_id = COALESCE(?, competition_id), "
            "reviewed_at = ? WHERE id = ?",
            (status, competition_id, now_str(), candidate_id),
        )
        return cur.rowcount > 0


def promote_candidate(candidate_id: int, data: dict,
                      db_path: Optional[str] = None) -> int:
    """审核通过：把候选正式插入 competitions，并在同一事务里标记 approved。

    参数 data 由审核 CLI 组装（名称/类别/级别/月份/官网/封面等）；
    候选状态不是 pending 时拒绝（防止重复提拔）。返回新 competitions.id。
    """
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("候选名称为空，不能入主表")
    level = data.get("level") or "national"
    if level not in ("national", "provincial"):
        level = "national"

    params = (
        name,
        data.get("category") or "待确认",
        _month_or_none(data.get("reg_start_month")),
        _month_or_none(data.get("reg_end_month")),
        1 if data.get("reg_cross_year") else 0,
        _clean_url(data.get("official_url")),
        _clean_image_url(data.get("image_url")),
        _month_or_none(data.get("contest_start_month")),
        _month_or_none(data.get("contest_end_month")),
        _date_or_none(data.get("reg_start_date")),
        _date_or_none(data.get("reg_end_date")),
        _date_or_none(data.get("contest_start_date")),
        _date_or_none(data.get("contest_end_date")),
        data.get("discover_source"),
        data.get("last_checked_at") or now_str(),
        1 if data.get("is_active", 1) else 0,
        level,
        data.get("level_label"),
        data.get("sort_order", 9999),
        data.get("tech_stack") or "[]",
    )

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id, status FROM competition_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError("候选不存在：%s" % candidate_id)
        if row["status"] != "pending":
            raise ValueError("候选 %s 状态为 %s，不能重复提拔" % (
                candidate_id, row["status"]))

        cur = conn.execute(
            """INSERT INTO competitions
               (name, category, reg_start_month, reg_end_month, reg_cross_year,
                official_url, image_url, contest_start_month, contest_end_month,
                reg_start_date, reg_end_date, contest_start_date, contest_end_date,
                discover_source, last_checked_at, is_active, level, level_label,
                sort_order, tech_stack)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            params,
        )
        competition_id = int(cur.lastrowid)

        updated = conn.execute(
            "UPDATE competition_candidates "
            "SET status = 'approved', competition_id = ?, reviewed_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (competition_id, now_str(), candidate_id),
        )
        if updated.rowcount != 1:
            # 同一事务内回滚，避免「主表插了、候选没标记」的脏状态
            raise RuntimeError("候选状态更新失败，已回滚")
        return competition_id

# ============================================================
# 十三、ai_jobs：AI 生成任务（攻略 / 灵感）
# ============================================================
# 设计见 AI_AUTOMATION_PLAN.md 第 2.2 节：
#   * 唯一部分索引 ux_ai_jobs_active 负责「同赛事同类任务同时只有 1 个活跃」；
#   * claim 采用 INSERT OR IGNORE + rowcount 判定，天然并发安全；
#   * 每次 claim 顺手回收 15 分钟未完成的 running（进程被杀/重启的僵尸）；
#   * tokens 落库用于成本对账，error 截断 500 字且不回传前端。

_REAP_STALE_AI_JOBS_SQL = """
UPDATE ai_jobs
   SET status = 'failed',
       finished_at = datetime('now', 'localtime'),
       error = 'stale: 任务超过 15 分钟未完成（进程退出/重启）'
 WHERE status = 'running'
   AND (
        started_at IS NULL
        OR datetime(started_at) < datetime('now', 'localtime', '-15 minutes')
   )
"""


def ai_jobs_table_exists(db_path: Optional[str] = None) -> bool:
    """候选老库可能还没建 ai_jobs；所有读取方都要先能容忍它不存在。"""
    with get_conn(db_path) as conn:
        return _table_exists(conn, "ai_jobs")


def ensure_ai_jobs_table(db_path: Optional[str] = None) -> bool:
    """幂等建 ai_jobs；返回 True 表示本次新建。"""
    with get_conn(db_path) as conn:
        existed = _table_exists(conn, "ai_jobs")
        conn.executescript(AI_JOBS_DDL)
    return not existed


def _reap_stale_ai_jobs(conn: sqlite3.Connection) -> int:
    cur = conn.execute(_REAP_STALE_AI_JOBS_SQL)
    return int(cur.rowcount or 0)


def claim_ai_job(kind: str, competition_id: int, *,
                 trigger: str = "manual",
                 requested_by: Optional[int] = None,
                 request_ip: Optional[str] = None,
                 cooldown_minutes: int = 0,
                 allow_existing: bool = False,
                 db_path: Optional[str] = None) -> dict:
    """原子抢占一个 AI 任务。

    返回::

        {"claimed": True,  "job_id": 12, "reason": None}
        {"claimed": False, "job_id": None, "reason": "active"}     # 已有活跃任务
        {"claimed": False, "job_id": None, "reason": "exists"}     # 攻略已存在
        {"claimed": False, "job_id": None, "reason": "cooldown",   # 失败冷却中
         "retry_after_sec": 900}

    `cooldown_minutes` 只对「最近一次任务是 failed」生效；每天定时任务用 3 天，
    按需接口用短冷却（默认 30 分钟），避免用户狂点。

    为什么用 ``BEGIN IMMEDIATE``：
        「先读攻略是否为空，再插入任务」如果是普通延迟事务，两条语句之间
        另一个线程可能刚好写完攻略并提交，于是会多插入一个注定被跳过的任务
        （实测竞态）。这里先拿写锁，再读再插，保证判断与插入是原子的。
    """
    if kind not in AI_JOB_KINDS:
        raise ValueError("kind 必须是 %s 之一，收到: %r" % (AI_JOB_KINDS, kind))
    if trigger not in ("on_demand", "daily", "manual"):
        raise ValueError("trigger 非法: %r" % (trigger,))
    cid = _check_competition_id(competition_id)

    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _reap_stale_ai_jobs(conn)

        if kind == "guide" and not allow_existing:
            row = conn.execute(
                "SELECT TRIM(COALESCE(full_process_guide, '')) AS guide "
                "FROM competitions WHERE id = ?",
                (cid,),
            ).fetchone()
            if row is None:
                raise ValueError("竞赛 ID=%s 不存在" % cid)
            if row["guide"]:
                return {"claimed": False, "job_id": None, "reason": "exists"}

        if cooldown_minutes and cooldown_minutes > 0:
            last = conn.execute(
                "SELECT id, status, finished_at FROM ai_jobs "
                "WHERE kind = ? AND competition_id = ? ORDER BY id DESC LIMIT 1",
                (kind, cid),
            ).fetchone()
            if (last is not None and last["status"] == "failed"
                    and last["finished_at"]):
                try:
                    finished = datetime.strptime(
                        last["finished_at"], "%Y-%m-%d %H:%M:%S"
                    )
                except (TypeError, ValueError):
                    finished = None
                if finished is not None:
                    elapsed = (datetime.now() - finished).total_seconds()
                    cooldown = cooldown_minutes * 60
                    if elapsed < cooldown:
                        return {
                            "claimed": False,
                            "job_id": None,
                            "reason": "cooldown",
                            "retry_after_sec": int(cooldown - elapsed),
                        }

        cur = conn.execute(
            "INSERT OR IGNORE INTO ai_jobs "
            "(kind, competition_id, trigger, status, requested_by, request_ip) "
            "VALUES (?, ?, ?, 'queued', ?, ?)",
            (kind, cid, trigger, requested_by, request_ip),
        )
        if cur.rowcount == 1:
            return {"claimed": True, "job_id": int(cur.lastrowid), "reason": None}

        active = conn.execute(
            "SELECT id FROM ai_jobs WHERE kind = ? AND competition_id = ? "
            "AND status IN ('queued', 'running') ORDER BY id ASC LIMIT 1",
            (kind, cid),
        ).fetchone()

    return {
        "claimed": False,
        "job_id": int(active["id"]) if active else None,
        "reason": "active",
    }


def start_ai_job(job_id: int, db_path: Optional[str] = None) -> bool:
    """queued -> running（幂等：只有 queued 能转，返回是否抢到）。"""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE ai_jobs SET status = 'running', "
            "started_at = datetime('now', 'localtime'), attempts = attempts + 1 "
            "WHERE id = ? AND status = 'queued'",
            (int(job_id),),
        )
        return cur.rowcount == 1


def finish_ai_job(job_id: int, tokens: Optional[int] = None,
                  db_path: Optional[str] = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE ai_jobs SET status = 'done', "
            "finished_at = datetime('now', 'localtime'), "
            "tokens = COALESCE(?, tokens) WHERE id = ?",
            (tokens, int(job_id)),
        )


def fail_ai_job(job_id: int, error: str, tokens: Optional[int] = None,
                db_path: Optional[str] = None) -> None:
    message = (error or "未知错误").strip()
    if len(message) > 500:
        message = message[:500] + "…"
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE ai_jobs SET status = 'failed', error = ?, "
            "finished_at = datetime('now', 'localtime'), "
            "tokens = COALESCE(?, tokens) WHERE id = ?",
            (message, tokens, int(job_id)),
        )


def skip_ai_job(job_id: int, reason: str = "",
                db_path: Optional[str] = None) -> None:
    """任务被判定为无需执行（如攻略已存在）：记 skipped，不消耗预算重试。"""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE ai_jobs SET status = 'skipped', error = ?, "
            "finished_at = datetime('now', 'localtime') WHERE id = ?",
            ((reason or "")[:500] or None, int(job_id)),
        )


def get_latest_ai_job(kind: str, competition_id: int,
                      db_path: Optional[str] = None) -> Optional[dict]:
    with get_conn(db_path) as conn:
        if not _table_exists(conn, "ai_jobs"):
            return None
        row = conn.execute(
            "SELECT * FROM ai_jobs WHERE kind = ? AND competition_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (kind, int(competition_id)),
        ).fetchone()
    return dict(row) if row else None


def count_ai_jobs_today(kind: Optional[str] = None,
                        db_path: Optional[str] = None) -> int:
    """当天已创建的任务数（含失败/跳过），用作全站每日预算。"""
    with get_conn(db_path) as conn:
        if not _table_exists(conn, "ai_jobs"):
            return 0
        sql = ("SELECT COUNT(*) AS n FROM ai_jobs "
               "WHERE created_at >= date('now', 'localtime')")
        params: tuple = ()
        if kind:
            sql += " AND kind = ?"
            params = (kind,)
        row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def count_active_ai_jobs(kind: Optional[str] = None,
                         db_path: Optional[str] = None) -> int:
    with get_conn(db_path) as conn:
        if not _table_exists(conn, "ai_jobs"):
            return 0
        sql = ("SELECT COUNT(*) AS n FROM ai_jobs "
               "WHERE status IN ('queued', 'running')")
        params: tuple = ()
        if kind:
            sql += " AND kind = ?"
            params = (kind,)
        row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def select_guide_candidates(limit: int = 4, *, min_awards: int = 0,
                            cooldown_days: int = 3,
                            include_provincial: bool = False,
                            db_path: Optional[str] = None) -> list:
    """按「热门」口径挑选待生成攻略的赛事（daily_ai_worker 与 dry-run 共用）。

    口径（2026-11-08 定，见方案 2.4）：
      1. 上架、攻略为空；
      2. 国家级 + 有官网（可核验、可导流）；`include_provincial` 时放宽级别；
      3. 排除活跃任务与 cooldown_days 内失败过的赛事（避免天天撞同一条）；
      4. 排序：获奖作品数 DESC -> sort_order（NULL 最后）-> id ASC。

    返回行含 `awards_count`，供灵感生成判断是否够料。
    """
    limit = max(1, int(limit))
    cooldown_days = max(0, int(cooldown_days))
    sql = [
        "SELECT c.id, c.name, c.category, c.level, c.official_url, c.sort_order, "
        "(SELECT COUNT(*) FROM awards a WHERE a.competition_id = c.id) AS awards_count "
        "FROM competitions c "
        "WHERE c.is_active = 1 "
        "AND TRIM(COALESCE(c.full_process_guide, '')) = ''"
    ]
    params: list = []
    if not include_provincial:
        sql.append("AND c.level = 'national'")
    sql.append("AND TRIM(COALESCE(c.official_url, '')) <> ''")

    with get_conn(db_path) as conn:
        has_jobs = _table_exists(conn, "ai_jobs")
        if has_jobs:
            sql.append(
                "AND NOT EXISTS (SELECT 1 FROM ai_jobs j "
                "WHERE j.kind = 'guide' AND j.competition_id = c.id "
                "AND (j.status IN ('queued', 'running') "
                "OR (j.status = 'failed' "
                "AND datetime(j.finished_at) > datetime('now', 'localtime', ?))))"
            )
            params.append("-%d days" % cooldown_days)
        sql.append(
            "ORDER BY awards_count DESC, "
            "CASE WHEN c.sort_order IS NULL THEN 1 ELSE 0 END, "
            "c.sort_order ASC, c.id ASC LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(" ".join(sql), params).fetchall()

    out = []
    for r in rows:
        item = dict(r)
        if int(item.get("awards_count") or 0) < int(min_awards):
            continue
        out.append(item)
    return out


def get_guide_state(competition_id: int) -> Optional[dict]:
    """返回某赛事攻略的对外状态（详情接口 / 前端按钮共用）。

    status: idle | queued | running | ready | failed
    """
    cid = int(competition_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, full_process_guide, guide_generated_at "
            "FROM competitions WHERE id = ?",
            (cid,),
        ).fetchone()
        if row is None:
            return None
        job = None
        if _table_exists(conn, "ai_jobs"):
            job = conn.execute(
                "SELECT * FROM ai_jobs WHERE kind = 'guide' AND competition_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (cid,),
            ).fetchone()

    guide = (row["full_process_guide"] or "").strip()
    if job is not None and job["status"] in AI_JOB_ACTIVE_STATUSES:
        status = job["status"]
    elif guide:
        status = "ready"
    elif job is not None and job["status"] == "failed":
        status = "failed"
    else:
        status = "idle"

    return {
        "status": status,
        "job_id": int(job["id"]) if job is not None else None,
        "attempts": int(job["attempts"]) if job is not None else 0,
        "finished_at": job["finished_at"] if job is not None else None,
        "generated_at": row["guide_generated_at"],
        "guide_length": len(guide),
    }


def get_inspiration_state(competition_id: int) -> Optional[dict]:
    """返回某赛事灵感生成（insights + ideas）的对外状态。

    status: idle | queued | running | ready | failed

    额外返回 awards_count：灵感是从获奖作品里提炼的，没有获奖作品就没有
    可提炼的素材，前端据此把「生成专属灵感」按钮置灰（2026-11-13 新增）。

    额外返回 has_project_awards：False=纯算法/知识类赛事（蓝桥杯等），
    获奖数据只有名单、没有项目作品，前端据此隐藏灵感入口（2026-11-14 新增）。
    """
    cid = int(competition_id)
    with get_conn() as conn:
        comp = conn.execute(
            "SELECT id, has_project_awards FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
        if comp is None:
            return None
        awards = conn.execute(
            "SELECT COUNT(*) AS n FROM awards WHERE competition_id = ?", (cid,)
        ).fetchone()
        ins = conn.execute(
            "SELECT COUNT(*) AS n, MAX(created_at) AS t FROM insights "
            "WHERE competition_id = ?", (cid,)
        ).fetchone()
        ideas = conn.execute(
            "SELECT COUNT(*) AS n FROM ideas WHERE competition_id = ?", (cid,)
        ).fetchone()
        job = None
        if _table_exists(conn, "ai_jobs"):
            job = conn.execute(
                "SELECT * FROM ai_jobs WHERE kind = 'inspiration' "
                "AND competition_id = ? ORDER BY id DESC LIMIT 1",
                (cid,),
            ).fetchone()

    insights_count = int(ins["n"] or 0) if ins is not None else 0
    ideas_count = int(ideas["n"] or 0) if ideas is not None else 0
    if job is not None and job["status"] in AI_JOB_ACTIVE_STATUSES:
        status = job["status"]
    elif insights_count or ideas_count:
        status = "ready"
    elif job is not None and job["status"] == "failed":
        status = "failed"
    else:
        status = "idle"

    return {
        "status": status,
        "job_id": int(job["id"]) if job is not None else None,
        "attempts": int(job["attempts"]) if job is not None else 0,
        "awards_count": int(awards["n"] or 0) if awards is not None else 0,
        "has_project_awards": bool(comp["has_project_awards"]
                                   if "has_project_awards" in comp.keys() else 1),
        "insights_count": insights_count,
        "ideas_count": ideas_count,
        "error": (
            job["error"]
            if job is not None and job["status"] == "failed"
            else None
        ),
        "generated_at": (ins["t"] if ins is not None else None),
    }


def count_orphan_ai_content(db_path: Optional[str] = None) -> dict:
    """统计 competition_id 为空或指向不存在赛事的 insights / ideas。

    这些是早期脚本在赛事先行删除/未归并时留下的孤儿数据，按赛事隔离的接口
    永远读不到，只占空间。清理见 cleanup_orphan_ai.py。
    """
    with get_conn(db_path) as conn:
        out = {}
        for table in ("insights", "ideas"):
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM %s WHERE competition_id IS NULL "
                "OR competition_id NOT IN (SELECT id FROM competitions)" % table
            ).fetchone()
            out[table] = int(row["n"]) if row else 0
    return out


def delete_orphan_ai_content(db_path: Optional[str] = None) -> dict:
    """删除孤儿 insights / ideas，返回各表删除行数（调用方负责备份）。"""
    with get_conn(db_path) as conn:
        out = {}
        for table in ("insights", "ideas"):
            cur = conn.execute(
                "DELETE FROM %s WHERE competition_id IS NULL "
                "OR competition_id NOT IN (SELECT id FROM competitions)" % table
            )
            out[table] = int(cur.rowcount or 0)
    return out


def guide_coverage(db_path: Optional[str] = None) -> dict:
    """攻略覆盖率（报告 / 体检用）。"""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN TRIM(COALESCE(full_process_guide, '')) <> '' "
            "THEN 1 ELSE 0 END) AS filled FROM competitions"
        ).fetchone()
    total = int(row["total"] or 0)
    filled = int(row["filled"] or 0)
    return {"total": total, "filled": filled, "missing": total - filled}

def select_inspiration_candidates(limit: int = 2, *, min_awards: int = 3,
                                  cooldown_days: int = 3,
                                  include_provincial: bool = False,
                                  db_path: Optional[str] = None) -> list:
    """挑选待生成灵感的赛事（有足够获奖作品且 insights/ideas 未齐）。

    与 select_guide_candidates 同一套「热门」排序，但额外要求：
      * 该赛事获奖作品数 >= min_awards（否则模型只能空想，analyzer 也会直接退出）；
      * insights 不足 3 条或 ideas 不足 4 条（未生成完整才值得再花 5 次调用）；
      * 排除 ai_jobs 中活跃 / 冷却期内的 inspiration 任务。

    返回行含 awards_count / insights_count / ideas_count。
    """
    limit = max(1, int(limit))
    cooldown_days = max(0, int(cooldown_days))
    sql = [
        "SELECT c.id, c.name, c.category, c.level, c.official_url, c.sort_order, "
        "(SELECT COUNT(*) FROM awards a WHERE a.competition_id = c.id) AS awards_count, "
        "(SELECT COUNT(*) FROM insights s WHERE s.competition_id = c.id) AS insights_count, "
        "(SELECT COUNT(*) FROM ideas i WHERE i.competition_id = c.id) AS ideas_count "
        "FROM competitions c "
        "WHERE c.is_active = 1"
    ]
    params: list = []
    if not include_provincial:
        sql.append("AND c.level = 'national'")
    sql.append("AND TRIM(COALESCE(c.official_url, '')) <> ''")

    with get_conn(db_path) as conn:
        has_jobs = _table_exists(conn, "ai_jobs")
        if has_jobs:
            sql.append(
                "AND NOT EXISTS (SELECT 1 FROM ai_jobs j "
                "WHERE j.kind = 'inspiration' AND j.competition_id = c.id "
                "AND (j.status IN ('queued', 'running') "
                "OR (j.status = 'failed' "
                "AND datetime(j.finished_at) > datetime('now', 'localtime', ?))))"
            )
            params.append("-%d days" % cooldown_days)
        sql.append(
            "ORDER BY awards_count DESC, "
            "CASE WHEN c.sort_order IS NULL THEN 1 ELSE 0 END, "
            "c.sort_order ASC, c.id ASC"
        )
        rows = conn.execute(" ".join(sql), params).fetchall()

    out = []
    for r in rows:
        item = dict(r)
        if int(item.get("awards_count") or 0) < int(min_awards):
            continue
        if int(item.get("insights_count") or 0) >= 3 and int(item.get("ideas_count") or 0) >= 4:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out
