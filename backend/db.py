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
                official_url, level, level_label, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        return cur.lastrowid


def update_competition(cid: int, data: dict) -> None:
    """
    按 id 更新比赛信息。仅更新 data 中出现的字段。
    """
    allowed = ("name", "category", "reg_start_month", "reg_end_month",
               "reg_cross_year", "official_url", "level", "level_label",
               "sort_order", "intro", "summary", "tech_stack")
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
        # 依据原始标题批量取候选（IN 查询），再在内存里按归一化标题归组
        placeholders = ",".join("?" * len(raw_titles))
        sql = (
            "SELECT id, title, source_url FROM awards "
            "WHERE title IN (%s) AND source_url IS NOT NULL AND TRIM(source_url) <> '' "
            "ORDER BY id ASC"
        ) % placeholders
        cur = conn.execute(sql, raw_titles)
        for row in cur.fetchall():
            d = dict(row)
            title = str(d.get("title") or "")
            url = (d.get("source_url") or "").strip()
            if not title or not url:
                continue
            key = re.sub(r"[\s\W_]+", "", title).lower()
            # 归一化键优先；原始标题键作为补充（仅当尚未记录时）
            if key and key not in result:
                result[key] = url
            if title not in result:
                result[title] = url
    except Exception:
        return {}
    return result


def list_awards_by_competition(competition_id: int, limit: int = 5) -> list:
    """按竞赛 id 取该赛事下的获奖作品，供 AI 聚合分析使用。

    - 仅取 review_status != 'rejected' 的作品（被拒绝的脏数据不参与分析）
    - 按 confidence 降序、id 降序，优先取更可信、更新的条目
    - tech_stack 为 JSON 字符串，这里解析为 list；解析失败降级为空列表
    - 返回字段：id / year / session / title / team / track /
      award_grade / level / summary / tech_stack / highlights
    """
    import json

    # 注意：这里不能直接 LIMIT，因为同一选题可能有多条记录
    # （不同年份 / 省赛与国赛各一条），若先 LIMIT 再去重会导致返回条数不足。
    # 因此先多取一些候选（limit * 5，上限 50），去重后再截断到 limit。
    scan_limit = max(int(limit) * 5, int(limit))
    scan_limit = min(scan_limit, 50)

    sql = (
        "SELECT id, year, session, title, team, track, "
        "award_grade, level, summary, tech_stack, highlights, source_url "
        "FROM awards WHERE competition_id = ? "
        "AND COALESCE(review_status, 'pending') <> 'rejected' "
        "ORDER BY COALESCE(confidence, 1.0) DESC, id DESC LIMIT ?"
    )
    with get_conn() as conn:
        _ensure_source_url_column(conn)
        rows = conn.execute(sql, (competition_id, scan_limit)).fetchall()

        # ---- 批量回填 source_url（跨赛事同标题兜底）----
        # 同一选题可能被收录进多场竞赛，链接往往只写在其中一条上。
        # 这里一次查询取出「同标题 -> 非空 URL」映射，避免逐条 N+1 扫描。
        title_keys = set()
        for r in rows:
            d = dict(r)
            k = re.sub(r"[\s\W_]+", "", str(d.get("title") or "")).lower()
            if k:
                title_keys.add(k)
                title_keys.add(str(d.get("title") or ""))

        filled = _fetch_source_url_map(conn, title_keys)
        if filled:
            new_rows = []
            for r in rows:
                d = dict(r)
                if not (d.get("source_url") or "").strip():
                    k = re.sub(r"[\s\W_]+", "", str(d.get("title") or "")).lower()
                    url = filled.get(k) or filled.get(str(d.get("title") or ""))
                    if url:
                        d["source_url"] = url
                new_rows.append(d)
            rows = new_rows

    # ---- 按标题去重：同标题多条时优先保留带 source_url 的那条 ----
    # rows 已按 COALESCE(confidence,1.0) DESC, id DESC 排序，
    # 因此同标题下"第一个有链接的记录"即最优候选。
    # 若整组都没有链接，则退化为保留排序最靠前的那条（原行为）。
    grouped = {}          # title_key -> dict
    order = []            # 保持首次出现顺序，保证结果按 confidence/id 排列
    for r in rows:
        d = dict(r)
        title_key = re.sub(r"[\s\W_]+", "", str(d.get("title") or "")).lower()

        if not title_key:                 # 标题为空：不参与去重，直接单独成组
            order.append(d)
            continue

        if title_key not in grouped:
            grouped[title_key] = d
            order.append(title_key)
            continue

        # 已有同标题记录：仅当"保留的是无链接、当前这条有链接"时才替换
        kept = grouped[title_key]
        kept_url = (kept.get("source_url") or "").strip()
        cur_url = (d.get("source_url") or "").strip()
        if not kept_url and cur_url:
            grouped[title_key] = d

    result = []
    for item in order:
        if len(result) >= int(limit):
            break
        d = grouped[item] if isinstance(item, str) else item

        raw_tech = d.get("tech_stack")
        if isinstance(raw_tech, str) and raw_tech.strip():
            try:
                parsed = json.loads(raw_tech)
            except (ValueError, TypeError):
                parsed = [t.strip() for t in raw_tech.split(",") if t.strip()]
            d["tech_stack"] = parsed if isinstance(parsed, list) else []
        else:
            d["tech_stack"] = []
        result.append(d)
    return result


