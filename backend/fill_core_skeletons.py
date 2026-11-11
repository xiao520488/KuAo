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

    # 5. 省级赛父赛事回退（只复制 参赛对象/内容范围，绝不写日期/封面/赛程）
    venv\\Scripts\\python.exe fill_core_skeletons.py --parent-fallback
    venv\\Scripts\\python.exe fill_core_skeletons.py --parent-fallback --ids 23,9,30

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
import re
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
SELECT c.id, c.name, c.discover_source, c.image_url, c.participants,
       c.content_scope, c.exam_schedule, c.reg_start_date,
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
                    "SELECT id, name, discover_source, image_url, participants, "
                    "content_scope, exam_schedule, reg_start_date, 0 AS children "
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


# ---------------------------------------------------------------------------
# 父赛事回退（省级赛 -> 国赛）
# ---------------------------------------------------------------------------
# 省级赛（如「计算机设计大赛·省级赛」「挑战杯省级…」）在赛氪索引里通常没有条目，
# 但它的参赛对象 / 内容范围与国赛高度重合。回退只复制这两类静态文本：
#   * 日期：绝对不写（省赛有自己的时间，宁缺勿错）；
#   * 封面：绝对不写（国赛海报不等于省赛海报）；
#   * 考试安排：不写（赛程里混着具体日期，同样有年份污染风险）。
PARENT_FALLBACK_FIELDS = ("participants", "content_scope")
PARENT_MATCH_MIN_SCORE = 0.92
_PARENT_SUFFIX_RE = re.compile(
    r"(?:[·•\-—_]?\s*(?:省级赛区|省级选拔赛|省级赛|省赛区|省赛|分赛区|赛区|省级))\s*$"
)
_PARENT_BRACKET_RE = re.compile(r"[（(]\s*(?:省级赛|省赛|省赛区|分赛区|赛区|省级)\s*[）)]\s*$")
_PARENT_INLINE_RE = re.compile(r"[·•\-—_]?\s*(?:省级选拔赛|省级赛|省赛区|省赛|省级)\s*")


def parent_variants(name: str) -> list:
    """由省级赛名称生成「可能的国赛名称」，按可信度从高到低去重。"""
    variants = []

    def add(text):
        text = (text or "").strip(" 　·•-—_")
        if len(text) >= 4 and text not in variants:
            variants.append(text)

    base = _PARENT_BRACKET_RE.sub("", name or "")
    base = _PARENT_SUFFIX_RE.sub("", base)
    add(base)
    inline = _PARENT_INLINE_RE.sub("", name or "")
    add(inline)
    # 「挑战杯」省级大学生… -> 「挑战杯」全国大学生…：国赛条目普遍带「全国」
    for text in (base, inline):
        if "全国" not in text and "大学生" in text:
            add(text.replace("大学生", "全国大学生", 1))
    return variants


def resolve_target(name: str, index: dict, allow_parent: bool) -> dict | None:
    """先按原名精确匹配，失败再按父赛事名回退。"""
    hit = saikr_match.resolve(name, index=index)
    if hit:
        return {"hit": hit, "mode": "direct", "variant": name}
    if not allow_parent:
        return None
    best = None
    for variant in parent_variants(name):
        if variant == name:
            continue
        hit = saikr_match.resolve(variant, index=index)
        if not hit:
            continue
        score = float(hit.get("score") or 0)
        if score < PARENT_MATCH_MIN_SCORE:
            continue
        if best is None or score > float(best["hit"].get("score") or 0):
            best = {"hit": hit, "mode": "parent", "variant": variant}
    return best


