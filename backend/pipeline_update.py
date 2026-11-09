# -*- coding: utf-8 -*-
"""pipeline_update.py —— 赛氪 + 通知页统一清洗（默认预检，--apply 才写库）

链路：
  1) 赛氪 API  -> 封面图（过 check_image 闸门）+ 报名月份
  2) 通知页/新闻页 -> 抓正文 -> 大模型抽比赛月份（沿用「截止日≠比赛时间」红线）
  3) 汇总 -> 每条赛事最终会有图/有时间/仍为空，输出预检报告

写库（--apply）红线：
  * 目标库是生产库 competitions.db 时必须显式 --allow-production；
  * 写前自动备份（sqlite backup API + integrity_check + 行数校验）；
  * 只填 NULL：image_url 仅补空，reg_* / contest_* 逐字段仅补 NULL，绝不覆盖已有值；
  * 封面写入前重新跑 check_image()，不合格宁可不写；
  * 打印「将写 / 将跳过」清单后才动库。

用法：
  .\\venv\\Scripts\\python.exe pipeline_update.py --db-path .\\test_crawl.db
  .\\venv\\Scripts\\python.exe pipeline_update.py --from-report .\\workspace\\pipeline_report_prod.json
  .\\venv\\Scripts\\python.exe pipeline_update.py --from-report .\\workspace\\pipeline_report_prod.json --apply --allow-production
"""
import argparse
import hashlib
import json
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime

import requests

import crawler_competitions as cc
import crawler_news as news
from batch_update_competitions import check_image

SAIKR_INFO = "https://apiv4buffer.saikr.com/api/pc/contest/info"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")

MIN_BODY_CHARS = 300      # 正文太短（SPA/导航页）就不烧大模型额度
STALE_DAYS = 400          # 赛氪页面报名结束超过该天数 -> 视为往年届次，整条跳过

log = logging.getLogger("pipeline")


def _utf8_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def parse_dt(value):
    """赛氪时间字符串 -> datetime；0 / -0001 视为无效。"""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("-0001") or value.startswith("1970"):
        return None
    try:
        dt = datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return dt if dt.year >= 2000 else None


# ---------------------------------------------------------------------------
# 阶段 1：赛氪（封面 + 报名）
# ---------------------------------------------------------------------------

def fetch_saikr(contest_id):
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                         "Referer": "https://www.saikr.com/"})
    resp = sess.get(SAIKR_INFO, params={"contest_id": contest_id}, timeout=25)
    payload = resp.json()
    if payload.get("code") != 200:
        raise RuntimeError("赛氪 code=%s msg=%s" % (payload.get("code"), payload.get("msg")))
    return payload["data"]


def saikr_stage(item, current, now):
    """返回 {status, reg, image, note}；status = ok / stale / no_match / error。"""
    sid = item.get("saikr_contest_id")
    if not sid:
        return {"status": "no_match", "note": item.get("note") or "赛氪无对应赛事"}

    try:
        data = fetch_saikr(sid)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "note": "赛氪接口失败：%r" % exc}

    start = parse_dt(data.get("regist_start_time"))
    end = parse_dt(data.get("regist_end_time"))
    edition_year = item.get("edition_year")

    stale = None
    if end and (now - end).days > STALE_DAYS:
        stale = "往年届次（报名已于 %s 结束）" % end.date()
    elif edition_year and edition_year < now.year - 1:
        stale = "往年届次（标题年份 %s）" % edition_year
    if stale:
        return {"status": "stale", "note": stale, "saikr_name": data.get("contest_name")}

    reg = None
    if start and end:
        reg = {
            "start": start.month,
            "end": end.month,
            "cross_year": 1 if end.year > start.year else 0,
            "raw": "%s ~ %s" % (start.date(), end.date()),
            # 精确日期轨：赛氪结构化字段本身带年月日，直接抄，不经模型
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": end.strftime("%Y-%m-%d"),
        }

    # 比赛时间：赛氪结构化 contest_*_time 同样精确到日（无效值/占位返回 None）
    contest_start = parse_dt(data.get("contest_start_time"))
    contest_end = parse_dt(data.get("contest_end_time"))
    contest = None
    if contest_start or contest_end:
        contest = {
            "start": contest_start.month if contest_start else None,
            "end": contest_end.month if contest_end else None,
            "start_date": contest_start.strftime("%Y-%m-%d") if contest_start else None,
            "end_date": contest_end.strftime("%Y-%m-%d") if contest_end else None,
        }

    pic = (data.get("web_pic_big") or "").strip()
    image = {"url": pic, "ok": False, "reason": "赛氪无 web_pic_big", "size": None}
    if pic:
        ok, reason, info = check_image(pic)
        image = {
            "url": pic,
            "ok": ok,
            "reason": reason or "通过",
            "size": "%sx%s" % (info.get("width"), info.get("height")) if info.get("width") else "?",
        }
    return {"status": "ok", "saikr_name": data.get("contest_name"), "reg": reg,
            "contest": contest, "image": image, "note": ""}


