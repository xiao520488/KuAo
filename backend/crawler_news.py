# -*- coding: utf-8 -*-
"""
crawler_news.py —— 高校新闻抓取（单页 / 列表批量）-> 大模型抽取 -> awards 入库

流程：
    1. requests 抓取一个公开的高校新闻页（默认已选好一篇真实报道）
    2. BeautifulSoup 定位正文容器，抽成纯文本，并单独收集正文里的超链接
    3. 交给 llm.chat_json 抽取：作品名称 / 所属赛事 / 年份 / 奖项等级 /
       团队·学校 / 简介 / 原文链接
    4. Python 侧做字段校验（重点：source_url 的「GitHub 优先，否则新闻页 URL」规则）
    5. 调 db.create_award 落库，原文链接写入 source_url 字段

用法（在 backend 目录、激活 venv 后）：
    python crawler_news.py --db-path test_crawl.db --dry-run   # 只抓取/抽取，不写库
    python crawler_news.py --db-path test_crawl.db             # 写测试库（推荐默认）
    python crawler_news.py --allow-production                  # 手动解除红线，写生产库（危险）
    python crawler_news.py --url "https://..." --db-path test_crawl.db
    python crawler_news.py --save-html --db-path test_crawl.db
    python crawler_news.py --show-prompt --db-path test_crawl.db
    python crawler_news.py --show-raw --db-path test_crawl.db
    python crawler_news.py --save-json out.json --db-path test_crawl.db

批量模式（列表页 -> 翻页 -> 详情页循环）：
    python crawler_news.py --list-url "https://x/news_more_list.jsp?...p=1" \\
        --db-path test_crawl.db --max-pages 4 --max-items 10

    # 显式翻页规则（模板必须含 {page}）
    python crawler_news.py --list-url "https://x/list" \\
        --page-url "https://x/list?p={page}" --pages 1-3 \\
        --db-path test_crawl.db --max-items 8

孤立记录策略：
    赛事名匹配不到 competitions 时，自动创建一条「占位赛事」并挂接 award：
        category = 待确认，sort_order = 9999，official_url = 新闻页 URL，
        intro/summary 注明「爬虫自动创建，待人工审核归并」。
    同一赛事名再次抓取会命中占位记录，不会重复创建；赛事名彻底缺失时
    统一挂到「待确认赛事（爬虫自动创建）」桶里，保证 competition_id 不为 NULL。

依赖：
    requests、beautifulsoup4（lxml 可选：装了用 lxml，没装自动退回 html.parser）

注意：
    - 本脚本会真实发网络请求、真实调用大模型（消耗额度）、真实写库；
    - 抓取内容仅用于本地演示，请遵守目标站点的 robots.txt 与转载规定。
"""

import argparse
import difflib
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from urllib.parse import (
    parse_qs, urlencode, urljoin, urlparse, urlunparse,
)

import requests
from bs4 import BeautifulSoup, FeatureNotFound

import db
import llm

logger = logging.getLogger("crawler_news")


# ===========================================================================
# 一、常量与配置
# ===========================================================================

# 默认测试页：湖南师范大学信息科学与工程学院
# 《我院学子首获中国大学生计算机设计大赛国赛一等奖》
# 选它的原因：
#   1) 公开可访问，正文完整（作品名/赛事/年份/奖项/团队/简介齐全）；
#   2) 赛事「中国大学生计算机设计大赛」能匹配到本地 competitions 表，
#      写入后可在前端对应赛事下直接看到这条新记录；
#   3) 正文没有 GitHub/文档链接，正好验证「原文链接回退为新闻页 URL」。
# 备选样本（数学建模类，赛事不在 competitions 表中，会得到 competition_id=NULL）：
#   https://dqgc.gxu.edu.cn/info/1057/4039.htm
DEFAULT_URL = "https://cise.hunnu.edu.cn/info/1165/7066.htm"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = (10, 20)   # (连接, 读取) 秒
MAX_FETCH_RETRIES = 3
MAX_BODY_CHARS = 6000        # 送给大模型的正文上限（字符），控制 token 成本
MIN_BODY_CHARS = 200         # 低于该长度认为「没找到正文容器」

# 赛事匹配不到 competitions 时，自动创建的占位赛事配置
PLACEHOLDER_CATEGORY = "待确认"
PLACEHOLDER_SORT = 9999
PLACEHOLDER_INTRO = (
    "由 crawler_news.py 自动创建（待人工审核后归并到正式赛事）。"
    "原始赛事名：{raw_name}；来源：{source_url}"
)
PLACEHOLDER_SUMMARY = "爬虫自动创建的占位赛事，待人工确认"
MISSING_COMPETITION_NAME = "待确认赛事（爬虫自动创建）"

# 详情页 URL 特征（VSB / 常见 CMS）
DETAIL_URL_PATTERNS = (
    re.compile(r"/info/\d+/\d+\.(?:html?|shtml|jsp)$", re.I),
    re.compile(r"/art/\d{4}/\d{1,2}/\d{1,2}/", re.I),
    re.compile(r"/\d{4}/\d{2}/\d{2}/[^/]+\.(?:html?|shtml)$", re.I),
    re.compile(r"/(?:content|article|detail|news)/\d+", re.I),
    re.compile(r"/[^/]*\d{4,}\.html?$", re.I),
)

# 列表页标题粗筛关键词（只用于少调一次大模型，最终由 is_award_news 判定）
AWARD_TITLE_KEYWORDS = (
    "获", "奖", "竞赛", "大赛", "冠军", "亚军", "季军", "喜报", "表彰", "最佳",
)

# 正文粗筛关键词：明显没有获奖信号的页面直接跳过，节省 LLM 额度
AWARD_PAGE_KEYWORDS = (
    "获奖", "荣获", "斩获", "获得", "一等奖", "二等奖", "三等奖", "特等奖",
    "金奖", "银奖", "铜奖", "冠军", "亚军", "季军", "喜报", "最佳",
)

BATCH_MIN_DELAY = 2.0
BATCH_MAX_DELAY = 5.0

# 正文容器候选选择器：按优先级从「CMS 专用容器」到「通用语义标签」
CONTENT_SELECTORS = (
    "#vsb_content", ".v_news_content", "#vsb_content_2", "#vsb_content_4",
    "#vsb_content_100", "#js_content", ".rich_media_content",
    "div.TRS_Editor", "div.article-content", "div.article_content",
    "div.news_content", "div.entry-content", "div.post-content",
    "div.content-main", "div#content", "div.content", "article", "main",
)

# 结构性噪音标签（脚本/样式/导航等），抽取前整块删除。
# 注意：不要删 <form>——不少高校 CMS（如 VSB）会把正文整块包在 form 里，
# 删掉会连正文一起丢；表单里的 input/button/select 已单独清理。
NOISE_TAGS = (
    "script", "style", "noscript", "iframe", "svg", "canvas",
    "button", "select", "input", "nav", "aside",
)

# 正文字块标签（按文档顺序收集）
BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "li", "blockquote", "pre")

# 容器 class/id 中命中这些词，视为导航 / 分享 / 版权等噪音
NOISE_ATTR_RE = re.compile(
    r"(breadcrumb|crumb|share|comment|copyright|sidebar|footer|nav|menu|"
    r"related|recommend|advert|banner|toolbar|pagination|qrcode|wechat|weixin)",
    re.IGNORECASE,
)

# 逐行过滤的页面套话
NOISE_LINE_RES = tuple(
    re.compile(p)
    for p in (
        r"^(上一条|下一条|上一篇|下一篇|返回列表|返回顶部)\b",
        r"^(点击次数|阅读次数|浏览次数)\s*[:：]?",
        r"^(版权所有|Copyright\b)",
        r"^(地址|邮编|邮政编码|电话|传真|邮箱)\s*[:：]",
        r"^(友情链接|相关链接|分享到|扫一扫|关闭窗口|打印本页)",
        r"^(当前位置|您的位置)\s*[:：>]",
    )
)

