# -*- coding: utf-8 -*-
"""backfill_dates.py —— 存量赛事「精确日期」补全探测（默认只读）

背景（2026-11-09 双轨制）：
    competitions 同时保留两套时间字段——
      * 月份轨：reg_start_month / reg_end_month / contest_*_month（老数据，永不删）；
      * 日期轨：reg_start_date / reg_end_date / contest_*_date（'YYYY-MM-DD' 或 'MM-DD'）。
    本脚本用升级后的抽取 Prompt 重新抓一次赛事的详情页/通知页，看模型能否从原文里
    把「精确到日」的时间抽出来，并逐条核对原文是否真的写了这一天（防模型编造）。

红线：
    * 默认 dry-run，只抓页面 + 调模型 + 打印对照表，不写库；
    * --apply 才写库，且只补 NULL（已有值一律保留），生产库还需 --allow-production；
    * 抽到「日」才写日期；只有月份时日期保持 NULL，绝不替模型补齐。

用法：
    # 1) 只看结果（读生产库 + 联网抓页面，不写库）
    .\\venv\\Scripts\\python.exe backfill_dates.py --limit 5

    # 2) 指定赛事
    .\\venv\\Scripts\\python.exe backfill_dates.py --ids 13,12,2,1,35

    # 3) 真写库（只补空；测试库）
    .\\venv\\Scripts\\python.exe backfill_dates.py --ids 13,12 --apply --db-path .\\test_crawl.db

    # 4) 写生产库（必须显式开闸）
    .\\venv\\Scripts\\python.exe backfill_dates.py --limit 5 --apply --allow-production
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import crawler_competitions as cc
import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")

# 只探测「有真实页面可抓」的赛事：discover_source 记录的就是抓取时的页面 URL
_PAGE_SQL = (
    "SELECT id, name, official_url, discover_source, "
    "reg_start_month, reg_end_month, reg_cross_year, "
    "contest_start_month, contest_end_month, "
    "reg_start_date, reg_end_date, contest_start_date, contest_end_date "
    "FROM competitions WHERE {where} ORDER BY id DESC"
)

DATE_FIELDS = ("reg_start_date", "reg_end_date",
               "contest_start_date", "contest_end_date")


def _setup_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _is_production(path):
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(PRODUCTION_DB_PATH)


def _load_rows(ids=None, limit=0):
    """读取候选赛事（只读，靠 db.get_conn 的普通连接不会写业务数据）。"""
    with db.get_conn() as conn:
        if ids:
            marks = ",".join("?" for _ in ids)
            sql = _PAGE_SQL.format(where="id IN (%s)" % marks)
            rows = conn.execute(sql, list(ids)).fetchall()
        else:
            sql = _PAGE_SQL.format(
                where="discover_source LIKE 'http%' AND official_url IS NOT NULL")
            rows = conn.execute(sql).fetchall()
    rows = [dict(r) for r in rows]
    if ids:
        order = {cid: i for i, cid in enumerate(ids)}
        rows.sort(key=lambda r: order.get(r["id"], 999))
    if limit:
        rows = rows[:limit]
    return rows


def _evidence(body, date_text):
    """原文是否真的写了这一天：返回 '有' / '无' / '—'（无日期）。"""
    if not date_text:
        return "—"
    parts = str(date_text).split("-")
    if len(parts) < 2:
        return "无"
    try:
        month, day = int(parts[-2]), int(parts[-1])
    except ValueError:
        return "无"
    patterns = (
        "%d月%d日" % (month, day),
        "%02d月%02d日" % (month, day),
        "%02d-%02d" % (month, day),
        "%d-%02d" % (month, day),
        "%02d/%02d" % (month, day),
        "%d/%d" % (month, day),
        "%02d.%02d" % (month, day),
    )
    flat = (body or "").replace(" ", "")
    return "有" if any(p in flat for p in patterns) else "无"


def _fmt(value):
    return "-" if value is None or value == "" else str(value)


def _print_row(rec):
    ext = rec["extracted"] or {}
    ev_cs = _evidence(rec["body"], ext.get("contest_start_date"))
    ev_ce = _evidence(rec["body"], ext.get("contest_end_date"))
    print("  id=%-3s %s" % (rec["id"], (rec["name"] or "")[:26]))
    print("        页面：%s" % rec["page_url"])
    print("        报名：月 %s-%s ｜ 日 %s ~ %s" % (
        _fmt(ext.get("reg_start_month")), _fmt(ext.get("reg_end_month")),
        _fmt(ext.get("reg_start_date")), _fmt(ext.get("reg_end_date"))))
    print("        比赛：月 %s-%s ｜ 日 %s ~ %s   （原文佐证：起 %s / 止 %s）" % (
        _fmt(ext.get("contest_start_month")), _fmt(ext.get("contest_end_month")),
        _fmt(ext.get("contest_start_date")), _fmt(ext.get("contest_end_date")),
        ev_cs, ev_ce))
    print("        confidence=%s ｜ 正文 %d 字 ｜ %s" % (
        _fmt(ext.get("confidence")), rec["body_chars"], rec["note"]))


def _plan_writes(rec, ext):
    """只补 NULL 的写入计划：[(field, old, new)]。"""
    ops = []
    cur = rec["current"]
    # 日期轨：原文抽到「日」才写；往年届次（官网挂着旧通知）一律不写
    this_year = datetime.now().year
    for field in DATE_FIELDS:
        new = ext.get(field)
        if not new or cur.get(field) not in (None, ""):
            continue
        try:
            year = int(str(new)[:4])
        except ValueError:
            continue
        if not (this_year - 1 <= year <= this_year + 1):
            continue
        ops.append((field, cur.get(field), new))
    # 月份轨：只补空，且只在日期抽到时用它反推（不覆盖已有月份）
    for field, date_field in (("contest_start_month", "contest_start_date"),
                              ("contest_end_month", "contest_end_date"),
                              ("reg_start_month", "reg_start_date"),
                              ("reg_end_month", "reg_end_date")):
        new = ext.get(field)
        if new is not None and cur.get(field) is None:
            ops.append((field, cur.get(field), new))
    return ops


def main(argv=None):
    _setup_console()
    ap = argparse.ArgumentParser(
        description="用新 Prompt 重新抓取赛事页，探测/回填精确日期（默认只读）")
    ap.add_argument("--ids", default=None, help="逗号分隔的 competitions.id；默认自动挑 5 条有页面的")
    ap.add_argument("--limit", type=int, default=5, help="不带 --ids 时最多探测几条（默认 5）")
    ap.add_argument("--db-path", default=None, help="目标库（默认生产库 competitions.db）")
    ap.add_argument("--max-chars", type=int, default=6000, help="正文截断长度")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--allow-production", action="store_true",
                    help="允许写生产库 competitions.db（必须同时 --apply）")
    ap.add_argument("--out", default=None, help="结果 JSON 路径")
    args = ap.parse_args(argv)

    if args.db_path:
        db.DB_PATH = os.path.abspath(os.path.expanduser(args.db_path))
    target = db.DB_PATH
    is_prod = _is_production(target)
    if args.apply and is_prod and not args.allow_production:
        print("[拒绝] 目标是生产库：%s；确需写入请加 --allow-production" % target)
        return 2

    ids = None
    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]

    rows = _load_rows(ids, args.limit)
    if not rows:
        print("[空] 没有可探测的赛事（需要 discover_source 为 http 页面）")
        return 1

    print("[目标库] %s%s" % (target, "" if args.apply else "（只读）"))
    print("[模式] %s" % ("APPLY 写库（只补 NULL）" if args.apply else "dry-run，不写库"))
    print("[范围] %d 条\n" % len(rows))

    results = []
    for index, row in enumerate(rows, 1):
        page_url = (row.get("discover_source") or "").strip()
        rec = {
            "id": row["id"], "name": row["name"], "page_url": page_url,
            "current": row, "extracted": None, "body": "", "body_chars": 0,
            "note": "", "ops": [], "applied": [],
        }
        print("(%d/%d) id=%s 抓取中：%s" % (index, len(rows), row["id"], page_url[:96]))
        try:
            page = cc.fetch_competition_page(page_url, validate_image=False)
            rec["body"] = page.get("body") or ""
            rec["body_chars"] = len(rec["body"])
            _raw, extracted = cc.extract_competition(page, args.max_chars)
            rec["extracted"] = extracted
            rec["note"] = "抽取成功"
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断批量
            rec["note"] = "失败：%s: %s" % (type(exc).__name__, exc)
            print("         ✗ %s" % rec["note"])
            results.append(rec)
            continue

        _print_row(rec)
        rec["ops"] = _plan_writes(rec, rec["extracted"])
        if args.apply and rec["ops"]:
            payload = {field: new for field, _old, new in rec["ops"]}
            db.update_competition(rec["id"], payload)
            rec["applied"] = [field for field, _old, _new in rec["ops"]]
            print("        ✓ 已写入：%s" % "、".join(
                "%s=%s" % (field, payload[field]) for field in rec["applied"]))
        elif rec["ops"]:
            print("        计划写入：%s" % "、".join(
                "%s=%s" % (field, new) for field, _old, new in rec["ops"]))
        else:
            print("        无新增（库里已有值或原文没给到日）")
        results.append(rec)
        if index < len(rows):
            time.sleep(2)

    ok = [r for r in results if r["extracted"]]
    with_date = [r for r in ok if any(r["extracted"].get(f) for f in
                                      ("contest_start_date", "contest_end_date"))]
    with_reg_date = [r for r in ok if any(r["extracted"].get(f) for f in
                                          ("reg_start_date", "reg_end_date"))]
    print("\n" + "=" * 74)
    print("汇总：抓取成功 %d/%d ｜ 抽出比赛日 %d ｜ 抽出报名日 %d ｜ 计划写入字段 %d" % (
        len(ok), len(results), len(with_date), len(with_reg_date),
        sum(len(r["ops"]) for r in results)))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or os.path.join(WORKSPACE_DIR, "backfill_dates_%s.json" % stamp)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"ran_at": stamp, "db": target, "apply": bool(args.apply),
                   "items": results}, fh, ensure_ascii=False, indent=2, default=str)
    print("[报告] %s" % out)
    return 0 if len(ok) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
