# -*- coding: utf-8 -*-
"""
crawler_competitions.py —— 赛事基础数据采集器（competitions 表）

目标：
    从单个赛事详情/通知页抓取并补全 competitions 表：
        比赛名称、级别、类别、报名月份、比赛月份、官网 URL、封面图 URL。

数据流：
    1. requests 抓取页面；
    2. 提取正文，提取 <head> 的 og:image（没有则回退正文首图，只存 URL 不下载）；
    3. llm.chat_json 抽成严格 JSON；
    4. 按名称匹配 competitions：
          命中 -> UPDATE 官网/时间/图片/类别/简介（可保留占位赛事原名）
          未命中 -> INSERT 新赛事；
    5. 回读目标库并打印关键字段。

红线：
    默认只允许写测试库（--db-path test_crawl.db）。
    生产库必须显式加 --allow-production，且会在 init_db 之前拦截。

用法：
    python crawler_competitions.py --url "https://..." --db-path test_crawl.db --dry-run
    python crawler_competitions.py --url "https://..." --db-path test_crawl.db
    python crawler_competitions.py --url "https://..." --db-path test_crawl.db --competition-id 34
    python crawler_competitions.py --url "https://..." --db-path test_crawl.db --update-name

依赖：requests、beautifulsoup4（复用 crawler_news.py / llm.py / db.py）。
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse

import crawler_news as news
import db
import image_guard as guard
import llm

logger = logging.getLogger("crawler_competitions")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")

# 前端 competitionCategory.js 的细分方向（只取工科/商科常用项）
CATEGORY_CHOICES = (
    "数学建模", "程序设计", "机器人", "工程机械", "土木建筑", "大数据",
    "交通车辆", "航空航天", "船舶海洋", "环境能源", "计算机&信息技术",
    "材料高分子", "电子&自动化", "人工智能",
    "数学", "物理", "化学化工", "健康生命&医学", "力学",
    "创业", "商业", "创青春",
)

SYSTEM_PROMPT = (
    "你是一个严谨的赛事信息抽取引擎，负责从高校通知/赛事详情页中提取赛事基础信息。"
    "只输出 JSON，不要输出任何解释性文字，不要使用 Markdown 代码围栏。"
    "严禁编造正文中不存在的日期、官网或赛事名称；没有的字段填 null。"
    # 红线（2026-11-07 新增）：通知页里最多的日期是“截止时间”，模型很容易张冠李戴
    "关于 contest_start_month / contest_end_month 的红线："
    "比赛时间只指比赛（含省赛、决赛）实际举办的时间段；"
    "严禁把报名截止时间、报名阶段、材料/作品提交截止时间、缴费截止时间当作比赛时间。"
    "如果正文只给出了报名或提交截止、没有明确的比赛举办时间，"
    "必须把 contest_start_month 与 contest_end_month 都留 null，不要猜测、不要用截止月份凑数。"
    "若只有“某月中旬开展”这类表述，可以填该月，但结束月份没有依据时必须留 null。"
    # 双轨制（2026-11-09）：月份 + 精确日期并存，日期只抄原文，绝不推算
    "关于 *_date 精确日期字段的红线（reg_start_date / reg_end_date / "
    "contest_start_date / contest_end_date）："
    "只有当正文明确写出「年-月-日」（如 2026-04-15、2026年4月15日）时，才填写对应的 *_date，"
    "格式统一为 YYYY-MM-DD；正文写「4月15日」这种没有年份的，填 MM-DD（如 04-15）；"
    "正文只给到月份（如「4 月」「4-5 月」）时，对应 *_date 必须留 null，"
    "严禁由月份推算、补齐或编造任何具体的「日」，也严禁自己加上原文没有的年份。"
    "填写了 *_date 时，同时把对应的 *_month 填成该日期所在的月份（两者必须一致）。"
)

SCHEMA_HINT = """只输出一个 JSON 对象，且仅包含以下 16 个键（值按正文填写，不要照抄说明文字）：
  name                 —— 字符串，赛事全称（去掉年份、届次、“关于举办”等前缀，保留品牌名）
  level                —— 字符串，只能是 national 或 provincial
  category             —— 字符串，必须从给定类别列表中选择一个
  reg_start_month      —— 整数 1-12 或 null，报名开始月份
  reg_end_month        —— 整数 1-12 或 null，报名结束月份
  reg_cross_year       —— 布尔值，报名是否跨年（如 11 月开始、次年 3 月结束）
  contest_start_month  —— 整数 1-12 或 null，比赛开始月份（只填比赛实际举办时间；
                          报名/材料提交截止时间不属于比赛时间，没有就留 null）
  contest_end_month    —— 整数 1-12 或 null，比赛结束月份（同上，没有依据必须留 null）
  reg_start_date       —— 字符串或 null，报名开始日；正文写明到「日」时填 YYYY-MM-DD（无年份填 MM-DD），
                          只给月份必须填 null，禁止推算
  reg_end_date         —— 字符串或 null，报名截止日，规则同上
  contest_start_date   —— 字符串或 null，比赛开始日，规则同上（不是报名截止日）
  contest_end_date     —— 字符串或 null，比赛结束日，规则同上
  official_url         —— 字符串或 null，赛事官网（http/https 开头，不是本新闻页）
  intro                —— 字符串，80~150 字赛事简介
  summary              —— 字符串，40~80 字赛事摘要
  confidence           —— 0~1 的小数