# 空白与中文排版清理
_SPACE_RE = re.compile(r"[ \t\r\f\v\u00a0\u3000]+")
_CJK_GAP_RE = re.compile(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])")

# 发布时间候选 meta
_DATE_META_KEYS = {
    "article:published_time", "pubdate", "publishdate", "publish_date",
    "date", "dc.date", "dc.date.issued", "og:published_time",
}


class CrawlError(RuntimeError):
    """抓取 / 解析 / 抽取流程中的可预期失败。"""


# ===========================================================================
# 二、抓取
# ===========================================================================

def fetch_html(url: str, *, timeout=REQUEST_TIMEOUT, retries: int = MAX_FETCH_RETRIES) -> str:
    """抓取网页并返回解码后的 HTML 文本（带重试与编码纠偏）。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    last_exc = None
    attempts = 0
    for attempt in range(1, retries + 1):
        attempts = attempt
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            resp.raise_for_status()
            text = _decode_response(resp)
            if len(text.strip()) < MIN_BODY_CHARS:
                raise CrawlError(f"响应内容过短（{len(text)} 字符），可能不是正常网页")
            logger.info(
                "抓取成功：HTTP %s，%d 字节，编码 %s",
                resp.status_code, len(resp.content), resp.encoding,
            )
            return text
        except (requests.RequestException, CrawlError) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # 404/403 这类 4xx 重试没有意义，直接失败返回（429 限流除外）
            if status and 400 <= status < 500 and status != 429:
                logger.error("抓取失败：HTTP %s（4xx，不重试）", status)
                break
            if attempt >= retries:
                break
            delay = 1.5 * attempt
            logger.warning("第 %d/%d 次抓取失败：%s，%g 秒后重试", attempt, retries, exc, delay)
            time.sleep(delay)

    raise CrawlError(f"抓取失败（已尝试 {attempts} 次）：{url} -> {last_exc}")


def _decode_response(resp: requests.Response) -> str:
    """
    修正 requests 的编码判断。

    很多高校站点返回 `Content-Type: text/html` 却不带 charset，
    requests 会按 RFC 默认当成 ISO-8859-1，中文直接乱码；这里改用
    apparent_encoding（charset-normalizer 探测）纠正。
    """
    ctype = (resp.headers.get("content-type") or "").lower()
    apparent = resp.apparent_encoding
    if "charset=" not in ctype:
        if apparent:
            resp.encoding = apparent
    text = resp.text
    # 兜底：解码后仍出现大量替换符，说明声明的 charset 不可信
    if text.count("\ufffd") > 5 and apparent and apparent.lower() != (resp.encoding or "").lower():
        resp.encoding = apparent
        text = resp.text
    return text


# ===========================================================================
# 三、正文抽取
# ===========================================================================

def _make_soup(html: str) -> BeautifulSoup:
    """优先 lxml（容错好、快），未安装则退回标准库 html.parser。"""
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        logger.warning("未安装 lxml，回退到 html.parser（解析质量略低）")
        return BeautifulSoup(html, "html.parser")


def _clean_text(text: str) -> str:
    """清理零宽字符、折叠空白，并去掉中文之间的多余空格。"""
    if not text:
        return ""
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        text = text.replace(ch, "")
    text = _SPACE_RE.sub(" ", text)
    text = _CJK_GAP_RE.sub("", text)
    text = re.sub(r"\s+([，。、；：！？）》」』】”’])", r"\1", text)
    text = re.sub(r"([（《【「『“‘])\s+", r"\1", text)
    return text.strip()


def _is_noise_node(tag) -> bool:
    """按标签名 / class / id 判断是否噪音节点。"""
    if tag.name in NOISE_TAGS:
        return True
    attrs = " ".join(
        filter(None, [tag.get("id") or "", " ".join(tag.get("class") or [])])
    )
    return bool(attrs and NOISE_ATTR_RE.search(attrs))


def _strip_noise(soup: BeautifulSoup) -> None:
    """删除脚本、样式、导航、分享、版权等噪音节点。"""
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.decomposed or tag.name in ("html", "body"):
            continue
        if _is_noise_node(tag):
            tag.decompose()


def _in_noise(node) -> bool:
    """节点本身或任一祖先属于噪音容器。"""
    if _is_noise_node(node):
        return True
    return any(_is_noise_node(parent) for parent in node.parents)


def _is_noise_line(text: str) -> bool:
    return any(rx.search(text) for rx in NOISE_LINE_RES)


def _extract_title(soup: BeautifulSoup) -> str:
    """页面标题：og:title > 文章标题节点 > h1 > <title>（站点 h1 常是页脚，故加长度门槛）。"""
    og = soup.find("meta", attrs={"property": "og:title"}) or soup.find(
        "meta", attrs={"name": "title"}
    )
    if og and (og.get("content") or "").strip():
        return _clean_text(og["content"])

    selectors = (
        ".article-title", ".article_title", ".post-title", ".tit", "#title",
    )
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = _clean_text(node.get_text(" ", strip=True))
            if text:
                return text

    h1 = soup.select_one("h1")
    if h1:
        text = _clean_text(h1.get_text(" ", strip=True))
        if len(text) >= 6:   # 过滤“联系我们”这类页脚标题
            return text

    if soup.title:
        return _clean_text(soup.title.get_text(" ", strip=True))
    return ""


def _extract_site_name(soup: BeautifulSoup) -> str:
    """提取站点/学校名（og:site_name 或 <title> 的“-学校/学院”后缀）。"""
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in ("og:site_name", "sitename", "site_name"):
            value = _clean_text(meta.get("content") or "")
            if value:
                return value

    if soup.title:
        full = _clean_text(soup.title.get_text(" ", strip=True))
        match = re.match(r"^(.*?)\s*[-_|–—]\s*([^-_|–—]{4,60})$", full)
        if match:
            tail = match.group(2).strip()
            if tail.endswith(("大学", "学院", "新闻网", "官网", "中学")):
                return tail
    return ""


def _fmt_date(match) -> str:
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def _extract_publish_date(soup: BeautifulSoup) -> str:
    """从 meta / 信息栏 / 短日期条提取发布日期（YYYY-MM-DD），失败返回空串。"""
    date_re = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")
    label_re = re.compile(
        r"(?:发布时间|发布日期|发表时间|时间|日期)\s*[:：]?\s*"
        r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})"
    )
    # 纯日期条：2025年07月31日 15:37 点击：[...]
    bare_re = re.compile(
        r"^(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?"
        r"(?:\s*\d{1,2}:\d{2})?\s*(?=(?:点击|浏览|阅读|次数|发布|作者|来源|$))"
    )

    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").lower()
        if key in _DATE_META_KEYS:
            content = (meta.get("content") or "").strip()
            m = date_re.search(content)
            if m:
                return _fmt_date(m)

    for selector in (
        ".titx", ".post-attr", ".post-info", ".arti_metas", ".article-meta",
        ".article-info", ".article_info", ".show_info", ".news_info",
        ".info-bar", ".info", ".pubtime", ".publish-time", ".publish_time",
        ".time", ".date", ".source",
    ):
        for node in soup.select(selector):
            text = node.get_text(" ", strip=True)
            m = label_re.search(text) or date_re.search(text)
            if m:
                return _fmt_date(m)

    # 兜底：找文本很短的日期条（部分 CMS 不写 class，如 <div align="center">）
    for node in soup.find_all(["div", "span", "font", "p", "li", "td"]):
        text = node.get_text(" ", strip=True)
        if not text or len(text) > 60:
            continue
        m = label_re.search(text) or bare_re.match(text)
        if m:
            return _fmt_date(m)
    return ""


def _pick_container(soup: BeautifulSoup):
    """按选择器优先级找正文容器，失败则用文本密度兜底。"""
    for selector in CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node is None or node.decomposed:
            continue
        if len(node.get_text(" ", strip=True)) >= MIN_BODY_CHARS:
            return node, selector

    best, best_score = None, 0.0
    for tag in soup.find_all(["div", "article", "section"]):
        if tag.decomposed or _is_noise_node(tag):
            continue
        paragraphs = tag.find_all("p")
        p_len = sum(len(p.get_text(strip=True)) for p in paragraphs)
        if p_len < 80:
            continue
        total_len = len(tag.get_text(strip=True)) or 1
        link_len = sum(len(a.get_text(strip=True)) for a in tag.find_all("a"))
        score = p_len + 0.1 * total_len
        if link_len / total_len > 0.5:   # 链接占比高，多半是导航 / 列表页
            score *= 0.3
        if score > best_score:
            best, best_score = tag, score

    if best is not None and best_score >= MIN_BODY_CHARS:
        return best, "密度兜底"
    return soup, "整页兜底"


def _extract_blocks(container) -> list:
    """按文档顺序抽取段落文本，过滤空行、套话与重复段落。"""
    lines, seen = [], set()
    for node in container.find_all(BLOCK_TAGS):
        if node.decomposed or _in_noise(node):
            continue
        text = _clean_text(node.get_text(" ", strip=True))
        if len(text) < 2 or not re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", text):
            continue
        if _is_noise_line(text) or text in seen:
            continue
        seen.add(text)
        lines.append(text)
    return lines


def _collect_links(container, base_url: str) -> list:
    """收集正文内所有 http(s) 链接（绝对化、去重），供 Prompt 参考。"""
    links, seen = [], set()
    for a in container.find_all("a", href=True):
        if a.decomposed or _in_noise(a):
            continue
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        absolute = urlunparse(parsed._replace(fragment=""))
        if absolute in seen:
            continue
        seen.add(absolute)
        label = _clean_text(a.get_text(" ", strip=True))[:80] or parsed.netloc
        links.append({"url": absolute, "text": label})
        if len(links) >= 50:
            break
    return links


def extract_page(html: str, url: str) -> dict:
    """HTML -> 标题 / 发布日期 / 正文纯文本 / 正文链接。"""
    soup = _make_soup(html)
    title = _strip_site_suffix(_extract_title(soup))
    site_name = _extract_site_name(soup)
    publish_date = _extract_publish_date(soup)

    _strip_noise(soup)
    container, selector = _pick_container(soup)

    lines = _extract_blocks(container)
    body = "\n".join(lines).strip()

    # 段落标签不足（如整篇塞在 span 里）时，退回容器整体文本
    if len(body) < MIN_BODY_CHARS:
        fallback = [
            _clean_text(x)
            for x in container.get_text("\n", strip=True).splitlines()
        ]
        fallback = [
            x for x in fallback
            if x and not _is_noise_line(x)
            and re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", x)
        ]
        candidate = "\n".join(dict.fromkeys(fallback))
        if len(candidate) > len(body):
            body = candidate

    if len(body) < MIN_BODY_CHARS:
        raise CrawlError(f"正文抽取失败（仅 {len(body)} 字符），请检查页面结构或选择器")

    return {
        "url": url,
        "title": title,
        "site_name": site_name,
        "publish_date": publish_date,
        "body": body,
        "links": _collect_links(container, url),
        "selector": selector,
    }


# ===========================================================================
# 三之二、列表页解析与翻页
# ===========================================================================

def _looks_like_detail_url(path: str) -> bool:
    """按 URL 路径判断是否像新闻详情页（而不是栏目页/列表页）。"""
    if not path:
        return False
    return any(pattern.search(path) for pattern in DETAIL_URL_PATTERNS)


def extract_detail_entries(html: str, list_url: str) -> list:
    """
    解析列表页，返回 [{"url": 详情页绝对 URL, "title": 链接文字}]。

    规则：
        - 只保留与列表页同域名的 http(s) 链接；
        - URL 必须命中详情页特征（/info/{栏目}/{id}.htm 等）；
        - 去掉锚点、去重，保持页面出现顺序。
    """
    soup = _make_soup(html)
    host = urlparse(list_url).netloc.lower()
    entries, seen = [], set()

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        absolute = urljoin(list_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower() != host:
            continue
        if not _looks_like_detail_url(parsed.path):
            continue
        clean = urlunparse(parsed._replace(fragment=""))
        if clean == list_url or clean in seen:
            continue
        seen.add(clean)
        title = _clean_text(anchor.get_text(" ", strip=True))
        entries.append({"url": clean, "title": title[:120]})

    return entries


def extract_detail_links(html: str, list_url: str) -> list:
    """任务入口：只返回详情页 URL 列表。"""
    return [entry["url"] for entry in extract_detail_entries(html, list_url)]


def fetch_detail_links(list_url: str) -> list:
    """便利入口：传入列表页 URL，抓取并返回该页的详情页 URL 列表。"""
    html = fetch_html(list_url)
    return extract_detail_links(html, list_url)


def find_next_page_url(html: str, page_url: str):
    """
    自动判断“下一页”：优先 rel=next / class=next，其次锚文本
    下一页 / 下页 / Next / › / » / >>。找不到返回 None。
    """
    soup = _make_soup(html)
    host = urlparse(page_url).netloc.lower()
    next_texts = {"下一页", "下页", "下一頁", "next", "›", "»", ">>"}

    for anchor in soup.find_all("a", href=True):
        text = _clean_text(anchor.get_text(" ", strip=True))
        rel_attr = anchor.get("rel") or []
        rel = " ".join(rel_attr) if isinstance(rel_attr, (list, tuple)) else str(rel_attr)
        cls_attr = anchor.get("class") or []
        cls = " ".join(cls_attr) if isinstance(cls_attr, (list, tuple)) else str(cls_attr)

        is_next = (
            "next" in rel.lower()
            or "next" in cls.lower()
            or text in next_texts
            or text.lower() in next_texts
        )
        if not is_next:
            continue

        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host:
            continue
        clean = urlunparse(parsed._replace(fragment=""))
        if clean and clean != page_url:
            return clean

    return None


def _parse_page_spec(spec: str) -> list:
    """解析页码表达式：'1-3' / '1,2,3' / '2-4,7'。"""
    pages = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                start, end = end, start
            pages.extend(range(start, end + 1))
        else:
            pages.append(int(part))
    if not pages:
        raise CrawlError("--pages 为空或格式非法")
    if any(page < 1 for page in pages):
        raise CrawlError("--pages 页码必须 >= 1")
    return pages


def build_page_urls(list_url: str, pages_spec=None, page_url_template=None) -> list:
    """
    生成显式翻页 URL：
        --page-url "https://x/list?p={page}" --pages 1-3
        --list-url "https://x/list?page=1" --pages 1-3   # 自动替换 page 参数
    """
    if not pages_spec and not page_url_template:
        return [list_url]

    pages = _parse_page_spec(pages_spec) if pages_spec else [1]
    if page_url_template:
        if "{page}" not in page_url_template:
            raise CrawlError("--page-url 必须包含 {page} 占位符")
        return [page_url_template.format(page=page) for page in pages]

    urls = []
    parsed = urlparse(list_url)
    for page in pages:
        query = parse_qs(parsed.query, keep_blank_values=True)
        query["page"] = [str(page)]
        urls.append(urlunparse(parsed._replace(query=urlencode(query, doseq=True))))
    return urls


def _looks_like_award_title(title: str) -> bool:
    """列表页标题粗筛：命中获奖/竞赛类关键词才进入详情抓取。"""
    text = title or ""
    return any(keyword in text for keyword in AWARD_TITLE_KEYWORDS)


def _looks_like_award_page(page: dict) -> bool:
    """正文粗筛：连“获奖/荣获/一等奖”都没有的页面不调用大模型。"""
    blob = f"{page.get('title') or ''}{page.get('body') or ''}"
    return any(keyword in blob for keyword in AWARD_PAGE_KEYWORDS)


def _polite_sleep(label: str = "") -> None:
    """模拟真人：每次网络动作之间随机等待 2~5 秒。"""
    delay = random.uniform(BATCH_MIN_DELAY, BATCH_MAX_DELAY)
    logger.info("随机延迟 %.1f 秒（%s）", delay, label or "批次间隔")
    time.sleep(delay)


# ===========================================================================
# 四、Prompt 与字段校验
# ===========================================================================

SYSTEM_PROMPT = (
    "你是一个严谨的信息抽取引擎，负责从高校新闻正文中提取获奖信息。"
    "先判断这篇新闻是否属于学科竞赛/创新创业竞赛/学术竞赛的获奖报道，再抽取字段。"
    "只输出 JSON，不要输出任何解释性文字，不要使用 Markdown 代码围栏。"
    "严禁编造正文中不存在的信息；正文没有的字段填 null 或空字符串。"
)

# 追加在 Prompt 末尾的格式说明（chat_json 会拼到【输出格式要求】下）。
# 刻意不写具体赛事名/年份示例，避免模型照抄示例而不是正文。
SCHEMA_HINT = """只输出一个 JSON 对象，且仅包含以下 9 个键（值按正文填写，不要照抄说明文字）：
  is_award_news —— 布尔值，是否为学科竞赛/创新创业竞赛/学术竞赛的获奖新闻；
                   招聘、会议、通知、体育比赛、教学活动、教师个人荣誉、奖学金公示填 false
  title       —— 字符串，作品名称，不带书名号
  competition —— 字符串，所属赛事全称，含届次/赛区
  year        —— 整数或 null，获奖年份（四位）
  award_level —— 字符串，奖项等级，保留“国家级/省级”等前缀
  team        —— 字符串，团队/学校，含学校、学院、参赛队员、指导教师
  summary     —— 字符串，80~200 字中文简介
  source_url  —— 字符串，http 或 https 开头的原文链接
  confidence  —— 0~1 的小数，表示抽取把握