# ---------------------------------------------------------------------------
# 阶段 2：通知页 / 新闻页（比赛时间）
# ---------------------------------------------------------------------------

def notice_stage(url, max_chars):
    """返回 {status, url, title, body_chars, contest, confidence, note}。"""
    out = {"status": "no_url", "url": url, "title": "", "body_chars": 0,
           "contest": None, "confidence": None, "note": ""}
    if not url:
        out["note"] = "无通知页/官网 URL"
        return out

    try:
        html = news.fetch_html(url, timeout=(8, 12), retries=1)
    except Exception as exc:  # noqa: BLE001
        out.update(status="error", note="抓取失败：%s" % type(exc).__name__)
        return out

    try:
        page = news.extract_page(html, url)
    except Exception as exc:  # noqa: BLE001
        out.update(status="error", note="正文抽取失败：%s" % type(exc).__name__)
        return out

    out["title"] = page.get("title") or ""
    out["body_chars"] = len(page.get("body") or "")
    if out["body_chars"] < MIN_BODY_CHARS:
        out["status"] = "empty"
        out["note"] = "正文仅 %d 字（SPA/首页），不调模型" % out["body_chars"]
        return out

    try:
        _raw, extracted = cc.extract_competition(page, max_chars)
    except Exception as exc:  # noqa: BLE001
        out.update(status="error", note="大模型失败：%s" % type(exc).__name__)
        return out

    start = extracted.get("contest_start_month")
    end = extracted.get("contest_end_month")
    out["confidence"] = extracted.get("confidence")
    if start or end:
        out.update(status="found", contest={
            "start": start,
            "end": end,
            # 2026-11-09：新 Prompt 会尽量给出精确到日的比赛时间；抽不到就保持 None
            "start_date": extracted.get("contest_start_date"),
            "end_date": extracted.get("contest_end_date"),
        })
        out["note"] = "模型给出比赛时间"
    else:
        out["status"] = "not_found"
        out["note"] = "正文无明确比赛时间（未把截止日当比赛时间）"
    return out


# ---------------------------------------------------------------------------
# 写库：备份 / 计划 / 落库（只填 NULL）
# ---------------------------------------------------------------------------