# ============================================================
# awards.source_url 列迁移（幂等）
# ============================================================

def _ensure_source_url_column(conn) -> None:
    """确保 awards 表存在 source_url 列，不存在则 ALTER 加上。

    幂等：通过 PRAGMA table_info 判断，已存在时直接返回。
    异常处理：ALTER 失败不抛给调用方，仅记录并忽略，
    避免老库/只读库导致整个查询链路 500。
    """
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(awards)").fetchall()}
        if not cols:
            return  # 表还不存在，交给建表逻辑
        if "source_url" in cols:
            return
        conn.execute("ALTER TABLE awards ADD COLUMN source_url TEXT")
        conn.commit()
    except sqlite3.OperationalError as exc:
        # 并发下另一个进程已加列、或数据库只读 —— 都不应中断业务
        try:
            conn.rollback()
        except Exception:
            pass
        if "duplicate column" not in str(exc).lower():
            print(f"[db] ensure source_url 列失败（已忽略）: {exc}")
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[db] ensure source_url 列异常（已忽略）: {exc}")


# ============================================================
# 单条获奖作品（awards）查询
# ============================================================

def get_award_by_id(award_id: int) -> Optional[dict]:
    """按作品 id 取单条获奖作品的完整信息，供单作品深度解析使用。

    - 返回 None 表示作品不存在
    - 已标记为 rejected 的作品同样返回 None（脏数据不对外暴露）
    - tech_stack 为 JSON 字符串，解析为 list；解析失败降级为 []
    - 返回字段：id / competition_id / year / session / title / team / track /
      award_grade / level / summary / highlights / tech_stack
    """
    import json

    sql = (
        "SELECT id, competition_id, year, session, title, team, track, "
        "award_grade, level, summary, highlights, tech_stack, source_url "
        "FROM awards WHERE id = ? "
        "AND COALESCE(review_status, 'pending') <> 'rejected'"
    )
    with get_conn() as conn:
        _ensure_source_url_column(conn)
        row = conn.execute(sql, (int(award_id),)).fetchone()
    if row is None:
        return None

    d = dict(row)

    raw_tech = d.get("tech_stack")
    if isinstance(raw_tech, str) and raw_tech.strip():
        try:
            parsed = json.loads(raw_tech)
        except (ValueError, TypeError):
            parsed = [t.strip() for t in raw_tech.split(",") if t.strip()]
        d["tech_stack"] = parsed if isinstance(parsed, list) else []
    elif isinstance(raw_tech, list):
        d["tech_stack"] = raw_tech
    else:
        d["tech_stack"] = []

    return d


# ============================================================
# 七、用户账号（鉴权地基）
# ============================================================

def create_user(username: str, password_hash: str) -> int:
    """
    新增用户，返回自增 id。

    依赖 schema_v2.sql 的 ux_users_username 唯一索引保证不会重名；
    调用方（routers/auth.py）应先做一次 exists_user 预检以便返回友好错误，
    但仍需在此捕获 IntegrityError 兜底并发抢注。
    """
    with get_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     "username TEXT NOT NULL,"
                     "password_hash TEXT NOT NULL,"
                     "created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))")
        cur = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hash),
        )
        return int(cur.lastrowid)