不要输出 JSON 以外的任何内容。"""


def build_prompt(page: dict, max_chars: int = MAX_BODY_CHARS) -> str:
    """拼装抽取 Prompt，明确「GitHub/文档链接优先，否则用新闻页 URL」。"""
    body = page["body"]
    truncated = len(body) > max_chars
    body = body[:max_chars]
    if truncated:
        body += "\n……（正文过长，已截断）"

    if page["links"]:
        link_lines = "\n".join(
            f"- {item['url']}（锚文本：{item['text']}）" for item in page["links"]
        )
    else:
        link_lines = "（正文中未发现超链接）"

    return (
        "【新闻页元信息】\n"
        f"网页 URL：{page['url']}\n"
        f"网页标题：{page['title'] or '（未取到标题）'}\n"
        f"站点/学校：{page.get('site_name') or '（未取到）'}\n"
        f"发布日期：{page['publish_date'] or '（未取到）'}\n\n"
        f"【正文纯文本】\n{body}\n\n"
        f"【正文中出现的链接】\n{link_lines}\n\n"
        "【抽取要求】\n"
        "1. 只依据上面的正文与链接抽取，不得编造；正文没有的信息填 null 或空字符串。\n"
        "2. 抽取这篇报道中最核心的一条获奖记录，字段含义：\n"
        "   - is_award_news：先判断本页是否为学科竞赛/创新创业竞赛/学术竞赛的获奖新闻；\n"
        "     招聘、会议、通知、体育比赛、教学活动、教师个人荣誉、奖学金公示一律填 false。\n"
        "   - title：作品名称。正文用《》标出的作品名要去掉书名号。\n"
        "   - competition：所属赛事全称，保留届次、杯赛名；不要把学校名写进赛事名。\n"
        "   - year：获奖年份（四位整数）。正文没写年份时，可参考“发布日期”的年份；\n"
        "     仍无法判断则填 null。\n"
        "   - award_level：奖项等级，如“全国一等奖”“省级二等奖”；保留级别前缀。\n"
        "   - team：团队/学校，优先写成“学校+学院+队员+指导教师”。正文只写“我院”\n"
        "     这类简称时，用上面的“站点/学校”补全学校名；正文出现多少写多少，\n"
        "     不要补人名。\n"
        "   - summary：80~200 字中文简介，概括作品解决的问题、使用的方法、取得的结果。\n"
        "   - confidence：0~1 的小数，表示你对本次抽取的把握。\n"
        "3. 【原文链接规则，极其重要】source_url 只能填一个 URL：\n"
        "   a) 如果正文或链接列表里出现了与获奖作品直接相关的 GitHub / GitLab / Gitee 仓库、\n"
        "      项目主页、论文 PDF、技术文档、数据集等链接，优先提取该链接作为原文链接；\n"
        "   b) 如果正文没有提到这类链接，必须原样使用上面给出的“网页 URL”作为原文链接\n"
        "      （新闻页本身就是原文）；\n"
        "   c) 不要返回导航栏、广告、图片、公众号二维码等无关链接；\n"
        "   d) 必须是完整的 http:// 或 https:// 链接，不要用相对路径。\n"
        "4. 不要输出 Markdown 标记，不要输出 JSON 以外的任何内容。"
    )


def _first_value(raw: dict, keys) -> object:
    """按别名顺序取第一个非空字段值（兼容模型返回中文字段名）。"""
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return None


def _to_year(value) -> object:
    """年份归一化：整数 / 字符串 / 带“年”字都能处理；非法返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        year = int(value)
    else:
        match = re.search(r"(19|20)\d{2}", str(value))
        if not match:
            return None
        year = int(match.group(0))
    return year if 1900 <= year <= 2100 else None


