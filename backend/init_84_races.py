# -*- coding: utf-8 -*-
"""init_84_races.py —— 把「教育部 84 项核心国赛」全量补齐进主库

流程（每一条缺失赛事）：
  1) 主库已有？—— 用 competition_whitelist.match() 反查，命中即跳过；
  2) 名称 -> 赛氪 contest_id：先查**本地索引** saikr_index.json（零网络），
     官方全称、别名都试，取最高分；本地没命中才走赛氪精准搜索接口兜底
     （1 次请求 + 5~10 秒延迟，仅兜底用）；
  3) 抓赛氪详情 -> 写库：封面过 image_guard、日期只写真实值、
     参赛对象/内容范围/考试安排经 AI 提纯后落库（`--no-ai` 可关闭，仅存原文抽取）；
  4) 因为已在白名单内，**不进候选池**，直接写 competitions。

合规定位（务必保留）：搜索接口对应 /search 页在赛氪 robots 中 Disallow，
因此它只做「离线一次性的存量补齐兜底」，不进任何 cron / 批量循环；
默认限速 6 秒、单次最多 84 条，跑完即止。

用法：
    .\\venv\\Scripts\\python.exe init_84_races.py                # dry-run（默认，只解析不写库）
    .\\venv\\Scripts\\python.exe init_84_races.py --probe        # dry-run + 顺带探详情（更慢）
    .\\venv\\Scripts\\python.exe init_84_races.py --apply --allow-production
    .\\venv\\Scripts\\python.exe init_84_races.py --apply --allow-production --no-ai
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "competitions.db")
PRODUCTION_NAME = "competitions.db"
SEARCH_URL = "https://apiv4buffer.saikr.com/api/pc/search/info"
REFERER = "https://www.saikr.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

sys.path.insert(0, BASE_DIR)
import competition_whitelist as whitelist  # noqa: E402
import db  # noqa: E402
import discover_common as common  # noqa: E402
import enrich_saikr_details as enrich  # noqa: E402
import saikr_match  # noqa: E402

PROBE_SECONDS = (5.0, 10.0)


def log(message):
    print(message, flush=True)


# ---------------------------------------------------------------- 主库现状
def present_indexes(conn):
    """主库现有赛事覆盖了白名单哪几项（返回 {白名单序号: 行信息}）。"""
    covered = {}
    for row in conn.execute(
            "SELECT id, name, COALESCE(category,'') AS category, "
            "       COALESCE(is_active,1) AS active FROM competitions"):
        hit = whitelist.match(row["name"])
        if hit:
            covered.setdefault(hit["index"], {
                "id": row["id"], "name": row["name"],
                "category": row["category"], "active": row["active"],
            })
    return covered


# ---------------------------------------------------------------- 名称解析
def resolve_local(name):
    """本地赛氪索引匹配（零网络）：官方全称 + 别名，取最高分；不通过校验即放弃。"""
    want = whitelist.match(name)
    candidates = [name]
    if want:
        candidates += list(whitelist.ALIASES.get(want["index"], ()))
    best = None
    for cand in candidates:
        got = saikr_match.resolve(cand)
        if not got:
            continue
        if want:
            ok, _why = _validate(got["matched_name"], want["index"], got["score"])
            if not ok:
                continue
        if best is None or got["score"] > best["score"]:
            best = dict(got, query=cand, via="本地索引")
    return best


def resolve_search(name, session, delay):
    """赛氪精准搜索兜底（每条 1 次请求 + 限速），按白名单序号校验同一条赛事。"""
    import requests

    want = whitelist.match(name)
    keyword = name
    if want:
        keyword = whitelist.WHITELIST[want["index"] - 1][1]
    params = {"type": 0, "keyword": keyword, "page": 1, "limit": 10}
    items = []
    try:
        for attempt in range(2):
            try:
                resp = session.get(
                    SEARCH_URL, params=params, timeout=25,
                    headers={"User-Agent": UA, "Referer": REFERER,
                             "Accept": "application/json, text/plain, */*"},
                )
                resp.raise_for_status()
                payload = resp.json()
                items = ((payload.get("data") or {}).get("list") or [])
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    return {"error": repr(exc)}
                time.sleep(3.0)
    finally:
        time.sleep(delay)

    best = None
    for item in items:
        contest_name = str(item.get("contest_name") or "").strip()
        hit = whitelist.match(contest_name)
        if want and (hit is None or hit["index"] != want["index"]):
            continue
        score = saikr_match._score(
            saikr_match.normalize_name(keyword),
            saikr_match.normalize_name(contest_name),
        )[0]
        if best is None or score > best["score"]:
            best = {
                "contest_id": int(item.get("contest_id")),
                "score": round(score, 4),
                "method": "search",
                "matched_name": contest_name,
                "url": "https://www.saikr.com/vse/%s" % (item.get("contest_url") or ""),
                "query": keyword,
                "via": "搜索接口",
            }
    if best and want:
        ok, why = _validate(best["matched_name"], want["index"], best["score"])
        if not ok:
            return {"rejected": why, "matched_name": best["matched_name"],
                    "score": best["score"]}
    return best or {}


# ---------------------------------------------------------------- 写库
def create_row(conn, entry):
    """建一条白名单赛事骨架行（详情由 enrich 填）。"""
    index, name, url = entry
    row = {
        "name": name,
        "category": whitelist.guess_category(name),
        "level": "national",
        "level_label": "国家级",
        "official_url": url or None,
        "is_active": 1,
        "sort_order": 9999,
    }
    return db.add_competition(row)


def tag_source(conn, cid):
    """db.add_competition 不认 discover_source，这里补上来源标记。"""
    conn.execute("UPDATE competitions SET discover_source='edu:84' WHERE id=?", (cid,))
    conn.commit()


DATE_FIELDS = ("reg_start_date", "reg_end_date", "contest_start_date", "contest_end_date",
               "reg_start_month", "reg_end_month", "contest_start_month", "contest_end_month")


def clear_dates(conn, cid):
    """往年届次只保留可复用的稳定字段，报名/比赛时间一律清空，避免学生看到过期日期。"""
    conn.execute("UPDATE competitions SET %s WHERE id=?"
                 % ", ".join("%s=NULL" % f for f in DATE_FIELDS), (cid,))
    conn.commit()


def backup(db_path):
    dst_dir = os.path.join(tempfile.gettempdir(), "kuo-verify")
    os.makedirs(dst_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(dst_dir, "competitions.bak-init84-%s.db" % stamp)
    shutil.copy2(db_path, dst)
    return dst


def short(text, width):
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def _edition_year(text):
    """从赛氪名称里取届次年份（无则 None）。"""
    years = [int(y) for y in __import__("re").findall(r"(20\d{2})", text or "")]
    return max(years) if years else None


def _official_core(index):
    """白名单官方名（去掉并列尾巴、末尾英文/括号）的归一化形式。"""
    name = whitelist.WHITELIST[index - 1][1]
    return whitelist.normalize(whitelist._base_name(name))


_PREFIX_RE = __import__("re").compile(
    r"^(?:(?:20\d{2})年?|第[一二三四五六七八九十百零\d]+届|【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\))+")

# 极少数赛事的官方名前面固定挂赞助商杯名（正大杯=市场调查、科云杯=财会…）。
# 只有「杯名 -> 白名单序号」对得上的才允许剥掉，避免「天府杯数学建模」这类
# 同名旁支靠杯名混进国赛名录。
CUP_PREFIX = {
    "高教社杯": 5,
    "正大杯": 20,
    "科云杯": 78,
    "工行杯": 55,
    "大唐杯": 41,
    "西门子杯": 23,
    "外教社杯": 57,
    "学创杯": 48,
    "外研社国才杯": 15,
}


def _strip_prefix(text):
    """剥掉「2026年」「第十二届」「【榜单赛事】」这类前缀。"""
    out = text or ""
    for _ in range(8):
        new = _PREFIX_RE.sub("", out)
        if new == out:
            break
        out = new
    return out


def _validate(cand_name, index, score, min_score=0.85):
    """赛氪名是否真的是这条白名单赛事。

    四道闸（越靠后越宽松）：
      ① 相似度 >= min_score（拒绝「——地球科学与技术学院」这类院内通知）；
      ② 剥掉年份/届次/【榜单】前缀（以及白名单内登记的赞助杯名）后，必须
         **以官方核心名开头**——「天府杯全国大学生数学建模竞赛」剥不掉
         「天府杯」，开头也不是官方名，直接否决，防止同名旁支冒充；
      ③ 赛氪名被官方名包含（如官方名带后缀而赛氪名更短）；
      ④ 命中白名单的长别名（>=6 字），兜住「论坛(大赛) vs 大赛」「赛例 vs 案例」。
    """
    core = _official_core(index)
    cand = whitelist.normalize(cand_name)
    if not core or not cand:
        return False, "名称无法归一化"
    if score < min_score:
        return False, "相似度 %.2f < %.2f" % (score, min_score)

    raw = _strip_prefix((cand_name or "").strip().lstrip("“”\"'「」《》"))
    for cup, idx in CUP_PREFIX.items():
        if idx == index and raw.startswith(cup):
            raw = _strip_prefix(raw[len(cup):])
            break
    body = whitelist.normalize(raw)
    core_short = __import__("re").sub(r"[a-z]+$", "", core)
    for head in (core, core_short):
        if head and (body.startswith(head) or cand.startswith(head)):
            return True, ""
    if cand and cand in core:
        return True, ""
    hit = whitelist.match(cand_name)
    aliases = whitelist.ALIASES.get(index, ())
    if hit and hit["index"] == index and len(hit["matched"] or "") >= 6 \
            and hit["matched"] in aliases:
        return True, ""
    return False, "赛氪名不是官方全称（疑似同名子赛事/院内赛）"


def main(argv=None):
    ap = argparse.ArgumentParser(description="84 项核心国赛全量补齐")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只解析不写库（默认）")
    mode.add_argument("--apply", action="store_true", help="真实写库")
    ap.add_argument("--db", "--db-path", dest="db", default=DEFAULT_DB)
    ap.add_argument("--allow-production", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--no-ai", action="store_true", help="不调模型提纯，只写原文抽取")
    ap.add_argument("--no-search", action="store_true",
                    help="禁用到赛氪搜索接口兜底（只用本地索引，0 额外请求）")
    ap.add_argument("--probe", action="store_true", help="dry-run 时也抓详情看能否取到字段")
    ap.add_argument("--delay", type=float, default=6.0,
                    help="搜索接口兜底请求间隔秒数（默认 6，建议 5~10）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理前 N 条（0=全部）")
    ap.add_argument("--skeletons", action="store_true",
                    help="赛氪没收录的也建骨架行（官方名+官网链接），保证 84 项齐全")
    ap.add_argument("--stale-partial", action="store_true",
                    help="往年届次只写稳定字段（参赛对象/范围/封面），日期清空不误导")
    args = ap.parse_args(argv)

    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        log("数据库不存在：%s" % db_path)
        return 2
    is_prod = os.path.basename(db_path).lower() == PRODUCTION_NAME
    if args.apply and is_prod and not args.allow_production:
        log("拒绝：目标是生产库 %s，必须显式加 --allow-production" % db_path)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    log("库：%s%s" % (db_path, "（生产库）" if is_prod else ""))
    log(whitelist.summary())

    covered = present_indexes(conn)
    missing = [e for e in whitelist.WHITELIST if e[0] not in covered]
    log("白名单 %d 项：主库已覆盖 %d 项，缺失 %d 项%s"
        % (len(whitelist.WHITELIST), len(covered), len(missing),
           "" if args.limit <= 0 else "（本次只处理前 %d 条）" % args.limit))
    if args.limit > 0:
        missing = missing[: args.limit]

    import requests
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": REFERER})

    results = []
    search_calls = 0
    for entry in missing:
        index, name, url = entry
        local = resolve_local(name)
        chosen = local
        # 本地只匹配到往年届次（2024 及更早）时，用搜索兜底找当前届
        stale_local = bool(local) and (
            (_edition_year(local.get("matched_name")) or 9999) < datetime.now().year - 1)
        if (chosen is None or stale_local) and not args.no_search:
            search_calls += 1
            got = resolve_search(name, session, args.delay)
            if got and got.get("rejected"):
                log("[搜索否决] #%d %s -> %s（%s）" % (
                    index, name, short(got.get("matched_name"), 30), got["rejected"]))
            elif got and not got.get("error"):
                if chosen is None or (_edition_year(got.get("matched_name")) or 0) > \
                        (_edition_year(chosen.get("matched_name")) or 0):
                    chosen = got
            elif got.get("error"):
                log("[搜索失败] #%d %s -> %s" % (index, name, got["error"]))
        probe = None
        if chosen and args.probe and not args.apply:
            try:
                detail = enrich.fetch_detail(session, chosen["contest_id"], retries=1)
                fields = enrich.extract_fields(detail)
                probe = {
                    "title": str(detail.get("contest_name") or "")[:28],
                    "has_participants": bool(fields.get("participants")),
                    "has_scope": bool(fields.get("content_scope")),
                    "has_cover": bool(detail.get("contest_pic") or detail.get("thumb_pic")),
                }
                time.sleep(1.2)
            except Exception as exc:  # noqa: BLE001
                probe = {"error": repr(exc)[:40]}
        results.append({"entry": entry, "local": local, "chosen": chosen, "probe": probe})
    session.close()

    # ---------------- 报表 ----------------
    log("\n%-4s %-34s %-8s %-8s %s" % ("#", "白名单赛事", "结果", "分数", "赛氪 / 说明"))
    log("-" * 108)
    ok = 0
    for item in results:
        index, name, _url = item["entry"]
        chosen = item["chosen"]
        if chosen:
            ok += 1
            extra = "%s（%s）" % (short(chosen.get("matched_name"), 40), chosen.get("via"))
            if item["probe"]:
                p = item["probe"]
                extra += "  详情:%s" % (p.get("error") if p.get("error")
                                        else "对象%s/范围%s/图%s" % (
                                            "✓" if p["has_participants"] else "✗",
                                            "✓" if p["has_scope"] else "✗",
                                            "✓" if p["has_cover"] else "✗"))
            log("%-4s %-34s %-8s %-8s %s" % (index, short(name, 34), "命中",
                                            "%.2f" % chosen["score"], extra))
        else:
            log("%-4s %-34s %-8s %-8s %s" % (index, short(name, 34), "未命中", "-",
                                            "赛氪未收录 / 名称差异过大"))
    log("-" * 108)
    unmatched = [i for i in results if not i["chosen"]]
    log("可匹配 %d 条，未匹配 %d 条；搜索接口兜底调用 %d 次（每条间隔 %.1fs）"
        % (ok, len(unmatched), search_calls, args.delay))
    if unmatched:
        log("\n未匹配清单（大概率赛氪没收录，需人工补官网或等下一届）：")
        for item in unmatched:
            log("  #%-2s %s" % (item["entry"][0], item["entry"][1]))

    if not args.apply:
        log("\n[dry-run] 未写库。确认后加 --apply%s 执行。"
            % (" --allow-production" if is_prod else ""))
        return 0

    # ---------------- 落库 ----------------
    if not args.no_backup:
        log("\n已备份：%s" % backup(db_path))

    written, failed = [], []
    for item in results:
        index, name, url = item["entry"]
        chosen = item["chosen"]
        if not chosen:
            continue
        try:
            cid = create_row(conn, (index, name, url))
            tag_source(conn, cid)
            log("[建行] #%d %s -> competition_id=%s" % (index, name, cid))
            if args.no_ai:
                detail = enrich.fetch_detail(enrich._session(), chosen["contest_id"])
                fields = enrich.extract_fields(detail)
                patch = {}
                for key in ("participants", "content_scope", "exam_schedule"):
                    value = (fields.get(key) or {}).get("text")
                    if value:
                        patch[key] = value
                if patch:
                    db.update_competition(cid, patch, db_path=db_path)
                filled = enrich.fill_dates_and_cover(db_path, cid, detail)
                result = {"fill": filled, "fields": sorted(patch)}
            else:
                result = enrich.enrich_competition(
                    db_path, cid, contest_id=chosen["contest_id"],
                    allow_name_match=False,
                )
                # 往年届次：稳定字段照样提纯，但日期清空
                if result.get("status") == "stale" and args.stale_partial:
                    past = enrich.enrich_competition(
                        db_path, cid, contest_id=chosen["contest_id"],
                        allow_name_match=False, allow_stale=True)
                    enrich.fill_dates_and_cover(
                        db_path, cid, enrich.fetch_detail(enrich._session(),
                                                          chosen["contest_id"]))
                    clear_dates(conn, cid)
                    result = dict(past, status="stale-partial")
                else:
                    # 详情里没有封面时再兜底补封面/日期
                    enrich.fill_dates_and_cover(
                        db_path, cid, enrich.fetch_detail(enrich._session(),
                                                          chosen["contest_id"]))
            written.append({"index": index, "id": cid, "name": name,
                            "saikr": chosen.get("matched_name"), "result": result})
            log("[写入] #%d %s（contest_id=%s，%s）"
                % (index, name, chosen["contest_id"], result.get("status") or "ok"))
        except Exception as exc:  # noqa: BLE001
            failed.append({"index": index, "name": name, "error": repr(exc)})
            log("[失败] #%d %s -> %r" % (index, name, exc))

    # ---------------- 赛氪没收录的：建骨架，保证 84 项目录齐全 ----------------
    skeletons = []
    if args.skeletons and unmatched:
        for item in unmatched:
            index, name, url = item["entry"]
            try:
                cid = create_row(conn, (index, name, url))
                tag_source(conn, cid)
                skeletons.append({"index": index, "id": cid, "name": name})
                log("[骨架] #%d %s -> competition_id=%s" % (index, name, cid))
            except Exception as exc:  # noqa: BLE001
                failed.append({"index": index, "name": name, "error": repr(exc)})
                log("[骨架失败] #%d %s -> %r" % (index, name, exc))

    total = conn.execute("SELECT COUNT(*) FROM competitions").fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM competitions WHERE COALESCE(is_active,1)=1").fetchone()[0]
    log("\n=== 完成 ===")
    log("新增 %d 条，骨架 %d 条，失败 %d 条；主库 %d 条（前台可见 %d 条）"
        % (len(written), len(skeletons), len(failed), total, active))
    if failed:
        for f in failed:
            log("  失败 #%s %s：%s" % (f["index"], f["name"], f["error"]))
    report_dir = os.path.join(common.WORKSPACE_DIR)
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(
        report_dir, "init_84_races_%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"written": written, "failed": failed, "skeletons": skeletons,
                   "unmatched": [i["entry"] for i in unmatched]},
                  f, ensure_ascii=False, indent=1)
    log("报告：%s" % report_path)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())