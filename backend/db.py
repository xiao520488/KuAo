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
    再做补列迁移（competitions + awards）、回填与索引创建。
    不会插入任何业务数据。
    """
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema_sql = f.read()
    with get_conn() as conn:
        conn.executescript(schema_sql)
        _migrate_columns(conn)
        _migrate_award_columns(conn)
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


def list_preparations(user_id: int) -> list:
    """列出用户备赛清单（JOIN 竞赛信息），最新加入的在前。

    返回 list[dict]，键：prep_id / competition_id / added_at /
    name / category / level / level_label / reg_start_month / reg_end_month /
    reg_cross_year / official_url / summary。
    """
    with get_conn() as conn:
        try:
            rows = conn.execute(
                """
                SELECT p.id              AS prep_id,
                       p.competition_id  AS competition_id,
                       p.created_at      AS added_at,
                       c.name            AS name,
                       c.category        AS category,
                       c.level           AS level,
                       c.level_label     AS level_label,
                       c.reg_start_month AS reg_start_month,
                       c.reg_end_month   AS reg_end_month,
                       c.reg_cross_year  AS reg_cross_year,
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

_AWARD_WRITABLE_FIELDS = (
    "competition_id", "year", "category", "title", "team", "advisor",
    "award_level", "summary", "tech_stack", "highlights", "detail_md",
    "source_url", "source_type", "raw_html_path", "confidence",
    "review_status", "embedding_state", "content_hash",
)

# 这些字段变化会影响向量语义，需要把 embedding_state 标脏
_AWARD_EMBEDDING_FIELDS = (
    "title", "summary", "highlights", "detail_md", "tech_stack",
    "category", "award_level", "year", "team", "advisor",
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