不要输出 JSON 以外的任何内容。"""


class CompetitionCrawlError(RuntimeError):
    """赛事页面抓取/抽取的可预期失败。"""


# ===========================================================================
# 一、页面抓取与图片提取
# ===========================================================================

def _clean_image_url(value: str, page_url: str) -> str:
    """把 //cdn... / 相对路径 统一成 http(s) 绝对 URL。"""
    if not value:
        return ""
    value = value.strip().strip("<>\"'")
    if not value or value.startswith("data:"):
        return ""
    if value.startswith("//"):
        value = "https:" + value
    absolute = urljoin(page_url, value)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return urlunparse(parsed._replace(fragment=""))


def _is_meaningful_image(src: str, img_tag) -> bool:
    """排除 logo/icon/二维码/头像/占位图等，保留正文配图。"""
    low = (src or "").lower()
    if not low or low.startswith(("data:", "javascript:")):
        return False
    bad_tokens = (
        "logo", "icon", "qrcode", "qr_code", "erweima", "ewm", "banner",
        "avatar", "wechat", "weixin", "loading", "blank", "spacer",
        "pixel", "1x1", "share_icon", "favicon", "placeholder",
    )
    if any(token in low for token in bad_tokens):
        return False
    if low.endswith(".svg"):
        return False
    # 图片本身文件名干净、但 alt/class 写着“扫码/加群”的，同样是二维码图
    if guard.is_qr_text(
        img_tag.get("alt"), img_tag.get("title"), img_tag.get("class")
    ):
        return False
    width = img_tag.get("width")
    height = img_tag.get("height")
    try:
        if width and int(str(width).replace("px", "")) < 100:
            return False
        if height and int(str(height).replace("px", "")) < 100:
            return False
    except ValueError:
        pass
    return True


def extract_image_url(html: str, page_url: str) -> str:
    """
    优先 <head> 的 og:image / twitter:image；没有则回退正文第一张有效配图。
    只返回 URL，不下载图片。
    """
    soup = news._make_soup(html)

    image_meta_keys = {
        "og:image", "og:image:url", "og:image:secure_url",
        "twitter:image", "twitter:image:src",
    }
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").strip().lower()
        if key in image_meta_keys:
            url = _clean_image_url(meta.get("content") or "", page_url)
            if url and not guard.is_bad_image_url(url):
                logger.info("封面图来自 %s：%s", key, url)
                return url
            if url:
                logger.warning("og:image 命中图片黑名单，跳过：%s", url)

    # 回退：正文容器里的第一张有效图片
    fallback_soup = news._make_soup(html)
    news._strip_noise(fallback_soup)
    container, selector = news._pick_container(fallback_soup)
    for img in container.find_all("img"):
        src = (img.get("src") or img.get("data-src")
               or img.get("data-original") or "").strip()
        if not _is_meaningful_image(src, img):
            continue
        url = _clean_image_url(src, page_url)
        if url and not guard.is_bad_image_url(url):
            logger.info("未找到 og:image，回退正文配图（容器 %s）：%s", selector, url)
            return url

    logger.warning("页面未找到 og:image，也没有可用的正文配图")
    return ""


