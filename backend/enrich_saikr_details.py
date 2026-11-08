# -*- coding: utf-8 -*-
"""enrich_saikr_details.py —— 赛氪详情补充器（只补空字段，默认 dry-run）

背景
----
详情页的「参赛对象 / 内容范围 / 考试安排」三张卡片依赖
`competitions.participants / content_scope / exam_schedule` 三列，
但这两列此前没有数据。赛氪详情 API 的 `content`（HTML 正文通知）里
包含「参赛对象 / 赛项设置 / 赛程安排」等章节，本脚本负责把它们清洗成
纯文本并按赛事回填。

合规与安全（与 CRAWLER_HANDOVER.md 第四/九节红线一致）
------------------------------------------------------
* 只处理生产库里已有的赛事（通过 `saikr_map.json` 的 contest_id 定点补全），
  不遍历全站、不发现新赛事；
* 请求间隔 3~5 秒（`--delay` 最低 3 秒），随机抖动；
* 默认 dry-run，只抓取 + 抽取 + 写预览 JSON，不碰数据库；
* `--apply` 才写库，且**只填 NULL/空串**，已有值一律保留；
* 目标库若是生产库，必须 `--apply --allow-production`，写前自动备份；
* 抽取不出来就保留 NULL，绝不编造。

数据流
------
    source-db（默认生产库，只读）  取 33 条赛事 id/name
        -> saikr_map.json          映射 contest_id
        -> 赛氪详情 API            GET .../contest/info?contest_id=xxx
        -> HTML 章节解析           参赛对象 / 内容范围 / 考试安排
        -> target-db（默认 test_crawl.db） 仅补空字段

用法
----
    # 1. 预演（默认）：只抓取并生成预览，不写库
    .\\venv\\Scripts\\python.exe enrich_saikr_details.py

    # 2. 只验证 3~5 条
    .\\venv\\Scripts\\python.exe enrich_saikr_details.py --ids 1,6,10,12

    # 3. 写测试库
    .\\venv\\Scripts\\python.exe enrich_saikr_details.py --apply

    # 4. 写生产库（必须先看过预览）
    .\\venv\\Scripts\\python.exe enrich_saikr_details.py --apply --allow-production \\
        --db-path .\\competitions.db --source-db .\\competitions.db
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
import discover_common as common

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
# 三、赛事映射与目标库
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


def load_source_competitions(db_path: str) -> list:
    if not os.path.exists(db_path):
        raise SystemExit("源库不存在：%s" % db_path)
    rows = _read_rows(db_path, "SELECT id, name FROM competitions ORDER BY id")
    if not rows:
        raise SystemExit("源库里没有 competitions 数据：%s" % db_path)
    return rows


def load_target_competitions(db_path: str) -> dict:
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
        "SELECT id, name, participants, content_scope, exam_schedule "
        "FROM competitions ORDER BY id",
    )
    return {int(row["id"]): row for row in rows}


def _norm_name(name: str) -> str:
    return db.normalize_competition_name(name or "")


def resolve_targets(source_rows: list, target_rows: dict,
                    mapping: dict) -> tuple:
    """为每条源赛事确定 (target_row, contest_id, 匹配方式)。

    优先用 saikr_map 的 competition_id；没有映射时按名称相似度兜底。
    返回 (targets, no_mapping)；targets 元素为 dict。
    """
    by_name = []
    for item in mapping.values():
        cid = item.get("saikr_contest_id")
        name = _norm_name(item.get("saikr_name") or "")
        if cid and name:
            by_name.append((name, int(cid), item))

    targets, no_mapping = [], []
    for source in source_rows:
        comp_id = int(source["id"])
        target = target_rows.get(comp_id)
        if target is None:
            # 目标库 id 不一致时按名称兜底定位
            norm = _norm_name(source["name"])
            for row in target_rows.values():
                if _norm_name(row["name"]) == norm:
                    target = row
                    break
        if target is None:
            no_mapping.append((comp_id, source["name"], "目标库无对应赛事"))
            continue

        item = mapping.get(comp_id)
        contest_id = item.get("saikr_contest_id") if item else None
        match = "saikr_map(%s)" % (item.get("match_type") or "?") if item else ""
        if not contest_id:
            norm = _norm_name(source["name"])
            scored = sorted(
                ((difflib.SequenceMatcher(None, norm, name).ratio(), cid)
                 for name, cid, _ in by_name if name),
                reverse=True,
            )
            if scored and scored[0][0] >= MIN_NAME_RATIO \
                    and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.03):
                contest_id = scored[0][1]
                match = "名称匹配(%.2f)" % scored[0][0]
        if not contest_id:
            no_mapping.append((comp_id, source["name"], "无赛氪 contest_id"))
            continue
        targets.append({
            "competition_id": comp_id,
            "target_id": int(target["id"]),
            "name": source["name"],
            "target": target,
            "contest_id": int(contest_id),
            "match": match,
        })
    return targets, no_mapping


# ============================================================
# 四、主流程
# ============================================================

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="赛氪详情补充器：只补 participants/content_scope/exam_schedule 空字段（默认 dry-run）"
    )
    parser.add_argument("--db-path", default=common.DEFAULT_DB_PATH,
                        help="目标库（默认 test_crawl.db）")
    parser.add_argument("--source-db", default=common.PRODUCTION_DB_PATH,
                        help="赛事清单来源库（默认生产库 competitions.db，只读）")
    parser.add_argument("--apply", action="store_true", help="真正写目标库")
    parser.add_argument("--allow-production", action="store_true",
                        help="允许写生产库（必须与 --apply 同用，且会先备份）")
    parser.add_argument("--ids", default="",
                        help="只处理指定赛事 id，逗号分隔，例如 1,6,10")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理多少条（0=不限）")
    parser.add_argument("--delay", default="3,5",
                        help="赛氪请求间隔秒，默认 3,5（最低 3 秒）")
    parser.add_argument("--max-chars", type=int, default=MAX_FIELD_LEN,
                        help="单字段最大字符数（默认 %d）" % MAX_FIELD_LEN)
    parser.add_argument("--preview", default="saikr_enrich_preview.json",
                        help="预览 JSON 文件名（写入 workspace/）")
    parser.add_argument("--allow-stale", action="store_true",
                        help="允许写入往年届次页面（默认沿用 400 天护栏跳过）")
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
    MAX_FIELD_LEN = args.max_chars

    target_db = os.path.abspath(args.db_path)
    source_db = os.path.abspath(args.source_db)
    if args.apply and common.is_production_db(target_db) \
            and not args.allow_production:
        raise SystemExit("拒绝写生产库：必须同时传 --allow-production")

    ids = _parse_ids(args.ids)
    mapping = load_map()
    source_rows = load_source_competitions(source_db)
    target_rows = load_target_competitions(target_db)

    targets, no_mapping = resolve_targets(source_rows, target_rows, mapping)
    if ids:
        targets = [t for t in targets if t["competition_id"] in ids]

    # 只处理「至少一个字段为空」的赛事；已补全的跳过，便于断点续跑
    pending, complete = [], []
    for target in targets:
        row = target["target"]
        missing = [field for field in DETAIL_FIELDS
                   if not str(row.get(field) or "").strip()]
        if not missing:
            complete.append(target)
            continue
        target["missing_fields"] = missing
        pending.append(target)
    if args.limit > 0:
        pending = pending[:args.limit]

    log("[目标库] %s" % target_db)
    log("[源库]   %s（只读）" % source_db)
    log("[映射] 源库 %d 条 / 可定位 %d 条 / 无映射 %d 条 / 已补全跳过 %d 条"
        % (len(source_rows), len(targets), len(no_mapping), len(complete)))
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

    preview, stats = [], {
        "considered": len(pending),
        "fetch_ok": fetch_ok,
        "fetch_failed": fetch_failed,
        "stale_skipped": 0,
        "updated_competitions": 0,
        "field_written": {field: 0 for field in DETAIL_FIELDS},
        "field_extracted": {field: 0 for field in DETAIL_FIELDS},
    }

    # 写库前统一备份（测试库也备份，成本可忽略；生产库另有 --allow-production 闸门）
    if args.apply:
        log("[备份] %s" % common.backup_db(target_db))

    for target in pending:
        contest_id = target["contest_id"]
        fields = extracted.get(contest_id) or {}
        stale = stale_map.get(contest_id) or ""
        entry = {
            "competition_id": target["competition_id"],
            "name": target["name"],
            "contest_id": contest_id,
            "match": target["match"],
            "missing_fields": target["missing_fields"],
            "stale": stale,
            "fields": {},
        }
        if stale and not args.allow_stale:
            # 沿用「报名结束超过 400 天 = 往年届次」护栏，不把旧届数据写进当前赛事
            stats["stale_skipped"] += 1
            preview.append(entry)
            continue
        for field in DETAIL_FIELDS:
            value = fields.get(field)
            if value:
                stats["field_extracted"][field] += 1
            if field not in target["missing_fields"] or not value:
                continue
            entry["fields"][field] = {
                "source": value["source"],
                "length": len(value["text"]),
                "text": value["text"],
            }
            if args.apply:
                written = db.fill_competition_detail_text(
                    target["target_id"],
                    **{field: value["text"]},
                    only_empty=True,
                    db_path=target_db,
                )
                if field in written:
                    stats["field_written"][field] += 1
                    entry["fields"][field]["written"] = True
        if entry["fields"] and any(
                info.get("written") for info in entry["fields"].values()):
            stats["updated_competitions"] += 1
        preview.append(entry)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "apply" if args.apply else "dry-run",
        "target_db": target_db,
        "source_db": source_db,
        "stats": stats,
        "no_mapping": [
            {"competition_id": comp_id, "name": name, "reason": reason}
            for comp_id, name, reason in no_mapping
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
    if args.apply:
        log("[写入] 更新比赛 %d 条｜" % stats["updated_competitions"] + " / ".join(
            "%s %d 个" % (FIELD_LABELS[field], stats["field_written"][field])
            for field in DETAIL_FIELDS))
    else:
        log("[预演] 未写库；确认预览后加 --apply 执行")
    if stats["stale_skipped"]:
        log("[跳过] 往年届次 %d 条（可用 --allow-stale 强制写入）"
            % stats["stale_skipped"])
    if no_mapping:
        log("[无映射] %d 条：%s" % (
            len(no_mapping),
            "、".join("%s(%s)" % (name[:18], reason)
                     for _, name, reason in no_mapping[:6]),
        ))
    return 0 if not fetch_failed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
