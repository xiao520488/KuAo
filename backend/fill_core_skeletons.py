# -*- coding: utf-8 -*-
"""fill_core_skeletons.py —— 核心赛事「骨架」补全（封面 / 日期 / 参赛对象）

背景
----
清理空壳水赛时保留了一批「有获奖作品 / 备赛灵感」的核心赛事，但它们的
`image_url` / `participants` / `reg_start_date` 三列仍全空——详情页只有骨架，
没有海报、没有报名日期、没有参赛对象。

本脚本不重新发明轮子，直接复用审核入库时的同一套补全链路
（`enrich_saikr_details.enrich_competition`）：

    赛事名称 -> saikr_match 本地索引匹配 contest_id（相似度阈值 0.86）
        -> 赛氪详情 API `contest/info`
        -> AI 提纯「参赛对象 / 内容范围 / 考试安排」（禁止照抄原文）
        -> 只补空字段：日期四列 + 封面（封面过 image_guard 闸门）

缺省处理范围 = 「三字段全空 + 至少有一条下游数据（awards / ideas /
insights / user_preparations）」的核心赛事；`--ids` 可显式指定。

用法
----
    # 1. 预演（默认，只读库 + 抓取 + 校验封面，不写库、不调 AI）
    venv\\Scripts\\python.exe fill_core_skeletons.py

    # 2. 真实写生产库（自动备份）
    venv\\Scripts\\python.exe fill_core_skeletons.py --apply --allow-production

    # 3. 只处理指定赛事
    venv\\Scripts\\python.exe fill_core_skeletons.py --ids 2,10,17

    # 4. 往年届次（报名结束超 400 天）也补：只写封面/参赛对象，日期列保持空
    venv\\Scripts\\python.exe fill_core_skeletons.py --apply --allow-production --allow-stale

合规
----
* 只处理库里已有赛事，不发现新赛事；
* 请求间隔默认 3~5 秒（`--delay` 下限 3 秒）；
* 默认 dry-run；`--apply` 写生产库必须 `--allow-production`，写前自动备份；
* 参赛对象等长文本必须经 AI 提纯，绝不写原文；抽不到就保持 NULL。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import discover_common as common  # noqa: E402
import enrich_saikr_details as enrich  # noqa: E402
import image_guard  # noqa: E402
import saikr_match  # noqa: E402

SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "competitions.db")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PREVIEW = os.path.join(BASE_DIR, ".verify", "fill_core_preview.json")

TARGET_SQL = """
SELECT c.id, c.name, c.discover_source,
       (SELECT COUNT(*) FROM awards a WHERE a.competition_id = c.id)
     + (SELECT COUNT(*) FROM ideas i WHERE i.competition_id = c.id)
     + (SELECT COUNT(*) FROM insights ins WHERE ins.competition_id = c.id)
     + (SELECT COUNT(*) FROM user_preparations up WHERE up.competition_id = c.id)
       AS children
FROM competitions c
WHERE COALESCE(TRIM(c.image_url), '') = ''
  AND COALESCE(TRIM(c.participants), '') = ''
  AND COALESCE(TRIM(c.reg_start_date), '') = ''
ORDER BY children DESC, c.id
"""


def log(message: str) -> None:
    print(message, flush=True)


def load_targets(db_path: str, ids: set | None = None) -> list:
    """三字段全空且有下游数据的核心赛事；ids 非空时按 id 取（不限三空）。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if ids:
            rows = []
            for cid in sorted(ids):
                row = conn.execute(
                    "SELECT id, name, discover_source, 0 AS children "
                    "FROM competitions WHERE id = ?", (cid,)
                ).fetchone()
                if row is None:
                    log("[跳过] 库中不存在 id=%s" % cid)
                    continue
                rows.append(row)
            return rows
        return conn.execute(TARGET_SQL).fetchall()
    finally:
        conn.close()


def plan_cover(detail: dict) -> dict:
    """预演封面会写哪一张（走真实闸门，避免 apply 时才发现被拒）。"""
    rejected = []
    for key in enrich.SAIKR_COVER_KEYS:
        url = str(detail.get(key) or "").strip()
        if not url.lower().startswith("http"):
            continue
        try:
            ok, reason, _info = image_guard.check_image(url)
        except Exception as exc:  # noqa: BLE001 - 校验异常按不合格处理
            ok, reason = False, "校验异常：%s" % exc
        if ok:
            return {"url": url, "ok": True, "reason": "", "key": key}
        rejected.append("%s（%s）" % (url, reason or "不合格"))
    if rejected:
        return {"url": None, "ok": False, "reason": "；".join(rejected), "key": None}
    return {"url": None, "ok": False, "reason": "详情无图", "key": None}


