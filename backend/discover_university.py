# -*- coding: utf-8 -*-
"""discover_university.py —— 高校「84 项竞赛目录」发现器（只写候选表）

适用页面（纯 HTML 文本列表，通常没有 <table>）：
  * 山东农业工程学院教务处《全国普通高校学科竞赛排行榜》：
    https://www.sdaeu.edu.cn/jwc/info/1047/2528.htm
  * 华南师范大学美术学院《最新 84 项赛事（附链接）》：
    http://aol.scnu.edu.cn/a/20241021/899.html

流程：
  1) 抓页面 -> 文本行/表格行提取「序号 + 赛事名 + 官网链接」；
  2) 剔除明显不是比赛的词（学术会议 / 作品展 / 年会展示 / 论坛 / 培训…）；
  3) 归一化赛事名 -> 与候选表 + 主表去重（同名或高度相似直接跳过）；
  4) 写入 competition_candidates（status=pending），绝不写 competitions。

安全：
  * 默认 dry-run，不写库；--apply 才写候选表；
  * 默认目标库 test_crawl.db；写生产库必须 --apply --allow-production，且自动备份；
  * 单页请求一次，多页之间默认 1~3 秒随机延迟。

用法：
  .\\venv\\Scripts\\python.exe discover_university.py
  .\\venv\\Scripts\\python.exe discover_university.py --apply
  .\\venv\\Scripts\\python.exe discover_university.py --url "http://aol.scnu.edu.cn/a/20241021/899.html" --apply
"""
import argparse
import hashlib
import os
import random
import re
import sys
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import db
import discover_common as common

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URLS = [
    "https://www.sdaeu.edu.cn/jwc/info/1047/2528.htm",
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

NUM_RE = re.compile(r"^(\d{1,3})\s*[、.．)）]\s*(.+)$")
# 「15、」独占一行，名称在下一行
NUM_ONLY_RE = re.compile(r"^(\d{1,3})\s*[、.．)）]\s*$")
# 「41“大唐杯”…」序号后没有分隔符
NUM_NO_SEP_RE = re.compile(r"^(\d{1,3})\s*([“\"「《].+)$")
# 无 scheme 的官网（如 www.g-ican.com / isclab.org.cn），补 http://
BARE_DOMAIN_RE = re.compile(
    r"^(?=.{4,120}$)(?:www\.)?[a-z0-9-]*[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+(?:/[^\s]*)?$",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s<>\"'）)】]+")

# 赛事正向词：含任意一个才可能是比赛（「奖」用于「时报金犊奖」这类命名）
POSITIVE_WORDS = ("大赛", "竞赛", "比赛", "挑战赛", "锦标赛", "联赛", "杯", "赛", "奖")
STRONG_WORDS = ("大赛", "竞赛", "比赛", "挑战赛", "锦标赛", "联赛", "杯")
# 明显不是比赛的词：命中且没有强赛事词时剔除
NON_COMPETITION_WORDS = (
    "学术会议", "会议", "论坛", "研讨会", "年会", "作品展", "展览", "展示",
    "展演", "讲座", "培训", "招聘", "表彰", "颁奖", "启动仪式", "总结会",
    "交流会", "沙龙", "工作坊", "奖学金",
)


def log(message: str) -> None:
    print(message, flush=True)


def clean_name(text: str) -> str:
    name = re.sub(r"\s+", " ", str(text or "")).strip()
    name = URL_RE.sub("", name).strip()
    return name.strip(" 、，,。；;：:·-—")


def extract_url(text: str) -> str:
    match = URL_RE.search(str(text or ""))
    if match:
        url = match.group(0).rstrip(".,;，。；）)】]》")
        return url if len(url) >= 10 else ""
    candidate = str(text or "").strip(" 　、，,。；;：:）)")
    if BARE_DOMAIN_RE.match(candidate):
        return "http://" + candidate
    return ""


def filter_reason(name: str) -> str:
    """返回剔除原因；空字符串表示通过。"""
    if len(name) < 4:
        return "名称过短"
    if len(name) > 120:
        return "名称过长"
    if name.startswith(("附件", "备注", "注：", "注:", "来源", "上一篇", "下一篇")):
        return "页脚/说明"
    has_strong = any(word in name for word in STRONG_WORDS)
    if not any(word in name for word in POSITIVE_WORDS):
        return "不含赛事关键词"
    if any(word in name for word in NON_COMPETITION_WORDS) and not has_strong:
        return "疑似非比赛"
    return ""


def _soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup


def extract_table_entries(soup: BeautifulSoup, page_url: str) -> list:
    """表格型目录：首列序号 + 第二列名称 + 行内 <a>。"""
    entries = []
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        first = cells[0].get_text(" ", strip=True)
        number = re.match(r"^(\d{1,3})\s*[、.．]?$", first)
        numbered = NUM_RE.match(first)
        if number:
            index = int(number.group(1))
            name = clean_name(cells[1].get_text(" ", strip=True))
        elif numbered:
            index = int(numbered.group(1))
            name = clean_name(numbered.group(2))
        else:
            continue
        link = row.find("a", href=True)
        href = (link.get("href") or "").strip() if link else ""
        if href.startswith(("#", "javascript:")):
            href = ""
        official_url = urljoin(page_url, href) if href else ""
        entries.append({
            "index": index, "name": name,
            "official_url": official_url, "mode": "table",
        })
    return entries


def match_entry_line(lines: list, index: int):
    """识别一行目录项，返回 (序号, 名称, 名称所在行号) 或 None。"""
    line = lines[index]
    match = NUM_RE.match(line)
    if match:
        return int(match.group(1)), match.group(2), index

    match = NUM_ONLY_RE.match(line)
    if match and index + 1 < len(lines):
        next_line = lines[index + 1]
        if (not URL_RE.search(next_line)
                and not NUM_RE.match(next_line)
                and not NUM_ONLY_RE.match(next_line)):
            return int(match.group(1)), next_line, index + 1

    match = NUM_NO_SEP_RE.match(line)
    if match:
        return int(match.group(1)), match.group(2), index
    return None


def extract_line_entries(soup: BeautifulSoup, page_url: str) -> list:
    """文本行型目录（84 项榜单页的主流形态）。"""
    lines = [x.strip() for x in soup.get_text("\n").split("\n") if x.strip()]
    start = None
    for i, line in enumerate(lines):
        if "竞赛目录" in line and any(
                key in line for key in ("网址", "赛事", "名单", "项目", "链接")):
            start = i
            break
    if start is None:
        for i, line in enumerate(lines):
            if NUM_RE.match(line):
                start = i
                break
    if start is None:
        return []

    entries, seen = [], set()
    for i in range(start, len(lines)):
        line = lines[i]
        # 84 项正文之后是「观察目录」，重新编号，必须停住
        if i > start and "观察目录" in line:
            break
        matched = match_entry_line(lines, i)
        if not matched:
            continue
        index, raw_name, name_line = matched
        if index in seen:          # 同一目录重复编号（如后半段观察目录）取第一次
            continue
        seen.add(index)

        name = clean_name(raw_name)
        official_url = extract_url(line)
        if not official_url:
            for j in range(name_line + 1, min(name_line + 5, len(lines))):
                # 遇到下一条目录项（任意序号格式）就停，避免借用下一条的官网
                if match_entry_line(lines, j):
                    break
                official_url = extract_url(lines[j])
                if official_url:
                    break
        entries.append({
            "index": index, "name": name,
            "official_url": official_url, "mode": "text",
        })
    return entries


def extract_roster(html: str, page_url: str) -> tuple:
    soup = _soup(html)
    table_entries = extract_table_entries(soup, page_url)
    if len(table_entries) >= 20:
        return table_entries, "table"
    line_entries = extract_line_entries(soup, page_url)
    if line_entries:
        return line_entries, "text"
    return table_entries, "table"


def fetch_html(url: str, retries: int = 2) -> str:
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=25, headers={"User-Agent": UA})
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or resp.encoding
            return resp.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("抓取失败：%r" % last_error)


