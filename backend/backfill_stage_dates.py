# -*- coding: utf-8 -*-
"""backfill_stage_dates.py —— 用已入库的 stage_schedule 反推顶部「比赛时间」。

背景（2026-11-13 傍晚）：
    阶段时间表（stage_schedule）已经抽出来了，但顶部 contest_* 四列没联动，
    详情页出现「下面有阶段时间表、上面比赛时间却写暂未收录」的矛盾。
    本脚本遍历所有 stage_schedule 非空的行，从阶段数组里聚合出总跨度：

        * 精确到日  -> contest_start_date / contest_end_date
        * 只到月    -> contest_start_month / contest_end_month（备注里的「2025年11月」）
        * 报名阶段不参与（报名是另一条时间线）
        * 只补空：已有值的列一律不动

红线：
    * 默认 dry-run，只打印不写；
    * --apply 写生产库必须再加 --allow-production，写前自动备份；
    * 不调用模型，纯本地聚合，可重复执行（幂等）。

用法：
    .\\venv\\Scripts\\python.exe backfill_stage_dates.py --ids 20
    .\\venv\\Scripts\\python.exe backfill_stage_dates.py
    .\\venv\\Scripts\\python.exe backfill_stage_dates.py --apply --allow-production
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

import db
import stage_schedule

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")

_QUERY = (
    "SELECT id, name, exam_schedule, stage_schedule, "
    "contest_start_date, contest_end_date, contest_start_month, contest_end_month "
    "FROM competitions WHERE stage_schedule IS NOT NULL "
    "AND TRIM(stage_schedule) NOT IN ('', '[]') {extra} ORDER BY id"
)


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _is_production(path: str) -> bool:
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(PRODUCTION_DB_PATH)


def _load_rows(ids=None, limit=0, only_missing=True) -> list:
    extra = ""
    if only_missing:
        extra = ("AND (contest_start_date IS NULL AND contest_end_date IS NULL "
                 "AND contest_start_month IS NULL AND contest_end_month IS NULL)")
    where = _QUERY.format(extra=extra)
    params = []
    if ids:
        where = _QUERY.format(extra="AND id IN (%s)" % ",".join("?" * len(ids)))
        params = list(ids)
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(where, params).fetchall()]
    if ids:
        order = {cid: i for i, cid in enumerate(ids)}
        rows.sort(key=lambda r: order.get(r["id"], 999))
    return rows[:limit] if limit else rows


def _show(row: dict) -> str:
    def fmt(field):
        value = row.get(field)
        return "" if value in (None, "") else str(value)

    left = "/".join(x for x in (fmt("contest_start_date"), fmt("contest_start_month")) if x)
    right = "/".join(x for x in (fmt("contest_end_date"), fmt("contest_end_month")) if x)
    return "%s ~ %s" % (left or "—", right or "—")


def main(argv=None) -> int:
    _setup_console()
    ap = argparse.ArgumentParser(
        description="从 stage_schedule 聚合顶部比赛时间（默认 dry-run）")
    ap.add_argument("--ids", default=None, help="逗号分隔的 competitions.id")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    ap.add_argument("--db-path", default=None, help="目标库（默认生产库 competitions.db）")
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--allow-production", action="store_true",
                    help="允许写生产库（必须同时 --apply）")
    ap.add_argument("--all", action="store_true", help="连顶部已有时间的行也检查（仍只补空）")
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
    rows = _load_rows(ids, args.limit, only_missing=not args.all)
    print("[目标库] %s%s" % (target, "" if args.apply else "（只读）"))
    print("[范围] %d 条「有阶段表」的赛事\n" % len(rows))
    if not rows:
        return 0

    now = datetime.now()
    items, written_total = [], 0
    for index, row in enumerate(rows, 1):
        cid = int(row["id"])
        stages = stage_schedule.loads(row.get("stage_schedule"))
        # 阶段数组本身就带「YYYY-MM-DD」，足以作为 date 列的佐证文本
        raw_text = "%s\n%s" % (row.get("exam_schedule") or "", stage_schedule.dumps(stages))
        patch = stage_schedule.plan_writes(dict(row), stages, raw_text, today=now.date())
        patch.pop(stage_schedule.COLUMN, None)  # 本脚本只补顶部时间，不写阶段表
        print("(%d/%d) id=%s %s" % (index, len(rows), cid, (row["name"] or "")[:34]))
        print("         现在：%s" % _show(row))
        if patch:
            span = stage_schedule.derive_span(stages, today=now.date())
            print("         聚合：%s" % json.dumps(span, ensure_ascii=False))
            print("         ✓ %s：%s" % ("已写入" if args.apply else "计划写入",
                                         "、".join("%s=%s" % (k, v) for k, v in patch.items())))
            if args.apply:
                db.update_competition(cid, patch, db_path=target)
            written_total += 1
        else:
            print("         - 阶段里没有可用日期（或顶部已有值）")
        items.append({"id": cid, "name": row["name"], "before": _show(row),
                      "patch": patch, "stages": stages})

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = {"ran_at": stamp, "db": target, "apply": bool(args.apply),
               "total": len(rows), "patched": written_total, "items": items}
    out = args.out or os.path.join(WORKSPACE_DIR, "backfill_stage_dates_%s.json" % stamp)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print("\n" + "=" * 70)
    print("汇总：处理 %d ｜ %s %d"
          % (len(rows), "写入" if args.apply else "计划写入", written_total))
    print("[报告] %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())