# -*- coding: utf-8 -*-
"""backfill_official_fields.py —— 从赛事官网正文抽取「参赛对象 / 内容范围 / 考核方式」

`enrich_saikr_details` 只能拿到赛氪详情里的结构化字段，赛氪没收录或详情空白的
18 条骨架就这么一直空着（数学建模、电子设计这类旗舰赛也在其中）。其实这些官网
大多有「大赛简介 / 竞赛规程 / 通知」页，把正文抓下来让模型**只做抽取**即可。

抽取与生成的边界（关键设计）：
  * 先抓页面、抽正文，再让模型从原文里摘出三个字段，提示词要求「原文没有就留空、
    禁止补充常识」——所有写入内容都能在原文里找到对应句子；
  * 抓不到正文（SPA、域名失效、只有导航）的赛事直接跳过并记录原因，不瞎编；
  * 只填空字段，已有内容一律不动；默认 dry-run，写生产库要 `--apply --allow-production`。

用法：
    .\\venv\\Scripts\\python.exe -X utf8 backfill_official_fields.py            # 预演
    .\\venv\\Scripts\\python.exe -X utf8 backfill_official_fields.py --apply --allow-production
    .\\venv\\Scripts\\python.exe -X utf8 backfill_official_fields.py --ids 60,61 --show-text
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import re
import sqlite3
import sys
import time
from urllib.parse import urljoin, urlparse

import requests

import db
import discover_common
import llm

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "competitions.db")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

FIELD_HINTS = ("参赛对象", "参赛资格", "参赛条件", "面向")
LINK_HINTS = ("简介", "介绍", "章程", "规程", "规则", "通知", "大赛", "竞赛", "关于", "组织")
SKIP_LINK_HINTS = ("登录", "注册", "下载", "English", "EN", "视频", "问答", "证书", "查询")

SYSTEM = (
    "你是赛事资料整理员。你只做信息抽取，不做创作：每个字段都必须能在给定原文里"
    "找到对应表述，原文没有的字段返回空字符串。禁止补充原文之外的常识。"
)
PROMPT = """下面是「{name}」官网页面的正文（可能含导航和噪音）。请只从中抽取以下字段，
用 JSON 返回；原文没写到的字段返回空字符串 ""。

字段说明：
- `participants`：参赛对象（面向哪些院校/专业/年级的学生，或何种身份的选手），60–140 字。
- `content_scope`：内容范围（比什么、赛题方向、组别/类别），80–200 字。
- `exam_schedule`：考核方式与赛程（比赛环节、形式、时长，如确实写了赛程阶段可一并写），80–200 字。

要求：简体中文，纯段落，不出现网址、不出现 Markdown；数字和时间只保留原文出现的。

正文开始
--------
{text}
--------
正文结束

