"""赛氪赛事「名称 -> contest_id」解析器（本地索引 + 相似度匹配）。

为什么需要它：教育部门名录（`edu-roster`）来源的候选，`source_url` 全部指向
同一个目录页（如 `https://www.sdaeu.edu.cn/jwc/info/1047/2528.htm`），
**没有任何 contest_id**。拿目录页去赛氪接口探测只能得到 404，入库即空壳。

赛氪没有开放关键词搜索接口（`/api/pc/contest/lists` 只认 page/limit/sort），
所以这里改为「一次性抓全量列表建本地索引（约 52 次请求，1.2s 间隔）→ 之后按名称
相似度在本地匹配」，索引带年龄校验，过期才重建。

用法：
    venv\\Scripts\\python.exe saikr_match.py --build          # 重建索引
    venv\\Scripts\\python.exe saikr_match.py --query 全国大学生统计建模大赛
    venv\\Scripts\\python.exe saikr_match.py --stats
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SAIKR_LISTS = "https://apiv4buffer.saikr.com/api/pc/contest/lists"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
SHANGHAI = timezone(timedelta(hours=8))

INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "saikr_index.json")
PAGE_LIMIT = 100          # 接口上限实测 100
PAGE_DELAY = 1.2          # 相邻请求间隔，避免打爆赛氪
MAX_AGE_DAYS = 14         # 索引超过 14 天视为过期
MIN_RATIO = 0.86          # 相似度阈值（低于此值宁可不匹配）
MIN_CONTAIN_LEN = 8       # 包含关系匹配要求的最小长度，防「大赛」误命中


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# 名称归一化
# ---------------------------------------------------------------------------
# 【倒计时4天】、第9届/第九届、2026年/2026、括号补充说明 —— 这些噪声会让
# 相似度掉到阈值以下，先剥掉再做比较。
_PREFIX_RE = re.compile(r"^[【\[（(][^】\]）)]{0,12}[】\]）)]\s*")
_EDITION_RE = re.compile(r"第[一二三四五六七八九十百零\d]{1,3}届")
_YEAR_RE = re.compile(r"(19|20)\d{2}\s*(年|年度)?")
_BRACKET_RE = re.compile(r"[（(【\[][^）)】\]]{0,30}[）)】\]]")
_PUNCT_RE = re.compile(r"[\s\u3000·•、，,。．.：:；;！!？?\"“”'‘’《》<>—\-_/\\|]+")


def normalize_name(name: str) -> str:
    """归一化赛事名：去前缀角标 / 届次 / 年份 / 括号补充 / 标点空格。"""
    text = str(name or "").strip()
    if not text:
        return ""
    text = _PREFIX_RE.sub("", text)
    text = _EDITION_RE.sub("", text)
    text = _BRACKET_RE.sub("", text)
    text = _YEAR_RE.sub("", text)
    text = _PUNCT_RE.sub("", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 索引构建
# ---------------------------------------------------------------------------
def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.saikr.com/",
    })
    return session


def fetch_page(session: requests.Session, page: int,
               limit: int = PAGE_LIMIT, retries: int = 2):
    """返回 (items, total)。"""
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(
                SAIKR_LISTS,
                params={"page": page, "limit": limit, "sort": 2},
                timeout=25,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") != 200:
                raise RuntimeError("赛氪 code=%s msg=%s" % (
                    payload.get("code"), payload.get("msg")))
            data = payload.get("data") or {}
            return list(data.get("list") or []), int(data.get("total") or 0)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError("赛氪列表请求失败（page=%s）：%r" % (page, last_error))


def build_index(force: bool = False, max_pages: int = 0) -> dict:
    """抓全量列表写本地索引；force=False 且索引仍新鲜时直接复用。"""
    if not force:
        existing = _read_index()
        if existing and not is_stale(existing):
            log("索引仍新鲜（%s，%d 条），跳过重建" % (
                existing.get("built_at"), len(existing.get("items") or [])))
            return existing

    session = _session()
    items: list = []
    total = 0
    page = 1
    started = time.time()
    try:
        while True:
            page_items, total = fetch_page(session, page)
            if not page_items:
                break
            for raw in page_items:
                contest_id = _positive_int(raw.get("contest_id"))
                name = str(raw.get("contest_name") or "").strip()
                if not contest_id or not name:
                    continue
                items.append({
                    "contest_id": contest_id,
                    "name": name,
                    "norm": normalize_name(name),
                    "url": str(raw.get("contest_url") or "").strip(),
                    "level": str(raw.get("level_name") or "").strip(),
                })
            log("  第 %d 页：累计 %d / %d（%.0fs）" % (
                page, len(items), total, time.time() - started))
            if total and len(items) >= total:
                break
            if max_pages and page >= max_pages:
                break
            page += 1
            time.sleep(PAGE_DELAY)
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass

    index = {
        "built_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "total": total or len(items),
        "items": items,
    }
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, INDEX_PATH)
    log("索引已写入 %s（%d 条，%.0fs）" % (
        INDEX_PATH, len(items), time.time() - started))
    return index


def _positive_int(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _read_index() -> dict | None:
    if not os.path.exists(INDEX_PATH):
        return None
    try:
        with open(INDEX_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_stale(index: dict, max_age_days: int = MAX_AGE_DAYS) -> bool:
    raw = str(index.get("built_at") or "")
    try:
        built = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if built.tzinfo is None:
        built = built.replace(tzinfo=SHANGHAI)
    return datetime.now(SHANGHAI) - built > timedelta(days=max_age_days)


def load_index(rebuild_if_missing: bool = True) -> dict:
    """读索引；缺失时自动建一次（首次约 60~90 秒）。"""
    index = _read_index()
    if index and index.get("items"):
        return index
    if not rebuild_if_missing:
        return {"items": [], "built_at": "", "total": 0}
    return build_index(force=True)


# ---------------------------------------------------------------------------
# 名称匹配
# ---------------------------------------------------------------------------
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_EDITION_NUM = re.compile(r"第\s*([0-9零〇一二三四五六七八九十百两]{1,6})\s*届")
_EDITION_YEAR = re.compile(r"(19|20)\d{2}")


def _cn_to_int(text: str) -> int:
    """「十二」「二十四」「11」-> int；无数字返回 0。"""
    text = str(text or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    total, num = 0, 0
    for ch in text:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
    return total + num


def edition_rank(name: str) -> tuple:
    """(届数, 年份)：同名多届次时用来挑「最新一届」。

    届数是主信号（「第17届」比「2021第13届」新）；没写届次但带年份的页面
    用年份兜底；两者都没有的页面（赛氪多数「当届」页就是这样）按最新处理，
    否则会被往届的编号页压过去。
    """
    text = str(name or "")
    year_hit = _EDITION_YEAR.search(text)
    edition_hit = _EDITION_NUM.search(text)
    year = int(year_hit.group(0)) if year_hit else 0
    ordinal = _cn_to_int(edition_hit.group(1)) if edition_hit else 0
    if not ordinal and not year:
        ordinal = 999  # 无届次无年份 = 当届页
    return (ordinal, year)


def _score(query: str, candidate: str) -> tuple:
    """返回 (分数, 方式)；包含关系额外加分。"""
    if not query or not candidate:
        return 0.0, ""
    if query == candidate:
        return 1.0, "exact"
    ratio = difflib.SequenceMatcher(None, query, candidate).ratio()
    if len(query) >= MIN_CONTAIN_LEN and len(candidate) >= MIN_CONTAIN_LEN:
        if query in candidate or candidate in query:
            # 包含关系：长度越接近越可信
            overlap = min(len(query), len(candidate)) / max(len(query), len(candidate))
            return max(ratio, 0.90 * overlap + 0.09), "contains"
    return ratio, "fuzzy"


def resolve(name: str, *, index: dict | None = None,
            min_ratio: float = MIN_RATIO) -> dict | None:
    """名称 -> 赛氪 contest_id；匹配不到返回 None。

    返回 {"contest_id", "score", "method", "matched_name", "url"}。
    """
    query = normalize_name(name)
    if len(query) < 4:
        return None
    index = index if index is not None else load_index()
    ranked = []
    for item in index.get("items") or []:
        norm = item.get("norm") or normalize_name(item.get("name") or "")
        score, method = _score(query, norm)
        if score >= min_ratio:
            ranked.append((score, method, item))
    if not ranked:
        return None
    # 同分多届次（「第十二届」「第11届」「第十届」归一化后完全一样）：取最新一届，
    # 否则会按索引顺序随机落到往届，enrich 随后因「往年届次」把数据整条跳过。
    ranked.sort(key=lambda entry: (entry[0],) + edition_rank(entry[2].get("name") or ""),
                reverse=True)
    best_score, best_method, best = ranked[0]
    # 第二名太接近 => 有歧义，宁可放弃（避免把 A 赛事的详情写到 B 上）
    if len(ranked) > 1 and best_score < 1.0 and best_score - ranked[1][0] < 0.02:
        return None
    return {
        "contest_id": int(best["contest_id"]),
        "score": round(best_score, 4),
        "method": best_method,
        "matched_name": best.get("name") or "",
        "url": best.get("url") or "",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="赛氪名称 -> contest_id 解析器")
    parser.add_argument("--build", action="store_true", help="重建本地索引")
    parser.add_argument("--force", action="store_true", help="忽略新鲜度强制重建")
    parser.add_argument("--refresh-if-stale", action="store_true",
                        help="仅在索引缺失/过期时重建（cron 用，新鲜则 0 网络直接退出）")
    parser.add_argument("--query", help="按名称查询 contest_id")
    parser.add_argument("--stats", action="store_true", help="查看索引状态")
    parser.add_argument("--max-pages", type=int, default=0, help="调试用：只抓前 N 页")
    args = parser.parse_args(argv)

    # cron 入口：每月 1/15 日 04:00 跑一次，索引还新鲜就只读本地退出（0 请求），
    # 避免每天重抓 52 页被赛氪封 IP。
    if args.refresh_if_stale:
        if args.build or args.force:
            parser.error("--refresh-if-stale 不能与 --build / --force 同时使用")
        current = _read_index()
        if current and current.get("items") and not is_stale(current):
            log("索引仍新鲜（构建于 %s，%d 条），跳过重建" % (
                current.get("built_at"), len(current.get("items") or [])))
            return 0
        index = build_index(force=True)
        log("索引已刷新：%d 条（%s）" % (
            len(index.get("items") or []), index.get("built_at")))
        return 0

    if args.build or args.force:
        index = build_index(force=args.force, max_pages=args.max_pages)
    else:
        index = load_index()

    if args.stats or not (args.build or args.query):
        log("索引：%s" % INDEX_PATH)
        log("条数：%d（赛氪 total=%s）" % (
            len(index.get("items") or []), index.get("total")))
        log("构建时间：%s（%s）" % (
            index.get("built_at") or "-",
            "已过期" if is_stale(index) else "新鲜"))

    if args.query:
        hit = resolve(args.query, index=index)
        if hit:
            log("命中：contest_id=%s 分数=%.3f 方式=%s" % (
                hit["contest_id"], hit["score"], hit["method"]))
            log("赛氪名：%s" % hit["matched_name"])
        else:
            log("未命中：%s" % args.query)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())