def _to_bool(value, default=None):
    """布尔归一化：兼容 true/false/1/0/是/否 等模型写法。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("false", "0", "no", "否", "不是", "非"):
        return False
    if text in ("true", "1", "yes", "是"):
        return True
    return default


def _strip_book_marks(text: str) -> str:
    return text.strip().strip("《》").strip()


def _strip_site_suffix(title: str) -> str:
    """去掉“文章标题-学校名”这类站点后缀（只处理明显像站点名的尾巴）。"""
    match = re.match(r"^(.*?)\s*[-_|–—]\s*([^-_|–—]{2,30})$", title)
    if not match:
        return title
    head, tail = match.group(1).strip(), match.group(2).strip()
    if len(head) >= 8 and any(k in tail for k in ("大学", "学院", "新闻网", "官网", "中学")):
        return head
    return title


def _team_has_school(team: str, site_name: str) -> bool:
    """判断 team 是否已经包含站点/学校名（允许只写学校简称）。"""
    for length in (len(site_name), 10, 8, 6, 4):
        if 4 <= length <= len(site_name) and site_name[:length] in team:
            return True
    return False


def _validate_source_url(raw_url, page_url: str, links: list) -> str:
    """
    校验大模型返回的 source_url：
        - 必须是 http(s) 绝对链接；
        - 命中正文链接或等于新闻页 URL：接受；
        - 同站点根路径（多半是导航首页）：拒绝并回退新闻页 URL；
        - 其它（例如模型补全的 GitHub 地址）：接受。
    返回最终写库的 URL。
    """
    if not raw_url:
        return page_url

    candidate = str(raw_url).strip().strip("<>\"'").strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        logger.warning("模型返回的 source_url 非法（%r），回退为新闻页 URL", raw_url)
        return page_url

    normalized = urlunparse(parsed._replace(fragment=""))
    known = {item["url"] for item in links}

    if normalized == page_url or normalized in known:
        return normalized

    page_host = urlparse(page_url).netloc.lower()
    if parsed.netloc.lower() == page_host and parsed.path in ("", "/"):
        logger.warning("模型返回的是站点首页（%s），回退为新闻页 URL", normalized)
        return page_url

    logger.info("采用模型给出的原文链接：%s", normalized)
    return normalized


def normalize_extraction(raw, page: dict) -> dict:
    """把模型输出规整成固定字段的 dict，并做兜底与校验。"""
    if isinstance(raw, list):
        raw = next((item for item in raw if isinstance(item, dict)), None)
    if not isinstance(raw, dict):
        raise CrawlError(f"模型未返回 JSON 对象：{type(raw).__name__}")

    # 兼容 {"awards": [...]} 这类包了一层的输出
    if "awards" in raw and isinstance(raw["awards"], list) and raw["awards"]:
        first = raw["awards"][0]
        if isinstance(first, dict):
            raw = first

    title = _first_value(raw, ("title", "work_name", "work_title", "作品名称", "作品名", "作品"))
    competition = _first_value(raw, ("competition", "contest", "event", "所属赛事", "赛事名称", "赛事"))
    award_level = _first_value(raw, ("award_level", "award", "prize", "奖项等级", "奖项", "获奖等级"))
    team = _first_value(raw, ("team", "school_team", "team_school", "团队", "团队/学校", "学校", "团队学校"))
    summary = _first_value(raw, ("summary", "intro", "description", "简介", "作品简介"))
    source_url = _first_value(raw, ("source_url", "url", "original_url", "原文链接", "链接"))
    year = _to_year(_first_value(raw, ("year", "年份", "获奖年份")))
    confidence = _first_value(raw, ("confidence", "置信度"))
    is_award = _to_bool(
        _first_value(raw, ("is_award_news", "is_award", "是否获奖新闻")),
        default=None,
    )

    title = _strip_book_marks(_clean_text(str(title))) if title else ""
    if not title:
        title = _strip_site_suffix(page["title"]) or "未命名获奖作品"
        logger.warning("模型未给出作品名称，标题回退为：%s", title)

    if is_award is None:
        # 模型没显式返回时，用关键字段是否齐全兜底判断
        is_award = bool(title and competition and award_level)

    try:
        confidence = float(confidence) if confidence is not None else 0.6
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = min(max(confidence, 0.0), 1.0)

    summary = _clean_text(str(summary))[:600] if summary else ""

    team_text = _clean_text(str(team)) if team else ""
    site_name = _clean_text(str(page.get("site_name") or ""))
    if site_name:
        if not team_text:
            team_text = site_name
        elif not _team_has_school(team_text, site_name):
            # 正文常用“我院”简称，用页面标题里的学校/学院补全，避免团队字段缺学校
            team_text = f"{site_name} {team_text}".strip()

    return {
        "title": title,
        "competition": _clean_text(str(competition)) if competition else "",
        "year": year,
        "award_level": _clean_text(str(award_level)) if award_level else "",
        "team": team_text,
        "summary": summary,
        "source_url": _validate_source_url(source_url, page["url"], page["links"]),
        "confidence": round(confidence, 2),
        "is_award_news": is_award,
    }


def extract_award(page: dict, max_chars: int = MAX_BODY_CHARS,
                  show_prompt: bool = False, show_raw: bool = False):
    """正文 -> 大模型 JSON -> (原始 raw dict, 规整后的获奖信息 dict)。"""
    prompt = build_prompt(page, max_chars)
    if show_prompt:
        print("\n" + "=" * 78 + "\n【最终 Prompt】\n" + "=" * 78)
        print(prompt)
        print("=" * 78 + "\n")

    logger.info("调用大模型抽取（model=%s，正文 %d 字符，链接 %d 个）……",
                llm.MODEL, min(len(page["body"]), max_chars), len(page["links"]))
    started = time.time()
    raw = llm.chat_json(
        prompt,
        system=SYSTEM_PROMPT,
        schema_hint=SCHEMA_HINT,
        temperature=0.1,
        max_tokens=3072,
    )
    logger.info("大模型返回，耗时 %.1f 秒", time.time() - started)

    if show_raw:
        print("\n" + "=" * 78 + "\n【大模型原始 JSON】\n" + "=" * 78)
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        print("=" * 78)

    return raw, normalize_extraction(raw, page)


# ===========================================================================
# 五、落库
# ===========================================================================

def _norm_name(name: str) -> str:
    """赛事名归一化：去空白、标点与引号，转小写，便于比对。"""
    if not name:
        return ""
    text = re.sub(r"[\s\u3000]+", "", str(name))
    text = re.sub(r"[，。、；：！？,.;:!?（）()\[\]【】《》\"'“”‘’\-—_/\\|]+", "", text)
    return text.lower()


# 赛事名模糊匹配阈值：过高会漏（转占位赛事，人工归并），过低会串台（错误挂到别的赛事）
FUZZY_MATCH_THRESHOLD = 0.86
# 子串匹配的最短赛事名长度（低于此长度的高频词，如「蓝桥杯」，容易把别的赛事吸进来）
MIN_SUBSTRING_NAME_LEN = 8
# 占位赛事不参与匹配，避免爬虫把自己的桶当成真赛事
PLACEHOLDER_NAME_MARK = "待确认"


def match_competition_id(name: str):
    """把抽取到的赛事名映射到 competitions.id。

    规则（由严到宽，宁可返回 None 走占位赛事，也不错误串台）：
    1. 归一化后完全相等 -> 直接命中；
    2. 双向子串命中 -> 取「赛事名最长」的那个（最具体）；若并列最长则视为歧义，返回 None；
    3. 相似度兜底 -> 阈值 0.86（原 0.75 过低，容易把近似赛事名吸过来）。

    None 的语义：调用方会创建/复用「待确认赛事（爬虫自动创建）」占位记录，
    由人工归并，比错误挂到蓝桥杯这类高名气赛事上安全得多。
    """
    if not name:
        return None
    try:
        competitions = db.list_competitions()
    except Exception as exc:  # noqa: BLE001 - 映射失败不应阻断写库
        logger.warning("读取 competitions 失败，跳过赛事映射：%s", exc)
        return None

    target = _norm_name(name)
    if not target:
        return None

    hits: list[tuple[int, object, str]] = []
    for comp in competitions:
        comp_name = _norm_name(comp.get("name") or "")
        if not comp_name or PLACEHOLDER_NAME_MARK in comp_name:
            continue
        if target == comp_name:
            logger.info("赛事名精确匹配：%s -> id=%s", name, comp.get("id"))
            return comp.get("id")
        if (len(comp_name) >= MIN_SUBSTRING_NAME_LEN and comp_name in target) or (
            len(target) >= MIN_SUBSTRING_NAME_LEN and target in comp_name
        ):
            hits.append((len(comp_name), comp.get("id"), comp_name))

    if hits:
        hits.sort(key=lambda item: item[0], reverse=True)
        if len(hits) > 1 and hits[0][0] == hits[1][0]:
            logger.warning(
                "赛事名同时命中多个赛事，无法判定归属，转占位待人工归并：%s -> %s",
                name,
                [hit[2] for hit in hits[:3]],
            )
            return None
        best = hits[0]
        logger.info("赛事名子串匹配：%s -> id=%s（赛事名 %s，长度 %s）", name, best[1], best[2], best[0])
        return best[1]

    best_id, best_ratio, best_name = None, 0.0, ""
    for comp in competitions:
        comp_name = _norm_name(comp.get("name") or "")
        if not comp_name or PLACEHOLDER_NAME_MARK in comp_name:
            continue
        ratio = difflib.SequenceMatcher(None, target, comp_name).ratio()
        if ratio > best_ratio:
            best_id, best_ratio, best_name = comp.get("id"), ratio, comp_name
    if best_id is not None and best_ratio >= FUZZY_MATCH_THRESHOLD:
        logger.warning(
            "赛事名模糊匹配（低置信，建议人工复核）：%s -> id=%s（相似度 %.2f，赛事名 %s）",
            name,
            best_id,
            best_ratio,
            best_name,
        )
        return best_id
    logger.info("赛事名未匹配到 competitions 表（相似度 %.2f）：%s", best_ratio, name)
    return None


def _canonical_competition_name(name: str) -> str:
    """占位赛事名归一化：去掉年份前缀与“第 N 届”，让同一赛事跨年份复用一条记录。"""
    text = _clean_text(str(name or ""))
    text = re.sub(r"^(19|20)\d{2}\s*年?\s*", "", text)
    text = re.sub(r"[（(]\s*第?\s*[0-9一二三四五六七八九十百零]+\s*届\s*[）)]", "", text)
    text = re.sub(r"[（(]\s*(19|20)\d{2}\s*[）)]", "", text)
    text = re.sub(r"第\s*[0-9一二三四五六七八九十百零]+\s*届\s*", "", text)
    text = re.sub(r"^(19|20)\d{2}\s*年?\s*", "", text)
    text = text.strip(" -—–·、，,;；")
    return text or _clean_text(str(name or ""))


def _infer_competition_level(name: str, award_level: str) -> str:
    """占位赛事级别：出现“省/赛区”即按省级，否则国家级。"""
    blob = f"{name or ''}{award_level or ''}"
    if any(keyword in blob for keyword in ("省赛", "省级", "赛区", "省一等奖", "省二等奖", "省三等奖")):
        return "provincial"
    return "national"


def ensure_competition(name: str, page: dict, award_level: str = "",
                       dry_run: bool = False):
    """
    保证 award 有可挂接的 competition_id。

    策略：先走正常匹配；匹配不到时自动创建「占位赛事」——
        category=待确认、sort_order=9999、official_url=新闻页 URL，
        intro/summary 明确标注由爬虫创建、待人工审核归并。
    同一赛事重复抓取会命中占位记录，不会重复创建；赛事名缺失时统一挂到
    固定占位桶，确保 competition_id 不为 NULL。

    返回 (competition_id, status)：
        matched / created / would_create / failed
    """
    raw_name = _clean_text(str(name or ""))
    effective_name = raw_name or MISSING_COMPETITION_NAME

    matched = match_competition_id(effective_name)
    if matched:
        return matched, "matched"

    if dry_run:
        logger.info("dry-run：赛事「%s」未匹配，真实运行时会自动创建占位赛事", effective_name)
        return None, "would_create"

    canonical = _canonical_competition_name(effective_name) if raw_name else effective_name
    try:
        # 再做一次归一化精确比对，避免年份/届次写法不同导致重复占位
        target = _norm_name(canonical)
        for comp in db.list_competitions():
            if _norm_name(comp.get("name") or "") == target:
                logger.info("命中已存在的占位赛事：%s -> id=%s", comp.get("name"), comp.get("id"))
                return comp.get("id"), "matched"

        level = _infer_competition_level(raw_name, award_level)
        cid = db.add_competition({
            "name": canonical,
            "category": PLACEHOLDER_CATEGORY,
            "official_url": page.get("url"),
            "level": level,
            "level_label": "省级" if level == "provincial" else "国家级",
            "sort_order": PLACEHOLDER_SORT,
        })
    except Exception as exc:  # noqa: BLE001 - 占位失败不能阻断 award 落库
        logger.error(
            "自动创建占位赛事失败（award 仍会保存，competition_raw 保留赛事名）：%r", exc
        )
        return None, "failed"

    # 说明文字写失败不影响挂接，占位记录已经创建成功
    try:
        db.update_competition(cid, {
            "intro": PLACEHOLDER_INTRO.format(
                raw_name=raw_name or effective_name, source_url=page.get("url") or ""
            ),
            "summary": PLACEHOLDER_SUMMARY,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("占位赛事说明写入失败（不影响挂接）：%r", exc)

    logger.warning(
        "赛事「%s」不在 competitions 表中，已自动创建占位赛事 id=%s（category=%s）",
        canonical, cid, PLACEHOLDER_CATEGORY,
    )
    return cid, "created"


def build_award_row(extracted: dict, page: dict, competition_id, raw_html_path=None) -> dict:
    """组装 db.create_award 需要的数据；source_url 存原文链接。"""
    hash_basis = "|".join(
        [extracted["source_url"], extracted["title"], str(extracted["year"] or "")]
    )
    content_hash = hashlib.sha256(hash_basis.encode("utf-8")).hexdigest()

    row = {
        "competition_id": competition_id,
        "competition_raw": extracted["competition"] or None,
        "title": extracted["title"],
        "year": extracted["year"],
        "award_level": extracted["award_level"] or None,
        # 老库/前端 Tab2 读的是 award_grade，这里双写保持兼容
        "award_grade": extracted["award_level"] or None,
        "team": extracted["team"] or None,
        "summary": extracted["summary"] or None,
        "source_url": extracted["source_url"],
        "source_type": "crawl",
        "confidence": extracted["confidence"],
        "review_status": "pending",
        # 按项目约定：SQLite 已更新、待同步 Chroma 的脏标记
        "embedding_state": "stale",
        "content_hash": content_hash,
    }
    if raw_html_path:
        row["raw_html_path"] = raw_html_path
    return row


def save_raw_html(html: str, url: str) -> str:
    """把原始 HTML 存到 workspace/crawl_cache，返回相对 backend 目录的路径。"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cache_dir = os.path.join(base_dir, "workspace", "crawl_cache")
    os.makedirs(cache_dir, exist_ok=True)
    host = urlparse(url).netloc.replace(":", "_") or "unknown"
    filename = f"{datetime.now():%Y%m%d-%H%M%S}_{host}.html"
    path = os.path.join(cache_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return os.path.relpath(path, base_dir)


def save_award(row: dict):
    """写入 awards 表；content_hash 冲突返回 (None, IntegrityError)，其它库错误也会捕获。"""
    try:
        db.init_db()
        return db.create_award(row), None
    except sqlite3.IntegrityError as exc:
        return None, exc
    except sqlite3.Error as exc:
        return None, exc


# ===========================================================================
# 六、列表页批量采集
# ===========================================================================

def _source_url_exists(url: str):
    """按 source_url 查重，返回已有 award id；查询失败返回 None（走 content_hash 兜底）。"""
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM awards WHERE source_url = ? LIMIT 1", (url,)
            ).fetchone()
        return row["id"] if row else None
    except sqlite3.Error as exc:
        logger.warning("source_url 查重失败（继续走 content_hash 兜底）：%r", exc)
        return None


