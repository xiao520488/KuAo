# -*- coding: utf-8 -*-
"""bulk_approve.py —— 批量审核「带精确日期」的候选赛事

用途：候选池里有 80 多条，逐条点太累。本脚本把 status='pending' 且
**带精确日期**（reg_start_date / reg_end_date / contest_start_date /
contest_end_date 任一非空）的候选，批量走与 review_candidates.py --approve
完全相同的入库逻辑（同一套去重、封面闸门、事务内标记 approved）。

不含日期的候选一律跳过，留给人工审核（只有月份的赛事不该被批量化放进去）。

用法（backend 目录）：
  # 1) 先干跑，看清会批准哪些，绝不写库
  .\\venv\\Scripts\\python.exe bulk_approve.py --dry-run

  # 2) 确认无误后，在测试库真跑
  .\\venv\\Scripts\\python.exe bulk_approve.py

  # 3) 要写生产库（competitions.db）必须显式授权，脚本会自动 backup
  .\\venv\\Scripts\\python.exe bulk_approve.py `
      --db-path .\\competitions.db --allow-production

可选：
  --skip-image   不联网校验封面（image_url 直接置空，走前端兜底）
  --with-ai      入库后跑简称提取 + 赛氪详情补全（慢：每条 20~60 秒）
  --limit 20     只处理前 N 条
  --force        主表已有同名/高相似赛事也强行入库（默认跳过）
"""
import argparse
import sys

import db
import discover_common as common
import review_candidates as rc

DEFAULT_SORT_ORDER = rc.DEFAULT_SORT_ORDER
DATE_FIELDS = ("reg_start_date", "reg_end_date",
               "contest_start_date", "contest_end_date")


def log(message: str) -> None:
    print(message, flush=True)


def precise_dates(candidate: dict, fields=None) -> dict:
    """返回该候选上非空的精确日期字段；全空则返回 {}。"""
    fields = fields or DATE_FIELDS
    return {
        field: str(candidate.get(field)).strip()
        for field in fields
        if candidate.get(field) not in (None, "") and str(candidate.get(field)).strip()
    }


def candidate_date_fields(db_path: str) -> list:
    """返回该库候选表实际存在的日期列（老库可能还没回填过日期列）。"""
    with db.get_conn(db_path) as conn:
        cols = [row[1] for row in conn.execute(
            "PRAGMA table_info(competition_candidates)")]
    return [field for field in DATE_FIELDS if field in cols]


def pick_targets(db_path: str, limit: int = 0) -> tuple:
    """返回 (待批列表, 跳过数)。只挑 pending 且带精确日期的候选。"""
    if not db.candidates_table_exists(db_path):
        raise SystemExit("候选表不存在：%s" % db_path)
    fields = candidate_date_fields(db_path)
    if not fields:
        raise SystemExit(
            "该库的候选表还没有日期列（%s 全都不存在），无法按日期筛选。\n"
            "请指向已回填过日期的库，例如：\n"
            "  .\\venv\\Scripts\\python.exe bulk_approve.py "
            "--dry-run --db-path .\\competitions.db" % " / ".join(DATE_FIELDS))
    rows = db.query_candidates(status="pending", limit=500, db_path=db_path)
    targets, skipped = [], 0
    for row in rows:
        if precise_dates(row, fields):
            targets.append(row)
        else:
            skipped += 1
    targets.sort(key=lambda r: r["id"])
    if limit and limit > 0:
        targets = targets[:limit]
    return targets, skipped


