# -*- coding: utf-8 -*-
"""
batch_update_competitions.py —— 生产库赛事「批量补全」（小步快跑 + 限流 + 降级）

做什么
------
遍历目标库里 `image_url` 为空（或比赛月份为空）的赛事，拿它的 `official_url`
当作入口页，走「抓取 -> 大模型抽取 -> 只填空字段」的流程，把这三类信息补上：
    封面图 URL / 比赛开始月份 / 比赛结束月份
顺带补：`official_url`（原本为空时）、`intro` / `summary`（原本为空时）。

为什么能守住红线
----------------
* **只填空，不覆盖**：`build_patch()` 只挑「库里当前为空」的字段写入，
  已有的 name / category / level / 月份一律不碰（除非 --update-name）。
* 默认 **dry-run**：不带 --apply 时只抓取+抽取，不写库。
* 写库前自动 **备份**（SQLite backup API，文件名带时间戳，不覆盖历史备份）。
* 写生产库必须显式 `--allow-production`（沿用 crawler_competitions.py 的约定）。
* 一条一条地写，每条独立 try/except：单条失败只记录，不中断整批。

限流与降级
----------
* 每条之间随机 sleep `--min-delay ~ --max-delay`（默认 5~10 秒）；
* 单条失败（抓取/大模型）自动重试 `--attempts` 次，退避 `--retry-delay` 秒；
* 抓不到、正文太短、模型抽不出、抽出来全是空 -> 记为 skipped 并继续，
  绝不"死磕"；最后汇总成报告（JSON 落盘，控制台只打印摘要）。

用法（在 backend 目录、激活 venv 后）
------------------------------------
    # 1. 演练 3 条（不写库，只看抽取质量）
    .\\venv\\Scripts\\python.exe batch_update_competitions.py --ids 10,12,13 --allow-production

    # 2. 真实写入 3 条（自动备份）
    .\\venv\\Scripts\\python.exe batch_update_competitions.py --ids 10,12,13 --apply --allow-production

    # 3. 放开跑剩余全部（先小批量确认质量再跑）
    .\\venv\\Scripts\\python.exe batch_update_competitions.py --limit 10 --apply --allow-production

    # 4. 只跑「比赛月份为空」的（封面已有但缺时间的）
    .\\venv\\Scripts\\python.exe batch_update_competitions.py --filter months --apply --allow-production
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime

import requests

import crawler_competitions as cc
import crawler_news as news
import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")

# 只允许"填空"的字段（库里已有非空值时不覆盖）
FILLABLE = (
    "image_url",
    "contest_start_month",
    "contest_end_month",
    "official_url",
    "intro",
    "summary",
    "category",
    "level_label",
)

# 封面图质量闸门：卡片是 4:3 的略宽图，Banner（如 855x200）裁进去只剩一条
IMAGE_MIN_WIDTH = 300
IMAGE_MIN_HEIGHT = 150
IMAGE_MAX_ASPECT = 2.6      # 宽/高 超过即当 banner 处理
IMAGE_MIN_ASPECT = 0.45     # 高/宽 超过 2.2（超长竖图）同样丢弃
IMAGE_MAX_BYTES = 3 * 1024 * 1024
IMAGE_CANDIDATES = 4        # 最多试几张候选图

FILTER_SQL = {
    "image": "(image_url IS NULL OR TRIM(image_url) = '')",
    "months": "(contest_start_month IS NULL AND contest_end_month IS NULL)",
    "any": "1=1",
}


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}")


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老环境 / 被重定向
            pass


# ---------------------------------------------------------------------------
# 候选与补丁
# ---------------------------------------------------------------------------

def load_candidates(*, ids=None, limit=None, filter_mode="image") -> list:
    """指定了 --ids 时以 id 为准（忽略 --filter），避免"我选了 id 却没跑"的困惑。"""
    if ids:
        where = "id IN (%s)" % ", ".join("?" for _ in ids)
        params = list(ids)
    else:
        where = FILTER_SQL[filter_mode]
        params = []
    sql = (
        "SELECT id, name, category, level, level_label, official_url, image_url, "
        "contest_start_month, contest_end_month, intro, summary "
        "FROM competitions WHERE " + where + " ORDER BY id"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in ("", "null", "None")
    return False


# 采集溯源字段：只要有抓取就一定更新，但不能算作"有新信息"
TRACE_FIELDS = ("discover_source", "last_checked_at")


def _is_blank_field(key: str, value) -> bool:
    if _is_blank(value):
        return True
    # 占位赛事的 category='待确认' 视为空，允许被真实类别覆盖
    if key == "category" and str(value).strip() == news.PLACEHOLDER_CATEGORY:
        return True
    return False


def build_patch(before: dict, extracted: dict, page: dict, *, update_name: bool = False) -> dict:
    """只挑「库里为空」的字段写入，避免覆盖人工/历史数据。"""
    patch = {}

    for key in FILLABLE:
        new_value = extracted.get(key)
        if _is_blank(new_value):
            continue
        if _is_blank_field(key, before.get(key)):
            patch[key] = new_value

    if update_name and extracted.get("name") and extracted["name"] != before.get("name"):
        patch["name"] = extracted["name"]

    # 采集溯源：任何时候都记录「这条数据是哪来的、什么时候看的」
    patch["discover_source"] = page["url"]
    patch["last_checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return patch


def _diff_keys(before: dict, patch: dict) -> list:
    """真正带来新信息的字段（排除采集溯源字段）。"""
    return [
        k for k in patch
        if k not in TRACE_FIELDS and before.get(k) != patch[k]
    ]


# ---------------------------------------------------------------------------
# 封面图质量闸门
# ---------------------------------------------------------------------------

def _image_dimensions(data: bytes):
    """从文件头解析宽高（PNG/JPEG/GIF/WebP），认不出返回 (None, None)。"""
    if len(data) < 24:
        return None, None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":  # JPEG：扫 SOFn 段
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(data[i + 5:i + 7], "big")
                width = int.from_bytes(data[i + 7:i + 9], "big")
                return width, height
            i += 2 + seg_len
        return None, None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":
            return (int.from_bytes(data[24:27], "little") + 1,
                    int.from_bytes(data[27:30], "little") + 1)
        if kind == b"VP8 ":
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
        if kind == b"VP8L":
            b = data[21:25]
            return (1 + (((b[1] & 0x3F) << 8) | b[0]),
                    1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6)))
    return None, None


def check_image(url: str, timeout=(8, 15)) -> tuple:
    """下载校验封面图：可达 + 真是图片 + 尺寸/比例适合 4:3 卡片。

    返回 (ok, reason, info)；reason 为空表示通过。
    """
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": news.USER_AGENT},
                            stream=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"图片请求失败（{type(exc).__name__}）", {}

    with resp:
        if resp.status_code != 200:
            return False, f"图片 HTTP {resp.status_code}", {}
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("image/"):
            return False, f"不是图片（Content-Type={ctype or '未知'}）", {}

        chunks, total = [], 0
        for chunk in resp.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > IMAGE_MAX_BYTES:
                break
        data = b"".join(chunks)

    width, height = _image_dimensions(data)
    info = {"bytes": len(data), "width": width, "height": height, "content_type": ctype}
    if not width or not height:
        # 认不出尺寸（少见的格式）时，只要确实是图片就放行
        return True, f"尺寸未知（{len(data) // 1024} KB），放行", info
    if width < IMAGE_MIN_WIDTH or height < IMAGE_MIN_HEIGHT:
        return False, f"尺寸过小 {width}x{height}", info
    ratio = width / height
    if ratio > IMAGE_MAX_ASPECT:
        return False, f"宽高比 {ratio:.2f} 过大（{width}x{height}，疑似 banner）", info
    if ratio < IMAGE_MIN_ASPECT:
        return False, f"高宽比 {height / width:.2f} 过大（{width}x{height}，竖图）", info
    return True, "", info


def collect_image_candidates(page: dict, limit: int = IMAGE_CANDIDATES) -> list:
    """按优先级列出候选封面：og:image 优先，其次正文图（去噪后）。"""
    html = page.get("html") or ""
    if not html:
        return [page["image_url"]] if page.get("image_url") else []

    soup = news._make_soup(html)
    seen, urls = set(), []

    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").strip().lower()
        if key in {"og:image", "og:image:url", "og:image:secure_url",
                   "twitter:image", "twitter:image:src"}:
            candidate = cc._clean_image_url(meta.get("content") or "", page["url"])
            if candidate and candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)

    clean_soup = news._make_soup(html)
    news._strip_noise(clean_soup)
    container, _ = news._pick_container(clean_soup)
    for img in container.find_all("img"):
        src = (img.get("src") or img.get("data-src") or img.get("data-original") or "").strip()
        if not cc._is_meaningful_image(src, img):
            continue
        candidate = cc._clean_image_url(src, page["url"])
        if candidate and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
        if len(urls) >= limit * 2:
            break

    return urls[:limit]


def pick_checked_cover(page: dict, extracted: dict, record: dict):
    """逐张校验候选图，返回第一个合格的 URL；全不合格返回 None。"""
    candidates = collect_image_candidates(page)
    if not candidates and extracted.get("image_url"):
        candidates = [extracted["image_url"]]

    record["image_candidates"] = []
    for candidate in candidates:
        ok, reason, info = check_image(candidate)
        item = {"url": candidate, "ok": ok, "reason": reason, **info}
        record["image_candidates"].append(item)
        if ok:
            log("校验", f"    封面通过：{info.get('width')}x{info.get('height')} "
                        f"{info.get('bytes', 0) // 1024} KB")
            return candidate
        log("校验", f"    丢弃候选图：{reason}  {candidate[:80]}")

    log("校验", "    没有合格的封面图，本次不写 image_url")
    return None


# ---------------------------------------------------------------------------
# 单条处理
# ---------------------------------------------------------------------------

def process_one(row: dict, args, index: int, total: int) -> dict:
    """抓取 + 抽取 + （可选）写库；任何异常都收敛成状态，不往上抛。"""
    started = time.time()
    record = {
        "id": row["id"],
        "name": row["name"],
        "used_url": row.get("official_url"),
        "status": "unknown",
        "reason": "",
        "attempts": 0,
        "body_chars": 0,
        "raw_llm_json": None,
        "extracted": None,
        "patch": {},
        "before": {k: row.get(k) for k in FILLABLE},
        "after": None,
        "elapsed_sec": 0,
    }

    url = (row.get("_entry_url") or row.get("official_url") or "").strip()
    if not url:
        record["status"] = "skipped"
        record["reason"] = "库里没有 official_url，无法定位官网（需人工给入口页）"
        record["elapsed_sec"] = round(time.time() - started, 1)
        return record

    page = None
    last_error = ""
    for attempt in range(1, args.attempts + 1):
        record["attempts"] = attempt
        try:
            log("抓取", f"[{index}/{total}] id={row['id']} {row['name'][:26]} -> {url}")
            page = cc.fetch_competition_page(url)
            record["body_chars"] = len(page.get("body") or "")
            break
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
            last_error = f"抓取失败：{str(exc)[:160]}"
            log("重试", f"    第 {attempt}/{args.attempts} 次失败：{last_error}")
            if attempt < args.attempts:
                time.sleep(args.retry_delay)

    if page is None:
        record["status"] = "skipped"
        record["reason"] = last_error or "抓取失败"
        record["elapsed_sec"] = round(time.time() - started, 1)
        return record

    if len(page.get("body") or "") < 200:
        record["status"] = "skipped"
        record["reason"] = f"正文过短（{len(page.get('body') or '')} 字符），判定为非内容页"
        record["elapsed_sec"] = round(time.time() - started, 1)
        return record

    # 大模型抽取（同样带一次重试）
    extracted = None
    for attempt in range(1, args.attempts + 1):
        try:
            log("抽取", f"    调用大模型（正文 {min(len(page['body']), args.max_chars)} 字符）")
            raw, extracted = cc.extract_competition(page, args.max_chars)
            record["raw_llm_json"] = raw
            record["extracted"] = extracted
            break
        except Exception as exc:  # noqa: BLE001
            last_error = f"大模型抽取失败：{str(exc)[:160]}"
            log("重试", f"    第 {attempt}/{args.attempts} 次失败：{last_error}")
            if attempt < args.attempts:
                time.sleep(args.retry_delay)

    if extracted is None:
        record["status"] = "skipped"
        record["reason"] = last_error or "大模型未返回可用结果"
        record["elapsed_sec"] = round(time.time() - started, 1)
        return record

    # 封面图质量闸门：不合格宁可不写，也不要往库里塞 banner / 404 死链
    if args.no_image_check:
        record["image_candidates"] = []
    else:
        record["extracted_image_raw"] = extracted.get("image_url")
        extracted["image_url"] = pick_checked_cover(page, extracted, record)

    patch = build_patch(row, extracted, page, update_name=args.update_name)
    record["patch"] = patch
    changed = _diff_keys(row, patch)

    if not changed:
        record["status"] = "skipped"
        record["reason"] = "抽取结果没有带来新信息（库里该有的都有，或模型抽空）"
        record["elapsed_sec"] = round(time.time() - started, 1)
        return record

    if not args.apply:
        record["status"] = "dry_run"
        record["reason"] = "演练模式，未写库"
        record["elapsed_sec"] = round(time.time() - started, 1)
        return record

    try:
        db.update_competition(row["id"], patch)
        record["after"] = {
            k: (db.get_competition(row["id"]) or {}).get(k) for k in FILLABLE
        }
        record["status"] = "updated"
        record["reason"] = f"写入 {len(changed)} 个字段：{', '.join(changed)}"
    except Exception as exc:  # noqa: BLE001
        record["status"] = "failed"
        record["reason"] = f"写库失败：{str(exc)[:160]}"

    record["elapsed_sec"] = round(time.time() - started, 1)
    return record


# ---------------------------------------------------------------------------
# 备份（复用合并脚本的实现，确保 WAL 下也一致）
# ---------------------------------------------------------------------------

def backup_target(db_path: str) -> str:
    from merge_test_competitions import make_backup

    return make_backup(db_path)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def print_record(record: dict, *, show_raw: bool) -> None:
    print("-" * 72)
    print(f"id={record['id']}  {record['name']}")
    print(f"  入口页 : {record['used_url']}")
    print(f"  状态   : {record['status']}  {record['reason']}")
    print(f"  尝试   : {record['attempts']} 次；正文 {record['body_chars']} 字符；"
          f"耗时 {record['elapsed_sec']}s")

    extracted = record.get("extracted") or {}
    if extracted:
        print(f"  抽取   : name={extracted.get('name')!r}")
        print(f"           category={extracted.get('category')!r} level={extracted.get('level')!r}")
        print(f"           contest={extracted.get('contest_start_month')}-"
              f"{extracted.get('contest_end_month')} "
              f"reg={extracted.get('reg_start_month')}-{extracted.get('reg_end_month')}")
        print(f"           official={extracted.get('official_url')!r}")
        image = extracted.get("image_url") or ""
        print(f"           image={image[:100]!r}")
        print(f"           confidence={extracted.get('confidence')}")

    if record.get("image_candidates"):
        print(f"  封面候选: {len(record['image_candidates'])} 张")
        for item in record["image_candidates"]:
            mark = "✓" if item["ok"] else "✗"
            size = (f"{item.get('width')}x{item.get('height')}"
                    if item.get("width") else "尺寸未知")
            print(f"           {mark} {size} {item.get('reason') or '合格'} "
                  f"{item['url'][:70]}")

    if record.get("patch"):
        print(f"  待写   : {json.dumps(record['patch'], ensure_ascii=False)[:300]}")

    if record.get("after"):
        after = record["after"]
        image = after.get("image_url") or ""
        print("  入库后 : image=%r contest=%s-%s" % (
            image[:80], after.get("contest_start_month"), after.get("contest_end_month")))

    if show_raw and record.get("raw_llm_json"):
        print("  --- 大模型原始 JSON ---")
        print("  " + json.dumps(record["raw_llm_json"], ensure_ascii=False, indent=2)
              .replace("\n", "\n  "))


def save_report(records: list, meta: dict) -> str:
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(WORKSPACE_DIR, f"batch_competitions_{stamp}.json")
    payload = dict(meta)
    payload["items"] = records
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def summarize(records: list) -> dict:
    stats = {"updated": 0, "dry_run": 0, "skipped": 0, "failed": 0}
    for r in records:
        stats[r["status"]] = stats.get(r["status"], 0) + 1
    return stats


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="批量补全生产库赛事的封面图 / 比赛月份（限流 + 重试 + 失败跳过）",
    )
    parser.add_argument("--db-path", default=PRODUCTION_DB_PATH,
                        help=f"目标数据库（默认 {PRODUCTION_DB_PATH}）")
    parser.add_argument("--allow-production", action="store_true",
                        help="写生产库 competitions.db 时必须显式加上")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认演练）")
    parser.add_argument("--no-backup", action="store_true", help="跳过写库前备份（不推荐）")
    parser.add_argument("--filter", default="image", choices=sorted(FILTER_SQL),
                        help="选哪些赛事：image=封面为空（默认）/ months=月份为空 / any=全部")
    parser.add_argument("--ids", default=None, help="只跑指定 id，逗号分隔，如 10,12,13")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少条")
    parser.add_argument("--min-delay", type=float, default=5.0, help="每条之间最小延迟秒数")
    parser.add_argument("--max-delay", type=float, default=10.0, help="每条之间最大延迟秒数")
    parser.add_argument("--retry-delay", type=float, default=5.0, help="单条重试退避秒数")
    parser.add_argument("--attempts", type=int, default=2, help="单条最多尝试次数")
    parser.add_argument("--max-chars", type=int, default=6000, help="送模型的正文截断长度")
    parser.add_argument("--update-name", action="store_true",
                        help="允许用抽取结果覆盖赛事名（默认不动现名）")
    parser.add_argument("--no-image-check", action="store_true",
                        help="跳过封面图可达性/尺寸校验（不推荐：会写进 banner 或死链）")
    parser.add_argument("--url-map", default=None, metavar="PATH",
                        help='用 JSON 覆盖入口页，形如 {"10": "https://xx/notice.htm"}；'
                             "官方首页抓不到内容时用它改指通知页")
    parser.add_argument("--show-raw", action="store_true", help="打印每条的大模型原始 JSON")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    _setup_console()
    args = parse_args(argv)

    db_path = os.path.abspath(args.db_path)
    if not os.path.exists(db_path):
        log("错误", f"数据库不存在：{db_path}")
        return 2
    if db_path == os.path.abspath(PRODUCTION_DB_PATH) and not args.allow_production:
        log("错误", "目标库是生产库 competitions.db，必须显式加 --allow-production 才允许运行")
        return 2

    db.DB_PATH = db_path

    print("=" * 72)
    print("赛事批量补全（抓取 -> 大模型抽取 -> 只填空）")
    print("=" * 72)
    log("目标库", db_path)
    log("模式", ("真实写库（先备份）" if args.apply else "演练：只抓取+抽取，不写库"))
    log("限流", f"每条之间 {args.min_delay:g}~{args.max_delay:g} 秒；"
                f"单条重试 {args.attempts} 次（退避 {args.retry_delay:g} 秒）")

    ids = None
    if args.ids:
        try:
            ids = [int(x) for x in args.ids.split(",") if x.strip()]
        except ValueError:
            log("错误", "--ids 需要逗号分隔的整数，例如 10,12,13")
            return 2

    candidates = load_candidates(ids=ids, limit=args.limit, filter_mode=args.filter)
    if not candidates:
        log("结论", "没有需要补全的赛事（筛选条件下没有记录）")
        return 0

    if args.url_map:
        try:
            with open(args.url_map, encoding="utf-8") as fh:
                url_map = json.load(fh)
            url_map = {str(k): str(v) for k, v in url_map.items()}
        except Exception as exc:  # noqa: BLE001
            log("错误", f"--url-map 读取失败：{exc}")
            return 2
        hit = 0
        for row in candidates:
            override = url_map.get(str(row["id"]))
            if override:
                row["_entry_url"] = override
                hit += 1
        log("入口", f"--url-map 覆盖了 {hit}/{len(candidates)} 条的入口页")

    log("候选", f"共 {len(candidates)} 条：" + "；".join(
        f"{c['id']}.{c['name'][:16]}" for c in candidates[:12]
    ) + ("…" if len(candidates) > 12 else ""))

    backup_path = ""
    if args.apply:
        if args.no_backup:
            log("备份", "已按 --no-backup 跳过备份（不推荐）")
        else:
            log("备份", "写库前先备份目标库…")
            try:
                backup_path = backup_target(db_path)
            except Exception as exc:  # noqa: BLE001 - 备份失败必须停
                log("错误", f"备份失败，已终止，未写入任何数据：{exc}")
                return 4
            log("备份", f"已生成 {backup_path}")

    started = datetime.now()
    records = []
    for idx, row in enumerate(candidates, start=1):
        records.append(process_one(row, args, idx, len(candidates)))
        if idx < len(candidates):
            delay = random.uniform(args.min_delay, args.max_delay)
            log("限流", f"    等待 {delay:.1f} 秒后继续…")
            time.sleep(delay)

    print()
    for record in records:
        print_record(record, show_raw=args.show_raw)

    stats = summarize(records)
    report_path = save_report(records, {
        "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db_path": db_path,
        "apply": bool(args.apply),
        "filter": args.filter,
        "ids": ids,
        "backup": backup_path,
        "stats": stats,
    })

    print("=" * 72)
    log("汇总", "，".join(f"{k}={v}" for k, v in sorted(stats.items())))
    for record in records:
        if record["status"] in ("skipped", "failed"):
            log("跳过", f"id={record['id']} {record['name'][:20]} —— {record['reason']}")
    log("报告", report_path)
    if args.apply:
        log("备份", backup_path)
        log("提示", "前端刷新即可看到已补全的赛事；未补全的仍显示占位图/待定")
    else:
        log("提示", "本次未写库；确认质量后加 --apply 重跑")
    return 0


if __name__ == "__main__":
    sys.exit(main())