def _print_batch_summary(stats: dict) -> None:
    summary_keys = (
        "list_url", "db_path", "pages_fetched", "discovered_links", "candidates",
        "attempted", "inserted", "skipped_duplicate", "skipped_non_award",
        "skipped_incomplete", "failed",
    )
    print("\n" + "=" * 78 + "\n【批量测试报告】\n" + "=" * 78)
    print(json.dumps({key: stats.get(key) for key in summary_keys},
                     ensure_ascii=False, indent=2))
    if stats.get("page_failures"):
        print("\n【列表页失败】")
        print(json.dumps(stats["page_failures"], ensure_ascii=False, indent=2))
    if stats.get("failures"):
        print("\n【单条失败明细】")
        print(json.dumps(stats["failures"], ensure_ascii=False, indent=2))
    print("\n【成功入库示例（最多 2 条）】")
    for item in stats.get("inserted_items", [])[:2]:
        print(json.dumps(item, ensure_ascii=False, indent=2))
    print("=" * 78)


def _run_batch(args) -> int:
    """列表页批量采集主控：翻页 -> 提链接 -> 逐条抓取/抽取/入库。"""
    if not args.db_path and not args.allow_production:
        logger.error("批量模式必须显式指定 --db-path（例如 --db-path test_crawl.db）")
        return 1
    if os.path.abspath(db.DB_PATH) == os.path.abspath(PRODUCTION_DB_PATH) and not args.allow_production:
        logger.error("批量模式禁止写生产库：%s", db.DB_PATH)
        return 1
    if args.max_items < 1:
        logger.error("--max-items 必须 >= 1")
        return 1
    if args.max_pages < 1:
        logger.error("--max-pages 必须 >= 1")
        return 1

    logger.info("批量模式：列表页=%s", args.list_url)
    logger.info("目标数据库=%s（测试库）", db.DB_PATH)
    logger.info("限制：最多 %d 页 / %d 条候选", args.max_pages, args.max_items)

    stats = {
        "list_url": args.list_url,
        "db_path": os.path.abspath(db.DB_PATH),
        "dry_run": bool(args.dry_run),
        "pages_fetched": 0,
        "discovered_links": 0,
        "candidates": 0,
        "attempted": 0,
        "inserted": 0,
        "skipped_duplicate": 0,
        "skipped_non_award": 0,
        "skipped_incomplete": 0,
        "failed": 0,
        "page_failures": [],
        "failures": [],
        "inserted_items": [],
    }

    entries, seen = [], set()

    def collect_entries(html: str, current_url: str) -> None:
        page_entries = extract_detail_entries(html, current_url)
        stats["discovered_links"] += len(page_entries)
        for entry in page_entries:
            if entry["url"] in seen:
                continue
            seen.add(entry["url"])
            if not args.no_title_filter and not _looks_like_award_title(entry["title"]):
                continue
            entries.append(entry)
        logger.info("列表页 %s：详情链接 %d 条，累计候选 %d 条",
                    current_url, len(page_entries), len(entries))

    explicit = bool(args.pages or args.page_url)
    if explicit:
        page_urls = build_page_urls(args.list_url, args.pages, args.page_url)[:args.max_pages]
        logger.info("显式翻页：%s", page_urls)
        for index, page_url in enumerate(page_urls, 1):
            if index > 1:
                _polite_sleep("翻页前")
            try:
                html = fetch_html(page_url)
            except CrawlError as exc:
                stats["page_failures"].append({"url": page_url, "error": str(exc)})
                logger.error("列表页抓取失败，跳过：%s -> %s", page_url, exc)
                continue
            stats["pages_fetched"] += 1
            collect_entries(html, page_url)
    else:
        current_url = args.list_url
        while current_url and stats["pages_fetched"] < args.max_pages:
            if stats["pages_fetched"] > 0:
                _polite_sleep("翻页前")
            try:
                html = fetch_html(current_url)
            except CrawlError as exc:
                stats["page_failures"].append({"url": current_url, "error": str(exc)})
                logger.error("列表页抓取失败，停止翻页：%s -> %s", current_url, exc)
                break
            stats["pages_fetched"] += 1
            collect_entries(html, current_url)
            next_url = find_next_page_url(html, current_url)
            if not next_url:
                logger.info("未检测到下一页，停止翻页")
                break
            logger.info("检测到下一页：%s", next_url)
            current_url = next_url

    stats["candidates"] = len(entries)
    candidates = entries[:args.max_items]
    logger.info("共发现 %d 条详情链接，标题粗筛后候选 %d 条，实际处理 %d 条",
                stats["discovered_links"], stats["candidates"], len(candidates))

    for index, entry in enumerate(candidates, 1):
        _polite_sleep(f"第 {index}/{len(candidates)} 条抓取前")
        stats["attempted"] += 1
        try:
            existing_id = _source_url_exists(entry["url"])
            if existing_id:
                stats["skipped_duplicate"] += 1
                logger.info("[%d] source_url 已存在（awards.id=%s），跳过：%s",
                            index, existing_id, entry["url"])
                continue

            detail_html = fetch_html(entry["url"])
            page = extract_page(detail_html, entry["url"])

            if not _looks_like_award_page(page):
                stats["skipped_non_award"] += 1
                logger.info("[%d] 正文无获奖信号，跳过：%s", index, page["title"] or entry["url"])
                continue

            _raw, extracted = extract_award(page, args.max_chars)

            if not extracted.get("is_award_news", True):
                stats["skipped_non_award"] += 1
                logger.info("[%d] 大模型判定非竞赛获奖新闻，跳过：%s",
                            index, extracted.get("title") or entry["url"])
                continue

            missing = [
                label for key, label in (
                    ("title", "作品名称"), ("competition", "所属赛事"),
                    ("year", "年份"), ("award_level", "奖项等级"),
                    ("team", "团队/学校"), ("summary", "简介"),
                    ("source_url", "原文链接"),
                ) if not extracted.get(key)
            ]
            if missing:
                stats["skipped_incomplete"] += 1
                logger.warning("[%d] 字段不完整（%s），跳过：%s",
                               index, "、".join(missing), entry["url"])
                continue

            competition_id, competition_status = ensure_competition(
                extracted["competition"], page, extracted["award_level"],
                dry_run=args.dry_run,
            )
            logger.info("[%d] 赛事挂接：competition_id=%s（%s）",
                        index, competition_id, competition_status)
            row = build_award_row(extracted, page, competition_id)

            if args.dry_run:
                logger.info("[%d] dry-run：不写库 -> %s", index, extracted["title"])
                continue

            award_id, error = save_award(row)
            if error is not None:
                if isinstance(error, sqlite3.IntegrityError):
                    stats["skipped_duplicate"] += 1
                    logger.info("[%d] content_hash 重复，跳过：%s", index, extracted["title"])
                else:
                    stats["failed"] += 1
                    stats["failures"].append({
                        "url": entry["url"], "stage": "db",
                        "error": f"{type(error).__name__}: {error}",
                    })
                    logger.error("[%d] 写库失败，跳过：%s -> %s", index, entry["url"], error)
                continue

            stats["inserted"] += 1
            item = {
                "id": award_id,
                "title": extracted["title"],
                "competition_id": competition_id,
                "competition": extracted["competition"],
                "year": extracted["year"],
                "award_level": extracted["award_level"],
                "team": extracted["team"],
                "summary": extracted["summary"],
                "source_url": extracted["source_url"],
                "confidence": extracted["confidence"],
                "review_status": "pending",
            }
            stats["inserted_items"].append(item)
            logger.info("[%d] 入库成功 awards.id=%s：%s", index, award_id, extracted["title"])

        except (CrawlError, llm.LLMError) as exc:
            stats["failed"] += 1
            stats["failures"].append({
                "url": entry["url"], "stage": "crawl/llm",
                "error": f"{type(exc).__name__}: {exc}",
            })
            logger.error("[%d] 抓取/抽取失败，跳过：%s -> %s", index, entry["url"], exc)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中断批量
            stats["failed"] += 1
            stats["failures"].append({
                "url": entry["url"], "stage": "unexpected",
                "error": f"{type(exc).__name__}: {exc}",
            })
            logger.exception("[%d] 未预期异常，跳过：%s", index, entry["url"])
        finally:
            if index < len(candidates):
                _polite_sleep(f"第 {index} 条完成后")

    stats["saved_at"] = datetime.now().isoformat(timespec="seconds")
    report_path = args.report or os.path.join(BASE_DIR, "workspace", "batch_report.json")
    _write_json(report_path, stats)
    _print_batch_summary(stats)

    if stats["pages_fetched"] == 0:
        return 1
    return 0


