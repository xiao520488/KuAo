# -*- coding: utf-8 -*-
"""清理「空壳赛事」：三个核心字段（封面 / 参赛对象 / 报名开始日期）全空的比赛。

判定口径：COALESCE(TRIM(x),'') = ''，即 NULL、空串、纯空白都算空。
这类赛事基本是「赛氪都没收录」的冷门水赛，没有信息价值，直接删掉。

用法：
    python cleanup_empty_shells.py                # 默认 dry-run，只打印不删
    python cleanup_empty_shells.py --dry-run      # 同上，显式写法
    python cleanup_empty_shells.py --apply        # 真删（连带 awards / ideas / insights / 备赛计划 / AI 任务）
    python cleanup_empty_shells.py --apply --db D:\\path\\copy.db   # 指定库（默认 backend/competitions.db）

删除前会先复制一份库到 %TEMP%\\kuo-verify\\competitions.bak-<时间戳>.db（--apply 时默认开，
--no-backup 可关）。级联范围：
    awards / ideas / insights / user_preparations / ai_jobs / ai_analysis_cache / ai_usage
以及 competition_candidates.competition_id 置 NULL（保留审核痕迹，避免悬挂外键）。
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根（本脚本在 backend/ 下）
DEFAULT_DB = os.path.join(BASE, "backend", "competitions.db")

# 按依赖顺序（先子后父）；表不存在会跳过
CHILD_TABLES = (
    "awards",
    "ideas",
    "insights",
    "user_preparations",
    "ai_jobs",
    "ai_analysis_cache",
    "ai_usage",
)

EMPTY_SQL = ("COALESCE(TRIM(image_url), '') = '' "
             "AND COALESCE(TRIM(participants), '') = '' "
             "AND COALESCE(TRIM(reg_start_date), '') = ''")


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone() is not None


def has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute("PRAGMA table_info(%s)" % table))


def find_shells(conn):
    rows = conn.execute(
        "SELECT id, name, COALESCE(level_label, level, '') AS lvl, "
        "       COALESCE(discover_source, '') AS src, created_at, "
        "       COALESCE(short_name, '') AS sn, official_url "
        "FROM competitions WHERE %s ORDER BY id" % EMPTY_SQL
    ).fetchall()
    out = []
    for row in rows:
        item = {
            "id": row[0], "name": row[1], "level": row[2],
            "source": row[3], "created_at": row[4],
            "short_name": row[5], "official_url": row[6],
            "deps": {},
        }
        for table in CHILD_TABLES:
            if table_exists(conn, table) and has_column(conn, table, "competition_id"):
                item["deps"][table] = conn.execute(
                    "SELECT COUNT(*) FROM %s WHERE competition_id = ?" % table,
                    (item["id"],),
                ).fetchone()[0]
        out.append(item)
    return out


def backup(db_path):
    dst_dir = os.path.join(tempfile.gettempdir(), "kuo-verify")
    os.makedirs(dst_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(dst_dir, "competitions.bak-%s.db" % stamp)
    shutil.copy2(db_path, dst)
    return dst


def main(argv=None):
    ap = argparse.ArgumentParser(description="清理三个核心字段全空的空壳赛事")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只打印，不删除（默认）")
    mode.add_argument("--apply", action="store_true", help="真实删除")
    ap.add_argument("--db", default=DEFAULT_DB, help="数据库路径，默认 backend/competitions.db")
    ap.add_argument("--no-backup", action="store_true", help="--apply 时不备份库")
    ap.add_argument("--limit", type=int, default=0, help="最多处理前 N 条（0=全部）")
    ap.add_argument("--include-with-data", action="store_true",
                    help="连「有 awards/ideas/insights/备赛计划」的空壳也删（默认只删彻底无关联数据的）")
    args = ap.parse_args(argv)

    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        print("数据库不存在：%s" % db_path)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        all_shells = find_shells(conn)
        def dep_sum(s):
            return sum(s["deps"].values())
        clean = [s for s in all_shells if dep_sum(s) == 0]
        dirty = [s for s in all_shells if dep_sum(s) > 0]
        shells = all_shells if args.include_with_data else clean
        if args.limit and args.limit > 0:
            shells = shells[: args.limit]

        total = conn.execute("SELECT COUNT(*) FROM competitions").fetchone()[0]
        print("库：%s" % db_path)
        print("比赛总数 %d；三字段全空 %d 条 = 可清理 %d 条 + 有数据待补全 %d 条\n"
              % (total, len(all_shells), len(clean), len(dirty)))

        dep_totals = {}
        if shells:
            print("%-5s %-34s %-6s %-9s %-19s %s"
                  % ("ID", "名称", "简称", "级别", "创建时间", "关联数据"))
            print("-" * 112)
            for s in shells:
                deps = s["deps"]
                for k, v in deps.items():
                    dep_totals[k] = dep_totals.get(k, 0) + v
                dep_txt = " ".join("%s=%d" % (k, v) for k, v in deps.items() if v) or "无"
                name = s["name"] if len(s["name"]) <= 32 else s["name"][:31] + "…"
                print("%-5s %-34s %-6s %-9s %-19s %s"
                      % (s["id"], name, s["short_name"][:6], s["level"] or "",
                         (s["created_at"] or "")[:19], dep_txt))
            print("-" * 112)
            print("关联数据合计：%s" % (
                "、".join("%s %d" % (k, v) for k, v in dep_totals.items() if v) or "无"))
        else:
            print("没有可清理的空壳赛事。")

        if dirty:
            print("\n【保留待补全】三字段全空但有引用数据，不删除（%d 条）：" % len(dirty))
            for s in dirty:
                dep_txt = " ".join("%s=%d" % (k, v) for k, v in s["deps"].items() if v)
                print("  #%-4s %-36s %s" % (s["id"], s["name"], dep_txt))
            print("  → 建议用 enrich 流程补 image_url / participants / reg_start_date，或经后台人工补录。")

        if not args.apply:
            print("\n[dry-run] 未删除任何数据。确认无误后加 --apply 执行。")
            return 0

        if not args.no_backup:
            print("\n已备份：%s" % backup(db_path))

        conn.execute("PRAGMA foreign_keys = ON")
        deleted = {t: 0 for t in CHILD_TABLES}
        cand_unlinked = 0
        with conn:  # 单事务，失败整体回滚
            for s in shells:
                cid = s["id"]
                for table in CHILD_TABLES:
                    if table_exists(conn, table) and has_column(conn, table, "competition_id"):
                        cur = conn.execute(
                            "DELETE FROM %s WHERE competition_id = ?" % table, (cid,))
                        deleted[table] += cur.rowcount
                if table_exists(conn, "competition_candidates"):
                    cur = conn.execute(
                        "UPDATE competition_candidates SET competition_id = NULL "
                        "WHERE competition_id = ?", (cid,))
                    cand_unlinked += cur.rowcount
                conn.execute("DELETE FROM competitions WHERE id = ?", (cid,))
                print("[管理员操作] 删除了赛事：%s" % s["name"])

        left = conn.execute("SELECT COUNT(*) FROM competitions").fetchone()[0]
        print("\n=== 完成 ===")
        print("删除比赛 %d 条，剩余 %d 条" % (len(shells), left))
        print("级联删除：%s" % (
            "、".join("%s %d" % (t, n) for t, n in deleted.items() if n) or "无"))
        print("候选解绑（competition_id 置 NULL）：%d 条" % cand_unlinked)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())