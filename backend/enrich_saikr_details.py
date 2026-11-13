# -*- coding: utf-8 -*-
"""enrich_saikr_details.py —— 赛氪详情补充器（AI 提纯 + 存量回填 / 每日巡检）

背景
----
详情页的「参赛对象 / 内容范围 / 考试安排」三张卡片依赖
`competitions.participants / content_scope / exam_schedule` 三列。
赛氪详情 API 的 `content`（HTML 正文通知）里有对应章节，但**原文不能直接入库**
（版权风险），必须先交给 `llm.py` 用自己的话提炼成结构化短句，再写库。

数据流
------
    目标库 competitions          取「三字段有空」的赛事（默认只读预览）
        -> saikr_map.json        映射 contest_id；无映射按名称相似度兜底
        -> 赛氪详情 API          GET .../contest/info?contest_id=xxx
        -> HTML 章节解析         参赛对象 / 内容范围 / 考试安排（原始文本）
        -> llm.py                **每个 contest_id 只调用一次**，三字段一起提纯
        -> 目标库                仅补空字段；AI 失败或疑似照抄 -> 保留 NULL

模式
----
    --backfill       存量：遍历所有三字段为空的赛事（不传模式时默认）
    --daily-update   每日：只处理最近 7 天新入库（created_at）且字段为空的赛事

合规与安全（与 CRAWLER_HANDOVER.md 第四/九节红线一致）
------------------------------------------------------
* 只处理库里已有的赛事，不遍历全站、不发现新赛事；
* 请求间隔 3~5 秒（`--delay` 最低 3 秒），随机抖动；
* 默认 dry-run，只抓取 + 抽取 + AI 提纯 + 写预览 JSON，不碰数据库；
* `--apply` 才写库，且**只填 NULL/空串**，已有值一律保留；
* `--apply` 必须走 AI 提纯；`--no-llm` 仅供 dry-run 调试抽取效果，禁止写库；
* 目标库若是生产库，必须 `--apply --allow-production`，写前自动备份；
* 抽不到 / AI 提炼失败 / 疑似整句照抄，一律保留 NULL，绝不写原文、绝不编造。

用法
----
    # 1. 存量回填预演（默认）：只抓取 + AI 提纯 + 生成预览，不写库
    .\\venv\\Scripts\\python.exe enrich_saikr_details.py --backfill

    # 2. 只验证 3~5 条
    .\\venv\\Scripts\\python.exe enrich_saikr_details.py --backfill --ids 1,6,10,12

    # 3. 写测试库
    .\\venv\\Scripts\\python.exe enrich_saikr_details.py --backfill --apply

    # 4. 每日巡检：只补最近 7 天新入库且字段为空的比赛
    .\\venv\\Scripts\\python.exe enrich_saikr_details.py --daily-update --apply

    # 5. 写生产库（必须先看过预览）
    .\\venv\\Scripts\\python.exe enrich_saikr_details.py --backfill --apply \\
        --allow-production --db-path .\\competitions.db
"""
import argparse
import difflib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

import db
import contest_dates
import discover_common as common
import image_guard
import llm
import saikr_match

SAIKR_DETAIL = "https://apiv4buffer.saikr.com/api/pc/contest/info"
MAP_PATH = os.path.join(common.BASE_DIR, "saikr_map.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DETAIL_FIELDS = ("participants", "content_scope", "exam_schedule")
FIELD_LABELS = {
    "participants": "参赛对象",
    "content_scope": "内容范围",
    "exam_schedule": "考试安排",
}

MAX_FIELD_LEN = 1200      # 单字段入库上限（按句末截断，避免卡片过长）
MIN_BODY_LEN = 16         # 章节正文过短视为没抽到，保留 NULL
MIN_NAME_RATIO = 0.93     # 名称兜底匹配阈值
SHANGHAI = timezone(timedelta(hours=8))
STALE_DAYS = 400          # 报名结束超过 400 天视为往年届次，整条跳过（沿用既有护栏）
DAILY_WINDOW_DAYS = 7     # --daily-update 只处理最近 N 天新入库的赛事

# AI 提纯（防版权）：每个 contest_id 只调用一次，三字段一起改写
AI_PROMPT_VER = "saikr-enrich-v2"
AI_MAX_TOKENS = 2000
AI_ITEM_MAX_CHARS = 50    # 单条短句上限（不含「1. 」编号）
AI_MAX_ITEMS = 8          # 单字段最多保留条目，避免列表失控
AI_COPY_WINDOW = 40       # 与原文连续重合 >= 40 字视为照抄，整字段丢弃（短专有名词列表不算）

# 章节关键词：同字段内按优先级排列，越靠前越可信
_FIELD_RULES = (
    ("participants", (
        "参赛对象", "竞赛对象", "参赛资格", "参赛范围", "参赛条件",
        "参与对象", "参赛人员", "参赛要求", "参赛报名", "报名资格",
    )),
    ("content_scope", (
        "竞赛内容", "比赛内容", "大赛内容", "赛事内容", "参赛内容",
        "大赛赛项", "比赛赛项", "赛题介绍", "赛道介绍", "赛事分类",
        "赛道设置", "赛道划分", "参赛项目类型", "项目类型",
        "竞赛主题", "比赛主题", "大赛主题", "竞赛方式", "竞赛形式",
        "竞赛规则",
    )),
    ("exam_schedule", (
        "赛程安排", "比赛时间安排", "竞赛时间安排", "赛事时间安排",
        "赛段安排", "时间安排", "竞赛安排", "比赛安排", "赛事安排",
        "大赛安排", "赛制安排", "比赛赛制", "竞赛赛制", "赛制",
        "竞赛日程", "日程安排", "考试安排", "竞赛阶段", "比赛阶段",
        "赛程",
    )),
)

# 章节正文质量闸门：避免把「奖项设置」「详见官网」这类空段落写进库
_PARTICIPANT_HINT = re.compile(
    r"(学生|在校|本科|专科|研究生|博士生|团队|队伍|年龄|教师|青年|选手)"
)
_SCHEDULE_HINT = re.compile(
    r"(\d{4}\s*年|\d{1,2}\s*月|上旬|中旬|下旬|初赛|复赛|决赛|"
    r"校赛|省赛|国赛|总决赛|赛段|阶段|报名时间)"
)
_FIELD_HINTS = {
    "participants": _PARTICIPANT_HINT,
    "exam_schedule": _SCHEDULE_HINT,
}

# 只提取这些块级元素的文本；父块含子块时跳过父块，避免正文重复
_BLOCK_TAGS = (
    "p", "div", "li", "tr", "td", "blockquote", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6",
)

_CN_NUM = "一二三四五六七八九十百零〇两"
_RE_LEVEL1 = re.compile(r"^(?:第[%s]+[章节部分讲]|[%s]+[、.．])" % (_CN_NUM, _CN_NUM))
_RE_LEVEL2 = re.compile(r"^[（(][%s]+[）)]" % _CN_NUM)
_RE_LEVEL3 = re.compile(r"^\d{1,2}[、.．)）]")
_RE_ANNEX = re.compile(r"^附件\s*\d*")
# 无序号的小标题（如「赛程安排」「赛事内容」「参赛要求」）常见于赛氪正文
_SECTION_NOUNS = re.compile(
    r"(安排|对象|要求|内容|赛项|赛制|介绍|范围|资格|主题|说明|"
    r"流程|阶段|奖项|规则|方式|时间|日程|赛道|组织|须知|条件)"
)


def log(message: str) -> None:
    print(message, flush=True)


def _setup_console() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老环境 / 已被重定向
            pass


# ============================================================
# 一、赛氪详情抓取
# ============================================================

def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.saikr.com/",
    })
    return session