def _is_production(path):
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(PRODUCTION_DB_PATH)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_backup(db_path):
    """sqlite backup API 备份（兼容 WAL），校验完整性 + 行数。返回 (backup, rows)。"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = "%s.bak-%s" % (db_path, ts)
    n = 1
    while os.path.exists(backup):
        backup = "%s.bak-%s-%d" % (db_path, ts, n)
        n += 1
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()

    ro = sqlite3.connect("file:%s?mode=ro" % backup.replace("\\", "/"), uri=True)
    try:
        integ = ro.execute("PRAGMA integrity_check").fetchone()[0]
        rows = ro.execute("SELECT COUNT(*) FROM competitions").fetchone()[0]
    finally:
        ro.close()
    if integ != "ok":
        raise RuntimeError("备份完整性校验失败：%s" % integ)
    return backup, rows


def coverage(conn):
    row = conn.execute(
        "SELECT COUNT(*),"
        " COALESCE(SUM(CASE WHEN image_url IS NOT NULL AND image_url <> '' THEN 1 ELSE 0 END), 0),"
        " COALESCE(SUM(CASE WHEN contest_start_month IS NOT NULL"
        "      OR contest_end_month IS NOT NULL THEN 1 ELSE 0 END), 0),"
        " COALESCE(SUM(CASE WHEN reg_start_month IS NOT NULL"
        "      AND reg_end_month IS NOT NULL THEN 1 ELSE 0 END), 0)"
        " FROM competitions").fetchone()
    return {"total": row[0], "image": row[1], "contest_time": row[2], "reg_complete": row[3]}


def coverage_from_current(current):
    return {
        "total": len(current),
        "image": sum(1 for r in current.values() if (r.get("image_url") or "").strip()),
        "contest_time": sum(1 for r in current.values()
                            if r.get("contest_start_month") is not None
                            or r.get("contest_end_month") is not None),
        "reg_complete": sum(1 for r in current.values()
                            if r.get("reg_start_month") is not None
                            and r.get("reg_end_month") is not None),
    }


def build_ops(current, proposals):
    """proposals: {cid: {image, reg(3元组), contest(2元组)}} -> 只补 NULL 的写计划。"""
    ops, skipped, warnings = [], [], []
    for cid in sorted(proposals):
        row = current.get(cid)
        if row is None:
            skipped.append((cid, "-", "库里不存在"))
            continue
        prop = proposals[cid]

        img = (prop.get("image") or "").strip()
        old_img = (row.get("image_url") or "").strip()
        if img and not old_img:
            ops.append((cid, "image_url", old_img, img, "saikr"))
        elif img and old_img != img:
            skipped.append((cid, "image_url", "库内已有封面，保留"))

        reg = prop.get("reg")
        if reg:
            reg_filled = 0
            for i, field in enumerate(("reg_start_month", "reg_end_month", "reg_cross_year")):
                new = reg[i] if i < len(reg) else None
                if new is None:
                    continue
                old = row.get(field)
                if old is None:
                    ops.append((cid, field, old, new, "saikr"))
                    reg_filled += 1
                elif old != new:
                    skipped.append((cid, field, "库内已有 %r，保留（来源 %r）" % (old, new)))
            if reg_filled and (row.get("reg_start_month") is None) != (row.get("reg_end_month") is None):
                warnings.append("id=%s 报名月份原本只缺一半，按「只补 NULL」仅补缺失字段，"
                                "结果可能混合两个来源，建议人工复核" % cid)

        contest = prop.get("contest")
        if contest:
            for i, field in enumerate(("contest_start_month", "contest_end_month")):
                new = contest[i] if i < len(contest) else None
                if new is None:
                    continue
                old = row.get(field)
                if old is None:
                    ops.append((cid, field, old, new, "notice"))
                elif old != new:
                    skipped.append((cid, field, "库内已有 %r，保留（来源 %r）" % (old, new)))

        # ---- 精确日期轨（2026-11-09）：同样只补 NULL ----
        for key, fields, src in (
            ("reg_dates", ("reg_start_date", "reg_end_date"), "saikr"),
            ("contest_dates", ("contest_start_date", "contest_end_date"), "notice"),
        ):
            dates = prop.get(key)
            if not dates:
                continue
            for i, field in enumerate(fields):
                new = dates[i] if i < len(dates) else None
                if not new:
                    continue
                old = row.get(field)
                if old is None:
                    ops.append((cid, field, old, new, src))
                elif old != new:
                    skipped.append((cid, field, "库内已有 %r，保留（来源 %r）" % (old, new)))
    return ops, skipped, warnings


def apply_ops(db_path, ops, now_iso):
    """逐字段 UPDATE，WHERE 里再带一次 NULL 闸门，双保险。"""
    conn = sqlite3.connect(db_path)
    applied, races = [], []
    try:
        with conn:
            for cid, field, old, new, src in ops:
                guard = "(image_url IS NULL OR image_url = '')" if field == "image_url" \
                    else "(%s IS NULL)" % field
                cur = conn.execute(
                    "UPDATE competitions SET %s = ?, discover_source = ?, last_checked_at = ? "
                    "WHERE id = ? AND %s" % (field, guard),
                    (new, src, now_iso, cid))
                (applied if cur.rowcount == 1 else races).append((cid, field, old, new, src))
    finally:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        conn.close()
    return applied, races


def run_write(args, db_path, current, proposals, now):
    ops, skipped, warnings = build_ops(current, proposals)
    n_img = sum(1 for o in ops if o[1] == "image_url")
    n_reg = sum(1 for o in ops if o[1].startswith("reg_"))
    n_ct = len(ops) - n_img - n_reg
    before = coverage_from_current(current)

    print("[模式] %s｜生产库开关：%s" % (
        "APPLY 真实写库" if args.apply else "dry-run（不写库）",
        "允许" if args.allow_production else "未开启"))
    print("[计划] 待写 %d 个字段（封面 %d / 报名 %d / 比赛时间 %d），涉及 %d 条赛事；"
          "保留或跳过 %d 项" % (len(ops), n_img, n_reg, n_ct,
                           len({o[0] for o in ops}), len(skipped)))
    for cid, field, old, new, src in ops:
        print("  [将写] id=%-3s %-19s %r -> %r（来源 %s）" % (cid, field, old, new, src))
    for w in warnings:
        print("  [注意] %s" % w)
    if skipped:
        show = skipped[:6]
        for cid, field, why in show:
            print("  [跳过] id=%-3s %-19s %s" % (cid, field, why))
        if len(skipped) > len(show):
            print("  [跳过] ……其余 %d 项（见报告 JSON）" % (len(skipped) - len(show)))

    if not ops:
        print("\n[结束] 没有可写字段（全部已有值或闸门不合格），库未改动。")
        return 0
    if not args.apply:
        print("\n[结束] dry-run：以上为写入计划，未动库。加 --apply 执行。")
        return 0

    # 封面写前复检（URL 可能已失效/换图）
    final_ops = []
    for op in ops:
        cid, field, _old, new, _src = op
        if field == "image_url":
            ok, reason, info = check_image(new)
            if not ok:
                skipped.append((cid, field, "写前复检未过：%s" % reason))
                print("  [拒收] id=%-3s 封面复检未过：%s" % (cid, reason))
                continue
            print("  [复检] id=%-3s 封面通过（%sx%s）" % (
                cid, info.get("width"), info.get("height")))
        final_ops.append(op)
    if not final_ops:
        print("\n[结束] 复检后没有可写字段，库未改动。")
        return 0

    backup, rows = make_backup(db_path)
    print("\n[备份] %s（%d 行，integrity_check=ok）" % (backup, rows))

    applied, races = apply_ops(db_path, final_ops, now.isoformat(timespec="seconds"))
    print("[写入] 成功 %d / 失败 %d" % (len(applied), len(races)))
    for cid, field, _old, _new, _src in races:
        print("  [未写入] id=%-3s %s（已被填过？）" % (cid, field))

    ro = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path).replace("\\", "/"), uri=True)
    try:
        after = coverage(ro)
    finally:
        ro.close()
    digest = sha256_of(db_path)

    total = after["total"] or 1
    print("\n[覆盖] 有图 %d/%d（%.1f%%）｜有比赛时间 %d/%d（%.1f%%）｜报名完整 %d/%d" % (
        after["image"], total, after["image"] * 100.0 / total,
        after["contest_time"], total, after["contest_time"] * 100.0 / total,
        after["reg_complete"], total))
    print("[SHA-256] %s" % digest)

    out = os.path.join("workspace", "pipeline_apply_%s.json" % now.strftime("%Y%m%d-%H%M%S"))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"applied_at": now.isoformat(timespec="seconds"), "db": db_path,
                   "backup": backup, "backup_rows": rows, "sha256": digest,
                   "before": before, "after": after, "applied": applied,
                   "skipped": skipped, "warnings": warnings},
                  fh, ensure_ascii=False, indent=2)
    print("[报告] %s" % out)
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="赛氪 + 通知页 统一清洗预检（只读）")
    ap.add_argument("--map", default="saikr_map.json")
    ap.add_argument("--url-map", default="url-map.json")
    ap.add_argument("--db-path", default="test_crawl.db")
    ap.add_argument("--ids", default=None, help="逗号分隔；默认全部映射赛事")
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-saikr", action="store_true")
    ap.add_argument("--skip-notice", action="store_true")
    ap.add_argument("--saikr-delay", default="3,5", help="赛氪请求间隔秒，如 3,5")
    ap.add_argument("--notice-delay", default="5,10", help="通知页请求间隔秒，如 5,10")
    ap.add_argument("--out", default="workspace/pipeline_report.json")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--allow-production", action="store_true",
                    help="允许写生产库 competitions.db（必须同时 --apply）")
    ap.add_argument("--from-report", default=None,
                    help="复用已保存的预检报告，跳过联网与大模型（仅做计划/写库）")
    args = ap.parse_args(argv)
    _utf8_stdout()
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")

    target = os.path.abspath(args.db_path)
    if args.apply and _is_production(target) and not args.allow_production:
        print("[拒绝] 目标是生产库：%s；必须显式加 --allow-production" % target)
        return 2
    if not os.path.exists(target):
        print("[拒绝] 目标库不存在：%s" % target)
        return 2

    dmin, dmax = [float(x) for x in args.saikr_delay.split(",")]
    nmin, nmax = [float(x) for x in args.notice_delay.split(",")]

    doc = json.load(open(args.map, encoding="utf-8"))
    url_map = json.load(open(args.url_map, encoding="utf-8")) if os.path.exists(args.url_map) else {}
    items = doc["items"]
    if args.ids:
        wanted = {int(x) for x in args.ids.split(",") if x.strip()}
        items = [x for x in items if x["competition_id"] in wanted]
    if args.limit:
        items = items[:args.limit]

    uri = "file:%s?mode=ro" % target.replace("\\", "/")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    current = {r["id"]: dict(r) for r in conn.execute(
        "select id,name,category,official_url,image_url,reg_start_month,reg_end_month,"
        "reg_cross_year,contest_start_month,contest_end_month,"
        "reg_start_date,reg_end_date,contest_start_date,contest_end_date,"
        "discover_source "
        "from competitions")}
    conn.close()

    now = datetime.now()
    print("[目标库] %s%s" % (args.db_path, "" if args.apply else "（只读）"))

    if args.from_report:
        raw = json.load(open(args.from_report, encoding="utf-8"))
        proposals = {}
        for it in raw.get("items", []):
            cid = it.get("competition_id")
            reg = it.get("reg")
            contest = it.get("contest")
            proposals[cid] = {"image": it.get("image"),
                              "reg": tuple(reg) if reg else None,
                              "contest": tuple(contest) if contest else None,
                              "reg_dates": (it.get("proposed") or {}).get("reg_dates"),
                              "contest_dates": (it.get("proposed") or {}).get("contest_dates")}
        print("[来源] 复用预检报告 %s（不联网、不调模型）" % args.from_report)
        st = raw.get("stats") or {}
        if st:
            print("[预检统计] 封面新增 %s / 比赛时间新增 %s / 报名冲突 %s（冲突一律保留库内值）"
                  % (st.get("image_add"), st.get("time_add"), st.get("reg_conflict")))
        print()
        return run_write(args, target, current, proposals, now)

    print("[范围] %d 条；赛氪=%s 通知页=%s" % (
        len(items), "跳过" if args.skip_saikr else "启用", "跳过" if args.skip_notice else "启用"))
    print("[红线] 只填 NULL；比赛时间只认比赛实际举办时间\n")

    report = []
    for idx, item in enumerate(items, 1):
        cid = item["competition_id"]
        row = current.get(cid)
        if row is None:
            print("  id=%-3s 库里不存在，跳过" % cid)
            continue

        saikr = {"status": "skipped", "note": "按参数跳过"}
        if not args.skip_saikr:
            saikr = saikr_stage(item, row, now)
            if idx < len(items):
                time.sleep(random.uniform(dmin, dmax))

        notice_url = url_map.get(str(cid)) or row.get("official_url") or ""
        notice_src = "url-map" if str(cid) in url_map else "official_url"
        notice = {"status": "skipped", "note": "按参数跳过", "url": notice_url}
        if not args.skip_notice:
            notice = notice_stage(notice_url, args.max_chars)
            notice["source"] = notice_src
            if idx < len(items):
                time.sleep(random.uniform(nmin, nmax))

        # ---- 合并（只填空，不覆盖库里已有值）----
        image_final = row.get("image_url")
        image_action = "keep"
        if not image_final and saikr.get("image", {}).get("ok"):
            image_final = saikr["image"]["url"]
            image_action = "add"
        elif not image_final and saikr.get("status") == "ok" and saikr.get("image", {}).get("url"):
            image_action = "reject"

        reg_final = (row.get("reg_start_month"), row.get("reg_end_month"), row.get("reg_cross_year"))
        reg_action = "keep"
        if saikr.get("reg"):
            r = saikr["reg"]
            reg_final = (r["start"], r["end"], r["cross_year"])
            reg_action = "add" if (row.get("reg_start_month"), row.get("reg_end_month"),
                                   row.get("reg_cross_year")) != reg_final else "same"

        contest_final = (row.get("contest_start_month"), row.get("contest_end_month"))
        contest_action = "keep"
        if not any(contest_final) and notice.get("contest"):
            c = notice["contest"]
            contest_final = (c["start"], c["end"])
            contest_action = "add"
        elif not any(contest_final) and saikr.get("contest"):
            c = saikr["contest"]
            contest_final = (c.get("start"), c.get("end"))
            contest_action = "add_saikr"
        elif notice.get("contest") and any(contest_final):
            contest_action = "keep_existing"

        # ---- 精确日期（第二轨）：赛氪结构化优先，通知页模型次之；只补空 ----
        reg_dates_final = (row.get("reg_start_date"), row.get("reg_end_date"))
        reg_dates_action = "keep"
        saikr_reg = saikr.get("reg") or {}
        if (saikr_reg.get("start_date") or saikr_reg.get("end_date")):
            reg_dates_final = (saikr_reg.get("start_date"), saikr_reg.get("end_date"))
            reg_dates_action = "add" if (row.get("reg_start_date"),
                                         row.get("reg_end_date")) != reg_dates_final else "same"

        contest_dates_final = (row.get("contest_start_date"), row.get("contest_end_date"))
        contest_dates_action = "keep"
        if not any(contest_dates_final):
            for candidate, src in ((notice.get("contest") or {}, "notice"),
                                   (saikr.get("contest") or {}, "saikr")):
                pair = (candidate.get("start_date"), candidate.get("end_date"))
                if any(pair):
                    contest_dates_final = pair
                    contest_dates_action = "add_%s" % src
                    break
        elif (notice.get("contest") or saikr.get("contest")) and any(contest_dates_final):
            contest_dates_action = "keep_existing"

        has_image = bool(image_final)
        has_time = any(contest_final)
        status = {(True, True): "full", (True, False): "image_only",
                  (False, True): "time_only", (False, False): "none"}[(has_image, has_time)]

        rec = {
            "competition_id": cid, "name": row["name"], "category": row["category"],
            "saikr": saikr, "notice": notice,
            "current": {k: row.get(k) for k in
                        ("image_url", "reg_start_month", "reg_end_month", "reg_cross_year",
                         "contest_start_month", "contest_end_month",
                         "reg_start_date", "reg_end_date",
                         "contest_start_date", "contest_end_date")},
            "proposed": {
                "image_url": image_final, "image_action": image_action,
                "reg": reg_final, "reg_action": reg_action,
                "reg_dates": reg_dates_final, "reg_dates_action": reg_dates_action,
                "contest": contest_final, "contest_action": contest_action,
                "contest_dates": contest_dates_final,
                "contest_dates_action": contest_dates_action,
            },
            "status": status,
        }
        report.append(rec)

        # ---- 控制台一行 ----
        if image_action == "add":
            cover_txt = "新增"
        elif image_action == "reject":
            cover_txt = "闸门拒收"
        elif image_final:
            cover_txt = "已有"
        else:
            cover_txt = {"stale": "往年跳过", "no_match": "无映射",
                         "error": "接口失败", "skipped": "跳过"}.get(saikr.get("status"), "无图")
        time_txt = {"add": "新增", "add_saikr": "新增(赛氪)", "keep": "已有",
                    "keep_existing": "保留"}[contest_action]
        if not has_time:
            time_txt = "仍缺"
        date_txt = ""
        if contest_dates_action.startswith("add"):
            date_txt = " +日(%s)" % ("~".join(
                str(x)[5:] if x else "?" for x in contest_dates_final))
        print("  %-3s %-24s 封面:%-6s 报名:%s-%s  比赛:%-4s(%s%s)  页面:%s" % (
            cid, (row["name"] or "")[:22], cover_txt,
            reg_final[0], reg_final[1], time_txt,
            ("%s-%s" % contest_final if has_time else "空"), date_txt,
            (notice.get("body_chars") or 0)))

    # ---- 汇总 ----
    def cnt(pred):
        return sum(1 for r in report if pred(r))

    stats = {
        "total": len(report),
        "saikr_ok": cnt(lambda r: r["saikr"].get("status") == "ok"),
        "saikr_stale": cnt(lambda r: r["saikr"].get("status") == "stale"),
        "saikr_no_match": cnt(lambda r: r["saikr"].get("status") == "no_match"),
        "cover_added": cnt(lambda r: r["proposed"]["image_action"] == "add"),
        "cover_kept": cnt(lambda r: r["proposed"]["image_action"] == "keep" and r["proposed"]["image_url"]),
        "cover_rejected": cnt(lambda r: r["proposed"]["image_action"] == "reject"),
        "cover_missing": cnt(lambda r: not r["proposed"]["image_url"]),
        "time_added": cnt(lambda r: r["proposed"]["contest_action"] in ("add", "add_saikr")),
        "date_added": cnt(lambda r: r["proposed"]["contest_dates_action"].startswith("add")),
        "reg_date_added": cnt(lambda r: r["proposed"]["reg_dates_action"] == "add"),
        "time_kept": cnt(lambda r: bool(r["current"]["contest_start_month"] or r["current"]["contest_end_month"])),
        "time_missing": cnt(lambda r: not any(r["proposed"]["contest"])),
        "full": cnt(lambda r: r["status"] == "full"),
        "image_only": cnt(lambda r: r["status"] == "image_only"),
        "time_only": cnt(lambda r: r["status"] == "time_only"),
        "none": cnt(lambda r: r["status"] == "none"),
    }

    print("\n" + "=" * 78)
    print("汇总（%d 条）" % stats["total"])
    print("  封面：新增 %(cover_added)d / 已有 %(cover_kept)d / 闸门拒收 %(cover_rejected)d / 仍空 %(cover_missing)d（含拒收）" % stats)
    print("  比赛时间：新增 %(time_added)d / 已有 %(time_kept)d / 仍空 %(time_missing)d" % stats)
    print("  精确日期：比赛日新增 %(date_added)d / 报名日新增 %(reg_date_added)d" % stats)
    print("  最终状态：图+时间 %(full)d / 只有图 %(image_only)d / 只有时间 %(time_only)d / 都没有 %(none)d" % stats)
    print("  赛氪：成功 %(saikr_ok)d / 往年跳过 %(saikr_stale)d / 无映射 %(saikr_no_match)d" % stats)

    miss_img = [r["competition_id"] for r in report if not r["proposed"]["image_url"]]
    miss_time = [r["competition_id"] for r in report if not any(r["proposed"]["contest"])]
    print("\n  仍无封面 id：%s" % (miss_img or "无"))
    print("  仍无比赛时间 id：%s" % (miss_time or "无"))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"ran_at": now.isoformat(timespec="seconds"), "db": args.db_path,
                   "stats": stats, "items": report}, fh, ensure_ascii=False, indent=2)
    print("\n[报告] %s" % args.out)

    if args.apply:
        proposals = {r["competition_id"]: {"image": r["proposed"]["image_url"],
                                           "reg": r["proposed"]["reg"],
                                           "contest": r["proposed"]["contest"],
                                           "reg_dates": r["proposed"]["reg_dates"],
                                           "contest_dates": r["proposed"]["contest_dates"]}
                     for r in report}
        print()
        return run_write(args, target, current, proposals, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
