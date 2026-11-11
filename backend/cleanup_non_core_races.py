# -*- coding: utf-8 -*-
"""cleanup_non_core_races.py —— 把主库收敛成「教育部 84 项核心国赛智库」

判定（按优先级，命中即定案）：
  R1 省级/区域级 + 无关联数据  -> 删除
     （名称命中 省赛/省级/区域赛/赛区/选拔赛…，见 competition_whitelist.PROVINCIAL_RE）
  R2 省级/区域级 + 有真实获奖等关联 -> **转占位**（category='待确认'、sort_order=9999、
     is_active=0：行与 awards 全保留，前端不再当核心赛事展示）
  R3 不在 84 项白名单 + 无关联数据 -> 删除（野生水赛）
  R4 不在 84 项白名单 + 有真实获奖 -> 保留并列入「待人工复核」（不自动动）

另可 `--wipe-candidates` 清空 competition_candidates（候选池归零）。

安全：
  * 默认 dry-run；--apply 才写库，且自动把库复制到 %TEMP%\\kuo-verify\\ 备份；
  * 写生产库需 --apply --allow-production（competitions.db 视为生产库）；
  * 删除走单事务，级联 awards / ideas / insights / user_preparations / ai_jobs /
    ai_analysis_cache / ai_usage，并把 competition_candidates.competition_id 置 NULL。

用法：
    .\\venv\\Scripts\\python.exe cleanup_non_core_races.py                 # dry-run
    .\\venv\\Scripts\\python.exe cleanup_non_core_races.py --apply --allow-production
    .\\venv\\Scripts\\python.exe cleanup_non_core_races.py --apply --allow-production --wipe-candidates
    .\\venv\\Scripts\\python.exe cleanup_non_core_races.py --db .\\test_crawl.db  # 测试库不用 allow-production
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "competitions.db")
PRODUCTION_NAME = "competitions.db"

sys.path.insert(0, BASE_DIR)
import competition_whitelist as whitelist  # noqa: E402

CHILD_TABLES = (
    "awards",
    "ideas",
    "insights",
    "user_preparations",
    "ai_jobs",
    "ai_analysis_cache",
    "ai_usage",
)

PLACEHOLDER_CATEGORY = "待确认"
PLACEHOLDER_SORT = 9999


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone() is not None


def has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute("PRAGMA table_info(%s)" % table))


def deps_of(conn, cid):
    deps = {}
    for table in CHILD_TABLES:
        if table_exists(conn, table) and has_column(conn, table, "competition_id"):
            deps[table] = conn.execute(
                "SELECT COUNT(*) FROM %s WHERE competition_id = ?" % table, (cid,)
            ).fetchone()[0]
    return deps


def classify(conn):
    """返回 [(row_dict, action, reason)]，action ∈ delete / placeholder / keep / skip。"""
    rows = conn.execute(
        "SELECT id, name, COALESCE(category,'') AS category, COALESCE(level,'') AS level, "
        "       COALESCE(level_label,'') AS level_label, COALESCE(discover_source,'') AS src "
        "FROM competitions ORDER BY id"
    ).fetchall()
    out = []
    for row in rows:
        cid, name = row[0], row[1]
        deps = deps_of(conn, cid)
        dep_sum = sum(deps.values())
        provincial = whitelist.is_provincial(name)
        hit = whitelist.match(name)

        if provincial and dep_sum == 0:
            action, reason = "delete", "省/区域赛且无关联数据"
        elif provincial:
            action, reason = "placeholder", "省/区域赛，有真实获奖→转占位"
        elif hit is None and dep_sum == 0:
            action, reason = "delete", "非 84 项白名单且无关联数据"
        elif hit is None:
            action, reason = "keep", "非白名单但有获奖→待人工复核（不自动改）"
        elif row["category"] == PLACEHOLDER_CATEGORY:
            action, reason = "keep", "白名单内占位赛事，等待补全"
        else:
            action, reason = "skip", "核心赛事，保留"
        out.append({
            "id": cid, "name": name, "category": row["category"],
            "level": row["level"], "level_label": row["level_label"], "src": row["src"],
            "deps": deps, "dep_sum": dep_sum,
            "whitelist": hit, "provincial": provincial,
            "action": action, "reason": reason,
        })
    return out


def backup(db_path):
    dst_dir = os.path.join(tempfile.gettempdir(), "kuo-verify")
    os.makedirs(dst_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(dst_dir, "competitions.bak-%s-%s.db"
                       % (os.path.basename(db_path).replace(".db", ""), stamp))
    shutil.copy2(db_path, dst)
    return dst


def short(text, width):
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def print_group(title, items, columns=True):
    if not items:
        return
    print("\n%s（%d 条）" % (title, len(items)))
    if columns:
        print("-" * 118)
        print("%-5s %-8s %-30s %-18s %-12s %s" % ("ID", "处置", "名称", "白名单", "关联", "原因"))
        print("-" * 118)
    for it in items:
        wl = ("#%d %s" % (it["whitelist"]["index"], short(it["whitelist"]["matched"], 14))
              if it["whitelist"] else ("[省级]" if it["provincial"] else "—"))
        dep_txt = " ".join("%s=%d" % (k, v) for k, v in it["deps"].items() if v) or "无"
        print("%-5s %-8s %-30s %-18s %-12s %s"
              % (it["id"], it["action"], short(it["name"], 30), short(wl, 18),
                 short(dep_txt, 12), it["reason"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="清理非核心赛事，收敛为 84 项国赛智库")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只打印（默认）")
    mode.add_argument("--apply", action="store_true", help="真实执行")
    ap.add_argument("--db", default=DEFAULT_DB, help="数据库路径，默认 backend/competitions.db")
    ap.add_argument("--allow-production", action="store_true", help="允许写 competitions.db")
    ap.add_argument("--no-backup", action="store_true", help="--apply 时不备份")
    ap.add_argument("--wipe-candidates", action="store_true",
                    help="同时清空 competition_candidates（候选池归零）")
    ap.add_argument("--keep-provisional-awards", action="store_true",
                    help="R2 也直接删除（连带获奖的省赛一起删，默认不删）")
    args = ap.parse_args(argv)

    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        print("数据库不存在：%s" % db_path)
        return 2
    is_prod = os.path.basename(db_path).lower() == PRODUCTION_NAME
    if args.apply and is_prod and not args.allow_production:
        print("拒绝：目标是生产库 %s，必须显式加 --allow-production" % db_path)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        print("库：%s%s" % (db_path, "（生产库）" if is_prod else ""))
        print(whitelist.summary())

        items = classify(conn)
        deletes = [i for i in items if i["action"] == "delete"]
        placeholders = [i for i in items if i["action"] == "placeholder"]
        keeps = [i for i in items if i["action"] == "keep"]
        skips = [i for i in items if i["action"] == "skip"]
        if args.keep_provisional_awards:
            deletes += [dict(i, action="delete", reason="省/区域赛（--keep-provisional-awards 强制删）")
                        for i in placeholders]
            placeholders = []

        total = len(items)
        print("\n总数 %d = 保留核心 %d + 转占位 %d + 待复核 %d + 删除 %d"
              % (total, len(skips), len(placeholders), len(keeps), len(deletes)))

        print_group("① 将删除", deletes)
        print_group("② 将转占位（保留行与获奖，前端隐藏）", placeholders)
        print_group("③ 待人工复核（非白名单但有获奖）", keeps)

        if args.wipe_candidates and table_exists(conn, "competition_candidates"):
            n = conn.execute("SELECT COUNT(*) FROM competition_candidates").fetchone()[0]
            print("\n④ 候选表 competition_candidates：%d 条将被清空" % n)

        if skips:
            print("\n⑤ 核心赛事保留：%d 条（略）" % len(skips))

        if not args.apply:
            print("\n[dry-run] 未改动任何数据。确认后加 --apply"
                  + (" --allow-production" if is_prod else "") + " 执行。")
            return 0

        if not args.no_backup:
            print("\n已备份：%s" % backup(db_path))

        deleted_rows = 0
        cascade = {t: 0 for t in CHILD_TABLES}
        unlinked = 0
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:  # 单事务
            for it in deletes:
                cid = it["id"]
                for table in CHILD_TABLES:
                    if table_exists(conn, table) and has_column(conn, table, "competition_id"):
                        cur = conn.execute(
                            "DELETE FROM %s WHERE competition_id = ?" % table, (cid,))
                        cascade[table] += cur.rowcount
                if table_exists(conn, "competition_candidates"):
                    cur = conn.execute(
                        "UPDATE competition_candidates SET competition_id = NULL "
                        "WHERE competition_id = ?", (cid,))
                    unlinked += cur.rowcount
                conn.execute("DELETE FROM competitions WHERE id = ?", (cid,))
                deleted_rows += 1
                print("[删除] #%s %s" % (cid, it["name"]))

            for it in placeholders:
                conn.execute(
                    "UPDATE competitions SET category = ?, sort_order = ?, is_active = 0 "
                    "WHERE id = ?", (PLACEHOLDER_CATEGORY, PLACEHOLDER_SORT, it["id"]))
                print("[占位] #%s %s" % (it["id"], it["name"]))

            wiped = 0
            if args.wipe_candidates and table_exists(conn, "competition_candidates"):
                wiped = conn.execute("DELETE FROM competition_candidates").rowcount

        left = conn.execute("SELECT COUNT(*) FROM competitions").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM competitions WHERE is_active = 1").fetchone()[0] \
            if has_column(conn, "competitions", "is_active") else left
        print("\n=== 完成 ===")
        print("删除 %d 条；转占位 %d 条；剩余 %d 条（前端可见 %d 条）"
              % (deleted_rows, len(placeholders), left, active))
        print("级联删除：%s" % ("、".join("%s %d" % (t, n) for t, n in cascade.items() if n) or "无"))
        print("候选解绑：%d 条" % unlinked)
        if args.wipe_candidates:
            print("候选表已清空：%d 条" % wiped)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())