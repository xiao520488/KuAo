"""清理「教育部门名录」来源的空壳赛事：退回候选表，等待按名称重新审核。

背景：`edu-roster` 候选的 `source_url` 全部指向同一个目录页
（如 https://www.sdaeu.edu.cn/jwc/info/1047/2528.htm），早期审批只写了主表、
没有补详情，于是 competitions 里留下一批「只有名字 + 简称」的空壳。

本脚本按「候选已通过 + 主表详情全空」识别这些行，把候选退回 pending 并删除
主表行；等审核端上线「名称 -> 赛氪」匹配后，再次通过即可补全真实数据。

安全设计：
- 默认 dry-run，只打印候选清单；加 `--apply` 才写库。
- 主表行若已挂 `awards`（获奖数据）或 `user_preparations`（用户收藏），
  **跳过不删**，只打印警告 —— 这些是真实用户/采集数据，不能连带丢失。
- 写生产库需 `--allow-production`（沿用全项目约定）。

用法：
    venv\\Scripts\\python.exe cleanup_directory_shells.py                # 预览
    venv\\Scripts\\python.exe cleanup_directory_shells.py --apply        # 退回候选
    venv\\Scripts\\python.exe cleanup_directory_shells.py --apply --db-path .\\test_crawl.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "competitions.db")
PRODUCTION_DB_NAME = "competitions.db"

# 判定「空壳」的字段：这些全空，说明主表只有名字/简称，没有任何真实内容
SHELL_FIELDS = (
    "image_url",
    "reg_start_date",
    "reg_end_date",
    "contest_start_date",
    "contest_end_date",
    "participants",
    "content_scope",
    "exam_schedule",
    "intro",
    "summary",
)

# 有真实数据的子表：命中即跳过删除
PROTECTED_CHILDREN = ("awards", "user_preparations")


def is_shell(row: dict) -> bool:
    return all(not str(row.get(field) or "").strip() for field in SHELL_FIELDS)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def find_shells(conn: sqlite3.Connection) -> list:
    """返回 [{candidate_id, competition_id, name, source_url, blocked_by}]。"""
    comp_cols = _columns(conn, "competitions")
    wanted = [f for f in SHELL_FIELDS if f in comp_cols]
    sql = """
        SELECT c.id AS candidate_id, c.raw_name, c.source_url, c.competition_id,
               p.id AS pid, p.short_name, %s
        FROM competition_candidates c
        JOIN competitions p ON p.id = c.competition_id
        WHERE c.source = 'edu-roster' AND c.status = 'approved'
    """ % ", ".join("p.%s" % f for f in wanted)
    out = []
    for row in conn.execute(sql):
        data = dict(row)
        if not is_shell(data):
            continue
        blocked = []
        for table in PROTECTED_CHILDREN:
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM %s WHERE competition_id = ?" % table,
                    (data["competition_id"],),
                ).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            if count:
                blocked.append("%s=%d" % (table, count))
        out.append({
            "candidate_id": data["candidate_id"],
            "competition_id": data["competition_id"],
            "name": data["raw_name"],
            "short_name": data.get("short_name"),
            "source_url": data.get("source_url"),
            "blocked_by": blocked,
        })
    return out


def revert(conn: sqlite3.Connection, shells: list) -> dict:
    """候选退回 pending + 删除主表行；受保护的行跳过。"""
    reverted, skipped = [], []
    for item in shells:
        if item["blocked_by"]:
            skipped.append(item)
            continue
        with conn:  # 单事务，失败整体回滚
            deleted = conn.execute("DELETE FROM competitions WHERE id = ?",
                                   (item["competition_id"],)).rowcount
            if not deleted:
                # 主表行已被别处删掉：仍然把候选退回 pending，避免状态悬空
                skipped.append(item)
                conn.execute(
                    "UPDATE competition_candidates "
                    "SET status = 'pending', competition_id = NULL, reviewed_at = NULL "
                    "WHERE id = ?",
                    (item["candidate_id"],),
                )
                continue
            conn.execute(
                "UPDATE competition_candidates "
                "SET status = 'pending', competition_id = NULL, reviewed_at = NULL "
                "WHERE id = ?",
                (item["candidate_id"],),
            )
        reverted.append(item)
    return {"reverted": reverted, "skipped": skipped}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="清理教育部门名录空壳赛事")
    parser.add_argument("--db-path", default=DEFAULT_DB, help="目标数据库")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    parser.add_argument("--allow-production", action="store_true",
                        help="允许写生产库 competitions.db")
    args = parser.parse_args(argv)

    db_path = os.path.abspath(args.db_path)
    if not os.path.exists(db_path):
        print("[错误] 数据库不存在：%s" % db_path)
        return 1
    if os.path.basename(db_path) == PRODUCTION_DB_NAME and args.apply \
            and not args.allow_production:
        print("[拒绝] 目标疑似生产库（%s），写库需加 --allow-production。" % db_path)
        return 2

    conn = _connect(db_path)
    try:
        shells = find_shells(conn)
        print("目标库：%s" % db_path)
        print("识别到空壳赛事：%d 条" % len(shells))
        if not shells:
            print("没有需要清理的行。")
            return 0

        print("\n%-6s %-6s %-34s %s" % ("候选", "赛事", "名称", "保护"))
        for item in shells:
            print("%-6s %-6s %-34s %s" % (
                item["candidate_id"], item["competition_id"],
                (item["name"] or "")[:34],
                "、".join(item["blocked_by"]) or "-",
            ))

        if not args.apply:
            print("\n[dry-run] 未写库。加 --apply 退回候选表。")
            return 0

        result = revert(conn, shells)
        print("\n已退回候选：%d 条（status=approved -> pending，主表行已删除）"
              % len(result["reverted"]))
        for item in result["reverted"]:
            print("  ✓ %s -> 候选 %s 待重新审核" % (item["name"][:30], item["candidate_id"]))
        if result["skipped"]:
            print("\n跳过（已有关联数据）：%d 条" % len(result["skipped"]))
            for item in result["skipped"]:
                print("  ! %s（赛事 %s，%s）" % (
                    item["name"][:30], item["competition_id"],
                    "、".join(item["blocked_by"])))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())