def dry_run(db_path: str, targets: list, *, delay, allow_stale: bool,
            allow_parent: bool, preview_path: str) -> int:
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
            item = {"id": row["id"], "name": name, "contest_id": None,
                    "match": None, "match_score": None, "matched_name": None,
                    "mode": "direct"}
            resolved = resolve_target(name, index, allow_parent)
            if resolved is None:
                log("  赛氪匹配: ✗ 未命中（保持空壳，需人工补）")
                if allow_parent:
                    log("  父赛事回退: 生成变体 %s 后仍未命中" % "、".join(parent_variants(name)[:3]))
                item["status"] = "no_match"
                results.append(item)
                continue
            hit = resolved["hit"]
            item["mode"] = resolved["mode"]
            if resolved["mode"] == "parent":
                empty_parent = [f for f in PARENT_FALLBACK_FIELDS
                                if not str(row[f] or "").strip()]
                if not empty_parent:
                    log("  父赛事回退: 命中 variant「%s」，但参赛对象/内容范围均已有值 → 无需写"
                        % resolved["variant"])
                    item["status"] = "complete"
                    results.append(item)
                    continue
                item["parent_variant"] = resolved["variant"]
                log("  父赛事回退: 「%s」未命中 → 变体「%s」命中 contest_id=%s（%.2f）"
                    % (name, resolved["variant"], hit["contest_id"], hit["score"]))
                log("  红线: 只复制 参赛对象/内容范围，日期、封面、赛程一律不写")
            else:
                log("  赛氪匹配: ✓ contest_id=%s（%s %.2f，赛氪名「%s」）"
                    % (hit["contest_id"], hit["method"], hit["score"], hit["matched_name"]))
            item.update(contest_id=int(hit["contest_id"]), match=hit["method"],
                        match_score=round(float(hit["score"]), 3),
                        matched_name=hit["matched_name"])
            try:
                detail = enrich.fetch_detail(session, int(hit["contest_id"]))
            except Exception as exc:  # noqa: BLE001 - 单条失败不阻断
                log("  详情抓取: ✗ %s" % exc)
                item["status"] = "fetch_failed"
                results.append(item)
                continue

            parent_mode = resolved["mode"] == "parent"
            stale = enrich.stale_reason(detail, now)
            item["stale"] = stale
            if stale and not allow_stale and not parent_mode:
                log("  时效闸门: %s → 整条跳过（要写往年封面/对象加 --allow-stale）" % stale)
                item["status"] = "stale"
                results.append(item)
                continue
            if stale:
                log("  时效闸门: %s → 只补封面/参赛对象，日期列保持空（避免写入往年日期）" % stale)
            item["skip_dates"] = bool(stale) or parent_mode

            if parent_mode:
                log("  封面: 跳过（父赛事海报不适用于省级赛）")
                item["cover"] = {"url": None, "ok": False, "reason": "父赛事回退不写封面", "key": None}
                log("  日期: 跳过（父赛事回退绝不写日期）")
                item["dates"] = {k: None for k in (
                    "reg_start_date", "reg_end_date", "contest_start_date", "contest_end_date")}
            else:
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
            if parent_mode:
                texts = {k: v for k, v in texts.items() if k in PARENT_FALLBACK_FIELDS}
            item["raw_chars"] = {k: len(v) for k, v in texts.items()}
            item["raw_preview"] = {k: v[:80] for k, v in texts.items()}
            for field, label in (("participants", "参赛对象"), ("content_scope", "内容范围"),
                                 ("exam_schedule", "考试安排")):
                if parent_mode and field == "exam_schedule":
                    log("  %s: 跳过（父赛事回退不写赛程）" % label)
                    continue
                text = texts.get(field) or ""
                log("  %s: %s" % (label, ("规则抽取 %d 字，apply 时交 AI 提纯" % len(text))
                                  if text else "页面无对应章节"))
            item["status"] = "ready"
            if parent_mode:
                fillable = {f: t for f, t in texts.items()
                            if t and not str(row[f] or "").strip()}
                item["fillable"] = sorted(fillable)
                if not fillable:
                    log("  · 父赛事详情没有可补章节，本次不写（保持原样）")
                    item["status"] = "complete"
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
              allow_stale: bool, allow_parent: bool, now,
              max_chars: int = enrich.MAX_FIELD_LEN) -> dict:
    """单条补全：名称匹配（可父赛事回退）-> 抓详情 -> AI 提纯 -> 只写空字段。"""
    row = _current_row(db_path, competition_id)
    if not row:
        return {"status": "missing"}
    name = row["name"]
    resolved = resolve_target(name, index, allow_parent)
    if resolved is None:
        return {"status": "no_match", "reason": "赛氪索引未命中"}
    hit = resolved["hit"]
    parent_mode = resolved["mode"] == "parent"
    contest_id = int(hit["contest_id"])
    if parent_mode:
        log("  父赛事回退: 变体「%s」-> contest_id=%s（%.2f）；只复制对象/范围"
            % (resolved["variant"], contest_id, hit["score"]))
    else:
        log("  赛氪匹配: contest_id=%s（%s %.2f）" % (contest_id, hit["method"], hit["score"]))

    if parent_mode:
        empty_parent = [f for f in PARENT_FALLBACK_FIELDS
                        if not str(row.get(f) or "").strip()]
        if not empty_parent:
            log("  · 参赛对象/内容范围均已有值，无需回退补全")
            return {"status": "complete", "contest_id": contest_id, "mode": "parent"}

    detail = enrich.fetch_detail(session, contest_id)
    stale = enrich.stale_reason(detail, now)
    if stale and not allow_stale and not parent_mode:
        return {"status": "stale", "reason": stale, "contest_id": contest_id}
    if stale:
        log("  时效闸门: %s → 跳过日期列" % stale)

    raw = enrich.extract_fields(detail)
    empty_fields = [field for field in enrich.DETAIL_FIELDS
                    if not str(row.get(field) or "").strip()]
    if parent_mode:
        empty_fields = [field for field in empty_fields
                        if field in PARENT_FALLBACK_FIELDS]
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
    elif parent_mode:
        log("  · 省级赛待补字段在父赛事详情里没有对应章节")
    else:
        if empty_fields:
            log("  · 空字段 %s 在详情页无对应章节（%s 已有值）"
                % ("/".join(empty_fields),
                   "/".join(f for f in enrich.DETAIL_FIELDS if f not in empty_fields) or "-"))
        else:
            log("  · 参赛对象/范围/安排 均已有值，无需补")

    patch = {}
    if parent_mode:
        log("  · 封面/日期/赛程：父赛事回退一律不写")
    else:
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
            "mode": resolved["mode"], "written": written, "patch": sorted(patch),
            "stale": stale}


def apply_run(db_path: str, targets: list, *, delay, allow_stale: bool,
              allow_parent: bool, max_chars: int = enrich.MAX_FIELD_LEN) -> int:
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
                                   allow_stale=allow_stale, allow_parent=allow_parent,
                                   now=now, max_chars=max_chars)
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
    p.add_argument("--parent-fallback", action="store_true", dest="parent_fallback",
                   help="省级赛未命中时回退到同名国赛：只复制参赛对象/内容范围，"
                        "绝不写日期、封面、赛程")
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
                         allow_parent=args.parent_fallback, max_chars=args.max_chars)
    return dry_run(db_path, targets, delay=delay, allow_stale=args.allow_stale,
                   allow_parent=args.parent_fallback, preview_path=args.preview)


if __name__ == "__main__":
    raise SystemExit(main())