只返回 JSON：{{"participants": "", "content_scope": "", "exam_schedule": ""}}
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def strip_html(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", page)
    page = re.sub(r"(?s)<!--.*?-->", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", page)
    text = re.sub(r"(?s)<[^>]+>", " ", page)
    text = htmllib.unescape(text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def good_text(text: str) -> bool:
    return len(text) >= 380 and ("参赛" in text or sum(text.count(k) for k in FIELD_HINTS) > 0)


def find_links(page: str, base_url: str, limit: int = 4) -> list:
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, re.I | re.S):
        href = htmllib.unescape(m.group(1).strip())
        label = re.sub(r"(?s)<[^>]+>", "", m.group(2))
        label = re.sub(r"\s+", " ", htmllib.unescape(label)).strip()
        if not label or len(label) > 30:
            continue
        if not any(h in label for h in LINK_HINTS):
            continue
        if any(s.lower() in label.lower() for s in SKIP_LINK_HINTS):
            continue
        if href.lower().startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href)
        if urlparse(url).scheme not in ("http", "https") or url in seen:
            continue
        seen.add(url)
        out.append((label, url))
        if len(out) >= limit:
            break
    return out


def fetch(session, url: str) -> tuple:
    resp = session.get(url, timeout=(8, 20), allow_redirects=True)
    resp.raise_for_status()
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "html" not in ctype and "text" not in ctype:
        raise ValueError("不是网页：%s" % ctype)
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.url, resp.text[:800_000]


def gather_text(session, url: str, trace: list) -> tuple:
    """返回 (正文, 命中页 URL)。先首页，再看 1–3 个简介/规程/通知页。"""
    final_url, page = fetch(session, url)
    text = strip_html(page)
    trace.append({"url": final_url, "len": len(text)})
    if good_text(text):
        return text, final_url
    for label, link in find_links(page, final_url):
        try:
            sub_final, sub_page = fetch(session, link)
        except Exception as exc:  # noqa: BLE001
            trace.append({"url": link, "error": str(exc)[:80]})
            continue
        sub_text = strip_html(sub_page)
        trace.append({"url": sub_final, "len": len(sub_text), "label": label})
        if good_text(sub_text):
            return sub_text, sub_final
        time.sleep(1.0)
    return text, final_url


def clip_for_prompt(text: str, keyword_window: int = 5200) -> str:
    """优先取关键词周围的正文，压掉导航噪音。"""
    pos = -1
    for k in FIELD_HINTS:
        pos = text.find(k)
        if pos >= 0:
            break
    if pos < 0:
        return text[:keyword_window]
    start = max(0, pos - 900)
    return text[start:start + keyword_window]


META_BAD = ("原文未", "未具体写明", "未说明", "仅提及", "原文中没有",
            "无法确定", "未提及", "页面未")


def tidy(value: str) -> str:
    """清掉模型的口头注释与断句，只留完整、有信息量的句子。"""
    value = re.sub(r"\s+", " ", value or "").strip()
    if any(bad in value for bad in META_BAD):
        return ""
    if len(value) < 12:
        return ""
    if not value.endswith(("。", "！", "？", "；")):
        value = value.rstrip("，、,;； ") + "。"
    return value


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(description="从官网正文抽取参赛对象/内容范围/考核方式")
    ap.add_argument("--db-path", default=DEFAULT_DB_PATH)
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--allow-production", action="store_true")
    ap.add_argument("--ids", default=None, help="只处理这些 id，逗号分隔")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=3.0, help="每条赛事之间的间隔秒数")
    ap.add_argument("--force", action="store_true", help="已有字段也允许覆盖")
    ap.add_argument("--show-text", action="store_true", help="打印抓到的正文片段")
    args = ap.parse_args(argv)

    db_path = os.path.abspath(os.path.expanduser(args.db_path))
    if not os.path.exists(db_path):
        log("[错误] 目标库不存在：%s" % db_path)
        return 4
    log("[目标库] %s" % db_path)
    if discover_common.is_production_db(db_path):
        log("[目标库] ⚠ 这是生产库")
        if args.apply and not args.allow_production:
            log("[拒绝] 写生产库必须同时传 --allow-production")
            return 4
    if not llm.llm_available():
        log("[错误] 模型不可用：%s" % llm.llm_config_hint())
        return 4

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id,name,official_url,COALESCE(participants,'') AS participants,"
           "COALESCE(content_scope,'') AS content_scope, COALESCE(exam_schedule,'') AS exam_schedule "
           "FROM competitions WHERE COALESCE(is_active,1)=1 AND COALESCE(official_url,'')<>'' "
           "ORDER BY id")
    rows = [r for r in conn.execute(sql)
            if args.force or not (r[3].strip() and r[4].strip() and r[5].strip())]
    if args.ids:
        wanted = {int(x) for x in args.ids.split(",") if x.strip()}
        rows = [r for r in rows if r["id"] in wanted]
    if args.limit > 0:
        rows = rows[: args.limit]
    if not rows:
        log("[跳过] 没有待处理的赛事")
        return 0
    log("[计划] 待处理 %d 条%s" % (len(rows), "" if args.apply else "（dry-run）"))

    if args.apply:
        log("[备份] %s" % discover_common.backup_db(db_path))

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    report = {"written": [], "skipped": [], "failed": []}
    for pos, row in enumerate(rows, 1):
        name = row["name"]
        trace: list = []
        try:
            text, hit_url = gather_text(session, row["official_url"].strip(), trace)
        except Exception as exc:  # noqa: BLE001
            report["failed"].append({"id": row["id"], "name": name, "reason": str(exc)[:120]})
            log("[%d/%d] %s 抓取失败：%s" % (pos, len(rows), name[:24], str(exc)[:80]))
            time.sleep(args.delay)
            continue

        if not good_text(text):
            report["skipped"].append({"id": row["id"], "name": name, "reason": "无可读正文",
                                      "trace": trace})
            log("[%d/%d] %s 跳过：官网无可读正文（%d 字）" % (pos, len(rows), name[:24], len(text)))
            time.sleep(args.delay)
            continue

        payload = clip_for_prompt(text)
        if args.show_text:
            log("  正文片段：%s…" % payload[:160].replace("\n", " "))
        try:
            data = llm.chat_json(PROMPT.format(name=name, text=payload), system=SYSTEM,
                                 temperature=0.2, max_tokens=1200,
                                 schema_hint=('{"participants": "", "content_scope": "", '
                                              '"exam_schedule": ""}'))
            if not isinstance(data, dict):
                raise ValueError("返回不是对象")
        except Exception as exc:  # noqa: BLE001
            report["failed"].append({"id": row["id"], "name": name, "reason": str(exc)[:120]})
            log("[%d/%d] %s 抽取失败：%r" % (pos, len(rows), name[:24], exc))
            time.sleep(args.delay)
            continue

        updates = {}
        for field in ("participants", "content_scope", "exam_schedule"):
            value = tidy(str(data.get(field) or ""))
            if not value:
                continue
            if row[field].strip() and not args.force:
                continue
            updates[field] = value
        if not updates:
            report["skipped"].append({"id": row["id"], "name": name, "reason": "模型未抽到字段",
                                      "trace": trace})
            log("[%d/%d] %s 未抽到可用字段" % (pos, len(rows), name[:24]))
            time.sleep(args.delay)
            continue

        if args.apply:
            db.update_competition(row["id"], updates, db_path=db_path)
        report["written"].append({"id": row["id"], "name": name, "source": hit_url,
                                  "fields": sorted(updates), "values": updates})
        log("[%d/%d] %s 写入 %s" % (pos, len(rows), name[:24], "/".join(sorted(updates))))
        for k, v in updates.items():
            log("      %s：%s" % (k, v[:100]))
        time.sleep(args.delay)

    out = os.path.join(os.path.dirname(db_path), "workspace",
                       "backfill_official_fields_%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(report, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log("\n[完成] 写入 %d 条，跳过 %d 条，失败 %d 条；报告 %s"
        % (len(report["written"]), len(report["skipped"]), len(report["failed"]), out))
    conn.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
