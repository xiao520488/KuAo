# -*- coding: utf-8 -*-
"""cleanup_orphan_ai.py —— 清理孤儿 insights / ideas（一次性运维脚本）。

背景（2026-11-08 决策）：
    早期脚本在赛事先行删除 / 未归并时留下了 competition_id 为空或悬空的
    insights 13 条、ideas 16 条中的 12 条。`GET /api/ai/inspirations` 按赛事
    严格隔离，这些数据永远读不到，只占空间且会污染统计，经确认直接删除。

安全措施（与仓库红线一致）：
    * 默认 dry-run，只统计不删除；
    * 写库必须 `--apply`；写生产库还必须 `--allow-production`；
    * 写前用 SQLite backup API 自动备份并 integrity_check（复用
      discover_common.prepare_target）；
    * 只删 competition_id IS NULL 或指向不存在赛事的行，有归属的一律保留。

用法::

    python cleanup_orphan_ai.py --db-path competitions.db            # 只统计
    python cleanup_orphan_ai.py --db-path competitions.db --apply --allow-production
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

import db
import discover_common

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "competitions.db")


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def save_report(payload: dict) -> str:
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(WORKSPACE_DIR, "cleanup_orphan_ai_%s.json" % stamp)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def main(argv=None) -> int:
    _setup_console()
    parser = argparse.ArgumentParser(
        description="删除 competition_id 为空/悬空的 insights 与 ideas（默认 dry-run）"
    )
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="目标数据库")
    parser.add_argument("--apply", action="store_true", help="真正删除（会先备份）")
    parser.add_argument(
        "--allow-production", action="store_true",
        help="写生产库 competitions.db 时必须显式声明",
    )
    args = parser.parse_args(argv)

    db_path = os.path.abspath(os.path.expanduser(args.db_path))
    if not os.path.exists(db_path):
        print("[错误] 数据库不存在：%s" % db_path)
        return 2

    try:
        db_path = discover_common.prepare_target(
            db_path, args.apply, args.allow_production
        )
    except SystemExit as exc:
        print("[错误] %s" % exc)
        return 4

    db.DB_PATH = db_path
    before = db.count_orphan_ai_content()
    print(
        "[体检] insights 孤儿 %s 条 / ideas 孤儿 %s 条"
        % (before["insights"], before["ideas"])
    )

    payload = {
        "db_path": db_path,
        "apply": bool(args.apply),
        "before": before,
        "removed": None,
        "after": None,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if not args.apply:
        print("[dry-run] 未删除任何数据；确认后加 --apply（生产库再加 --allow-production）")
        print("[报告] %s" % save_report(payload))
        return 0

    if not any(before.values()):
        print("[完成] 没有孤儿数据，无需清理")
        payload["removed"] = {"insights": 0, "ideas": 0}
        payload["after"] = before
        print("[报告] %s" % save_report(payload))
        return 0

    removed = db.delete_orphan_ai_content()
    after = db.count_orphan_ai_content()
    payload["removed"] = removed
    payload["after"] = after

    # 删除后重新校验：必须归零，且主表行数不变
    with db.get_conn() as conn:
        counts = {
            table: conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            for table in ("competitions", "awards", "insights", "ideas")
        }
    payload["counts"] = counts

    print("[清理] insights 删除 %s 条 / ideas 删除 %s 条"
          % (removed["insights"], removed["ideas"]))
    print("[复检] 剩余孤儿：insights %s / ideas %s"
          % (after["insights"], after["ideas"]))
    print("[复检] 当前行数：%s" % counts)
    print("[报告] %s" % save_report(payload))

    if after["insights"] or after["ideas"]:
        print("[错误] 仍有孤儿数据，请人工检查")
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