def extract_image_candidates(html: str, page_url: str, limit: int = 6) -> list:
    """按优先级列出所有候选封面 URL（og:image 优先，其次正文配图）。

    只做 URL/标签层面的粗筛，不联网；真正的尺寸/二维码校验由
    ``image_guard.check_image`` 负责。
    """
    soup = news._make_soup(html)
    seen, urls = set(), []

    image_meta_keys = {
        "og:image", "og:image:url", "og:image:secure_url",
        "twitter:image", "twitter:image:src",
    }
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").strip().lower()
        if key not in image_meta_keys:
            continue
        candidate = _clean_image_url(meta.get("content") or "", page_url)
        if candidate and not guard.is_bad_image_url(candidate) and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)

    fallback_soup = news._make_soup(html)
    news._strip_noise(fallback_soup)
    container, _ = news._pick_container(fallback_soup)
    for img in container.find_all("img"):
        src = (img.get("src") or img.get("data-src")
               or img.get("data-original") or "").strip()
        if not _is_meaningful_image(src, img):
            continue
        candidate = _clean_image_url(src, page_url)
        if candidate and not guard.is_bad_image_url(candidate) and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
        if len(urls) >= limit:
            break

    return urls[:limit]


def fetch_competition_page(url: str, *, validate_image: bool = True) -> dict:
    """抓取页面 + 抽正文 + 抽封面图 URL。

    封面图必须通过 ``image_guard`` 的严格闸门（URL 黑名单 / 尺寸比例 /
    二维码内容识别）；所有候选都不合格时把 ``image_url`` 置空，让前端
    走两字简称兜底，绝不把垃圾图写进库。

    ``validate_image=False`` 只做 URL/标签层粗筛、不下载图片，供
    batch_update_competitions.py 使用（它在 LLM 抽取后会自己跑完整闸门，
    避免同一张图下载两次）。

    额外把原始 HTML 挂在 page["html"]，方便上层在封面图不合格时重新挑
    候选图，而不必二次抓取。
    """
    html = news.fetch_html(url)
    page = news.extract_page(html, url)
    page["html"] = html

    candidates = extract_image_candidates(html, url)
    page["image_candidates"] = []
    page["image_url"] = ""
    if not validate_image:
        page["image_url"] = candidates[0] if candidates else ""
        return page

    for candidate in candidates:
        ok, reason, info = guard.check_image(candidate)
        page["image_candidates"].append(
            {"url": candidate, "ok": ok, "reason": reason, **info}
        )
        if ok:
            page["image_url"] = candidate
            logger.info(
                "封面图通过严格闸门：%sx%s  %s",
                info.get("width"), info.get("height"), candidate,
            )
            break
        logger.warning("丢弃候选封面图：%s  %s", reason, candidate)

    if not page["image_url"]:
        logger.warning("没有通过闸门的封面图，image_url 置空（前端走简称兜底）")
    return page


# ===========================================================================
# 二、Prompt 与字段清洗
# ===========================================================================