def plan_dates(detail: dict) -> dict:
    out = {}
    for column, key in enrich.SAIKR_DATE_KEYS:
        moment = enrich._parse_api_time(detail.get(key))
        out[column] = moment.strftime("%Y-%m-%d") if moment else None
    return out


def dry_run(db_path: str, targets: list, *, delay, allow_stale: bool,
            preview_path: str) -> int:
    log("[模式] dry-run（只读库 + 抓取 + 校验，不写库、不调 AI）")
    log("[目标库] %s" % db_path)
    log("[计划] %d 条核心赛事\n" % len(targets))

    index = saikr_match.load_index()
    session = enrich._session()
    now = datetime.now(SHANGHAI)
    results = []
    try:
        for order, row in enumerate(targets):
            name = row["name"]
            log("─" * 72)
            log("#%s %s（下游数据 %s 条）" % (row["id"], name, row["children"]))
            hit = saikr_match.resolve(name, index=index)
            item = {"id": row["id"], "name": name, "contest_id": None,
                    "match": None, "match_score": None, "matched_name": None}
            if not hit:
                log("  赛氪匹配: ✗ 未命中（保持空壳，需人工补）")
                item["status"] = "no_match"
                results.append(item)
                continue
            item.update(contest_id=int(hit["contest_id"]), match=hit["method"],
                        match_score=round(float(hit["score"]), 3),
                        matched_name=hit["matched_name"])
            log("  赛氪匹配: ✓ contest_id=%s（%s %.2f，赛氪名「%s」）"
                % (hit["contest_id"], hit["method"], hit["score"], hit["matched_name"]))
            try:
                detail = enrich.fetch_detail(session, int(hit["contest_id"]))
            except Exception as exc:  # noqa: BLE001 - 单条失败不阻断
                log("  详情抓取: ✗ %s" % exc)
                item["status"] = "fetch_failed"
                results.append(item)
                continue

            stale = enrich.stale_reason(detail, now)
            item["stale"] = stale
            if stale and not allow_stale:
                log("  时效闸门: %s → 整条跳过（要写往年封面/对象加 --allow-stale）" % stale)
                item["status"] = "stale"
                results.append(item)
                continue
            if stale:
                log("  时效闸门: %s → 只补封面/参赛对象，日期列保持空（避免写入往年日期）" % stale)
            item["skip_dates"] = bool(stale)

            cover = plan_cover(detail)
            item["cover"] = cover
            if cover["ok"]:
                log("  封面: ✓ %s（字段 %s）" % (cover["url"], cover["key"]))
            elif cover["url"]:
                log("  封面: ✗ 被闸门拒绝（%s）→ 保持空，前端走兜底" % cover["reason"])
            else:
                log("  封面: — %s" % cover["reason"])

            dates = plan_dates(detail)
            item["dates"] = dates
            log("  日期: %s" % (
                "跳过（往年届次）" if stale else
                "reg %s ~ %s ｜ contest %s ~ %s" % (
                    dates["reg_start_date"] or "-", dates["reg_end_date"] or "-",
                    dates["contest_start_date"] or "-", dates["contest_end_date"] or "-")))

            raw = enrich.extract_fields(detail)
            texts = {k: (v.get("text") or "") if isinstance(v, dict) else str(v or "")
                     for k, v in raw.items()}
            item["raw_chars"] = {k: len(v) for k, v in texts.items()}
            item["raw_preview"] = {k: v[:80] for k, v in texts.items()}
            for field, label in (("participants", "参赛对象"), ("content_scope", "内容范围"),
                                 ("exam_schedule", "考试安排")):
                text = texts.get(field) or ""
                log("  %s: %s" % (label, ("规则抽取 %d 字，apply 时交 AI 提纯" % len(text))
                                  if text else "页面无对应章节"))
            item["status"] = "ready"
            results.append(item)

            if order < len(targets) - 1:
                wait = random.uniform(*delay)
                log("  [限速] %.1f 秒" % wait)
                time.sleep(wait)
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass

    ready = [r for r in results if r["status"] == "ready"]
    log("\n" + "═" * 72)
    log("预演汇总：可补全 %d 条 / 未命中 %d 条 / 抓取失败 %d 条"
        % (len(ready), sum(1 for r in results if r["status"] == "no_match"),
           sum(1 for r in results if r["status"] == "fetch_failed")))
    if ready:
        log("\n%3s %-30s %-25s %-6s %-6s %-8s" % ("id", "赛事", "赛氪命中", "封面", "日期", "对象"))
        for r in ready:
            log("%3s %-30s %-25s %-6s %-6s %-8s" % (
                r["id"], r["name"][:28],
                (r["matched_name"] or "")[:23],
                "✓" if r["cover"]["ok"] else "✗",
                "✓" if r["dates"]["reg_start_date"] else "-",
                "%d字" % r["raw_chars"].get("participants", 0)))
    skipped = [r for r in results if r["status"] != "ready"]
    if skipped:
        log("\n未处理：" + "；".join("#%s %s（%s）" % (r["id"], r["name"], r["status"])
                                    for r in skipped))

    os.makedirs(os.path.dirname(os.path.abspath(preview_path)), exist_ok=True)
    with open(preview_path, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": now.isoformat(),
                   "db_path": db_path, "items": results},
                  fh, ensure_ascii=False, indent=2)
    log("\n预览明细已写入：%s" % preview_path)
    log("确认无误后执行：venv\\Scripts\\python.exe fill_core_skeletons.py --apply --allow-production")
    return 0 if ready else 1


