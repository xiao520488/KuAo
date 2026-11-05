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
"""

from typing import Optional
from contextlib import contextmanager
import os
import re
import sqlite3
from datetime import datetime

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
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
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
"""

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


def init_db() -> None:
    """
    初始化数据库：执行 schema_v2.sql 建表（IF NOT EXISTS，可重复调用），
    再做补列迁移、回填与索引创建。不会插入任何业务数据。
    """
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema_sql = f.read()
    with get_conn() as conn:
        conn.executescript(schema_sql)
        _migrate_columns(conn)
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
                official_url, level, level_label, sort_order, full_process_guide)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        return cur.lastrowid


def update_competition(cid: int, data: dict) -> None:
    """
    按 id 更新比赛信息。仅更新 data 中出现的字段。
    """
    allowed = ("name", "category", "reg_start_month", "reg_end_month",
               "reg_cross_year", "official_url", "level", "level_label",
               "sort_order", "intro", "summary", "tech_stack",
               "full_process_guide")
    fields, values = [], []
    for key in allowed:
        if key in data:
            val = data[key]
            if key == "reg_cross_year":
                val = 1 if val else 0
            if key == "level" and val not in ("national", "provincial"):
                val = "national"
            fields.append(f"{key} = ?")
            values.append(val)
    if not fields:
        return
    values.append(cid)
    with get_conn() as conn:
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


def delete_competition(cid: int) -> None:
    """按 id 删除比赛。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM competitions WHERE id = ?", (cid,))


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
                      keyword: Optional[str] = None) -> list:
    """
    条件查询比赛列表。
    - category: 模糊匹配类别
    - month: 报名月覆盖该月份（考虑跨年区间）
    - keyword: 名称模糊匹配
    """
    sql = "SELECT * FROM competitions WHERE 1=1"
    params = []
    if category:
        sql += " AND category LIKE ?"
        params.append(f"%{category}%")
    if keyword:
        sql += " AND name LIKE ?"
        params.append(f"%{keyword}%")
    sql += _ORDER_BY_CLAUSE
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    result = [_enrich(r) for r in rows]
    if month:
        result = [r for r in result
                  if _month_in_range(month, r.get("reg_start_month"),
                                     r.get("reg_end_month"),
                                     r.get("reg_cross_year"))]
    return result


def get_categories() -> list:
    """返回去重后的类别列表（非空）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM competitions "
            "WHERE category IS NOT NULL AND TRIM(category) <> '' "
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
    """按竞赛 id 查询获奖作品列表（按年份倒序、id 升序）。"""
    sql = ("SELECT * FROM awards WHERE competition_id = ? "
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
           "intro, summary, full_process_guide FROM competitions")
    if only_missing:
        sql += (" WHERE full_process_guide IS NULL "
                "OR TRIM(full_process_guide) = ''")
    sql += " ORDER BY id ASC"
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [_enrich(r) for r in rows]


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