def get_user_by_username(username: str) -> Optional[dict]:
    """按用户名查询用户，返回 {id, username, password_hash, created_at} 或 None。"""
    with get_conn() as conn:
        try:
            row = conn.execute(
                "SELECT id, username, password_hash, created_at "
                "FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        except sqlite3.OperationalError:
            # users 表尚未创建（老库未跑 init_db）
            return None
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    """按 id 查询用户（不含密码哈希，供鉴权依赖返回当前用户信息）。"""
    with get_conn() as conn:
        try:
            row = conn.execute(
                "SELECT id, username, created_at FROM users WHERE id = ?",
                (int(user_id),),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return dict(row) if row else None


# ============================================================
# ai_usage: per-user daily AI quota (atomic consume)
# ============================================================

def _today_str() -> str:
    """Local date as 'YYYY-MM-DD'. Quota resets at local midnight."""
    return datetime.now().strftime("%Y-%m-%d")


def consume_ai_quota(user_id: int, limit: int) -> dict:
    """Atomically consume one AI call for today.

    Concurrency
    -----------
    The whole operation is a *single* conditional UPDATE:

        UPDATE ai_usage SET used = used + 1
        WHERE user_id = ? AND usage_date = ? AND used < ?

    SQLite serializes writes, so two simultaneous requests can never both
    see ``used < limit`` and both increment past the cap. We rely on
    ``cur.rowcount`` (1 == we won the slot, 0 == quota exhausted) instead of
    a read-then-write, which would race.

    The first call of the day creates the row with used=1 via the same
    guarded INSERT (``used < limit`` is implied by 1 <= limit).

    Returns
    -------
    dict with keys:
        allowed (bool), used (int), limit (int), remaining (int)
    On rejection ``used`` equals the exhausted value (== limit).
    """
    limit = int(limit)
    uid = int(user_id)
    today = _today_str()

    # limit <= 0 means "unlimited" (dev escape hatch)
    if limit <= 0:
        return {"allowed": True, "used": 0, "limit": 0, "remaining": -1}

    with get_conn() as conn:
        # 1) fast path: row already exists for today -> guarded increment
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

        # 2) either no row yet, or quota exhausted -> disambiguate
        row = conn.execute(
            "SELECT used FROM ai_usage WHERE user_id = ? AND usage_date = ?",
            (uid, today),
        ).fetchone()

        if row is not None:
            # row exists and guard failed => exhausted
            used = int(row["used"])
            return {"allowed": False, "used": used, "limit": limit, "remaining": 0}

        # 3) first call today: create the row with used=1.
        #    INSERT OR IGNORE: if a concurrent request created it first,
        #    rowcount == 0 and we fall back to the guarded UPDATE.
        cur = conn.execute(
            "INSERT OR IGNORE INTO ai_usage (user_id, usage_date, used) "
            "VALUES (?, ?, 1)",
            (uid, today),
        )
        if cur.rowcount == 1:
            return {"allowed": True, "used": 1, "limit": limit,
                    "remaining": max(limit - 1, 0)}

        # lost the insert race -> retry the guarded increment once
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
    """Peek at today's usage without consuming. Safe for /me-style display."""
    limit = int(limit)
    with get_conn() as conn:
        try:
            row = conn.execute(
                "SELECT used FROM ai_usage WHERE user_id = ? AND usage_date = ?",
                (int(user_id), _today_str()),
            ).fetchone()
        except sqlite3.OperationalError:
            return {"used": 0, "limit": limit, "remaining": max(limit, 0)}
    used = int(row["used"]) if row else 0
    if limit <= 0:
        return {"used": used, "limit": 0, "remaining": -1}
    return {"used": used, "limit": limit, "remaining": max(limit - used, 0)}


# ============================================================
# user_preparations (auth-required): per-user prep list
# ============================================================

def add_preparation(user_id: int, competition_id: int) -> dict:
    """Add a competition to the user's prep list.

    Idempotent: repeated calls return the existing row instead of raising.
    Returns {"id":..., "created": bool}
    Raises ValueError if the competition does not exist.
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
    """Remove a competition from the prep list. Returns True if a row was deleted."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM user_preparations WHERE user_id = ? AND competition_id = ?",
            (int(user_id), int(competition_id)),
        )
        return cur.rowcount > 0


def list_preparations(user_id: int) -> list:
    """List the user's prep items joined with competition info, newest first."""
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
            # table not created yet (init_db never ran)
            return []
    return [dict(r) for r in rows]


def is_prepared(user_id: int, competition_id: int) -> bool:
    """Whether the competition is already in the user's prep list."""
    with get_conn() as conn:
        try:
            row = conn.execute(
                "SELECT 1 FROM user_preparations WHERE user_id = ? AND competition_id = ?",
                (int(user_id), int(competition_id)),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
    return row is not None


def exists_user(username: str) -> bool:
    """用户名是否已存在。"""
    return get_user_by_username(username) is not None