def _current_row(db_path: str, competition_id: int) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, name, image_url, participants, content_scope, exam_schedule, "
            "reg_start_date, reg_end_date, contest_start_date, contest_end_date "
            "FROM competitions WHERE id = ?", (competition_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def apply_one(db_path: str, competition_id: int, *, index: dict, session,
              allow_stale: bool, now, max_chars: int = enrich.MAX_FIELD_LEN) -> dict:
    """单条补全：名称匹配 -> 抓详情 -> AI 提纯 -> 只写空字段。"""
    row = _current_row(db_path, competition_id)
    if not row:
        return {"status": "missing"}
    name = row["name"]
    hit = saikr_match.resolve(name, index=index)
    if not hit:
        return {"status": "no_match", "reason": "赛氪索引未命中"}
    contest_id = int(hit["contest_id"])
    log("  赛氪匹配: contest_id=%s（%s %.2f）" % (contest_id, hit["method"], hit["score"]))

    detail = enrich.fetch_detail(session, contest_id)
    stale = enrich.stale_reason(detail, now)
    if stale and not allow_stale:
        return {"status": "stale", "reason": stale, "contest_id": contest_id}
    if stale:
        log("  时效闸门: %s → 跳过日期列" % stale)

    raw = enrich.extract_fields(detail)
    empty_fields = [field for field in enrich.DETAIL_FIELDS
                    if not str(row.get(field) or "").strip()]
    needed = {field: raw[field] for field in empty_fields if field in raw}
    written = []
    if needed:
        purified = enrich.purify_fields(name, needed, max_chars=max_chars)
        if purified:
            written = sorted(db.fill_competition_detail_text(
                competition_id, only_empty=True, db_path=db_path,
                **{field: value["text"] for field, value in purified.items()},
            ))
            for field in written:
                log("  ✓ %s：%d 字（AI 提纯）" % (field, len(purified[field]["text"])))
        else:
            log("  · AI 未产出可用短句，长文本保持空")
    else:
        if empty_fields:
            log("  · 空字段 %s 在详情页无对应章节（%s 已有值）"
                % ("/".join(empty_fields),
                   "/".join(f for f in enrich.DETAIL_FIELDS if f not in empty_fields) or "-"))
        else:
            log("  · 参赛对象/范围/安排 均已有值，无需补")

    patch = {}
    cover = plan_cover(detail)
    if cover["ok"] and not str(row.get("image_url") or "").strip():
        patch["image_url"] = cover["url"]
        log("  ✓ 封面：%s" % cover["url"])
    elif not cover["ok"]:
        log("  · 封面保持空：%s" % cover["reason"])

    if not stale:
        for column, key in enrich.SAIKR_DATE_KEYS:
            if str(row.get(column) or "").strip():
                continue
            moment = enrich._parse_api_time(detail.get(key))
            if moment is not None:
                patch[column] = moment.strftime("%Y-%m-%d")
                log("  ✓ 日期 %s = %s" % (column, patch[column]))
    else:
        log("  · 日期列保持空（往年届次，避免写错年份）")

    if patch:
        db.update_competition(competition_id, patch, db_path=db_path)

    status = "updated" if (written or patch) else "exists"
    return {"status": status, "contest_id": contest_id, "match": hit["method"],
            "written": written, "patch": sorted(patch), "stale": stale}


def apply_run(db_path: str, targets: list, *, delay, allow_stale: bool,
              max_chars: int = enrich.MAX_FIELD_LEN) -> int:
    if common.is_production_db(db_path):
        log("[备份] 生产库写前备份：%s" % common.backup_db(db_path))
    log("[模式] apply（AI 提纯 + 只写空字段）")
    log("[目标库] %s" % db_path)
    log("[计划] %d 条核心赛事\n" % len(targets))

    index = saikr_match.load_index()
    session = enrich._session()
    now = datetime.now(SHANGHAI)
    ok = fail = 0
    summary = []
    try:
        for order, row in enumerate(targets):
            cid, name = int(row["id"]), row["name"]
            log("─" * 72)
            log("#%s %s" % (cid, name))
            try:
                result = apply_one(db_path, cid, index=index, session=session,
                                   allow_stale=allow_stale, now=now,
                                   max_chars=max_chars)
            except Exception as exc:  # noqa: BLE001 - 单条失败不阻断
                log("  ✗ 异常：%s" % exc)
                result = {"status": "error", "reason": str(exc)}

            status = result.get("status")
            if status == "updated":
                ok += 1
            else:
                fail += 1
                log("  · 未写入：%s%s" % (status, ("（%s）" % result["reason"])
                                         if result.get("reason") else ""))
            summary.append({"id": cid, "name": name, **result})
            if order < len(targets) - 1:
                wait = random.uniform(*delay)
                log("  [限速] %.1f 秒" % wait)
                time.sleep(wait)
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass

    log("\n" + "═" * 72)
    log("写库汇总：成功 %d 条 / 未写入 %d 条" % (ok, fail))
    for r in summary:
        log("  #%-3s %-28s %s" % (r["id"], r["name"][:26], r["status"]))
    return 0 if fail == 0 else 2


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="核心赛事骨架（封面/日期/参赛对象）补全")
    p.add_argument("--db-path", default=DEFAULT_DB, help="目标库，默认 backend/competitions.db")
    p.add_argument("--ids", default="", help="只处理这些 id，逗号分隔")
    p.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    p.add_argument("--allow-production", action="store_true", help="允许写生产库（配合 --apply）")
    p.add_argument("--allow-stale", action="store_true",
                   help="往年届次（报名结束超 400 天）也补封面/对象，但日期列保持空")
    p.add_argument("--delay", default="3,5", help="赛氪请求间隔 min,max 秒，下限 3")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 条")
    p.add_argument("--max-chars", type=int, default=enrich.MAX_FIELD_LEN,
                   help="单字段最大字数，默认 %d" % enrich.MAX_FIELD_LEN)
    p.add_argument("--preview", default=DEFAULT_PREVIEW, help="dry-run 预览 JSON 输出路径")
    return p.parse_args(argv)


