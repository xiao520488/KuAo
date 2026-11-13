# -*- coding: utf-8 -*-
"""backfill_stage_schedule.py —— 用已入库的正文反推「阶段级时间表」。

背景（2026-11-13 重构）：
    主表原来只有一个 contest_start_date 区间，详情页只能把 exam_schedule
    当段落渲染。本脚本对「有正文、无 stage_schedule」的行补做一次阶段抽取，
    结果过 stage_schedule 护栏（原文佐证 / 年份合理 / 起止有序）后只补空，
    并用阶段跨度补空的 contest_start_date / contest_end_date。

红线：
    * 默认 dry-run，只打印不写；
    * --apply 只补 NULL（已有 stage_schedule / 日期一律不动），写前自动备份；
    * 写生产库必须再加 --allow-production；
    * 原文没有「月+日」佐证的日期一律置 NULL，绝不编造。

用法：
    .\\venv\\Scripts\\python.exe backfill_stage_schedule.py --ids 107
    .\\venv\\Scripts\\python.exe backfill_stage_schedule.py --limit 20
    .\\venv\\Scripts\\python.exe backfill_stage_schedule.py --apply --allow-production
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import db
import llm
import stage_schedule

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")

# 预筛：正文里出现「月日」或阶段词才值得调模型（不改变护栏结果，只省票）
_HAS_STAGE = re.compile(
    r"\d{1,2}\s*月|\d{1,2}\s*[-/.]\s*\d{1,2}|初赛|复赛|决赛|报名|赛程|进度")

_QUERY = (
    "SELECT id, name, exam_schedule, stage_schedule, "
    "contest_start_date, contest_end_date, contest_start_month, contest_end_month "
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


def _load_rows(ids=None, limit=0, prefilter=True, only_missing=True) -> list:
    where = ["exam_schedule IS NOT NULL", "TRIM(exam_schedule) <> ''"]
    if only_missing:
        where.append("(stage_schedule IS NULL OR TRIM(stage_schedule) IN ('', '[]'))")
    if ids:
        where = ["id IN (%s)" % ",".join(str(int(i)) for i in ids)]
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            _QUERY.format(where=" AND ".join(where))).fetchall()]
    if ids:
        order = {cid: i for i, cid in enumerate(ids)}
        rows.sort(key=lambda r: order.get(r["id"], 999))
    if prefilter:
        rows = [r for r in rows if _HAS_STAGE.search(r["exam_schedule"] or "")]
    return rows[:limit] if limit else rows


def main(argv=None) -> int:
    _setup_console()
    ap = argparse.ArgumentParser(
        description="从已入库正文反推阶段时间表 stage_schedule（默认 dry-run）")
    ap.add_argument("--ids", default=None, help="逗号分隔的 competitions.id")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    ap.add_argument("--db-path", default=None, help="目标库（默认生产库 competitions.db）")
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--allow-production", action="store_true",
                    help="允许写生产库（必须同时 --apply）")
    ap.add_argument("--delay", type=float, default=0.0, help="每次模型调用后等待秒数")
    ap.add_argument("--all", action="store_true", help="连已有 stage_schedule 的行也重跑（仍只补空）")
    ap.add_argument("--no-prefilter", action="store_true", help="不按正文预筛")
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
    rows = _load_rows(ids, args.limit, prefilter=not args.no_prefilter,
                      only_missing=not args.all)
    print("[目标库] %s%s" % (target, "" if args.apply else "（只读）"))
    print("[范围] %d 条「有正文、无阶段表」的赛事\n" % len(rows))
    if not rows:
        return 0

    now = datetime.now()
    usage_before = llm.get_usage()
    items, written_total = [], 0
    for index, row in enumerate(rows, 1):
        cid = int(row["id"])
        print("(%d/%d) id=%s %s" % (index, len(rows), cid, (row["name"] or "")[:34]))
        raw_text = row["exam_schedule"] or ""
        raw_stages, error = [], ""
        try:
            raw_stages = _extract(row["name"] or "", raw_text)
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断
            error = "%s: %s" % (type(exc).__name__, exc)
            print("         ✗ 抽取失败：%s" % error)

        stages = stage_schedule.guard_stages(raw_stages, raw_text, today=now.date())
        if args.apply:
            patch = stage_schedule.apply(target, cid, stages, raw_text, today=now.date())
        else:
            patch = stage_schedule.plan_from_db(target, cid, stages, raw_text,
                                                today=now.date())
        if stages:
            print("         阶段：%s" % json.dumps(stages, ensure_ascii=False))
        if patch:
            print("         ✓ %s：%s" % ("已写入" if args.apply else "计划写入",
                                         "、".join("%s=%s" % (k, str(v)[:60])
                                                   for k, v in patch.items())))
            written_total += 1
        elif raw_stages:
            print("         - 模型给的 %d 个阶段全部未过护栏（无佐证/年份越界/已有值）"
                  % len(raw_stages))
        else:
            print("         - 正文里没有可确认的阶段")
        items.append({"id": cid, "name": row["name"], "raw_stages": raw_stages,
                      "stages": stages, "patch": patch, "error": error})
        if args.delay:
            time.sleep(args.delay)

    usage_after = llm.get_usage()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = {
        "ran_at": stamp, "db": target, "apply": bool(args.apply),
        "total": len(rows),
        "with_stages": sum(1 for it in items if it["stages"]),
        "patched": written_total,
        "tokens": usage_after.get("total_tokens", 0) - usage_before.get("total_tokens", 0),
        "items": items,
    }
    out = args.out or os.path.join(WORKSPACE_DIR, "backfill_stage_schedule_%s.json" % stamp)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print("\n" + "=" * 70)
    print("汇总：处理 %d ｜ 抽出阶段 %d ｜ %s %d ｜ tokens %d"
          % (len(rows), payload["with_stages"],
             "写入" if args.apply else "计划写入", written_total, payload["tokens"]))
    print("[报告] %s" % out)
    return 0 if not any(it["error"] for it in items) else 1


def _extract(name: str, text: str) -> list:
    """模型原始阶段数组（未过护栏，调用方负责 guard_stages）。"""
    return stage_schedule.extract_raw(name, text)


if __name__ == "__main__":
    sys.exit(main())