def build_candidates(entries: list, page_url: str) -> tuple:
    candidates, filtered = [], []
    missing_url = 0
    for entry in entries:
        reason = filter_reason(entry["name"])
        if reason:
            filtered.append({
                "name": entry["name"], "reason": reason,
                "index": entry["index"],
            })
            continue
        norm_name = db.normalize_competition_name(entry["name"])
        if not norm_name:
            filtered.append({
                "name": entry["name"], "reason": "归一化后为空",
                "index": entry["index"],
            })
            continue
        if not entry["official_url"]:
            missing_url += 1
        digest = hashlib.sha1(norm_name.encode("utf-8")).hexdigest()[:16]
        candidates.append({
            "source": "edu-roster",
            "source_key": "edu-roster:%s" % digest,
            "source_url": page_url,
            "raw_name": entry["name"],
            "norm_name": norm_name,
            "official_url": entry["official_url"] or None,
            "category_hint": common.guess_category(entry["name"]),
            "raw_json": {
                "page": page_url,
                "index": entry["index"],
                "mode": entry["mode"],
                "extracted_at": datetime.now().isoformat(timespec="seconds"),
            },
        })
    return candidates, filtered, missing_url


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="高校竞赛目录页 -> competition_candidates（默认 dry-run）"
    )
    parser.add_argument("--url", action="append", default=[],
                        help="目录页 URL，可重复；默认山东农业工程学院 84 项页")
    parser.add_argument("--db-path", default=common.DEFAULT_DB_PATH,
                        help="目标库（默认 test_crawl.db）")
    parser.add_argument("--apply", action="store_true", help="真正写候选表")
    parser.add_argument("--allow-production", action="store_true",
                        help="允许写生产库（必须与 --apply 同用，且会先备份）")
    parser.add_argument("--delay", default="1,3", help="多页之间随机延迟秒，如 1,3")
    parser.add_argument("--preview", default="roster_preview.json",
                        help="预览 JSON 文件名（写入 workspace/）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    urls = args.url or DEFAULT_URLS
    try:
        delay_min, delay_max = [float(x) for x in args.delay.split(",")]
    except ValueError:
        raise SystemExit("--delay 格式应为 min,max，例如 1,3")
    if delay_min < 0 or delay_max < delay_min:
        raise SystemExit("--delay 数值不合法")

    if args.apply and common.is_production_db(args.db_path) \
            and not args.allow_production:
        raise SystemExit("拒绝写生产库：必须同时传 --allow-production")

    all_candidates, pages = [], []
    for index, url in enumerate(urls):
        log("[抓取] %s" % url)
        try:
            html = fetch_html(url)
        except Exception as exc:  # noqa: BLE001
            log("[失败] %s（%r）" % (url, exc))
            continue
        entries, mode = extract_roster(html, url)
        candidates, filtered, missing_url = build_candidates(entries, url)
        log("[解析] %d 条（%s）｜过滤 %d｜缺官网 %d" % (
            len(entries), "表格" if mode == "table" else "文本行",
            len(filtered), missing_url,
        ))
        for item in filtered[:5]:
            log("        - %s（%s）" % (item["name"], item["reason"]))
        all_candidates.extend(candidates)
        pages.append({
            "url": url, "mode": mode, "entries": len(entries),
            "filtered": filtered, "candidates": len(candidates),
        })
        if index < len(urls) - 1:
            time.sleep(random.uniform(delay_min, delay_max))

    if not all_candidates:
        log("[结果] 没有可写入的候选，退出")
        return 1

    # 多页之间按归一化名本地去重，避免 dry-run 把同一赛事算两次
    unique, seen = [], set()
    for item in all_candidates:
        if item["norm_name"] in seen:
            continue
        seen.add(item["norm_name"])
        unique.append(item)

    preview_path = common.save_preview(args.preview, {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pages": pages,
        "candidates": [
            {key: item[key] for key in
             ("raw_name", "norm_name", "official_url", "category_hint", "source_key")}
            for item in unique
        ],
    })
    log("[预览] %s（%d 条）" % (preview_path, len(unique)))

    if args.apply:
        common.prepare_target(args.db_path, args.apply, args.allow_production)
    stats = common.ingest(unique, args.db_path, args.apply)
    common.print_ingest_summary(stats, args.apply)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