def main(argv=None) -> int:
    enrich._setup_console()
    args = parse_args(argv)
    try:
        delay = tuple(float(x) for x in args.delay.split(","))
        if len(delay) != 2:
            raise ValueError
    except ValueError:
        raise SystemExit("--delay 格式应为 min,max，例如 3,5")
    if min(delay) < 3:
        raise SystemExit("赛氪请求间隔不得低于 3 秒（默认 3,5）")

    ids = set()
    for part in args.ids.split(","):
        part = part.strip()
        if part:
            if not part.isdigit():
                raise SystemExit("--ids 只能传正整数：%r" % args.ids)
            ids.add(int(part))

    db_path = os.path.abspath(args.db_path)
    if not os.path.exists(db_path):
        raise SystemExit("数据库不存在：%s" % db_path)
    if args.apply and common.is_production_db(db_path) and not args.allow_production:
        raise SystemExit("拒绝写生产库：必须同时传 --allow-production")

    targets = load_targets(db_path, ids or None)
    if args.limit > 0:
        targets = targets[:args.limit]
    if not targets:
        log("没有需要补全的赛事（三字段全空且有下游数据的核心赛事为 0 条）")
        return 0

    if args.apply:
        return apply_run(db_path, targets, delay=delay, allow_stale=args.allow_stale,
                         max_chars=args.max_chars)
    return dry_run(db_path, targets, delay=delay, allow_stale=args.allow_stale,
                   preview_path=args.preview)


if __name__ == "__main__":
    raise SystemExit(main())