# ===========================================================================
# 七、命令行入口
# ===========================================================================

def _setup_console() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 避免中文日志乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老环境 / 已被重定向
            pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="抓取一篇高校新闻 -> 大模型抽取获奖信息 -> 写入 awards 表"
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="新闻页 URL（单页模式）")
    parser.add_argument("--list-url", default=None,
                        help="列表页 URL（提供后进入批量模式）")
    parser.add_argument("--max-pages", type=int, default=1,
                        help="批量模式最多翻多少页（默认 1）")
    parser.add_argument("--pages", default=None,
                        help="显式页码，如 1-3 或 1,2,3（配合 --page-url 或 ?page= 规则）")
    parser.add_argument("--page-url", default=None, metavar="TEMPLATE",
                        help="翻页 URL 模板，必须包含 {page}，如 'https://x/list?p={page}'")
    parser.add_argument("--max-items", type=int, default=8,
                        help="批量模式最多处理多少条候选详情页（默认 8）")
    parser.add_argument("--no-title-filter", action="store_true",
                        help="批量模式不按列表标题粗筛，全部详情页都尝试")
    parser.add_argument("--report", default=None, metavar="PATH",
                        help="批量报告 JSON 路径（默认 workspace/batch_report.json）")
    parser.add_argument("--db-path", default=None,
                        help="指定数据库文件（测试时指向副本，避免写生产库）")
    parser.add_argument("--allow-production", action="store_true",
                        help="解除红线：显式允许写生产库 competitions.db（危险，默认拒绝）")
    parser.add_argument("--dry-run", action="store_true", help="只抓取和抽取，不写数据库")
    parser.add_argument("--show-prompt", action="store_true", help="打印最终 Prompt")
    parser.add_argument("--show-raw", action="store_true", help="打印大模型返回的原始 JSON")
    parser.add_argument("--save-json", default=None, metavar="PATH",
                        help="把原始 JSON / 清洗结果 / 入库行 / 读回记录写入文件")
    parser.add_argument("--save-html", action="store_true",
                        help="把原始 HTML 存到 workspace/crawl_cache 并写入 raw_html_path")
    parser.add_argument("--max-chars", type=int, default=MAX_BODY_CHARS,
                        help=f"送给大模型的正文上限（默认 {MAX_BODY_CHARS}）")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser.parse_args(argv)


