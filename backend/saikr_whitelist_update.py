# -*- coding: utf-8 -*-
"""saikr_whitelist_update.py —— 白名单赛事每日自动更新（全系统唯一的爬虫）

只做一件事：把「84 项核心国赛白名单」里每一条，从赛氪详情接口拉一次最新详情，
把**时间、赛程、官网、简介**这些基础字段 UPDATE 回 competitions 表。

绝不触碰的东西（数据保留红线）
------------------------------
* ``awards`` / ``ideas`` / ``user_preparations`` / ``users`` / ``ai_*`` 等表：**一行不读不写**；
  写库前后各自数一次行数，不一致直接回滚并报错。
* ``competitions.image_url``：前端已统一用程序化封面，这里不再抓图，也不写入（保持 NULL）。
* ``competitions.name / category / level / track``：赛事身份字段，只由人工/白名单维护。
* ``competitions.intro``、以及**已存在的** ``summary``：文案字段，不覆盖（避免版权与风格回退）。

写入策略
--------
* 日期类（reg_* / contest_* / stage_schedule / last_checked_at）：API 给出合理新值时**刷新**，
  新一届赛事换了时间能自动跟上；不合理（占位符、年份离谱、往年届次）一律跳过。
* 官网、简介：**只补空**，不覆盖已有值。
* 默认 dry-run，`--apply` 才落盘；写生产库必须 `--apply --allow-production`（沿用旧红线）。
* 落盘前自动备份（SQLite backup API，含已提交 WAL 的完整快照）。

用法::

    ./venv/bin/python saikr_whitelist_update.py                  # 演练：只打印计划
    ./venv/bin/python saikr_whitelist_update.py --limit 3        # 只试前 3 条
    ./venv/bin/python saikr_whitelist_update.py --apply --allow-production   # cron 用
    ./venv/bin/python saikr_whitelist_update.py --ids 1,6,10     # 指定竞赛 id 重跑

退出码：0 正常；2 参数/目标库错误；3 备份失败；4 红线校验失败（已回滚）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

SHANGHAI = timezone(timedelta(hours=8))

# ---- 可写列（白名单，代码里也不允许出现别的列名）----
WRITABLE = (
    "reg_start_date", "reg_end_date",
    "reg_start_month", "reg_end_month", "reg_cross_year",
    "contest_start_date", "contest_end_date",
    "stage_schedule",
    "official_url", "summary",
    "last_checked_at",
)

# ---- 红线：这些表一行都不许变 ----
PROTECTED_TABLES = ("awards", "ideas", "user_preparations", "users", "ai_usage", "ai_jobs")

STALE_DAYS = 400          # 报名截止超过这么久 = 往年届次，跳过
DELAY_CHOICES = (3.0, 5.0)


def log(message: str) -> None:
    print(message, flush=True)


# ============================================================
# 目标库 / 备份 / 红线校验
# ============================================================

def resolve_db_path(raw: str) -> str:
    path = os.path.abspath(raw or os.path.join(BASE_DIR, "competitions.db"))
    if not os.path.exists(path):
        log("[错误] 目标库不存在：%s" % path)
        sys.exit(2)
    return path


def is_production(path: str) -> bool:
    return os.path.abspath(path) == os.path.join(BASE_DIR, "competitions.db")


def count_rows(conn: sqlite3.Connection, tables) -> dict:
    out = {}
    for table in tables:
        try:
            out[table] = int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
        except sqlite3.Error:
            out[table] = -1        # 表不存在也记录下来，避免误判
    return out


def backup_db(path: str) -> str:
    """SQLite backup API 备份（WAL 安全），返回备份文件路径。"""
    stamp = datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    dest = os.path.join(backup_dir, "competitions.db.bak-%s" % stamp)

    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
            dst.execute("PRAGMA integrity_check").fetchone()
        finally:
            dst.close()
    finally:
        src.close()
    log("[备份] %s（%.1f KB）" % (dest, os.path.getsize(dest) / 1024.0))
    return dest


# ============================================================
# 赛氪侧：映射 + 详情
# ============================================================

def load_saikr_map() -> dict:
    path = os.path.join(BASE_DIR, "saikr_map.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        int(item["competition_id"]): int(item["saikr_contest_id"])
        for item in payload.get("items") or []
        if item.get("competition_id") and item.get("saikr_contest_id")
    }


def resolve_contest_id(name: str, mapped: dict, cid: int, matcher) -> int | None:
    """先查 saikr_map.json，再用赛氪列表索引按名称解析。"""
    hit = mapped.get(cid)
    if hit:
        return hit
    try:
        found = matcher.resolve(name)
    except Exception as exc:  # noqa: BLE001
        log("  [映射] 解析失败 %s：%r" % (name, exc))
        return None
    if not found or not found.get("contest_id"):
        return None
    score = float(found.get("score") or 0)
    if score < 0.9:            # 匹配不够确信宁可不动，也不要写错赛事
        log("  [映射] 跳过（相似度 %.2f）%s -> %s" % (score, name, found.get("matched_name")))
        return None
    return int(found["contest_id"])


def _iso(dt) -> str | None:
    return dt.strftime("%Y-%m-%d") if dt else None


def _sane(dt, now: datetime) -> bool:
    """年份合理性：不能是占位符（-0001 等），也不能是 5 年前/3 年后的离谱值。"""
    if dt is None:
        return False
    return now.year - 1 <= dt.year <= now.year + 2


_STAGE_FROM_API = ("contest_stage",)


def build_patch(detail: dict, row: dict, now: datetime, parse_time) -> tuple[dict, list]:
    """把赛氪详情整理成只含可写列的补丁；返回 (patch, 说明)。"""
    patch: dict = {}
    notes: list[str] = []

    # ---- 1. 报名窗口（新一届换时间时刷新）----
    reg_start = parse_time(detail.get("regist_start_time"))
    reg_end = parse_time(detail.get("regist_end_time"))
    if _sane(reg_start, now):
        patch["reg_start_date"] = _iso(reg_start)
        patch["reg_start_month"] = reg_start.month
    if _sane(reg_end, now):
        patch["reg_end_date"] = _iso(reg_end)
        patch["reg_end_month"] = reg_end.month
    if _sane(reg_start, now) and _sane(reg_end, now):
        patch["reg_cross_year"] = 1 if reg_end.year != reg_start.year else 0

    # ---- 2. 比赛日期：API 的 contest_*_time 常是占位符，退回赛程表 ----
    c_start = parse_time(detail.get("contest_start_time"))
    c_end = parse_time(detail.get("contest_end_time"))
    if _sane(c_start, now):
        patch["contest_start_date"] = _iso(c_start)
    if _sane(c_end, now):
        patch["contest_end_date"] = _iso(c_end)

    stage_list = ((detail.get("contest_stage") or {}).get("list") or []) \
        if isinstance(detail.get("contest_stage"), dict) else []
    stages = []
    for item in stage_list:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        start = parse_time(item.get("start_time") or item.get("start_date"))
        end = parse_time(item.get("end_time") or item.get("end_date"))
        if not title or not (_sane(start, now) or _sane(end, now)):
            continue
        stages.append({
            "stage": title[:20],
            "start_date": _iso(start) if _sane(start, now) else None,
            "end_date": _iso(end) if _sane(end, now) else None,
            "note": "",
        })
    if stages:
        patch["stage_schedule"] = json.dumps(stages, ensure_ascii=False)
        starts = [s["start_date"] for s in stages if s["start_date"]]
        ends = [s["end_date"] for s in stages if s["end_date"]]
        if starts and "contest_start_date" not in patch:
            patch["contest_start_date"] = min(starts)
        if ends and "contest_end_date" not in patch:
            patch["contest_end_date"] = max(ends)

    # ---- 3. 官网：只补空 ----
    if not (row.get("official_url") or "").strip():
        for key in ("external_link", "old_url", "top_contest_url"):
            url = str(detail.get(key) or "").strip()
            if url.startswith("http"):
                patch["official_url"] = url
                notes.append("补官网")
                break

    # ---- 4. 简介：只补空，且用结构化字段拼一句，不搬正文（版权）----
    if not (row.get("summary") or "").strip():
        org = str(detail.get("organiser_name") or detail.get("organiser") or "").strip()
        level = str(detail.get("contest_level") or "").strip()
        category = str(detail.get("contest_class_first") or "").strip()
        bits = []
        if org:
            bits.append("由%s主办" % org[:40])
        if category:
            bits.append("面向高校学生的%s类赛事" % category[:20])
        if not bits and level:
            bits.append("%s赛事" % level[:20])
        if bits:
            patch["summary"] = "，".join(bits) + "。"
            notes.append("补简介")

    # ---- 5. 只看“值真的变了”的列，减少无意义写库 ----
    changed = {}
    for key, value in patch.items():
        if value is None:
            continue
        current = row.get(key)
        if isinstance(current, (int, float)) and isinstance(value, (int, float)):
            if int(current) == int(value):
                continue
        elif str(current or "") == str(value):
            continue
        changed[key] = value
    return changed, notes


# ============================================================
# 主流程
# ============================================================

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="白名单赛事每日自动更新（赛氪，唯一爬虫）")
    parser.add_argument("--db-path", default=os.path.join(BASE_DIR, "competitions.db"))
    parser.add_argument("--apply", action="store_true", help="真正写库（默认演练）")
    parser.add_argument("--allow-production", action="store_true", help="允许写生产库")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试用）")
    parser.add_argument("--ids", default="", help="只处理指定竞赛 id，逗号分隔")
    parser.add_argument("--delay", default="3,5", help="每次请求间隔区间（秒），如 3,5")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出汇总")
    args = parser.parse_args(argv)

    db_path = resolve_db_path(args.db_path)
    if is_production(db_path) and args.apply and not args.allow_production:
        log("[错误] 目标库是生产库，写库必须显式加 --apply --allow-production")
        return 2

    try:
        low, _, high = args.delay.partition(",")
        delay_low, delay_high = float(low), float(high or low)
    except ValueError:
        delay_low = delay_high = 4.0
    delay_mid = max(3.0, (delay_low + delay_high) / 2.0)

    import competition_whitelist as whitelist
    import enrich_saikr_details as enrich
    import saikr_match

    now = datetime.now(SHANGHAI)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, summary, official_url, image_url, reg_start_date, reg_end_date, "
            "reg_start_month, reg_end_month, reg_cross_year, contest_start_date, contest_end_date, "
            "stage_schedule, last_checked_at FROM competitions ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()

    wanted_ids = {int(x) for x in str(args.ids).replace("，", ",").split(",") if x.strip().isdigit()}
    targets = []
    for row in rows:
        if wanted_ids and int(row["id"]) not in wanted_ids:
            continue
        if not whitelist.is_whitelisted(row["name"]):
            continue                      # 白名单之外的一律不碰
        targets.append(row)
    if args.limit > 0:
        targets = targets[: args.limit]

    log("[目标] %s → 白名单赛事 %d 条%s" % (
        os.path.relpath(db_path, BASE_DIR), len(targets), "（演练，不写库）" if not args.apply else ""))

    mapped = load_saikr_map()
    session = enrich._session()
    plans = []
    skipped = []

    for index, row in enumerate(targets, 1):
        name = row["name"]
        contest_id = resolve_contest_id(name, mapped, int(row["id"]), saikr_match)
        if not contest_id:
            skipped.append((name, "无赛氪映射"))
            log("  %2d/%d 跳过 %s（无赛氪映射）" % (index, len(targets), name))
            continue
        try:
            detail = enrich.fetch_detail(session, contest_id)
        except Exception as exc:  # noqa: BLE001
            skipped.append((name, "详情请求失败"))
            log("  %2d/%d 跳过 %s（详情请求失败：%r）" % (index, len(targets), name, exc))
            time.sleep(delay_mid)
            continue

        if enrich.stale_reason(detail, now):
            skipped.append((name, enrich.stale_reason(detail, now)))
            log("  %2d/%d 跳过 %s（%s）" % (index, len(targets), name, enrich.stale_reason(detail, now)))
            time.sleep(delay_mid)
            continue

        patch, notes = build_patch(detail, row, now, enrich._parse_api_time)
        patch["last_checked_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        plans.append({"id": int(row["id"]), "name": name, "contest_id": contest_id,
                      "patch": patch, "notes": notes})
        log("  %2d/%d %s ← %s%s" % (
            index, len(targets), name, ", ".join(patch.keys()), ("（%s）" % "、".join(notes)) if notes else ""))
        time.sleep(delay_mid)

    # ---- 演练：只打印 ----
    if not args.apply:
        log("\n[演练] 将更新 %d 条，跳过 %d 条。加 --apply 落盘。" % (len(plans), len(skipped)))
        if args.json:
            print(json.dumps({"mode": "dry-run", "plans": plans, "skipped": skipped},
                             ensure_ascii=False, indent=2))
        return 0

    if not plans:
        log("\n[完成] 没有需要更新的赛事（全部跳过）。")
        return 0

    backup_path = backup_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        before = count_rows(conn, PROTECTED_TABLES)
        written = 0
        with conn:                                  # 事务：异常自动回滚
            for plan in plans:
                patch = {k: v for k, v in plan["patch"].items() if k in WRITABLE}
                if not patch:
                    continue
                sets = ", ".join("%s = ?" % key for key in patch)
                values = list(patch.values()) + [plan["id"]]
                cur = conn.execute("UPDATE competitions SET %s WHERE id = ?" % sets, values)
                written += cur.rowcount
            after = count_rows(conn, PROTECTED_TABLES)
            if before != after:
                raise RuntimeError("红线校验失败：受保护表行数变化 %s -> %s" % (before, after))
    except Exception as exc:  # noqa: BLE001
        conn.close()
        log("\n[失败] %r" % exc)
        log("[回滚] 已还原，未写入任何数据。备份位于 %s" % backup_path)
        return 4
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    log("\n[完成] 更新 %d 行 / %d 条赛事，跳过 %d 条。备份：%s" % (
        written, len(plans), len(skipped), os.path.basename(backup_path)))
    if args.json:
        print(json.dumps({"mode": "apply", "updated": written, "plans": plans, "skipped": skipped},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())