def fetch_detail(session: requests.Session, contest_id: int,
                 retries: int = 2) -> dict:
    """请求赛氪详情 API，返回 data 字典；失败抛 RuntimeError。"""
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(
                SAIKR_DETAIL,
                params={"contest_id": contest_id},
                timeout=25,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") != 200:
                raise RuntimeError("赛氪 code=%s msg=%s" % (
                    payload.get("code"), payload.get("msg")))
            data = payload.get("data")
            if not isinstance(data, dict):
                raise RuntimeError("详情 data 为空")
            return data
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("赛氪详情请求失败 contest_id=%s：%r" % (contest_id, last_error))


def _parse_api_time(value):
    """赛氪时间字段：可能是 Unix 秒，也可能是「2024/05/09 18:26:00」。

    无效值 / 0000 年占位返回 None。
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        stamp = int(text)
    except ValueError:
        stamp = None
    if stamp is not None:
        if stamp <= 0:
            return None
        try:
            return datetime.fromtimestamp(stamp, tz=SHANGHAI)
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    return None


def stale_reason(detail: dict, now: datetime) -> str:
    """沿用「报名结束超过 400 天 = 往年届次」护栏；返回跳过原因或空串。"""
    reg_end = _parse_api_time(detail.get("regist_end_time"))
    reg_start = _parse_api_time(detail.get("regist_start_time"))
    if reg_end and (now - reg_end).days > STALE_DAYS:
        return "往年届次（报名截止 %s）" % reg_end.strftime("%Y-%m-%d")
    if reg_start and reg_start.year < now.year - 2:
        return "往年届次（报名开始 %s）" % reg_start.strftime("%Y-%m-%d")
    return ""


# ============================================================
# 二、HTML -> 章节 -> 三个字段
# ============================================================

def _clean_inline(text: str) -> str:
    """压缩空白、去零宽字符/NBSP，保留中文标点。"""
    text = str(text or "")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[\u00a0\u3000]", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def html_to_blocks(html: str) -> list:
    """把正文 HTML 切成 [(tag, text)] 叶子块序列（跳过含子块的父块）。

    表格行按「单元格 | 单元格」压成一行，保留赛程表的列对应关系。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    blocks = []
    block_tag_set = set(_BLOCK_TAGS)
    for tag in soup.find_all(_BLOCK_TAGS):
        if tag.name == "tr":
            cells = [_clean_inline(cell.get_text(" ", strip=True))
                     for cell in tag.find_all(["td", "th"])]
            cells = [cell for cell in cells if cell]
            if cells:
                blocks.append(("tr", " | ".join(cells)))
            continue
        if tag.find_parent("tr"):
            continue
        # 含块级子元素 -> 交给子元素输出，父块跳过，避免正文重复
        if tag.find(lambda child: child.name in block_tag_set):
            continue
        text = _clean_inline(tag.get_text(" ", strip=True))
        if text:
            blocks.append((tag.name, text))
    return blocks


def _heading_level(tag: str, text: str) -> int:
    """判断块是不是章节标题，返回 0(正文) / 1 / 2 / 3。"""
    if tag in ("h1", "h2"):
        return 1
    if tag == "h3":
        return 2
    if tag in ("h4", "h5", "h6"):
        return 3
    if _RE_ANNEX.match(text):
        return 1
    if _RE_LEVEL2.match(text):
        return 2
    if _RE_LEVEL1.match(text):
        return 1
    if _RE_LEVEL3.match(text) and len(text) <= 60:
        return 3
    if tag in ("strong", "b") and len(text) <= 40 \
            and not text.endswith(("。", "；", "，", ".", ";")):
        return 2
    # 无序号短标题：p/div 且 <=24 字、无句末标点、含章节类名词
    # 归为二级标题，避免把「资格要求：」这类子标题当成一级标题截断父章节
    plain = text.rstrip("：:")
    if tag in ("p", "div") and 2 <= len(plain) <= 24 \
            and not text.endswith(("。", "；", "，", ".", ";", "！", "？")) \
            and _SECTION_NOUNS.search(plain):
        return 2
    return 0


def _strip_numbering(title: str) -> str:
    """去掉「一、」「（二）」「3.」等序号，便于关键词匹配。"""
    text = re.sub(r"^第[%s]+[章节部分讲]\s*" % _CN_NUM, "", title)
    text = re.sub(r"^[%s]+[、.．]\s*" % _CN_NUM, "", text)
    text = re.sub(r"^[（(][%s]+[）)]\s*" % _CN_NUM, "", text)
    text = re.sub(r"^\d{1,2}[、.．)）]\s*", "", text)
    return text.strip()


def _section_body(nodes: list, index: int) -> str:
    """取第 index 个标题之下的正文，直到遇到同级或更高级标题。"""
    level = nodes[index][0]
    lines = []
    for node_level, text in nodes[index + 1:]:
        if node_level and node_level <= level:
            break
        lines.append(text)
    return "\n".join(lines).strip()


_RE_SCOPE_LEAD = re.compile(
    r"(分设|设置了?|共设|下设|分为|包括|涵盖)[^。；]{0,16}"
    r"(大类|赛道|赛项|组别|主题|方向|赛题)"
)


def _fallback_content_scope(nodes: list) -> str:
    """无「赛项/内容」小标题时，从正文里找「大赛分设 N 个大类/赛道」的段落。"""
    for node_level, text in nodes:
        if node_level:
            continue
        if not (30 <= len(text) <= 800):
            continue
        match = _RE_SCOPE_LEAD.search(text)
        # 只认段落开头附近的概述句，避免把获奖/评审规则误当赛项
        if match and match.start() <= 10:
            return text
    return ""


def _truncate(text: str, limit: int) -> str:
    """按句末/换行截断，尽量不留半句。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind("。"), head.rfind("；"), head.rfind("\n"))
    if cut < limit * 0.5:
        cut = limit
    return head[:cut].rstrip("，、；,; ") + "…"


_NOISE_LINE = re.compile(
    r"^(?:大赛|官方)?(?:网址|官网|网站|联系方式|联系电话|咨询电话|"
    r"电子邮箱|邮箱|联系人|QQ群?)\s*[：:]"
)
_ONLY_URL = re.compile(r"^(?:https?://|www\.)\S+$")
_PAGE_NUMBER = re.compile(r"^[（(]?\d{1,3}[）)]?$")
_CONTACT_HINT = re.compile(
    r"(官网|网址|网站|联系方式|联系电话|咨询电话|电子邮箱|邮箱|QQ群|微信公众号)"
)


def _clean_body(text: str) -> str:
    """正文去噪：去掉官网/联系方式/纯 URL/页码/连续重复行。"""
    lines = []
    for raw in str(text or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _NOISE_LINE.match(line) or _ONLY_URL.match(line) \
                or _PAGE_NUMBER.match(line):
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    # 末尾的联系方式/官网行（常带 URL）一并去掉，正文中间的链接保留
    while lines and re.search(r"https?://|www\.", lines[-1]) \
            and _CONTACT_HINT.search(lines[-1]) and len(lines[-1]) <= 100:
        lines.pop()
    return "\n".join(lines)


def extract_from_content(html: str) -> dict:
    """从 content HTML 抽取三个字段。

    返回 {field: {"text": str, "source": "章节：<标题>"}}；抽不到则不出现该键。
    """
    blocks = html_to_blocks(html)
    nodes = [(_heading_level(tag, text), text) for tag, text in blocks]
    headings = [(i, text) for i, (level, text) in enumerate(nodes) if level]

    picked = {}
    for field, keywords in _FIELD_RULES:
        best = None  # (priority, index, body)
        best_title = ""
        for index, title in headings:
            plain = _strip_numbering(title)
            # 「应用赛道设置3个特别奖」这类奖项标题不是内容范围
            if field == "content_scope" and "奖" in plain:
                continue
            for priority, keyword in enumerate(keywords):
                if keyword not in plain:
                    continue
                body = _clean_body(_section_body(nodes, index))
                if len(body) < MIN_BODY_LEN:
                    continue
                hint = _FIELD_HINTS.get(field)
                if hint and not hint.search(body):
                    continue
                if best is None or priority < best[0]:
                    best = (priority, index, body)
                    best_title = title
                break  # 同一标题只按最高优先级关键词计一次
        if best:
            picked[field] = {
                "text": _truncate(best[2], MAX_FIELD_LEN),
                "source": "章节：%s" % _strip_numbering(best_title)[:40],
            }

    if "content_scope" not in picked:
        fallback = _fallback_content_scope(nodes)
        if fallback:
            picked["content_scope"] = {
                "text": _truncate(_clean_body(fallback), MAX_FIELD_LEN),
                "source": "正文：赛项概述段",
            }
    return picked


def _format_stage(stage: dict) -> str:
    """把 contest_stage.list 格式化成「名称：内容（起 - 止）」多行文本。"""
    if not isinstance(stage, dict):
        return ""
    lines = []
    for item in stage.get("list") or []:
        if not isinstance(item, dict):
            continue
        name = _clean_inline(item.get("name"))
        content = _clean_inline(item.get("content"))
        start = _clean_inline(item.get("start_time"))
        end = _clean_inline(item.get("end_time"))
        if not (name or content):
            continue
        title = name or content
        body = content if content and content != name else ""
        when = " - ".join(part for part in (start, end) if part)
        line = title
        if body:
            line += "：" + body
        if when:
            line += "（%s）" % when
        lines.append(line)
    return "\n".join(lines)


def extract_fields(detail: dict) -> dict:
    """综合 structured 字段 + content 正文，返回三个字段的抽取结果。"""
    result = extract_from_content(detail.get("content") or "")

    # 参赛对象：participation_detail.explain/detail 是平台结构化字段，优先采用
    participation = detail.get("participation_detail") or {}
    if isinstance(participation, dict):
        explain = _clean_inline(participation.get("explain"))
        extra = _clean_inline(participation.get("detail"))
        structured = "\n".join(part for part in (explain, extra) if part)
        if len(structured) >= 8:
            result["participants"] = {
                "text": _truncate(structured, MAX_FIELD_LEN),
                "source": "结构化：participation_detail",
            }

    # 考试安排：章节没抽到时，退回平台「竞赛阶段」结构化赛程
    if "exam_schedule" not in result:
        stage_text = _format_stage(detail.get("contest_stage") or {})
        if len(stage_text) >= 8:
            result["exam_schedule"] = {
                "text": _truncate(stage_text, MAX_FIELD_LEN),
                "source": "结构化：contest_stage",
            }
    return result


# ============================================================
# 三、AI 提纯（防版权：原文不写库，只写模型改写后的短句）
# ============================================================

AI_PURIFY_SYSTEM = (
    "你是严谨的赛事信息编辑。把用户给出的原始通知文本改写成简明的事实条目，"
    "必须用自己的话概括，不得整句照抄，不得添加原文没有的信息。只输出 JSON。"
)

AI_PURIFY_SCHEMA_HINT = (
    '{"participants": "1. ...\\n2. ...", "content_scope": "1. ...", '
    '"exam_schedule": "1. ...", "extracted_contest_date": "2026-11-14", '
    '"extracted_contest_end_date": null}'
)

# 模型输出的比赛日期键 -> 主表日期列（2026-11-13：赛氪 API 的 contest_*_time
# 是占位符 '-0001/11/30'，比赛日期只能从「考试安排」正文里提炼）
_AI_DATE_KEYS = {
    "contest_start_date": "extracted_contest_date",
    "contest_end_date": "extracted_contest_end_date",
}

_RE_PURIFY_ITEM = re.compile(
    r"^\s*(?:\d{1,2}\s*[.、．)）]|[（(]\s*\d{1,2}\s*[）)]|[-*•·])\s*"
)
_RE_PURIFY_LABEL = re.compile(
    r"^(?:参赛对象|内容范围|考试安排|参赛资格|竞赛内容|赛程安排|赛事安排)\s*[：:]\s*"
)
_RE_PURIFY_SPLIT = re.compile(r"[；;]\s*(?=\d{1,2}\s*[.、．)）])")
_RE_PURIFY_INLINE = re.compile(r"(?<=\S)\s+(?=\d{1,2}\s*[.、．)）])")


def _cap_item(text: str, limit: int = AI_ITEM_MAX_CHARS) -> str:
    """把单条短句压到 limit 字以内（优先在标点处截断）。"""
    text = _clean_inline(text).strip(" \t，、；,;。")
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind("。"), head.rfind("；"), head.rfind("，"),
              head.rfind("、"), head.rfind(" "))
    if cut >= int(limit * 0.6):
        head = head[:cut]
    head = head.rstrip(" \t，、；,;。")
    if len(head) >= limit:
        head = head[:limit - 1].rstrip(" \t，、；,;。")
    return head + "…"


def _normalize_purified(value) -> str:
    """模型输出 -> 「1. 短句」列表；每条 <=50 字、最多 8 条、去重。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        chunks = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("text") or item.get("content") or ""
            chunks.append(str(item))
        text = "\n".join(chunks)
    elif isinstance(value, dict):
        text = str(value.get("text") or value.get("content") or "")
    else:
        text = str(value)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```[a-zA-Z]*", "", text).replace("```", "")
    text = _RE_PURIFY_SPLIT.sub("\n", text)
    text = _RE_PURIFY_INLINE.sub("\n", text)

    items = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        line = _RE_PURIFY_LABEL.sub("", line).strip()
        line = _RE_PURIFY_ITEM.sub("", line).strip()
        if not line:
            continue
        # 仍超长的行按分号拆成多条，避免一条塞进整段
        parts = [line]
        if len(line) > AI_ITEM_MAX_CHARS:
            parts = [part.strip() for part in re.split(r"[；;]", line) if part.strip()]
        for part in parts:
            part = _cap_item(part)
            if part and part not in items:
                items.append(part)
        if len(items) >= AI_MAX_ITEMS:
            break
    items = items[:AI_MAX_ITEMS]
    # 前端卡片是 <p> 纯文本，用分号连接可在不依赖 CSS 的情况下保持「1. …；2. …」结构
    return "；".join("%d. %s" % (index, item)
                    for index, item in enumerate(items, 1))


def _looks_copied(purified: str, raw: str) -> str:
    """检测与原文的连续重合片段（>= AI_COPY_WINDOW 字），返回重合串或空。"""
    def _flat(text: str) -> str:
        text = re.sub(r"(?:^|[；;\n])\s*\d{1,2}\s*[.、．)）]\s*",
                      "", str(text or ""))
        return re.sub(r"\s+", "", text)

    left, right = _flat(purified), _flat(raw)
    if len(left) < AI_COPY_WINDOW or len(right) < AI_COPY_WINDOW:
        return ""
    for index in range(len(left) - AI_COPY_WINDOW + 1):
        piece = left[index:index + AI_COPY_WINDOW]
        if piece in right:
            return piece
    return ""


def build_purify_prompt(name: str, raw_fields: dict) -> str:
    """把待提纯的原始章节拼成一次 LLM 调用的 Prompt（三字段共用一次调用）。"""
    parts = [
        "请把下面赛事的原始通知文本改写成可直接展示的结构化短句。",
        "",
        "赛事名称：%s" % (name or "（未提供）"),
        "",
        "字段说明：",
        "- participants（参赛对象）：谁可以参赛（身份、学历、年龄、团队人数等）",
        "- content_scope（内容范围）：比赛内容、赛项、赛道、主题、作品类型",
        "- exam_schedule（考试安排）：赛程阶段与时间节点（报名、初赛、复赛、决赛、提交截止等）",
        "- extracted_contest_date（比赛开始日）/ extracted_contest_end_date（比赛结束日）：",
        "  只抽「比赛本身」（初赛/复赛/决赛/答辩/评审）的日期，输出 YYYY-MM-DD；",
        "  报名起止、材料提交截止、缴费截止都不算比赛日期；",
        "  原文只写「11月14日」没有年份的，按赛程推断（报名在 2025 年、决赛在次年春天就写 2026）；",
        "  只有月份没有具体某一天（如「7月至8月」）或正文没写，就输出 null，绝不编造。",
        "",
        "硬性要求：",
        "1. 必须用自己的话概括，禁止整句照抄；除赛事/赛项等专有名词外，连续引用不超过 12 个字。",
        "2. 每个字段输出 1~%d 条，每条一行，用「1. 」「2. 」编号。" % AI_MAX_ITEMS,
        "3. 每条不超过 %d 个汉字；只写原文有的事实，原文没有的日期/人数不要猜测。" % AI_ITEM_MAX_CHARS,
        "4. 某个字段没有对应信息就返回空字符串，不要编造；日期没有就返回 null。",
        "5. extracted_contest_date 必须与 exam_schedule 里的说法一致，不能凭空多出一个日期。",
        "6. 不要输出 Markdown、标题或解释。",
        "",
        "原始文本：",
    ]
    for field in DETAIL_FIELDS:
        item = raw_fields.get(field)
        if not item:
            continue
        text = item.get("text") if isinstance(item, dict) else item
        text = _clean_inline(text)
        if not text:
            continue
        parts.append("【%s】" % FIELD_LABELS[field])
        parts.append(text)
        parts.append("")
    return "\n".join(parts)


def _pick_ai_value(data: dict, field: str):
    """兼容模型用中文字段名或包一层 fields 的输出。"""
    for key in (field, FIELD_LABELS.get(field)):
        if key and key in data:
            return data[key]
    return None


def purify_fields(name: str, raw_fields: dict,
                  *, max_chars: int = MAX_FIELD_LEN) -> tuple:
    """调用一次 llm.py，把原始章节提炼为结构化短句，并顺带抽取比赛日期。

    返回 ``(fields, dates)``：
        fields: ``{field: {"text", "source", "raw_chars"}}``
        dates:  ``{"contest_start_date": "2026-11-14", ...}``（未经护栏校验，
                调用方必须过 ``contest_dates.plan_date_writes`` 才能入库）

    LLM 失败时抛 llm.LLMError，调用方必须保留 NULL，**绝不能回落写原文**。
    """
    needed = {}
    for field in DETAIL_FIELDS:
        item = raw_fields.get(field)
        if not item:
            continue
        text = _clean_inline(item.get("text") if isinstance(item, dict) else item)
        if len(text) < MIN_BODY_LEN:
            continue
        source = item.get("source") if isinstance(item, dict) else ""
        needed[field] = {"text": text, "source": source or "赛氪详情"}
    if not needed:
        return {}, {}

    data = llm.chat_json(
        build_purify_prompt(name, needed),
        system=AI_PURIFY_SYSTEM,
        schema_hint=AI_PURIFY_SCHEMA_HINT,
        temperature=0.2,
        max_tokens=AI_MAX_TOKENS,
        retries=1,
    )
    if not isinstance(data, dict):
        raise llm.LLMError("AI 提纯返回的不是 JSON 对象：%r" % type(data).__name__)
    if isinstance(data.get("fields"), dict):
        data = data["fields"]

    result = {}
    for field in DETAIL_FIELDS:
        if field not in needed:
            continue
        text = _normalize_purified(_pick_ai_value(data, field))
        if not text:
            continue
        overlap = _looks_copied(text, needed[field]["text"])
        if overlap:
            log("[AI] 丢弃疑似照抄字段 %s（连续重合 %d 字：%s…）"
                % (FIELD_LABELS[field], len(overlap), overlap[:12]))
            continue
        result[field] = {
            "text": _truncate(text, max_chars),
            "source": "AI提纯：%s｜%s" % (needed[field]["source"], AI_PROMPT_VER),
            "raw_chars": len(needed[field]["text"]),
        }

    dates = {}
    for column, key in _AI_DATE_KEYS.items():
        value = _pick_ai_value(data, key)
        if value in (None, "", "null", "None", "无"):
            continue
        dates[column] = str(value).strip()
    return result, dates


# ============================================================
# 四、赛事映射与目标库
# ============================================================

def load_map() -> dict:
    if not os.path.exists(MAP_PATH):
        raise SystemExit("缺少映射文件：%s" % MAP_PATH)
    with open(MAP_PATH, encoding="utf-8") as f:
        payload = json.load(f)
    mapping = {}
    for item in payload.get("items") or []:
        comp_id = item.get("competition_id")
        if comp_id is None:
            continue
        mapping[int(comp_id)] = item
    return mapping


def _read_rows(db_path: str, sql: str) -> list:
    """只读连接取行（不建 -wal、不写任何数据）。"""
    import sqlite3
    from pathlib import Path

    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def load_target_competitions(db_path: str) -> dict:
    """读取目标库赛事清单（含 created_at，供 --daily-update 判断新入库）。"""
    if not os.path.exists(db_path):
        raise SystemExit("目标库不存在：%s" % db_path)
    columns = [r["name"] for r in _read_rows(
        db_path, "PRAGMA table_info(competitions)")]
    missing = [field for field in DETAIL_FIELDS if field not in columns]
    if missing:
        raise SystemExit(
            "目标库缺少列：%s\n  请先执行：python migrate_production.py "
            "--db-path %s --apply" % ("、".join(missing), db_path)
        )
    rows = _read_rows(
        db_path,
        "SELECT id, name, created_at, participants, content_scope, exam_schedule "
        "FROM competitions ORDER BY id",
    )
    return {int(row["id"]): row for row in rows}


# 赛氪详情 API 的时间字段 -> 主表「双轨制」日期列（只补空，绝不覆盖）
SAIKR_DATE_KEYS = (
    ("reg_start_date", "regist_start_time"),
    ("reg_end_date", "regist_end_time"),
    ("contest_start_date", "contest_start_time"),
    ("contest_end_date", "contest_end_time"),
)

# 封面候选键：接口不同版本字段名不一致，按优先级依次尝试
# （实测 saikr 详情用 web_pic_big，占位符为空串时继续试下一个）
SAIKR_COVER_KEYS = ("web_pic_big", "web_pic", "image", "cover", "cover_url",
                    "poster", "banner", "image_url")


def fill_dates_and_cover(target_db: str, competition_id: int, detail: dict) -> dict:
    """用赛氪详情补「空的日期字段 / 封面」，已有值一律不动。

    封面必须过 image_guard 闸门（二维码、banner、宽高比超限都会被拒），
    不合格就保持 NULL，由前端走无图兜底，绝不写脏图。

    返回 {"dates": [已写列名], "cover": URL 或 None}。
    """
    rows = _read_rows(
        target_db,
        "SELECT reg_start_date, reg_end_date, contest_start_date, "
        "contest_end_date, image_url FROM competitions WHERE id = %d"
        % int(competition_id),
    )
    if not rows:
        return {"dates": [], "cover": None}
    row = rows[0]

    patch = {}
    dates = []
    for column, key in SAIKR_DATE_KEYS:
        if str(row.get(column) or "").strip():
            continue
        moment = _parse_api_time(detail.get(key))
        if moment is not None:
            patch[column] = moment.strftime("%Y-%m-%d")
            dates.append(column)

    cover = None
    if not str(row.get("image_url") or "").strip():
        for key in SAIKR_COVER_KEYS:
            url = str(detail.get(key) or "").strip()
            if not url.lower().startswith("http"):
                continue
            try:
                ok, _reason, _info = image_guard.check_image(url)
            except Exception:  # noqa: BLE001 - 校验异常按不合格处理
                ok = False
            if ok:
                patch["image_url"] = url
                cover = url
                break

    if patch:
        db.update_competition(int(competition_id), patch, db_path=target_db)
    return {"dates": dates, "cover": cover}


def _norm_name(name: str) -> str:
    return db.normalize_competition_name(name or "")


def _parse_created_at(value):
    """解析 SQLite 的 created_at（本地时间，无时区）为东八区 datetime。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    return None


def _build_by_name(mapping: dict) -> list:
    """saikr_map -> [(归一化赛氪名, contest_id)]，供名称相似度兜底。"""
    by_name = []
    for item in mapping.values():
        cid = item.get("saikr_contest_id")
        name = _norm_name(item.get("saikr_name") or "")
        if cid and name:
            by_name.append((name, int(cid)))
    return by_name


def resolve_contest_id(row: dict, mapping: dict, by_name: list) -> tuple:
    """返回 (contest_id, 匹配方式)；无法定位返回 (None, "")。"""
    comp_id = int(row["id"])
    item = mapping.get(comp_id)
    contest_id = item.get("saikr_contest_id") if item else None
    match = "saikr_map(%s)" % (item.get("match_type") or "?") if item else ""
    if contest_id:
        return int(contest_id), match

    norm = _norm_name(row.get("name") or "")
    scored = sorted(
        ((difflib.SequenceMatcher(None, norm, name).ratio(), cid)
         for name, cid in by_name if name),
        reverse=True,
    )
    if scored and scored[0][0] >= MIN_NAME_RATIO \
            and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.03):
        return int(scored[0][1]), "名称匹配(%.2f)" % scored[0][0]
    return None, ""


def select_targets(target_rows: dict, mapping: dict, *,
                   mode: str = "backfill", ids=None,
                   days: int = DAILY_WINDOW_DAYS, now=None) -> dict:
    """按模式挑出待补赛事。

    backfill       := 所有三字段有空值的赛事；
    daily          := 其中 created_at 在最近 days 天内的（新入库）；
    ids            := 只处理指定 id（仍受 mode / 空字段约束）。
    """
    ids = set(ids or ())
    by_name = _build_by_name(mapping)
    now = now or datetime.now(SHANGHAI)
    cutoff = now - timedelta(days=days)
    targets, no_mapping = [], []
    complete = outside_window = 0

    for row in target_rows.values():
        comp_id = int(row["id"])
        if ids and comp_id not in ids:
            continue
        missing = [field for field in DETAIL_FIELDS
                   if not str(row.get(field) or "").strip()]
        if not missing:
            complete += 1
            continue
        if mode == "daily":
            created = _parse_created_at(row.get("created_at"))
            if created is None or created < cutoff:
                outside_window += 1
                continue
        contest_id, match = resolve_contest_id(row, mapping, by_name)
        if not contest_id:
            no_mapping.append((comp_id, row.get("name") or "", "无赛氪 contest_id"))
            continue
        targets.append({
            "competition_id": comp_id,
            "target_id": comp_id,
            "name": row.get("name") or "",
            "target": row,
            "contest_id": int(contest_id),
            "match": match,
            "missing_fields": missing,
        })

    return {
        "targets": targets,
        "no_mapping": no_mapping,
        "complete": complete,
        "outside_window": outside_window,
    }


# ============================================================
# 五、主流程
# ============================================================

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="赛氪详情补充器：抓取 + AI 提纯 participants/content_scope/"
                    "exam_schedule，只补空字段（默认 dry-run）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--backfill", dest="mode", action="store_const",
                      const="backfill",
                      help="存量模式：遍历所有三字段为空的赛事（默认）")
    mode.add_argument("--daily-update", dest="mode", action="store_const",
                      const="daily",
                      help="每日模式：只处理最近 --days 天新入库且字段为空的赛事")
    parser.set_defaults(mode="backfill")

    parser.add_argument("--db-path", default=common.DEFAULT_DB_PATH,
                        help="目标库（默认 test_crawl.db）")
    parser.add_argument("--source-db", default=None,
                        help="[已弃用] 兼容旧命令；清单改为直接读 --db-path")
    parser.add_argument("--apply", action="store_true", help="真正写目标库")
    parser.add_argument("--allow-production", action="store_true",
                        help="允许写生产库（必须与 --apply 同用，且会先备份）")
    parser.add_argument("--ids", default="",
                        help="只处理指定赛事 id，逗号分隔，例如 1,6,10")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理多少条（0=不限）")
    parser.add_argument("--days", type=int, default=DAILY_WINDOW_DAYS,
                        help="--daily-update 的新入库窗口天数（默认 %d）"
                             % DAILY_WINDOW_DAYS)
    parser.add_argument("--delay", default="3,5",
                        help="赛氪请求间隔秒，默认 3,5（最低 3 秒）")
    parser.add_argument("--max-chars", type=int, default=MAX_FIELD_LEN,
                        help="单字段最大字符数（默认 %d）" % MAX_FIELD_LEN)
    parser.add_argument("--preview", default="saikr_enrich_preview.json",
                        help="预览 JSON 文件名（写入 workspace/）")
    parser.add_argument("--allow-stale", action="store_true",
                        help="允许写入往年届次页面（默认沿用 400 天护栏跳过）")
    parser.add_argument("--no-llm", action="store_true",
                        help="只用规则抽取、不调模型（仅限 dry-run；禁止与 --apply 同用）")
    return parser.parse_args(argv)


def _parse_ids(raw: str) -> set:
    if not (raw or "").strip():
        return set()
    result = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise SystemExit("--ids 只能传正整数，逗号分隔：%r" % raw)
        result.add(int(part))
    return result


def enrich_competition(target_db: str, competition_id: int, *,
                       contest_id=None, allow_stale: bool = False,
                       allow_name_match: bool = False,
                       max_chars=None, session=None, mapping=None,
                       now=None) -> dict:
    """抓取 + AI 提纯 + 写库，供 CLI 与 review_candidates.approve 复用。

    allow_name_match=True 时，若拿不到 contest_id（如教育部门名录来源的候选，
    其 source_url 只是目录页），会用「赛事名称」到本地赛氪索引里匹配一次，
    匹配失败的状态码为 no_match（CLI 默认仍是 no_mapping，行为不变）。

    调用方负责：生产库备份、生产库权限校验、异常兜底。
    返回 status：updated / complete / no_mapping / no_match / stale / no_fields / ai_empty / exists。
    """
    target_db = os.path.abspath(target_db)
    rows = load_target_competitions(target_db)
    row = rows.get(int(competition_id))
    if row is None:
        raise ValueError("目标库没有该赛事：id=%s" % competition_id)

    missing = [field for field in DETAIL_FIELDS
               if not str(row.get(field) or "").strip()]
    if not missing:
        return {"status": "complete", "competition_id": int(competition_id)}

    name = row.get("name") or ""
    if contest_id is None:
        mapping = mapping if mapping is not None else load_map()
        contest_id, match = resolve_contest_id(row, mapping, _build_by_name(mapping))
        if not contest_id and allow_name_match:
            # 名录类候选没有 contest_id，source_url 又只是目录页（同一链接指所有赛事），
            # 唯一可靠的锚点就是赛事名称 -> 本地赛氪索引（saikr_match）。
            hit = saikr_match.resolve(name)
            if hit:
                contest_id = int(hit["contest_id"])
                match = "名称匹配(%s/%.2f)" % (hit["method"], hit["score"])
                log("[赛氪匹配] 名称「%s」-> contest_id=%s（%s，%.2f，赛氪名「%s」）" % (
                    name, contest_id, hit["method"], hit["score"],
                    hit["matched_name"]))
            else:
                log("[赛氪匹配] 名称「%s」未匹配到赛氪" % name)
    else:
        contest_id, match = int(contest_id), "candidate"
    if not contest_id:
        return {
            "status": "no_mapping" if not allow_name_match else "no_match",
            "competition_id": int(competition_id),
            "reason": "无赛氪 contest_id" if not allow_name_match
                      else "通过名称未匹配到赛氪",
        }

    owns_session = session is None
    session = session or _session()
    try:
        detail = fetch_detail(session, contest_id)
    finally:
        if owns_session:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响结果
                pass

    now = now or datetime.now(SHANGHAI)
    stale = stale_reason(detail, now)
    if stale and not allow_stale:
        return {"status": "stale", "competition_id": int(competition_id),
                "contest_id": contest_id, "reason": stale}

    raw = extract_fields(detail)
    needed = {field: raw[field] for field in DETAIL_FIELDS
              if field in missing and field in raw}
    if not needed:
        return {"status": "no_fields", "competition_id": int(competition_id),
                "contest_id": contest_id, "reason": "页面无可用章节"}

    purified, extracted_dates = purify_fields(
        row.get("name") or "", needed, max_chars=max_chars or MAX_FIELD_LEN)
    if not purified and not extracted_dates:
        return {"status": "ai_empty", "competition_id": int(competition_id),
                "contest_id": contest_id, "reason": "AI 未产出可用短句"}

    written = db.fill_competition_detail_text(
        int(competition_id),
        only_empty=True,
        db_path=target_db,
        **{field: value["text"] for field, value in purified.items()},
    )
    # 顺手补「空的日期字段 + 封面」：审核入库的新赛事通常这两类也是空壳，
    # 只补空、封面过 image_guard 闸门，失败保持 NULL 走前端兜底。
    extra = fill_dates_and_cover(target_db, int(competition_id), detail)
    date_patch = {}
    if extracted_dates:
        raw_text = (raw.get("exam_schedule") or {}).get("text") or ""
        date_patch = contest_dates.apply(
            target_db, int(competition_id), extracted_dates, raw_text)
    changed = (bool(written) or bool(extra["dates"]) or bool(extra["cover"])
               or bool(date_patch))
    return {
        "status": "updated" if changed else "exists",
        "competition_id": int(competition_id),
        "contest_id": contest_id,
        "match": match,
        "written": sorted(written),
        "extra": extra,
        "date_patch": date_patch,
        "fields": purified,
    }


def main(argv=None) -> int:
    _setup_console()
    global MAX_FIELD_LEN
    args = parse_args(argv)

    try:
        delay_min, delay_max = [float(x) for x in args.delay.split(",")]
    except ValueError:
        raise SystemExit("--delay 格式应为 min,max，例如 3,5")
    if delay_min < 3 or delay_max < delay_min:
        raise SystemExit("赛氪请求间隔不得低于 3 秒（默认 3,5）")
    if args.max_chars < 100:
        raise SystemExit("--max-chars 至少 100")
    if args.days < 1:
        raise SystemExit("--days 必须大于 0")
    if args.apply and args.no_llm:
        raise SystemExit("--no-llm 禁止与 --apply 同用：入库必须走 AI 提纯")
    MAX_FIELD_LEN = args.max_chars

    target_db = os.path.abspath(args.db_path)
    if args.apply and common.is_production_db(target_db) \
            and not args.allow_production:
        raise SystemExit("拒绝写生产库：必须同时传 --allow-production")
    if args.source_db:
        log("[提示] --source-db 已弃用；赛事清单改为直接读目标库")

    ids = _parse_ids(args.ids)
    mapping = load_map()
    target_rows = load_target_competitions(target_db)
    selection = select_targets(
        target_rows, mapping, mode=args.mode, ids=ids, days=args.days,
    )
    pending = selection["targets"]
    if args.limit > 0:
        pending = pending[:args.limit]

    mode_label = ("backfill（存量）" if args.mode == "backfill"
                  else "daily-update（最近 %d 天新入库）" % args.days)
    log("[模式] %s%s" % (mode_label, "｜dry-run" if not args.apply else "｜apply"))
    log("[目标库] %s" % target_db)
    log("[映射] 目标库 %d 条 / 可定位 %d 条 / 无映射 %d 条 / 已补全 %d 条 / 窗口外 %d 条"
        % (len(target_rows), len(pending), len(selection["no_mapping"]),
           selection["complete"], selection["outside_window"]))
    log("[计划] 本次处理 %d 条赛事（只写空字段）" % len(pending))

    # 同一 contest_id 只请求一次，父/子赛事复用同一份详情
    by_contest = {}
    for target in pending:
        by_contest.setdefault(target["contest_id"], []).append(target)

    session = _session()
    extracted = {}
    stale_map = {}
    fetch_ok = fetch_failed = 0
    now = datetime.now(SHANGHAI)
    for order, contest_id in enumerate(sorted(by_contest)):
        try:
            detail = fetch_detail(session, contest_id)
            extracted[contest_id] = extract_fields(detail)
            stale_map[contest_id] = stale_reason(detail, now)
            fetch_ok += 1
            got = sum(1 for value in extracted[contest_id].values() if value)
            stale_note = ("｜%s" % stale_map[contest_id]) if stale_map[contest_id] else ""
            log("[抓取] contest_id=%s -> %s（抽取 %d/%d 字段）%s"
                % (contest_id, (detail.get("contest_name") or "")[:32],
                   got, len(DETAIL_FIELDS), stale_note))
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断
            fetch_failed += 1
            log("[抓取] ✗ contest_id=%s：%s" % (contest_id, exc))
        if order < len(by_contest) - 1:
            wait = random.uniform(delay_min, delay_max)
            log("[限速] 等待 %.1f 秒" % wait)
            time.sleep(wait)

    # AI 提纯：每个 contest_id 一次；失败/照抄一律保留 NULL，绝不写原文
    purified = {}
    dates_by_contest = {}
    ai_calls = ai_failed = 0
    usage_before = llm.get_usage()
    if args.no_llm:
        log("[AI] --no-llm：跳过提纯，仅预览规则抽取结果（不会写库）")
    else:
        for contest_id in sorted(by_contest):
            fields = extracted.get(contest_id) or {}
            wanted = set()
            for target in by_contest[contest_id]:
                wanted.update(target["missing_fields"])
            needed = {field: fields[field] for field in DETAIL_FIELDS
                      if field in wanted and field in fields}
            if not needed:
                continue
            name = by_contest[contest_id][0]["name"]
            try:
                result, dates = purify_fields(name, needed, max_chars=MAX_FIELD_LEN)
                purified[contest_id] = result
                dates_by_contest[contest_id] = dates
                ai_calls += 1
                log("[AI] contest_id=%s -> 提纯 %d/%d 字段%s"
                    % (contest_id, len(result), len(needed),
                       "" if result else "（⚠ 无可用短句，保留 NULL）"))
                if dates:
                    log("[AI] contest_id=%s -> 比赛日期候选 %s" % (contest_id, dates))
            except Exception as exc:  # noqa: BLE001 - 单条 AI 失败不阻断批量
                ai_failed += 1
                purified[contest_id] = {}
                dates_by_contest[contest_id] = {}
                log("[AI] ✗ contest_id=%s：%s（保留 NULL，不写原文）"
                    % (contest_id, exc))
    usage_after = llm.get_usage()
    ai_tokens = usage_after.get("total_tokens", 0) - usage_before.get("total_tokens", 0)

    stats = {
        "mode": args.mode,
        "days": args.days,
        "considered": len(pending),
        "fetch_ok": fetch_ok,
        "fetch_failed": fetch_failed,
        "stale_skipped": 0,
        "ai_calls": ai_calls,
        "ai_failed": ai_failed,
        "ai_tokens": ai_tokens,
        "ai_fields": sum(len(value) for value in purified.values()),
        "updated_competitions": 0,
        "date_written": 0,
        "field_written": {field: 0 for field in DETAIL_FIELDS},
        "field_extracted": {field: 0 for field in DETAIL_FIELDS},
    }

    # 写库前统一备份（测试库也备份，成本可忽略；生产库另有 --allow-production 闸门）
    if args.apply and pending:
        log("[备份] %s" % common.backup_db(target_db))

    preview = []
    for target in pending:
        contest_id = target["contest_id"]
        raw_fields = extracted.get(contest_id) or {}
        ai_fields = purified.get(contest_id) or {}
        stale = stale_map.get(contest_id) or ""
        entry = {
            "competition_id": target["competition_id"],
            "name": target["name"],
            "contest_id": contest_id,
            "match": target["match"],
            "missing_fields": target["missing_fields"],
            "stale": stale,
            "ai": "skipped" if args.no_llm else "purified",
            "fields": {},
        }
        if stale and not args.allow_stale:
            # 沿用「报名结束超过 400 天 = 往年届次」护栏，不把旧届数据写进当前赛事
            stats["stale_skipped"] += 1
            preview.append(entry)
            continue

        payload = {}
        for field in target["missing_fields"]:
            raw_value = raw_fields.get(field)
            if raw_value:
                stats["field_extracted"][field] += 1
            value = raw_value if args.no_llm else ai_fields.get(field)
            if not value:
                continue
            payload[field] = value["text"]
            entry["fields"][field] = {
                "source": value.get("source") or "",
                "length": len(value["text"]),
                "text": value["text"],
                "raw_chars": value.get("raw_chars"),
            }
        if args.apply and payload:
            written = db.fill_competition_detail_text(
                target["target_id"],
                only_empty=True,
                db_path=target_db,
                **payload,
            )
            for field in written:
                stats["field_written"][field] += 1
                entry["fields"][field]["written"] = True
        if entry["fields"] and any(
                info.get("written") for info in entry["fields"].values()):
            stats["updated_competitions"] += 1

        # 比赛日期：AI 从「考试安排」正文里提的候选值，过 contest_dates 护栏后只补空
        ai_dates = dates_by_contest.get(contest_id) or {}
        if ai_dates:
            raw_text = (raw_fields.get("exam_schedule") or {}).get("text") or ""
            if args.apply:
                date_patch = contest_dates.apply(
                    target_db, target["target_id"], ai_dates, raw_text)
            else:
                date_patch = contest_dates.plan_from_db(
                    target_db, target["target_id"], ai_dates, raw_text)
            if date_patch:
                entry["date_patch"] = date_patch
                if args.apply:
                    stats["date_written"] += 1
                    stats["updated_competitions"] += 1
        preview.append(entry)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": args.mode,
        "apply": bool(args.apply),
        "ai_prompt_ver": AI_PROMPT_VER,
        "target_db": target_db,
        "stats": stats,
        "no_mapping": [
            {"competition_id": comp_id, "name": name, "reason": reason}
            for comp_id, name, reason in selection["no_mapping"]
        ],
        "items": preview,
    }
    preview_path = common.save_preview(args.preview, payload)
    log("[预览] %s" % preview_path)

    # 摘要
    log("-" * 60)
    log("[抽取] " + " / ".join(
        "%s %d 条" % (FIELD_LABELS[field], stats["field_extracted"][field])
        for field in DETAIL_FIELDS))
    log("[AI] 调用 %d 次 / 产出 %d 个字段 / 失败 %d 次 / tokens %d"
        % (stats["ai_calls"], stats["ai_fields"], stats["ai_failed"],
           stats["ai_tokens"]))
    if args.apply:
        log("[写入] 更新比赛 %d 条｜" % stats["updated_competitions"] + " / ".join(
            "%s %d 个" % (FIELD_LABELS[field], stats["field_written"][field])
            for field in DETAIL_FIELDS) + "｜比赛日期 %d 条" % stats["date_written"])
    else:
        log("[预演] 未写库；确认预览后加 --apply 执行")
    if stats["stale_skipped"]:
        log("[跳过] 往年届次 %d 条（可用 --allow-stale 强制写入）"
            % stats["stale_skipped"])
    if args.mode == "daily" and selection["outside_window"]:
        log("[窗口] 入库超过 %d 天的 %d 条未处理（--backfill 可全量）"
            % (args.days, selection["outside_window"]))
    if selection["no_mapping"]:
        log("[无映射] %d 条：%s" % (
            len(selection["no_mapping"]),
            "、".join("%s(%s)" % (name[:18], reason)
                     for _, name, reason in selection["no_mapping"][:6]),
        ))
    return 0 if not fetch_failed and not ai_failed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