def _write_json(path: str, payload: dict) -> None:
    """把联调结果写成 UTF-8 JSON，便于事后复查。"""
    try:
        absolute = os.path.abspath(os.path.expanduser(path))
        parent = os.path.dirname(absolute)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(absolute, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        logger.info("联调结果已写入：%s", absolute)
    except OSError as exc:
        logger.warning("联调结果 JSON 写入失败：%s", exc)


def _read_back(award_id: int):
    """从目标库读回刚写入的 award，失败返回 None（不影响已成功的写入）。"""
    try:
        return db.get_award_by_id(award_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("读回 awards.id=%s 失败：%r", award_id, exc)
        return None


def _print_saved(saved: dict) -> None:
    """打印读回的关键字段，供人工核对。"""
    fields = (
        "id", "competition_id", "competition_raw", "title", "year",
        "award_level", "award_grade", "team", "summary", "source_url",
        "source_type", "confidence", "review_status", "embedding_state",
        "content_hash",
    )
    print("\n" + "=" * 78 + "\n【从目标库读回 awards 记录】\n" + "=" * 78)
    print(json.dumps({key: saved.get(key) for key in fields},
                     ensure_ascii=False, indent=2))

    competition_id = saved.get("competition_id")
    if competition_id:
        try:
            comp = db.get_competition(competition_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读回 competitions.id=%s 失败：%r", competition_id, exc)
            comp = None
        if comp:
            print("\n【关联的 competitions 记录】")
            print(json.dumps(
                {key: comp.get(key) for key in
                 ("id", "name", "category", "level", "official_url",
                  "sort_order", "summary")},
                ensure_ascii=False, indent=2,
            ))
    print("=" * 78)


def _run(argv=None) -> int:
    _setup_console()
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.max_chars < 500:
        logger.error("--max-chars 至少 500")
        return 1

    if args.db_path:
        db.DB_PATH = os.path.abspath(os.path.expanduser(args.db_path))

    target_is_production = (
        os.path.abspath(db.DB_PATH) == os.path.abspath(PRODUCTION_DB_PATH)
    )

    # 工程转正后的红线：真实写库必须显式指定测试库；确需生产必须 --allow-production
    if not args.dry_run:
        if not args.db_path and not args.allow_production:
            logger.error(
                "真实写库必须指定 --db-path（测试库，如 test_crawl.db）；"
                "确需写生产库请显式加 --allow-production"
            )
            return 1
        if target_is_production and not args.allow_production:
            logger.error("检测到生产库路径，已拒绝写入：%s（如确需写入请加 --allow-production）",
                         db.DB_PATH)
            return 1

    # 批量模式红线提前到 init_db 之前，避免任何 schema/迁移写入生产库
    if args.list_url:
        if not args.db_path and not args.allow_production:
            logger.error("批量模式必须显式指定 --db-path（例如 --db-path test_crawl.db）")
            return 1
        if target_is_production and not args.allow_production:
            logger.error("批量模式禁止写生产库：%s", db.DB_PATH)
            return 1

    # 真实写库前先确保目标库表结构就绪（只作用于 db.DB_PATH，测试时即副本）
    if not args.dry_run:
        try:
            db.init_db()
            logger.info("目标库初始化完成：%s", db.DB_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.error("数据库初始化失败：%r", exc)
            return 1

    # 批量模式：列表页 -> 翻页 -> 详情页循环
    if args.list_url:
        return _run_batch(args)

    url = args.url.strip()
    logger.info("目标网页：%s", url)
    logger.info("目标数据库：%s", db.DB_PATH)
    logger.info("模式：%s", "dry-run（不写库）" if args.dry_run else "真实写库")

    # 1) 抓取 + 抽正文
    try:
        html = fetch_html(url)
        page = extract_page(html, url)
    except CrawlError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("页面标题：%s", page["title"] or "（空）")
    logger.info("发布日期：%s", page["publish_date"] or "（未识别）")
    logger.info("正文容器：%s；正文 %d 字符；正文链接 %d 个",
                page["selector"], len(page["body"]), len(page["links"]))

    raw_html_path = None
    if args.save_html:
        try:
            raw_html_path = save_raw_html(html, url)
            logger.info("原始 HTML 已存档：%s", raw_html_path)
        except OSError as exc:
            logger.warning("原始 HTML 存档失败：%s", exc)

    # 2) 大模型抽取
    try:
        raw, extracted = extract_award(
            page, args.max_chars,
            show_prompt=args.show_prompt, show_raw=args.show_raw,
        )
    except llm.LLMError as exc:
        logger.error("大模型调用失败：%s", exc)
        return 1
    except CrawlError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("清洗后结果：\n%s", json.dumps(extracted, ensure_ascii=False, indent=2))

    missing = [
        label for key, label in (
            ("title", "作品名称"), ("competition", "所属赛事"), ("year", "年份"),
            ("award_level", "奖项等级"), ("team", "团队/学校"),
            ("summary", "简介"), ("source_url", "原文链接"),
        ) if not extracted.get(key)
    ]
    if missing:
        logger.warning("抽取字段为空：%s", "、".join(missing))
    else:
        logger.info("字段完整性检查：7 个必填字段全部非空")

    if not extracted.get("is_award_news", True):
        logger.warning("大模型判定该页不是竞赛获奖新闻，跳过写库")
        return 0

    # 3) 赛事挂接（未知赛事自动创建占位，避免 competition_id 为 NULL）
    competition_id, competition_status = ensure_competition(
        extracted["competition"], page, extracted["award_level"],
        dry_run=args.dry_run,
    )
    logger.info("赛事挂接：competition_id=%s（%s）", competition_id, competition_status)

    row = build_award_row(extracted, page, competition_id, raw_html_path)

    award_id, saved, error = None, None, None
    exit_code = 0

    if args.dry_run:
        logger.info("dry-run：以下数据未写入数据库")
        print(json.dumps(row, ensure_ascii=False, indent=2))
    else:
        # 4) 写库（只写 db.DB_PATH 指向的库）
        award_id, error = save_award(row)
        if error is not None:
            if isinstance(error, sqlite3.IntegrityError):
                logger.warning("写入被跳过：content_hash 已存在（同一原文/作品已收录）。%s", error)
                exit_code = 2
            else:
                logger.error("写入数据库失败：%s", error)
                exit_code = 1
        else:
            logger.info("写入成功：awards.id=%s", award_id)
            saved = _read_back(award_id)
            if saved:
                _print_saved(saved)
            else:
                logger.warning("写入成功但读回失败，请手动查询 awards.id=%s", award_id)

    # 5) 落一份联调结果（dry-run 也写，便于比对）
    if args.save_json:
        payload = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "db_path": os.path.abspath(db.DB_PATH),
            "dry_run": bool(args.dry_run),
            "page": {
                "url": page["url"],
                "title": page["title"],
                "site_name": page.get("site_name"),
                "publish_date": page["publish_date"],
                "selector": page["selector"],
                "body_chars": len(page["body"]),
                "links": page["links"],
            },
            "raw_llm_json": raw,
            "extracted": extracted,
            "competition_id": competition_id,
            "competition_status": competition_status,
            "row": row,
            "award_id": award_id,
            "read_back": saved,
            "error": str(error) if error else None,
        }
        _write_json(args.save_json, payload)

    return exit_code


def main(argv=None) -> int:
    """顶层安全网：任何未预期异常都转成退出码 1，不把 traceback 抛给调用方。"""
    try:
        return _run(argv)
    except Exception as exc:  # noqa: BLE001
        logger.exception("未捕获异常，进程安全退出：%r", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