def build_prompt(page: dict, max_chars: int = 6000) -> str:
    body = page["body"]
    truncated = len(body) > max_chars
    body = body[:max_chars]
    if truncated:
        body += "\n……（正文过长，已截断）"

    return (
        "【页面元信息】\n"
        f"页面 URL：{page['url']}\n"
        f"页面标题：{page['title'] or '（未取到）'}\n"
        f"站点：{page.get('site_name') or '（未取到）'}\n"
        f"发布日期：{page['publish_date'] or '（未取到）'}\n\n"
        f"【正文纯文本】\n{body}\n\n"
        "【抽取要求】\n"
        "1. 只依据上面的正文抽取，不得编造；没有的信息填 null。\n"
        "2. name：赛事全称。去掉“2026年”“第16届”“关于举办”等年份/届次/通知前缀，\n"
        "   保留赛事品牌名（如“中国教育机器人大赛”）。\n"
        "3. level：全国/国家/中国/国际赛事填 national；省级/省赛/赛区填 provincial。\n"
        "4. category：必须从以下列表中选择最贴近的一个：\n"
        f"   {', '.join(CATEGORY_CHOICES)}\n"
        "5. 时间字段分两轨，月份与精确日期都要填（原文有就填，没有就 null）：\n"
        "   - 月份轨：reg_start_month / reg_end_month / contest_start_month / contest_end_month，\n"
        "     统一用 1-12 的整数，不要输出日期字符串；\n"
        "     · reg_start_month / reg_end_month：报名开始/结束月份；\n"
        "       若只给出截止日期，reg_start_month 填 null，reg_end_month 填截止月。\n"
        "     · contest_start_month / contest_end_month：比赛开始/结束月份；\n"
        "       多阶段赛事（省赛+总决赛）取最早到最晚的月份。\n"
        "   - 日期轨：reg_start_date / reg_end_date / contest_start_date / contest_end_date，\n"
        "     只在正文明确写到「日」时填写，格式 YYYY-MM-DD（无年份写 MM-DD，如 04-15）；\n"
        "     正文只给月份（如「4 月」「4-5 月」）时，对应 *_date 一律 null；\n"
        "     严禁由月份推算具体日期、严禁自己补年份；日期与对应月份必须一致。\n"
        "   - reg_cross_year：报名是否跨年（开始月 > 结束月或正文明确写“次年”）。\n"
        "6. official_url：赛事官方网站，不是本新闻/转载页面；\n"
        "   优先官网主站，不要填报名系统/报名链接（除非页面没有官网）；\n"
        "   正文出现网址时优先使用，必须补全为 http:// 或 https://。\n"
        "   没有官网就填 null，不要拿当前页面 URL 充数。\n"
        "7. intro：80~150 字中文简介；summary：40~80 字摘要。\n"
        "8. confidence：0~1 的小数。\n"
        "9. 不要输出 Markdown，不要输出 JSON 以外的内容。"
    )


def _first_value(raw: dict, keys):
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return None


