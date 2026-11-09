# -*- coding: utf-8 -*-
"""backfill_candidates_date.py —— 候选池「精确日期」补全（默认只读）

背景（双轨制）：
    competition_candidates 与主表一样有月份轨 + 日期轨两套字段；候选池里的存量数据
    只有月份，日期四列全空。本脚本按成本从低到高两条路走：

    ① 赛氪候选：raw_json 里本来就存着 regist_start_time / regist_end_time /
       contest_start_time / contest_end_time 四个时间戳，直接换算成北京时间日期——
       纯结构化推算，零网络、零模型、绝不可能编造。
    ② 其余候选（edu-roster 等）：用升级后的抽取 Prompt 抓 official_url / source_url，
       并把抽到的每个日期回原文做「字符串级」核对（_evidence）；原文里找不到这一天的
       一律丢掉，宁可不写也不编造。

红线：
    * 默认 dry-run，不写库；--apply 才写，且只补 NULL（已有值保留）；
    * 候选池不是主表，--apply 不需要生产库开闸，但会打印目标库路径；
    * 日期只写「原文/时间戳真的有」的那一端，缺的一端保持 NULL。

用法：
    .\\venv\\Scripts\\python.exe backfill_candidates_date.py                 # 只看结果
    .\\venv\\Scripts\\python.exe backfill_candidates_date.py --only saikr    # 只跑赛氪（秒级）
    .\\venv\\Scripts\\python.exe backfill_candidates_date.py --only llm --limit 10
    .\\venv\\Scripts\\python.exe backfill_candidates_date.py --apply         # 真写库
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import crawler_competitions as cc
import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
BJ = timezone(timedelta(hours=8))

DATE_FIELDS = ("reg_start_date", "reg_end_date",
               "contest_start_date", "contest_end_date")

# 赛氪时间戳 -> 日期字段
SAIKR_TS = (
    ("regist_start_time", "reg_start_date"),
    ("regist_end_time", "reg_end_date"),
    ("contest_start_time", "contest_start_date"),
    ("contest_end_time", "contest_end_date"),
)

_SQL = (
    "SELECT id, source, source_key, source_url, official_url, raw_name, norm_name, "
    "reg_start_month, reg_end_month, reg_cross_year, "
    "contest_start_month, contest_end_month, raw_json, status, "
    "reg_start_date, reg_end_date, contest_start_date, contest_end_date "
    "FROM competition_candidates "
    "WHERE reg_start_date IS NULL AND reg_end_date IS NULL "
    "  AND contest_start_date IS NULL AND contest_end_date IS NULL {extra} "
    "ORDER BY id DESC"
)


def _setup_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _ts_to_date(value):
    """秒级时间戳 -> 'YYYY-MM-DD'（北京时间）；0 / 越界 / 非数字一律 None。"""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        dt = datetime.fromtimestamp(ts, tz=BJ)
    except (OverflowError, OSError, ValueError):
        return None
    if dt.year < 2000 or dt.year > 2100:
        return None
    return dt.strftime("%Y-%m-%d")


def _saikr_dates(raw_json):
    """从赛氪 raw_json 推日期：{field: 'YYYY-MM-DD'}；解析不出就是空 dict。"""
    try:
        raw = json.loads(raw_json or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, field in SAIKR_TS:
        date_text = _ts_to_date(raw.get(key))
        if date_text:
            out[field] = date_text
    return out


def _evidence(body, date_text):
    """日期是否真的出现在原文里（复用 backfill_dates 的判定，避免两份实现漂移）。"""
    from backfill_dates import _evidence as core
    return core(body, date_text)


def _drop_unsupported(dates, body):
    """把原文里找不到佐证的日期剔掉（防模型编造）；返回 (保留, 丢弃)。"""
    kept, dropped = {}, {}
    for field, value in dates.items():
        if not value:
            continue
        if _evidence(body, value) == "有":
            kept[field] = value
        else:
            dropped[field] = value
    return kept, dropped


def _drop_stale(dates, today=None):
    """剔掉「往年届次」日期：官网常年挂着旧年份通知，抽出来的 2022 年日期是真有原文的，
    但对今年的赛事毫无意义。只保留 [今年-1, 今年+1] 区间内的日期。"""
    today = today or datetime.now(BJ)
    lo, hi = today.year - 1, today.year + 1
    kept, dropped = {}, {}
    for field, value in dates.items():
        try:
            year = int(str(value)[:4])
        except ValueError:
            dropped[field] = value
            continue
        if lo <= year <= hi:
            kept[field] = value
        else:
            dropped[field] = value
    return kept, dropped


def _plan_writes(row, dates):
    """只补 NULL：返回 {field: value} 与跨年标记。"""
    payload = {}
    for field in DATE_FIELDS:
        value = dates.get(field)
        if value and not row.get(field):
            payload[field] = value

    # 报名起止都在同一年 -> 跨年标记跟着日期走（仅在两个日期都拿得到时修）
    cross = None
    start = payload.get("reg_start_date") or row.get("reg_start_date")
    end = payload.get("reg_end_date") or row.get("reg_end_date")
    if start and end and len(start) == 10 and len(end) == 10:
        cross = 1 if start[:4] != end[:4] else 0
    return payload, cross


def _write(conn, row, payload, cross):
    if not payload:
        return []
    sets = ", ".join("%s = COALESCE(%s, ?)" % (f, f) for f in payload)
    params = list(payload.values())
    if cross is not None and row.get("reg_cross_year") != cross:
        sets += ", reg_cross_year = ?"
        params.append(cross)
    params.append(row["id"])
    conn.execute("UPDATE competition_candidates SET %s WHERE id = ?" % sets, params)
    return sorted(payload)


def _fetch_and_extract(url, max_chars):
    page = cc.fetch_competition_page(url, validate_image=False)
    body = page.get("body") or ""
    _raw, extracted = cc.extract_competition(page, max_chars)
    return body, extracted


def main(argv=None):
    _setup_console()
    ap = argparse.ArgumentParser(
        description="给候选池补精确日期（赛氪时间戳优先，其余走页面抽取 + 原文核对）")
    ap.add_argument("--only", choices=("all", "saikr", "llm"), default="all")
    ap.add_argument("--ids", default=None, help="逗号分隔的候选 id")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几条（0 = 全部）")
    ap.add_argument("--delay", type=float, default=3.5, help="联网抓取间隔秒数（默认 3.5）")
    ap.add_argument("--db-path", default=None, help="目标库（默认 backend/competitions.db）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    if args.db_path:
        db.DB_PATH = os.path.abspath(os.path.expanduser(args.db_path))

    extra = ""
    params = []
    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        extra = " AND id IN (%s)" % ",".join("?" for _ in ids)
        params = ids
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(_SQL.format(extra=extra), params).fetchall()]

    if args.only == "saikr":
        rows = [r for r in rows if _saikr_dates(r.get("raw_json"))]
    elif args.only == "llm":
        rows = [r for r in rows if not _saikr_dates(r.get("raw_json"))]
    if args.limit:
        rows = rows[:args.limit]

    print("[目标库] %s%s" % (db.DB_PATH, "" if args.apply else "（dry-run，不写库）"))
    print("[范围] %d 条候选，模式 %s，抓取间隔 %.1fs\n" % (
        len(rows), args.only, args.delay))
    if not rows:
        print("[空] 没有需要补日期的候选")
        return 0

    stats = {"saikr": 0, "llm_ok": 0, "llm_fail": 0, "no_source": 0,
             "dates_added": 0, "rows_updated": 0}
    results = []
    fetched = 0  # 已经联网抓取的次数，用来控制延迟

    with db.get_conn() as conn:
        for index, row in enumerate(rows, 1):
            rec = {"id": row["id"], "name": row.get("norm_name") or row.get("raw_name"),
                   "source": row.get("source"), "method": "", "dates": {},
                   "dropped": {}, "written": [], "note": ""}

            dates = _saikr_dates(row.get("raw_json"))
            if dates:
                rec["method"] = "saikr-raw_json"
                stats["saikr"] += 1
            else:
                url = (row.get("official_url") or "").strip()
                if not url:
                    src = (row.get("source_url") or "").strip()
                    # edu-roster 的 source_url 是「名单页」，不是赛事通知，抓了也没日期
                    url = src if src and "edu-roster" not in (row.get("source") or "") else ""
                if not url:
                    rec["note"] = "无可用页面（只有名单页）"
                    stats["no_source"] += 1
                    print("(%d/%d) id=%-3s %-24s 跳过：%s" % (
                        index, len(rows), row["id"], (rec["name"] or "")[:24], rec["note"]))
                    results.append(rec)
                    continue
                if fetched:
                    time.sleep(max(args.delay, 0))
                fetched += 1
                print("(%d/%d) id=%-3s %-24s 抓取：%s" % (
                    index, len(rows), row["id"], (rec["name"] or "")[:24], url[:80]))
                try:
                    body, extracted = _fetch_and_extract(url, args.max_chars)
                    raw_dates = {f: extracted.get(f) for f in DATE_FIELDS if extracted.get(f)}
                    evidenced, rec["dropped"] = _drop_unsupported(raw_dates, body)
                    dates, stale = _drop_stale(evidenced)
                    rec["dropped"].update(stale)
                    rec["method"] = "page-extract"
                    stats["llm_ok"] += 1
                    rec["note"] = "正文 %d 字" % len(body)
                except Exception as exc:  # noqa: BLE001 - 单条失败不阻断批量
                    rec["method"] = "page-extract"
                    rec["note"] = "失败：%s: %s" % (type(exc).__name__, exc)
                    stats["llm_fail"] += 1
                    print("        ✗ %s" % rec["note"])
                    results.append(rec)
                    continue

            payload, cross = _plan_writes(row, dates)
            rec["dates"] = dates
            if rec["dropped"]:
                print("        ! 丢弃：%s（原文无佐证或往年届次）" % "、".join(
                    "%s=%s" % (k, v) for k, v in sorted(rec["dropped"].items())))
            if not payload:
                rec["note"] = rec["note"] or "无新增（库里已有值或原文没给到日）"
                print("        - %s" % rec["note"])
                results.append(rec)
                continue

            if args.apply:
                rec["written"] = _write(conn, row, payload, cross)
                stats["rows_updated"] += 1
                stats["dates_added"] += len(rec["written"])
            else:
                rec["written"] = sorted(payload)
            print("        → %s%s" % (
                "、".join("%s=%s" % (k, payload[k]) for k in sorted(payload)),
                "（已写入）" if args.apply else "（计划写入）"))
            results.append(rec)

        if args.apply:
            conn.commit()

    print("\n" + "=" * 74)
    print("候选池汇总：处理 %d ｜ 赛氪时间戳 %d ｜ 页面抽取 %d（失败 %d）｜ 无页面 %d" % (
        len(results), stats["saikr"], stats["llm_ok"], stats["llm_fail"],
        stats["no_source"]))
    print("           %s %d 条，新增日期字段 %d 个" % (
        "已写入" if args.apply else "可写入", stats["rows_updated"],
        stats["dates_added"] or sum(len(r["written"]) for r in results)))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or os.path.join(
        WORKSPACE_DIR, "backfill_candidates_%s.json" % stamp)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"ran_at": stamp, "db": db.DB_PATH, "apply": bool(args.apply),
                   "stats": stats, "items": results}, fh,
                  ensure_ascii=False, indent=2, default=str)
    print("[报告] %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