def approve_one(candidate: dict, args, index: int, total: int) -> str:
    """入库单条候选；返回 'ok' / 'skip' / 'fail'。"""
    cid = candidate["id"]
    raw_name = (candidate.get("raw_name") or "").strip()
    dates = precise_dates(candidate)
    tag = "[%d/%d] 候选 %s《%s》" % (index, total, cid, raw_name[:28])
    log("%s 日期=%s" % (tag, " ".join("%s" % v for v in dates.values())))

    # 1) 去重：只查主表，避免命中候选自身
    data = rc.build_competition_data(candidate, None)
    name = (data.get("name") or "").strip()
    norm = db.normalize_competition_name(name) if name else candidate.get("norm_name")
    duplicate = db.find_duplicate_candidate(
        norm or "", official_url=candidate.get("official_url"),
        db_path=args.db_path, include_candidates=False,
    )
    if duplicate and not args.force:
        log("     ↳ 跳过：主表已有 id=%s《%s》（%s）" % (
            duplicate["id"], duplicate["name"], duplicate["reason"]))
        return "skip"

    # 2) 封面闸门（联网，可跳过）
    if args.skip_image:
        image_url = None
        log("     ↳ 封面：按 --skip-image 不校验，image_url 置空（前端兜底）")
    else:
        try:
            image_url, note = rc.verify_cover(candidate)
        except Exception as exc:  # noqa: BLE001 - 网络异常不该中断整批
            image_url, note = None, "封面校验异常（%s），按无封面入库" % exc
        log("     ↳ 封面：%s" % note)

    if args.dry_run:
        log("     ↳ [干跑] 将入库（未写库）")
        return "ok"

    # 3) 入库（promote_candidate 内部单事务：插主表 + 标记 approved）
    data = rc.build_competition_data(candidate, image_url)
    data["sort_order"] = DEFAULT_SORT_ORDER
    try:
        competition_id = db.promote_candidate(cid, data, db_path=args.db_path)
    except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
        log("     ↳ 失败：%s" % exc)
        return "fail"
    log("     ↳ 已入库 competitions id=%s" % competition_id)

    # 4) 可选增强（慢，默认关）
    if args.with_ai:
        rc.generate_short_name_after_approve(args, competition_id, name)
        rc.enrich_after_approve(args, candidate, competition_id)
    return "ok"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="批量审核带精确日期的 pending 候选（默认目标库 test_crawl.db）"
    )
    parser.add_argument("--db-path", default=common.DEFAULT_DB_PATH,
                        help="目标库路径（默认 test_crawl.db）")
    parser.add_argument("--allow-production", action="store_true",
                        help="允许写生产库 competitions.db（会自动 backup）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要批准的清单，不写库")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理 N 条（0 = 不限）")
    parser.add_argument("--skip-image", action="store_true",
                        help="不联网校验封面，image_url 置空")
    parser.add_argument("--with-ai", action="store_true",
                        help="入库后跑简称提取 + 赛氪详情补全（很慢）")
    parser.add_argument("--force", action="store_true",
                        help="主表已有相似赛事也强行入库")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    log("[目标库] %s%s" % (args.db_path, "（干跑，不写库）" if args.dry_run else ""))
    if not args.dry_run:
        rc.require_production_permission(args.db_path, args.allow_production)
    # 复用 review_candidates 的增强函数，它按 args.skip_enrich 判断是否调模型
    args.skip_enrich = not args.with_ai

    targets, skipped = pick_targets(args.db_path, args.limit)
    log("[待批] pending 且带精确日期 %d 条；无日期跳过 %d 条" % (len(targets), skipped))
    if not targets:
        log("[完成] 没有需要批准的候选")
        return 0

    if not args.dry_run:
        rc.write_backup_if_production(args.db_path)

    counts = {"ok": 0, "skip": 0, "fail": 0}
    for i, candidate in enumerate(targets, 1):
        counts[approve_one(candidate, args, i, len(targets))] += 1

    verb = "可批准" if args.dry_run else "已批准"
    log("\n[完成] %s %d 条；跳过 %d 条（主表重复 %d / 无日期 %d）；失败 %d 条" % (
        verb, counts["ok"], counts["skip"] + skipped, counts["skip"], skipped,
        counts["fail"]))
    if args.dry_run:
        log("干跑结束，去掉 --dry-run 才会真正写库")
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())