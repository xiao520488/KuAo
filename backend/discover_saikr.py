# -*- coding: utf-8 -*-
"""discover_saikr.py —— 赛氪每日低频增量发现（只写候选表）

范围（用户 2026-11-08 拍板）：
  * 每天只抓列表页前 2 页，每页 10 条 = 最多 20 条；
  * 请求间隔 3~5 秒（red line 最低 3 秒；robots.txt 的 Crawl-delay 是 10，
    本脚本按用户要求取 3~5 秒，若怕封禁可用 --delay 8,10）；
  * 只提取列表字段（名称/封面/级别/报名窗口/学科），不抓详情正文；
  * 全部写入 competition_candidates，绝不自动写 competitions。

去重：
  * UNIQUE(source, source_key) + 名称归一化/相似度双保险；
  * 候选表或主表已有同名/高度相似赛事 -> 跳过。

安全：
  * 默认 dry-run；--apply 才写候选表；
  * 默认目标库 test_crawl.db；写生产库必须 --apply --allow-production 且自动备份；
  * 每日守卫：同一天重复 --apply 会被拒绝，确需重跑加 --force。

用法：
  .\\venv\\Scripts\\python.exe discover_saikr.py
  .\\venv\\Scripts\\python.exe discover_saikr.py --apply
  .\\venv\\Scripts\\python.exe discover_saikr.py --apply --force --delay 8,10
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import db
import discover_common as common
import competition_whitelist as whitelist

SAIKR_LIST = "https://apiv4buffer.saikr.com/api/pc/contest/lists"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
SHANGHAI = timezone(timedelta(hours=8))

STALE_DAYS = 400          # 报名结束超过 400 天 -> 往年届次，整条跳过
MAX_PAGES = 2             # 每日只允许前 2 页
MAX_ITEMS = 20            # 每日最多 20 条
STATE_PATH = os.path.join(common.WORKSPACE_DIR, "saikr_daily_state.json")

# 赛氪学科二级分类 -> 现有类别（只映射到库里已有的分类，未知保持「待确认」）
CLASS_HINTS = {
    "math": "数学建模",
    "computer": "计算机综合与软件",
    "programming": "程序设计",
    "robot": "机器人",
    "ai": "安全与人工智能",
    "security": "安全与人工智能",
    "iot": "物联网/嵌入式/ICT",
}


def log(message: str) -> None:
    print(message, flush=True)


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.saikr.com/",
    })
    return session


def fetch_list_page(session: requests.Session, page: int,
                    limit: int, retries: int = 2) -> list:
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(
                SAIKR_LIST,
                params={"page": page, "limit": limit, "sort": 2},
                timeout=25,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") != 200:
                raise RuntimeError("赛氪 code=%s msg=%s" % (
                    payload.get("code"), payload.get("msg")))
            data = payload.get("data") or {}
            return list(data.get("list") or [])
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError("赛氪列表请求失败：%r" % last_error)


def to_datetime(value):
    """Unix 秒 -> 东八区 datetime；0 / -1 / 非法值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        stamp = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    try:
        dt = datetime.fromtimestamp(stamp, tz=SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None
    return dt if dt.year >= 2000 else None


def detail_url(item: dict) -> str:
    slug = str(item.get("contest_url") or "").strip()
    if not slug:
        return ""
    if slug.startswith("http://") or slug.startswith("https://"):
        return slug
    return "https://www.saikr.com/" + slug.lstrip("/")


def clean_display_name(name: str) -> str:
    """去掉赛氪列表常见的营销前后缀，保留赛事正式名称。"""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    text = re.sub(r"^(?:【[^】]*】|\[[^\]]*\])\s*", "", text)
    text = re.sub(r"\s*(?:【[^】]*】|\[[^\]]*\])\s*$", "", text)
    return text.strip()


def build_candidate(item: dict, now: datetime) -> tuple:
    """返回 (候选 dict, None) 或 (None, 跳过原因)。"""
    name = clean_display_name(item.get("contest_name"))
    if len(name) < 4:
        return None, "名称缺失/过短"
    # 白名单闸门（2026-11-11）：只收教育部 84 项核心国赛，非白名单/省级一律不进候选池
    allowed, gate_reason = whitelist.gate(name)
    if not allowed:
        return None, "非核心赛：%s" % gate_reason
    try:
        if int(item.get("is_exam") or 0) == 1:
            return None, "考试类"
    except (TypeError, ValueError):
        pass

    contest_id = item.get("contest_id")
    if not contest_id:
        return None, "缺少 contest_id"

    reg_start = to_datetime(item.get("regist_start_time"))
    reg_end = to_datetime(item.get("regist_end_time"))
    if reg_end and (now - reg_end).days > STALE_DAYS:
        return None, "往年届次"
    if reg_start and reg_start.year < now.year - 2:
        return None, "往年届次"

    contest_start = to_datetime(item.get("contest_start_time"))
    contest_end = to_datetime(item.get("contest_end_time"))

    reg_cross = 1 if (reg_start and reg_end and reg_end.year > reg_start.year) else 0
    category = common.guess_category(name)
    if category == "待确认":
        category = CLASS_HINTS.get(
            str(item.get("contest_class_second") or "").strip().lower(),
            "待确认",
        )

    candidate = {
        "source": "saikr",
        "source_key": "saikr:%s" % contest_id,
        "source_url": detail_url(item) or None,
        "raw_name": name,
        "norm_name": db.normalize_competition_name(name),
        # 列表接口没有官网外链；按映射规则不使用 saikr 域名冒充官网
        "official_url": None,
        "image_url": str(item.get("thumb_pic") or "").strip() or None,
        "level_label": str(item.get("level_name") or "").strip() or None,
        "category_hint": category,
        "reg_start_month": reg_start.month if reg_start else None,
        "reg_end_month": reg_end.month if reg_end else None,
        "reg_cross_year": reg_cross,
        "contest_start_month": contest_start.month if contest_start else None,
        "contest_end_month": contest_end.month if contest_end else None,
        "raw_json": {
            **item,
            "_fetched_at": now.isoformat(timespec="seconds"),
        },
    }
    return candidate, None


def check_daily_guard(force: bool) -> None:
    if force or not os.path.exists(STATE_PATH):
        return
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return
    if state.get("date") == datetime.now().date().isoformat():
        raise SystemExit(
            "今日已抓取过赛氪（%s，%s 条）。确需重跑请加 --force。"
            % (state.get("at", "?"), state.get("count", "?"))
        )


def mark_daily_run(count: int) -> None:
    os.makedirs(common.WORKSPACE_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "date": datetime.now().date().isoformat(),
            "at": datetime.now().isoformat(timespec="seconds"),
            "count": count,
        }, f, ensure_ascii=False, indent=2)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="赛氪列表前 2 页 -> competition_candidates（默认 dry-run）"
    )
    parser.add_argument("--db-path", default=common.DEFAULT_DB_PATH,
                        help="目标库（默认 test_crawl.db）")
    parser.add_argument("--apply", action="store_true", help="真正写候选表")
    parser.add_argument("--allow-production", action="store_true",
                        help="允许写生产库（必须与 --apply 同用，且会先备份）")
    parser.add_argument("--pages", type=int, default=MAX_PAGES,
                        help="页数（最多 2，默认 2）")
    parser.add_argument("--limit", type=int, default=10,
                        help="每页条数（默认 10，两页共 20）")
    parser.add_argument("--max-items", type=int, default=MAX_ITEMS,
                        help="本次最多处理条数（默认 20）")
    parser.add_argument("--delay", default="3,5",
                        help="请求间隔秒，默认 3,5（最低 3 秒）")
    parser.add_argument("--preview", default="saikr_preview.json",
                        help="预览 JSON 文件名（写入 workspace/）")
    parser.add_argument("--force", action="store_true",
                        help="忽略每日一次守卫，强制重跑")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.pages < 1 or args.pages > MAX_PAGES:
        raise SystemExit("每日只允许抓取前 %d 页（--pages）" % MAX_PAGES)
    if args.limit < 1 or args.limit > 20:
        raise SystemExit("--limit 合法范围 1~20")
    if args.max_items < 1 or args.max_items > MAX_ITEMS:
        raise SystemExit("--max-items 合法范围 1~%d" % MAX_ITEMS)
    try:
        delay_min, delay_max = [float(x) for x in args.delay.split(",")]
    except ValueError:
        raise SystemExit("--delay 格式应为 min,max，例如 3,5")
    if delay_min < 3 or delay_max < delay_min:
        raise SystemExit("赛氪请求间隔不得低于 3 秒（默认 3,5）")

    if args.apply and common.is_production_db(args.db_path) \
            and not args.allow_production:
        raise SystemExit("拒绝写生产库：必须同时传 --allow-production")
    if args.apply:
        check_daily_guard(args.force)

    session = _session()
    now = datetime.now(SHANGHAI)
    raw_items, pages = [], []
    for page in range(1, args.pages + 1):
        items = fetch_list_page(session, page, args.limit)
        log("[赛氪] 第 %d/%d 页 -> %d 条" % (page, args.pages, len(items)))
        raw_items.extend(items)
        pages.append({"page": page, "count": len(items)})
        if page < args.pages:
            wait = random.uniform(delay_min, delay_max)
            log("[限速] 等待 %.1f 秒后再请求下一页" % wait)
            time.sleep(wait)

    # 同一页内 / 跨页按 contest_id 去重，再截断到每日上限
    unique_items, seen_ids = [], set()
    for item in raw_items:
        contest_id = item.get("contest_id")
        if not contest_id or contest_id in seen_ids:
            continue
        seen_ids.add(contest_id)
        unique_items.append(item)
    unique_items = unique_items[:args.max_items]

    candidates, skipped = [], {}
    for item in unique_items:
        candidate, reason = build_candidate(item, now)
        if candidate:
            candidates.append(candidate)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1

    log("[解析] %d 条｜可用候选 %d｜跳过 %d %s" % (
        len(unique_items), len(candidates), sum(skipped.values()),
        ("（" + " / ".join("%s %d" % (k, v) for k, v in skipped.items()) + "）")
        if skipped else "",
    ))

    preview_path = common.save_preview(args.preview, {
        "generated_at": now.isoformat(timespec="seconds"),
        "pages": pages,
        "skipped": skipped,
        "candidates": [
            {key: item[key] for key in
             ("raw_name", "norm_name", "source_url", "image_url",
              "level_label", "category_hint", "reg_start_month",
              "reg_end_month", "contest_start_month", "contest_end_month",
              "source_key")}
            for item in candidates
        ],
    })
    log("[预览] %s（%d 条）" % (preview_path, len(candidates)))

    if not candidates:
        log("[结果] 本次没有可用候选，退出")
        return 1

    if args.apply:
        common.prepare_target(args.db_path, args.apply, args.allow_production)
    stats = common.ingest(candidates, args.db_path, args.apply)
    common.print_ingest_summary(stats, args.apply)

    if args.apply:
        mark_daily_run(stats["inserted"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
