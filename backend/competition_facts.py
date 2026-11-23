# -*- coding: utf-8 -*-
"""赛事事实抽取与校验（competition_facts.py）

背景
----
历史数据只有赛氪一个来源：赛氪没有的赛事，我们就没有条目；赛氪日期错的
赛事（如中国大学生计算机设计大赛），我们跟着错。本模块把数据源从「爬虫
驱动」改成「官方链接驱动」：

  * 每条赛事强绑定 ``official_url``；
  * 定时/手动任务访问官网 → LLM 抽 ``{报名起止, 比赛起止}`` → 写入
    ``competition_facts_staging``（待人工确认），**绝不直接改主表**；
  * 管理员在后台「赛事校正台」逐条采纳 / 标记官方未公布 / 忽略；
  * 采纳后写主表，同时把「字段 → 值 → 来源 URL → 核对时间」记进
    ``competition_fact_ledger``，前台可展示"信息核对于 X 月 X 日"。

安全闸门（与 contest_dates.py 一致，宁可留空也不写错）
  * 只接受 ``YYYY-MM-DD`` 与 ``MM-DD``；
  * 年份超出 [今年-1, 今年+1] 丢弃；
  * 起止倒挂丢弃对应字段；
  * 抽取结果必须带 evidence（原文片段），后台展示给人工复核。

CLI 用法::

    python competition_facts.py --ids 10,23 --dry-run
    python competition_facts.py --limit 5
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

import db

log = logging.getLogger(__name__)

SHANGHAI = timezone(timedelta(hours=8))

# 参与校对的四个日期字段（主表列名）
FACT_FIELDS = (
    "reg_start_date",
    "reg_end_date",
    "contest_start_date",
    "contest_end_date",
)

FIELD_LABELS = {
    "reg_start_date": "报名开始",
    "reg_end_date": "报名截止",
    "contest_start_date": "比赛开始",
    "contest_end_date": "比赛结束",
}

MIN_YEAR_OFFSET = -1
MAX_YEAR_OFFSET = 1

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_MAX_HTML_BYTES = 600_000
_MAX_TEXT_CHARS = 8000
_MAX_PDF_BYTES = 8_000_000


# ---------------------------------------------------------------------------
# 取页面
# ---------------------------------------------------------------------------
_RE_SCRIPT = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_RE_TAG = re.compile(r"(?s)<[^>]+>")
_RE_SPACE = re.compile(r"[ \t\r\f\v\u00a0]+")
_RE_BLANK = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    """去脚本/样式/标签，压缩空白，保留换行（日期常按行排布）。"""
    text = _RE_SCRIPT.sub(" ", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section)>", "\n", text)
    text = _RE_TAG.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&")
        .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    )
    text = _RE_SPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _RE_BLANK.sub("\n\n", text).strip()


def fetch_html(url: str, *, timeout: int = 20) -> str:
    """抓页面原始 HTML；失败抛 RuntimeError（调用方只记日志）。"""
    if not url or not str(url).startswith(("http://", "https://")):
        raise RuntimeError("官网地址无效：%r" % url)
    resp = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        allow_redirects=True,
    )
    resp.raise_for_status()
    raw = resp.content[:_MAX_HTML_BYTES]
    return _decode_html(raw, resp)


_RE_CHARSET = re.compile(rb"(?i)charset\s*=\s*[\"']?\s*([a-z0-9_\-]+)")


def _decode_html(raw: bytes, resp: requests.Response) -> str:
    """按 meta charset / 响应头猜编码解码；中文站多为 gb2312/gbk，别当 utf-8 硬解。"""
    candidates: List[str] = []
    header = (resp.encoding or "").strip().lower()
    if header and header not in ("iso-8859-1", "ascii"):
        candidates.append(header)
    match = _RE_CHARSET.search(raw[:4096])
    if match:
        sniffed = match.group(1).decode("ascii", "ignore").lower()
        if sniffed in ("gb2312", "gbk", "gb_2312-80"):
            sniffed = "gb18030"
        if sniffed:
            candidates.append(sniffed)
    candidates += ["utf-8", "gb18030"]
    for enc in candidates:
        try:
            text = raw.decode(enc, errors="replace")
        except LookupError:
            continue
        # 替换符过多说明猜错了，换下一个
        if text.count("\ufffd") <= max(2, len(text) // 500):
            return text
    return raw.decode("utf-8", errors="replace")


def fetch_page(url: str, *, timeout: int = 20) -> str:
    """抓官网页面并转纯文本；失败抛 RuntimeError（调用方只记日志）。"""
    text = html_to_text(fetch_html(url, timeout=timeout))
    if len(text) < 80:
        raise RuntimeError("页面正文过短（%d 字），可能是 JS 渲染页" % len(text))
    return text[:_MAX_TEXT_CHARS]


# ---------------------------------------------------------------------------
# 首页没日期 → 顺着「通知/公告/赛程」链接再抓一级
# ---------------------------------------------------------------------------
_RE_A = re.compile(r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>")
_LINK_HINTS = (
    "通知", "公告", "赛程", "日程", "时间安排", "安排", "简章", "规程",
    "新闻", "动态", "notice", "news", "announce", "schedule", "contest",
)
_SKIP_EXT = (
    ".zip", ".rar", ".doc", ".docx", ".xls", ".xlsx",
    ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mp3",
)
_SCORE_WORDS = ("报名", "决赛", "初赛", "复赛", "参赛", "赛程", "日程", "省赛", "国赛")
_RE_DATEISH = re.compile(r"(\d{1,2}\s*[-月/]\s*\d{1,2}|\d{4}\s*年)")
_PDF_HINTS = ("通知", "简章", "规程", "赛程", "日程", "方案", "附件", "办法")


def fetch_pdf_text(url: str, *, timeout: int = 25) -> str:
    """下载 PDF 并抽文字（官方通知常是 PDF 附件）。"""
    if not url or not str(url).startswith(("http://", "https://")):
        raise RuntimeError("PDF 地址无效：%r" % url)
    resp = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        allow_redirects=True,
    )
    resp.raise_for_status()
    raw = resp.content[:_MAX_PDF_BYTES]
    if not raw.lstrip()[:4] == b"%PDF":
        raise RuntimeError("不是 PDF 文件")
    try:
        from pdfminer.high_level import extract_text as _pdf_text
        from io import BytesIO

        text = _pdf_text(BytesIO(raw)) or ""
    except ImportError as exc:
        raise RuntimeError("服务器缺少 pdfminer，无法解析 PDF 通知") from exc
    text = _RE_SPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if len(text) < 60:
        raise RuntimeError("PDF 里没抽到可用文字（可能是扫描件）")
    return text[:_MAX_TEXT_CHARS]


def fetch_text_any(url: str, *, timeout: int = 20) -> str:
    """按扩展名选 PDF 或 HTML 抓取。"""
    if urlparse(url).path.lower().endswith(".pdf"):
        return fetch_pdf_text(url, timeout=max(timeout, 25))
    return fetch_page(url, timeout=timeout)


def discover_notice_links(html: str, base_url: str, *, limit: int = 6,
                          relaxed: bool = False) -> List[str]:
    """从页面挑出「可能是通知/赛程正文」的同域链接。

    relaxed=True 时放宽标题判断（列表页里的文章标题常是「08-30 2026年…决赛」），
    用于第二跳。
    """
    base = urlparse(base_url)
    seen = {base_url.rstrip("/")}
    out: List[str] = []
    for href, frag in _RE_A.findall(html or ""):
        href = (href or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        link = urljoin(base_url, href).split("#")[0]
        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https") or parsed.netloc != base.netloc:
            continue
        if parsed.path.lower().endswith(_SKIP_EXT):
            continue
        label = html_to_text(frag)[:60].strip()
        is_pdf = parsed.path.lower().endswith(".pdf")
        if not label:
            if not is_pdf:
                continue
            label = parsed.path.rsplit("/", 1)[-1]
        low = label.lower()
        if is_pdf:
            # PDF 附件通常叫「点击下载 / 附件1」，只要标题像通知就收
            if not any(hint in low for hint in _PDF_HINTS):
                continue
        elif relaxed:
            if len(label) < 6:
                continue
            keywordish = any(hint in low for hint in _LINK_HINTS)
            dateish = bool(_RE_DATEISH.search(label))
            if not (keywordish or dateish):
                continue
        elif not any(hint in low for hint in _LINK_HINTS):
            continue
        key = link.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(link)
        if len(out) >= limit:
            break
    return out


def _page_score(text: str, year: int) -> int:
    score = len(date_tokens(text)) * 2
    score += sum(2 for word in _SCORE_WORDS if word in text)
    if str(year) in text:
        score += 4
    if len(text) > 3000:
        score += 1
    return score


def fetch_notice_pages(url: str, *, year: Optional[int] = None,
                       max_pages: int = 11, timeout: int = 10,
                       depth: int = 2) -> List[Dict[str, Any]]:
    """首页抓不到日期时，顺着链接往下找「含日期的那一页」。

    两跳：首页 → 通知列表/栏目页 → 具体通知正文（第二跳放宽标题判断，
    因为大量站点首页只挂「08-30 2026年…决赛」这样的文章标题）。
    总抓取页数受 max_pages 限制，PDF 最多解析 2 个。
    """
    year = year or datetime.now(SHANGHAI).year
    try:
        home_html = fetch_html(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 抓不到就当没有
        log.info("[探索] %s 首页抓取失败：%s", url, exc)
        return None

    found: List[Dict[str, Any]] = []
    fetched = 0
    pdf_budget = 2

    def _grab(link: str) -> Optional[str]:
        """抓候选页，HTML 页返回原文 HTML，PDF 返回抽取文本（失败 None）。"""
        nonlocal fetched, pdf_budget
        if fetched >= max_pages:
            return None
        is_pdf = urlparse(link).path.lower().endswith(".pdf")
        if is_pdf:
            if pdf_budget <= 0:
                return None
            pdf_budget -= 1
        fetched += 1
        try:
            if is_pdf:
                return fetch_pdf_text(link, timeout=max(timeout, 25))
            return fetch_html(link, timeout=timeout)
        except Exception:  # noqa: BLE001
            return None

    def _consider(link: str, raw: Optional[str]) -> None:
        if not raw:
            return
        text = raw if "<" not in raw[:200] else html_to_text(raw)
        if len(text) < 80 or not date_tokens(text):
            return
        found.append({"url": link, "text": text[:_MAX_TEXT_CHARS],
                      "score": _page_score(text, year)})

    level1 = discover_notice_links(home_html, url, limit=8)
    level2_seed: List[tuple] = []
    for link in level1:
        raw = _grab(link)
        if not raw:
            continue
        text = raw if "<" not in raw[:200] else html_to_text(raw)
        if date_tokens(text):
            _consider(link, raw)
        elif depth > 1 and len(level2_seed) < 2 and "<" in raw[:200]:
            # 列表/栏目页本身没有日期：从里面继续找文章链接
            level2_seed.append((link, raw))

    if not found and depth > 1:
        seen = {url.rstrip("/")}
        for base_link, base_html in level2_seed:
            for link in discover_notice_links(base_html, base_link, limit=6, relaxed=True):
                key = link.rstrip("/")
                if key in seen or fetched >= max_pages:
                    continue
                seen.add(key)
                _consider(link, _grab(link))
    found.sort(key=lambda item: item["score"], reverse=True)
    dedup: List[Dict[str, Any]] = []
    seen_urls = set()
    for item in found:
        if item["url"] in seen_urls:
            continue
        seen_urls.add(item["url"])
        dedup.append(item)
    return dedup[:3]


# ---------------------------------------------------------------------------
# 日期护栏
# ---------------------------------------------------------------------------
_RE_MD_TOKEN = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")


def date_tokens(text: str) -> set:
    """页面正文里出现过的 (月, 日) 集合，用于反幻觉校验。"""
    return {(int(m), int(d)) for m, d in _RE_MD_TOKEN.findall(text or "")}


def _mentions(text: str, iso: str) -> bool:
    """日期是否真在正文里出现过（M月D日 / MM-DD / YYYY-MM-DD 任一形态）。"""
    if not iso:
        return False
    month, day = int(iso[5:7]), int(iso[8:10])
    flat = re.sub(r"\s+", "", text or "")
    if iso in flat or ("%02d-%02d" % (month, day)) in flat:
        return True
    return "%d月%d" % (month, day) in flat or "%02d月%02d" % (month, day) in flat


def evidence_in_text(text: str, evidence: str) -> bool:
    """模型给的证据片段必须真的出自原文，否则整条不可信。"""
    snippet = re.sub(r"\s+", "", evidence or "")
    if len(snippet) < 4:
        return False
    return snippet[:24] in re.sub(r"\s+", "", text or "")


_RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RE_MD = re.compile(r"^(\d{2})-(\d{2})$")


def _valid_iso(iso: str, *, today: date) -> bool:
    m = _RE_ISO.match(iso)
    if not m:
        return False
    try:
        parsed = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    return today.year + MIN_YEAR_OFFSET <= parsed.year <= today.year + MAX_YEAR_OFFSET


def _normalize_date(value: Any, *, today: date) -> Optional[str]:
    """归一化为 YYYY-MM-DD；不合法返回 None（宁可留空，不猜）。"""
    if value in (None, "", "null", "None"):
        return None
    text = str(value).strip()
    if _RE_ISO.match(text):
        return text if _valid_iso(text, today=today) else None
    m = _RE_MD.match(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            candidate = date(today.year, month, day)
        except ValueError:
            return None
        if candidate < today - timedelta(days=180):
            candidate = date(today.year + 1, month, day)
        return candidate.isoformat() if _valid_iso(candidate.isoformat(), today=today) else None
    return None


def _parse_any(value: Any, *, today: date) -> Optional[date]:
    iso = _normalize_date(value, today=today)
    if not iso:
        return None
    try:
        return date(int(iso[:4]), int(iso[5:7]), int(iso[8:10]))
    except ValueError:
        return None


def sanitize_facts(facts: Dict[str, Any], *, today: Optional[date] = None) -> Dict[str, str]:
    """模型输出 -> 可信日期字典。所有不合法/倒挂的字段直接丢弃。"""
    today = today or datetime.now(SHANGHAI).date()
    clean: Dict[str, str] = {}
    for field in FACT_FIELDS:
        iso = _normalize_date(facts.get(field), today=today)
        if iso:
            clean[field] = iso
    reg_s, reg_e = clean.get("reg_start_date"), clean.get("reg_end_date")
    if reg_s and reg_e and reg_e < reg_s:
        log.warning("[事实] 丢弃报名截止 %s：早于开始 %s", reg_e, reg_s)
        clean.pop("reg_end_date", None)
    con_s, con_e = clean.get("contest_start_date"), clean.get("contest_end_date")
    if con_s and con_e and con_e < con_s:
        log.warning("[事实] 丢弃比赛结束 %s：早于开始 %s", con_e, con_s)
        clean.pop("contest_end_date", None)

    # 跨对矛盾（2026-11-23 补）：报名和比赛必须属于同一届。
    # 官网常见「本届报名时间 + 往届比赛时间」并列，模型会把两届拼成一条
    # （线上实证：#115 报名截止 2026-01-25 配 2025-01-31~02-15 的比赛，
    #  即报名晚于比赛结束）。这类数据按「比赛时间取自往届」处理：丢比赛对，
    # 保留报名对（报名原文通常是最新一届的），避免把陈旧赛期写进主表。
    reg_s, reg_e = clean.get("reg_start_date"), clean.get("reg_end_date")
    con_s, con_e = clean.get("contest_start_date"), clean.get("contest_end_date")
    if con_s or con_e:
        reg_mark = reg_e or reg_s
        con_mark = con_s or con_e
        conflict = ""
        if reg_mark and con_mark and reg_mark > con_mark:
            conflict = "报名(%s)晚于比赛(%s)" % (reg_mark, con_mark)
        elif reg_s and con_e and con_e < reg_s:
            conflict = "比赛结束(%s)早于报名开始(%s)" % (con_e, reg_s)
        if conflict:
            log.warning("[事实] 丢弃比赛日期：%s，两者不同届", conflict)
            clean.pop("contest_start_date", None)
            clean.pop("contest_end_date", None)
    return clean


# ---------------------------------------------------------------------------
# AI 抽取
# ---------------------------------------------------------------------------
EXTRACT_SYSTEM = (
    "你是严谨的赛事时间抽取器。只从给定网页文本中抽取报名与比赛的起止日期，"
    "绝不猜测、绝不编造；原文没写年份的不要自己编年份，原文只有月份的就输出 null。"
    "只输出 JSON。"
)

EXTRACT_SCHEMA_HINT = json.dumps(
    {
        "reg_start_date": "2026-03-01",
        "reg_end_date": "2026-04-30",
        "contest_start_date": None,
        "contest_end_date": "2026-07-25",
        "evidence": "报名时间：3月1日至4月30日；决赛于7月25日举行",
    },
    ensure_ascii=False,
)


def build_extract_prompt(name: str, text: str) -> str:
    return "\n".join([
        "从下面的网页文本中抽取该赛事的「报名时间」和「比赛时间」起止日期。",
        "",
        "赛事名称：%s" % (name or "（未提供）"),
        "",
        "规则：",
        "1. 报名开始/截止 → reg_start_date / reg_end_date；"
        "初赛、复赛、决赛、答辩等比赛环节 → contest_start_date / contest_end_date。",
        "2. 只输出 YYYY-MM-DD。原文只有「3月1日」没有年份时，按上下文推断唯一可能的年份；"
        "推断不唯一就输出 null。",
        "3. 原文只给月份（如「7月」）或「7月中旬」等模糊表述，输出 null，不要编造具体某一天。",
        "4. 一届赛事有多个阶段时，contest_start_date 取最早比赛阶段，"
        "contest_end_date 取最晚（如决赛结束）。",
        "5. 同一届赛事分多个赛项/赛道、各自报名时间不同（例如「软件赛、电子赛」与"
        "「视觉艺术设计赛」「专项赛」并列，或「全国赛」与「省赛」「校内赛」并列）时："
        "只取主赛项那一段——即覆盖全体高校在校生、面向全国的那个赛项（通常是正文最先出现、"
        "参赛对象最宽的那个）。分赛项/专项/校内选拔的日期一律忽略。"
        "evidence 里必须写出你取的是哪个赛项，例如「软件赛、电子赛报名时间：…」。",
        "6. evidence 必须是原文中出现过的片段（≤60 字），用于人工复核；找不到任何日期就输出 null。",
        "",
        "只输出 JSON，不要解释。格式：%s" % EXTRACT_SCHEMA_HINT,
        "",
        "原文：",
        str(text or "")[:7000],
    ])


def locate_evidence(text: str, evidence: str) -> str:
    """模型证据常是两句拼接；找出其中最长且确实出现在原文里的片段。"""
    flat = re.sub(r"\s+", "", evidence or "")
    hay = re.sub(r"\s+", "", text or "")
    if len(flat) < 6:
        return ""
    for size in (min(24, len(flat)), 18, 14, 10, 8):
        for i in range(0, len(flat) - size + 1):
            piece = flat[i:i + size]
            if piece in hay:
                return piece
    return ""


def snippet_around_fact(text: str, facts: Dict[str, str]) -> str:
    """从原文里截出日期所在的那一小段，保证人工复核看到的是真原文。"""
    src = re.sub(r"[ \t]+", " ", text or "")
    for iso in facts.values():
        for form in ("%d月%d日" % (int(iso[5:7]), int(iso[8:10])),
                     "%02d月%02d日" % (int(iso[5:7]), int(iso[8:10])),
                     iso, "%02d-%02d" % (int(iso[5:7]), int(iso[8:10])),
                     "%d月%d" % (int(iso[5:7]), int(iso[8:10]))):
            idx = src.find(form)
            if idx < 0:
                continue
            raw = src[max(0, idx - 40): idx + len(form) + 40]
            return re.sub(r"\s+", " ", raw).strip()
    return ""


def extract_facts(name: str, text: str, *, today: Optional[date] = None) -> Dict[str, Any]:
    """调一次模型抽取；返回 {"facts": {...}, "evidence": str}。失败抛 llm.LLMError。"""
    import llm

    data = llm.chat_json(
        build_extract_prompt(name, text),
        system=EXTRACT_SYSTEM,
        schema_hint=EXTRACT_SCHEMA_HINT,
        temperature=0.0,
        max_tokens=1200,
        retries=1,
    )
    if not isinstance(data, dict):
        raise llm.LLMError("事实抽取返回的不是 JSON 对象：%r" % type(data).__name__)
    if isinstance(data.get("fields"), dict):
        data = data["fields"]
    facts = sanitize_facts(data, today=today)
    evidence = str(data.get("evidence") or "").strip()[:200]
    # 反幻觉：证据片段必须真的出自原文，日期也必须在正文里出现过。
    # 官网首页常常只有导航和往届新闻，模型容易「凭记忆」编日期。
    # 模型常把两句话拼在一起当证据（原文里不连续），这里改成：先尝试从原文
    # 里截取证据中最长的真实片段，截不到就退化为「日期所在的原句」——
    # 两者都是原文，绝不放行模型自造文本。
    if facts and not evidence_in_text(text, evidence):
        located = locate_evidence(text, evidence)
        if located:
            log.info("[事实] 证据为跨句拼接，已改为原文片段：%s", located[:40])
            evidence = located
        else:
            evidence = snippet_around_fact(text, facts)
            if evidence:
                log.info("[事实] 证据与原文无交集，改用日期所在原句：%s", evidence[:40])
    dropped = [f for f, v in facts.items() if not _mentions(text, v)]
    if dropped:
        log.warning("[事实] 丢弃正文中不存在的日期字段：%s", dropped)
        facts = {f: v for f, v in facts.items() if f not in dropped}
    return {"facts": facts, "evidence": evidence}


# ---------------------------------------------------------------------------
# 与库中现值对比
# ---------------------------------------------------------------------------
def diff_against(competition: Dict[str, Any], facts: Dict[str, str]) -> List[Dict[str, Any]]:
    """返回字段级差异列表：[{field, label, current, incoming, kind}]。"""
    diffs: List[Dict[str, Any]] = []
    for field in FACT_FIELDS:
        incoming = facts.get(field)
        if not incoming:
            continue
        current = str(competition.get(field) or "").strip()
        if current == incoming:
            continue
        diffs.append({
            "field": field,
            "label": FIELD_LABELS[field],
            "current": current or None,
            "incoming": incoming,
            "kind": "change" if current else "fill",
        })
    return diffs


# ---------------------------------------------------------------------------
# 状态机：由日期推导当前阶段（不落库，随时可重算）
# ---------------------------------------------------------------------------
STATUS_LABELS = {
    "open": "报名中",
    "upcoming": "未开始",
    "closed": "已截止",
    "ongoing": "进行中",
    "finished": "已结束",
    "unknown": "待核实",
}


def compute_status(row: Dict[str, Any], *, today: Optional[date] = None) -> Dict[str, Any]:
    """根据四个日期推导阶段 + 倒计时。

    优先级：已结束 > 进行中 > 已截止 > 未开始 > 报名中 > 待核实。
    比赛时间存在时以比赛时间为主（报名截止后比赛开始前，落「已截止」）。
    """
    today = today or datetime.now(SHANGHAI).date()
    reg_s = _parse_any(row.get("reg_start_date"), today=today)
    reg_e = _parse_any(row.get("reg_end_date"), today=today)
    con_s = _parse_any(row.get("contest_start_date"), today=today)
    con_e = _parse_any(row.get("contest_end_date"), today=today)

    status = "unknown"
    if con_e and today > con_e:
        status = "finished"
    elif con_s and con_e and con_s <= today <= con_e:
        status = "ongoing"
    elif con_s and not con_e and con_s <= today:
        status = "ongoing"
    elif reg_e and today > reg_e:
        status = "closed" if not (con_s and today <= con_s) else "closed"
    elif reg_s and today < reg_s:
        status = "upcoming"
    elif reg_s and reg_e and reg_s <= today <= reg_e:
        status = "open"
    elif reg_e and today <= reg_e:
        status = "open"
    elif con_s and today < con_s:
        status = "upcoming"

    days_left = None
    if status in ("open", "upcoming") and reg_e:
        days_left = (reg_e - today).days
    elif status == "ongoing" and con_e:
        days_left = (con_e - today).days
    return {"status": status, "status_label": STATUS_LABELS[status], "days_left": days_left}


# ---------------------------------------------------------------------------
# 暂存队列：抓一条 -> 抽取 -> 写 staging（不碰主表）
# ---------------------------------------------------------------------------
def stage_one(competition_id: int, *, today: Optional[date] = None,
              source_url: Optional[str] = None) -> Dict[str, Any]:
    """对单条赛事执行「抓官网 → 抽日期 → 写待确认队列」。

    source_url: 优先用这条链接（官网首页没有日期时，管理员可填具体通知页）。
    """
    today = today or datetime.now(SHANGHAI).date()
    row = db.get_competition(int(competition_id))
    if not row:
        return {"ok": False, "competition_id": competition_id, "error": "赛事不存在"}
    url = (source_url or row.get("official_url") or "").strip()
    if not url:
        return {"ok": False, "competition_id": competition_id, "name": row["name"],
                "error": "缺少官网地址"}
    used_url = url
    first_error = ""
    candidates: List[Dict[str, Any]] = []
    try:
        text = fetch_text_any(url)
        if date_tokens(text):
            candidates.append({"url": url, "text": text, "score": 0})
        else:
            first_error = "页面里没有「几月几日」这类日期"
    except Exception as exc:  # noqa: BLE001 - 单条失败不影响批量
        first_error = str(exc)[:200]
        log.info("[事实] %s 首页不可用：%s", row["name"], exc)

    if not candidates:
        # 首页是导航/JS 渲染/无日期：顺着「通知/公告/赛程」链接往下找正文
        candidates = fetch_notice_pages(url, year=today.year)
        if not candidates:
            log.warning("[事实] %s 抽取失败：%s", row["name"], first_error)
            return {"ok": False, "competition_id": competition_id, "name": row["name"],
                    "error": (first_error or "官网没有可确认的日期") +
                             "；也没在首页找到含日期的通知页，请手动填通知链接"}

    result: Optional[Dict[str, Any]] = None
    last_error = ""
    for cand in candidates[:3]:
        try:
            attempt = extract_facts(row["name"], cand["text"], today=today)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响批量
            last_error = str(exc)[:200]
            continue
        if attempt["facts"]:
            result, used_url = attempt, cand["url"]
            break
        last_error = last_error or "官网未见可确认日期"
    if result is None:
        log.warning("[事实] %s 抽取失败：%s", row["name"], last_error)
        return {"ok": False, "competition_id": competition_id, "name": row["name"],
                "error": last_error}

    facts = result["facts"]
    diffs = diff_against(row, facts)
    snapshot = {field: (row.get(field) or None) for field in FACT_FIELDS}
    if not facts:
        # 页面能抓、但模型没抽出任何可信日期：记一笔「已抓过」好让 7 天冷却生效，
        # 但直接标记 rejected，不往人工核对队列里灌空行。
        sid = db.add_fact_staging(
            competition_id=int(competition_id),
            source_url=used_url,
            payload={"facts": {}, "evidence": result["evidence"], "diffs": []},
            current_snapshot=snapshot,
        )
        db.decide_fact_staging(sid, "rejected", note="官网未见可确认日期",
                               decided_by="system")
        return {"ok": False, "competition_id": int(competition_id), "name": row["name"],
                "error": "官网页面里没找到可确认的日期"}
    sid = db.add_fact_staging(
        competition_id=int(competition_id),
        source_url=used_url,
        payload={"facts": facts, "evidence": result["evidence"], "diffs": diffs},
        current_snapshot=snapshot,
    )
    return {
        "ok": True,
        "staging_id": sid,
        "competition_id": int(competition_id),
        "name": row["name"],
        "diffs": diffs,
        "facts": facts,
        "evidence": result["evidence"],
    }


def stage_batch(ids: Optional[List[int]] = None, *, limit: int = 5,
                source_url: Optional[str] = None,
                deadline_seconds: int = 90) -> Dict[str, Any]:
    """批量入队。默认挑「有官网 + 从未核对过」的赛事。

    下钻抓通知页时单条最慢能到 40 秒以上，所以整体有 deadline：
    超时的剩余条目不处理（下次轮次再来），保证 HTTP 请求和定时任务都跑得完。
    """
    if ids:
        targets = [int(i) for i in ids][:max(1, limit)]
    else:
        targets = db.list_competitions_for_fact_check(limit=max(1, limit))
    started = time.monotonic()
    results: List[Dict[str, Any]] = []
    for cid in targets:
        if results and time.monotonic() - started > deadline_seconds:
            results.append({"ok": False, "competition_id": cid,
                            "error": "本轮时间预算用尽，留到下次"})
            continue
        results.append(stage_one(cid, source_url=source_url))
    return {
        "total": len(results),
        "queued": sum(1 for r in results if r.get("ok")),
        "results": results,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="抓官网抽取赛事日期，写入待确认队列")
    parser.add_argument("--ids", default="", help="逗号分隔的赛事 ID")
    parser.add_argument("--limit", type=int, default=5, help="最多处理几条，默认 5")
    parser.add_argument("--dry-run", action="store_true", help="只打印抽取结果，不写队列")
    args = parser.parse_args()

    ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()]
    if args.dry_run:
        targets = ids or db.list_competitions_for_fact_check(limit=args.limit)
        for cid in targets:
            row = db.get_competition(cid)
            if not row:
                continue
            url = (row.get("official_url") or "").strip()
            print("\n==", cid, row["name"], url)
            if not url:
                print("  跳过：没有官网地址")
                continue
            try:
                result = extract_facts(row["name"], fetch_page(url))
            except Exception as exc:  # noqa: BLE001
                print("  失败：", exc)
                continue
            print("  facts:", result["facts"])
            print("  evidence:", result["evidence"])
            print("  diff:", diff_against(row, result["facts"]))
        return

    print(json.dumps(stage_batch(ids or None, limit=args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()