# -*- coding: utf-8 -*-
"""backfill_contest_dates.py —— 用已入库的「考试安排」正文反推比赛日期。

背景（2026-11-13）：
    赛氪 API 的 contest_start_time 是 '-0001/11/30' 占位符，比赛日期从来不是从接口来的；
    但 AI 早把「决赛于11月14日举行」写进了 exam_schedule，结构化字段却一直空着。
    本脚本对「有正文、无比赛日期」的行补做一次日期抽取，结果过 contest_dates 护栏后只补空。

红线：
    * 默认 dry-run，只打印不写；
    * --apply 只补 NULL（已有日期/月份一律不动），写之前自动备份；
    * 写生产库必须再加 --allow-production；
    * 正文里没有该「月+日」佐证的日期一律丢弃（防模型编造）。

用法：
    # 1) 看清单与抽取结果（不写库）
    .\\venv\\Scripts\\python.exe backfill_contest_dates.py --limit 20

    # 2) 只跑指定赛事
    .\\venv\\Scripts\\python.exe backfill_contest_dates.py --ids 1,10,12

    # 3) 写测试库
    .\\venv\\Scripts\\python.exe backfill_contest_dates.py --apply --db-path .\\test_crawl.db

    # 4) 写生产库（必须显式开闸）
    .\\venv\\Scripts\\python.exe backfill_contest_dates.py --apply --allow-production
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import contest_dates
import db
import llm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")

# 正文里有「月日」才值得调模型（护栏本身就要求正文佐证，预筛不改变结果，只省票）
_HAS_DAY = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\d{1,2}\s*[-/.]\s*\d{1,2}")

_QUERY = (
    "SELECT id, name, exam_schedule, contest_start_date, contest_end_date, "
    "contest_start_month, contest_end_month, is_active "
    "FROM competitions WHERE {where} ORDER BY id"
)


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _is_production(path: str) -> bool:
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(PRODUCTION_DB_PATH)


def _load_rows(ids=None, limit=0, prefilter=True) -> list:
    where = ["contest_start_date IS NULL", "exam_schedule IS NOT NULL",
             "TRIM(exam_schedule) <> ''"]
    if ids:
        where = ["id IN (%s)" % ",".join(str(int(i)) for i in ids)]
    elif prefilter:
        where.append("is_active = 1")
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            _QUERY.format(where=" AND ".join(where))).fetchall()]
    if ids:
        order = {cid: i for i, cid in enumerate(ids)}
        rows.sort(key=lambda r: order.get(r["id"], 999))
    if prefilter:
        rows = [r for r in rows if _HAS_DAY.search(r["exam_schedule"] or "")]
    return rows[:limit] if limit else rows


def main(argv=None) -> int:
    _setup_console()
    ap = argparse.ArgumentParser(
        description="从已入库的「考试安排」正文反推比赛日期（默认 dry-run）")
    ap.add_argument("--ids", default=None, help="逗号分隔的 competitions.id")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    ap.add_argument("--db-path", default=None, help="目标库（默认生产库 competitions.db）")
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--allow-production", action="store_true",
                    help="允许写生产库（必须同时 --apply）")
    ap.add_argument("--delay", type=float, default=0.0, help="每次模型调用后等待秒数")
    ap.add_argument("--no-prefilter", action="store_true",
                    help="不按「正文含月日」预筛，全部交给模型判断")
    ap.add_argument("--out", default=None, help="报告 JSON 路径")
    args = ap.parse_args(argv)

    if args.db_path:
        db.DB_PATH = os.path.abspath(os.path.expanduser(args.db_path))
    target = db.DB_PATH
    if args.apply and _is_production(target) and not args.allow_production:
        print("[拒绝] 目标是生产库：%s；确需写入请加 --allow-production" % target)
        return 2
    if args.apply:
        from discover_common import backup_db

        print("[备份] %s" % backup_db(target))

    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    rows = _load_rows(ids, args.limit, prefilter=not args.no_prefilter)
    print("[目标库] %s%s" % (target, "" if args.apply else "（只读）"))
    print("[范围] %d 条「有考试安排正文、无比赛开始日」的赛事\n" % len(rows))
    if not rows:
        return 0

    usage_before = llm.get_usage()
    items = []
    written_total = 0
    for index, row in enumerate(rows, 1):
        cid = int(row["id"])
        print("(%d/%d) id=%s %s" % (index, len(rows), cid, (row["name"] or "")[:34]))
        dates = {}
        error = ""
        try:
            dates = contest_dates.extract_dates(row["name"] or "",
                                                row["exam_schedule"] or "")
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断
            error = "%s: %s" % (type(exc).__name__, exc)
            print("         ✗ 抽取失败：%s" % error)

        raw_text = row["exam_schedule"] or ""
        if args.apply:
            patch = contest_dates.apply(target, cid, dates, raw_text)
        else:
            patch = contest_dates.plan_from_db(target, cid, dates, raw_text)
        if patch:
            print("         ✓ %s：%s" % ("已写入" if args.apply else "计划写入",
                                         "、".join("%s=%s" % kv for kv in patch.items())))
            written_total += 1
        elif dates:
            print("         - 模型给的 %s 未通过护栏（正文无佐证/年份越界/已有值）" % dates)
        else:
            print("         - 正文里没有可确认的比赛日")
        items.append({"id": cid, "name": row["name"], "dates": dates,
                      "patch": patch, "error": error})
        if args.delay:
            time.sleep(args.delay)

    usage_after = llm.get_usage()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = {
        "ran_at": stamp, "db": target, "apply": bool(args.apply),
        "total": len(rows),
        "with_dates": sum(1 for it in items if it["dates"]),
        "patched": written_total,
        "tokens": usage_after.get("total_tokens", 0) - usage_before.get("total_tokens", 0),
        "items": items,
    }
    out = args.out or os.path.join(WORKSPACE_DIR, "backfill_contest_dates_%s.json" % stamp)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print("\n" + "=" * 70)
    print("汇总：处理 %d ｜ 模型抽出日期 %d ｜ %s %d ｜ tokens %d"
          % (len(rows), payload["with_dates"],
             "写入" if args.apply else "计划写入", written_total, payload["tokens"]))
    print("[报告] %s" % out)
    return 0 if not any(it["error"] for it in items) else 1


if __name__ == "__main__":
    sys.exit(main())