def _to_month(value):
    """月份归一化：支持 7 / "7" / "2026-07" / "7月" 等。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        month = int(value)
        return month if 1 <= month <= 12 else None
    text = str(value).strip()
    match = re.search(r"(?<!\d)(\d{1,2})\s*月", text)
    if not match:
        match = re.search(r"(?:^|[-/.])(\d{1,2})(?:[-/.]|$)", text)
    if not match:
        return None
    month = int(match.group(1))
    return month if 1 <= month <= 12 else None


def _to_date(value):
    """精确日期归一化：'2026-04-15' / '2026/4/15' / '4月15日' / '04-15'。

    只认「年-月-日」或「月-日」；只有月份（如 '4月'、'2026-04'）一律 None。
    实现委托 db._date_or_none，保证「模型输出 -> 入库」只有一套口径。
    """
    return db._date_or_none(value)


def _month_of_date(date_text):
    """从归一化后的日期串取月份（'2026-04-15' / '04-15' -> 4）。"""
    if not date_text:
        return None
    try:
        return int(str(date_text).split("-")[-2])
    except (ValueError, IndexError):
        return None


def _year_of_date(date_text):
    """带年份的日期返回年份，'MM-DD' 返回 None。"""
    text = str(date_text or "")
    if len(text) == 10 and text[4] == "-":
        try:
            return int(text[:4])
        except ValueError:
            return None
    return None


def _to_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "是", "跨年"):
        return True
    if text in ("false", "0", "no", "否", "不跨年"):
        return False
    return default


def _canonical_name(name: str) -> str:
    """赛事名归一化：去掉年份、届次、“关于举办”等前缀。"""
    text = news._clean_text(str(name or ""))
    text = re.sub(r"^关于(?:举办|开展|组织|做好)", "", text)
    text = re.sub(r"^(19|20)\d{2}\s*年?\s*", "", text)
    text = re.sub(r"第\s*[0-9一二三四五六七八九十百零]+\s*届\s*", "", text)
    text = re.sub(r"^(19|20)\d{2}\s*年?\s*", "", text)
    return text.strip(" -—–·、，,;；")


def _normalize_level(value, name: str) -> str:
    """级别归一化：优先信任模型显式值，避免“赛区”被误判为省级。"""
    value_text = str(value or "").strip().lower()
    if "provincial" in value_text or "省级" in value_text or "省赛" in value_text:
        return "provincial"
    if "national" in value_text or "国家级" in value_text or "全国" in value_text:
        return "national"

    text = str(name or "")
    if "省级" in text or "省赛" in text:
        return "provincial"
    if "全国" in text or "中国" in text or "国家" in text:
        return "national"
    return "provincial" if "省" in text else "national"


def _normalize_category(value, name: str) -> str:
    """把模型输出/赛事名映射到前端可识别的细分方向。"""
    raw = str(value or "").strip()
    if raw in CATEGORY_CHOICES:
        return raw

    text = f"{raw}{name or ''}"
    keyword_map = (
        ("数学建模", ("数学建模",)),
        ("程序设计", ("程序设计", "编程", "蓝桥杯")),
        ("机器人", ("机器人", "robocup", "robocon")),
        ("人工智能", ("人工智能", "ai")),
        ("大数据", ("大数据", "数据科学", "数据分析")),
        ("计算机&信息技术", ("计算机", "软件", "信息技术", "网络安全", "信息安全",
                              "物联网", "互联网", "ict")),
        ("电子&自动化", ("电子", "电气", "自动化", "嵌入式", "控制")),
        ("工程机械", ("机械", "工程机械")),
        ("创业", ("创业", "创新", "互联网+", "挑战杯", "双创")),
        ("数学", ("数学",)),
        ("物理", ("物理",)),
        ("化学化工", ("化学", "化工")),
        ("健康生命&医学", ("医学", "生命", "生物", "健康")),
    )
    lower = text.lower()
    for category, keywords in keyword_map:
        if any(keyword.lower() in lower for keyword in keywords):
            return category
    # 模型给了无法识别的分类时，保留原文；完全没有则归到计算机&信息技术
    return raw or "计算机&信息技术"


def _normalize_official_url(value, page_url: str) -> str:
    if not value:
        return ""
    text = str(value).strip().strip("<>\"'")
    if text.startswith("//"):
        text = "https:" + text
    elif re.match(r"^www\.", text, re.I):
        text = "https://" + text
    elif not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    absolute = _clean_image_url(text, page_url)
    if not absolute:
        return ""
    if absolute.rstrip("/") == page_url.rstrip("/"):
        logger.warning("模型把当前新闻页当成官网，已忽略：%s", absolute)
        return ""
    return absolute


def normalize_extraction(raw, page: dict) -> dict:
    if isinstance(raw, list):
        raw = next((item for item in raw if isinstance(item, dict)), None)
    if not isinstance(raw, dict):
        raise CompetitionCrawlError(f"模型未返回 JSON 对象：{type(raw).__name__}")

    name = _canonical_name(_first_value(raw, ("name", "competition", "赛事名称", "比赛名称")))
    if not name:
        name = _canonical_name(page.get("title") or "")
        logger.warning("模型未给出赛事名，回退页面标题：%s", name)
    if not name:
        raise CompetitionCrawlError("赛事名称为空，无法入库")

    level = _normalize_level(_first_value(raw, ("level", "级别")), name)
    category = _normalize_category(
        _first_value(raw, ("category", "类别", "分类")), name
    )

    reg_start = _to_month(_first_value(raw, ("reg_start_month", "报名开始月份", "报名开始时间")))
    reg_end = _to_month(_first_value(raw, ("reg_end_month", "报名结束月份", "报名截止时间")))
    contest_start = _to_month(
        _first_value(raw, ("contest_start_month", "比赛开始月份", "比赛开始时间"))
    )
    contest_end = _to_month(
        _first_value(raw, ("contest_end_month", "比赛结束月份", "比赛结束时间"))
    )

    # 精确日期（双轨制的第二轨）：只认原文写明的「日」，不推算、不补年份
    reg_start_date = _to_date(
        _first_value(raw, ("reg_start_date", "报名开始日期", "报名开始日"))
    )
    reg_end_date = _to_date(
        _first_value(raw, ("reg_end_date", "报名结束日期", "报名截止日期"))
    )
    contest_start_date = _to_date(
        _first_value(raw, ("contest_start_date", "比赛开始日期", "比赛开始日"))
    )
    contest_end_date = _to_date(
        _first_value(raw, ("contest_end_date", "比赛结束日期", "比赛结束日"))
    )

    # 两轨冲突时以精确日期为准（日期是更细的观测，月份只是它的投影）
    if reg_start_date:
        reg_start = _month_of_date(reg_start_date)
    if reg_end_date:
        reg_end = _month_of_date(reg_end_date)
    if contest_start_date:
        contest_start = _month_of_date(contest_start_date)
    if contest_end_date:
        contest_end = _month_of_date(contest_end_date)

    reg_cross = _to_bool(
        _first_value(raw, ("reg_cross_year", "报名是否跨年")),
        default=bool(reg_start and reg_end and reg_start > reg_end),
    )
    if reg_start_date and reg_end_date:
        start_year, end_year = _year_of_date(reg_start_date), _year_of_date(reg_end_date)
        if start_year and end_year:
            reg_cross = end_year > start_year

    if contest_start and contest_end and contest_end < contest_start:
        contest_end = contest_start
    # 结束日早于开始日 = 模型抄错年份/顺序，宁可留空也不写错时间
    if contest_start_date and contest_end_date and contest_end_date < contest_start_date:
        logger.warning("比赛结束日早于开始日，丢弃结束日：%s < %s",
                       contest_end_date, contest_start_date)
        contest_end_date = None
    if reg_start_date and reg_end_date and reg_end_date < reg_start_date:
        logger.warning("报名结束日早于开始日，丢弃结束日：%s < %s",
                       reg_end_date, reg_start_date)
        reg_end_date = None

    official_url = _normalize_official_url(
        _first_value(raw, ("official_url", "官网", "官方网站", "赛事官网")),
        page["url"],
    )
    if not official_url:
        logger.warning("未提取到有效官网 URL")

    intro = news._clean_text(str(_first_value(raw, ("intro", "简介", "赛事简介")) or ""))[:500]
    summary = news._clean_text(str(_first_value(raw, ("summary", "摘要")) or ""))[:200]

    confidence = _first_value(raw, ("confidence", "置信度"))
    try:
        confidence = float(confidence) if confidence is not None else 0.6
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = min(max(confidence, 0.0), 1.0)

    return {
        "name": name,
        "level": level,
        "level_label": "省级" if level == "provincial" else "国家级",
        "category": category,
        "reg_start_month": reg_start,
        "reg_end_month": reg_end,
        "reg_cross_year": bool(reg_cross),
        "contest_start_month": contest_start,
        "contest_end_month": contest_end,
        "reg_start_date": reg_start_date,
        "reg_end_date": reg_end_date,
        "contest_start_date": contest_start_date,
        "contest_end_date": contest_end_date,
        "official_url": official_url or None,
        "image_url": page.get("image_url") or None,
        "intro": intro or None,
        "summary": summary or None,
        "confidence": round(confidence, 2),
    }


def extract_competition(page: dict, max_chars: int = 6000,
                        show_prompt: bool = False, show_raw: bool = False) -> tuple:
    prompt = build_prompt(page, max_chars)
    if show_prompt:
        print("\n" + "=" * 78 + "\n【赛事抽取 Prompt】\n" + "=" * 78)
        print(prompt)
        print("=" * 78)

    logger.info("调用大模型抽取赛事信息（model=%s，正文 %d 字符）……",
                llm.MODEL, min(len(page["body"]), max_chars))
    raw = llm.chat_json(
        prompt,
        system=SYSTEM_PROMPT,
        schema_hint=SCHEMA_HINT,
        temperature=0.1,
        max_tokens=2048,
    )
    if show_raw:
        print("\n" + "=" * 78 + "\n【大模型原始 JSON】\n" + "=" * 78)
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        print("=" * 78)

    return raw, normalize_extraction(raw, page)


# ===========================================================================
# 三、入库：匹配 / 更新 / 插入
# ===========================================================================

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_update_data(extracted: dict, page: dict, *, include_name: bool) -> dict:
    """组装 update_competition 需要的字段；空值不覆盖已有信息。"""
    data = {
        "category": extracted["category"],
        "level": extracted["level"],
        "level_label": extracted["level_label"],
        "reg_cross_year": 1 if extracted["reg_cross_year"] else 0,
        "discover_source": page["url"],
        "last_checked_at": _now(),
        "is_active": 1,
    }
    if include_name:
        data["name"] = extracted["name"]
    for key in ("official_url", "image_url", "intro", "summary"):
        value = extracted.get(key)
        if value:
            data[key] = value
    for key in ("reg_start_month", "reg_end_month",
                "contest_start_month", "contest_end_month"):
        value = extracted.get(key)
        if value is not None:
            data[key] = value
    # 精确日期：只有模型真的抽到「日」才写，避免把已有日期清成 NULL
    for key in ("reg_start_date", "reg_end_date",
                "contest_start_date", "contest_end_date"):
        value = extracted.get(key)
        if value:
            data[key] = value
    return data


def save_competition(extracted: dict, page: dict, *,
                     competition_id=None, update_name: bool = False,
                     dry_run: bool = False, force_insert: bool = False) -> tuple:
    """
    返回 (competition_id, action, before, after)。
    action: updated / inserted / would_update / would_insert
    """
    matched_id = None if force_insert else competition_id
    if matched_id is None and not force_insert:
        matched_id = news.match_competition_id(extracted["name"])

    before = db.get_competition(matched_id) if matched_id else None
    if matched_id and before is None:
        raise CompetitionCrawlError(f"指定的 competition_id={matched_id} 不存在")

    if dry_run:
        action = "would_update" if matched_id else "would_insert"
        return matched_id, action, before, None

    if matched_id:
        data = build_update_data(
            extracted, page, include_name=bool(update_name)
        )
        db.update_competition(matched_id, data)
        after = db.get_competition(matched_id)
        return matched_id, "updated", before, after

    insert_data = {
        "name": extracted["name"],
        "category": extracted["category"],
        "level": extracted["level"],
        "level_label": extracted["level_label"],
        "official_url": extracted.get("official_url"),
        "reg_start_month": extracted.get("reg_start_month"),
        "reg_end_month": extracted.get("reg_end_month"),
        "reg_start_date": extracted.get("reg_start_date"),
        "reg_end_date": extracted.get("reg_end_date"),
        "reg_cross_year": 1 if extracted.get("reg_cross_year") else 0,
        "sort_order": 9999,
    }
    new_id = db.add_competition(insert_data)
    update_data = build_update_data(extracted, page, include_name=True)
    db.update_competition(new_id, update_data)
    after = db.get_competition(new_id)
    return new_id, "inserted", None, after


# ===========================================================================
# 四、命令行入口
# ===========================================================================

def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="赛事基础数据采集器：抓取赛事页 -> 大模型抽取 -> 更新/插入 competitions"
    )
    parser.add_argument("--url", required=True, help="赛事详情/通知页 URL")
    parser.add_argument("--db-path", default=None,
                        help="目标数据库（测试请用 test_crawl.db）")
    parser.add_argument("--allow-production", action="store_true",
                        help="解除红线：显式允许写生产库 competitions.db（危险）")
    parser.add_argument("--competition-id", type=int, default=None,
                        help="强制更新指定赛事 ID（用于占位赛事补全测试）")
    parser.add_argument("--update-name", action="store_true",
                        help="命中已有赛事时也更新 name（默认保留原赛事名）")
    parser.add_argument("--force-insert", action="store_true",
                        help="跳过名称匹配，强制插入新赛事（用于插入路径测试）")
    parser.add_argument("--dry-run", action="store_true", help="只抽取，不写库")
    parser.add_argument("--show-prompt", action="store_true", help="打印抽取 Prompt")
    parser.add_argument("--show-raw", action="store_true", help="打印大模型原始 JSON")
    parser.add_argument("--save-json", default=None, metavar="PATH",
                        help="保存原始 JSON / 清洗结果 / 入库前后记录")
    parser.add_argument("--max-chars", type=int, default=6000,
                        help="正文截断长度（默认 6000）")
    parser.add_argument("--verbose", action="store_true", help="DEBUG 日志")
    return parser.parse_args(argv)


def _write_json(path: str, payload: dict) -> None:
    try:
        absolute = os.path.abspath(os.path.expanduser(path))
        parent = os.path.dirname(absolute)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(absolute, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        logger.info("结果已写入：%s", absolute)
    except OSError as exc:
        logger.warning("结果 JSON 写入失败：%s", exc)


def _print_record(record: dict, title: str) -> None:
    fields = (
        "id", "name", "category", "level", "level_label",
        "reg_start_month", "reg_end_month", "reg_cross_year",
        "contest_start_month", "contest_end_month",
        "reg_start_date", "reg_end_date",
        "contest_start_date", "contest_end_date",
        "official_url", "image_url", "intro", "summary", "discover_source",
        "last_checked_at", "is_active",
    )
    print(f"\n【{title}】")
    print(json.dumps({key: record.get(key) for key in fields},
                     ensure_ascii=False, indent=2))


def _run(argv=None) -> int:
    _setup_console()
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.db_path:
        db.DB_PATH = os.path.abspath(os.path.expanduser(args.db_path))
    target_is_production = (
        os.path.abspath(db.DB_PATH) == os.path.abspath(PRODUCTION_DB_PATH)
    )

    # 红线：init_db 之前拦截生产库
    if not args.dry_run:
        if not args.db_path and not args.allow_production:
            logger.error("真实写库必须指定 --db-path（测试库）；确需生产请加 --allow-production")
            return 1
        if target_is_production and not args.allow_production:
            logger.error("检测到生产库路径，已拒绝写入：%s（如确需写入请加 --allow-production）",
                         db.DB_PATH)
            return 1

    logger.info("赛事页面：%s", args.url)
    logger.info("目标数据库：%s", db.DB_PATH)
    logger.info("模式：%s", "dry-run（不写库）" if args.dry_run else "真实写库")

    if not args.dry_run:
        try:
            db.init_db()
            logger.info("目标库初始化完成（含 image_url / contest_month 迁移）")
        except Exception as exc:  # noqa: BLE001
            logger.error("数据库初始化失败：%r", exc)
            return 1

    # 1) 抓取 + 正文 + 图片
    try:
        page = fetch_competition_page(args.url)
    except news.CrawlError as exc:
        logger.error("页面抓取/正文抽取失败：%s", exc)
        return 1

    logger.info("页面标题：%s", page["title"] or "（空）")
    logger.info("正文 %d 字符；图片：%s", len(page["body"]),
                page.get("image_url") or "（未找到）")

    # 2) AI 抽取
    try:
        raw, extracted = extract_competition(
            page, args.max_chars,
            show_prompt=args.show_prompt, show_raw=args.show_raw,
        )
    except llm.LLMError as exc:
        logger.error("大模型调用失败：%s", exc)
        return 1
    except CompetitionCrawlError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("清洗后赛事信息：\n%s",
                json.dumps(extracted, ensure_ascii=False, indent=2))

    # 3) 匹配 / 更新 / 插入
    try:
        competition_id, action, before, after = save_competition(
            extracted, page,
            competition_id=args.competition_id,
            update_name=args.update_name,
            dry_run=args.dry_run,
            force_insert=args.force_insert,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("赛事入库失败：%r", exc)
        return 1

    logger.info("入库完成：competition_id=%s，action=%s", competition_id, action)
    if before:
        _print_record(before, "更新前")
    if after:
        _print_record(after, "更新后 / 新记录")

    if args.save_json:
        _write_json(args.save_json, {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "db_path": os.path.abspath(db.DB_PATH),
            "dry_run": bool(args.dry_run),
            "page": {
                "url": page["url"],
                "title": page["title"],
                "site_name": page.get("site_name"),
                "publish_date": page["publish_date"],
                "image_url": page.get("image_url"),
            },
            "raw_llm_json": raw,
            "extracted": extracted,
            "competition_id": competition_id,
            "action": action,
            "before": before,
            "after": after,
        })

    return 0


def main(argv=None) -> int:
    try:
        return _run(argv)
    except Exception as exc:  # noqa: BLE001
        logger.exception("未捕获异常，进程安全退出：%r", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
