# -*- coding: utf-8 -*-
"""contest_dates.py —— 从「考试安排」正文提炼比赛日期，并安全反写主表。

背景（2026-11-13 排查结论）：
    赛氪详情 API 的 ``contest_start_time`` / ``contest_end_time`` 是占位符
    ``'-0001/11/30 00:00:00'``（表示「未设置」），所以 91 条赛事里只有 5 条有比赛日期；
    但 AI 早就把「决赛于11月14日举行」写进了 ``exam_schedule`` 正文，
    结构化字段 ``contest_start_date`` 却仍是空——文本与结构化数据脱节。

本模块负责三件事：
    1. ``build_extract_prompt`` / ``extract_dates``：让模型在提炼正文的同时，
       额外输出 ``extracted_contest_date``（正文没写就 null）；
    2. ``plan_date_writes``：护栏校验（只补空 / 原文佐证 / 年份合理 / 起止有序）；
    3. ``apply``：把通过校验的日期写回 ``contest_*_date`` 与 ``contest_*_month``。

红线：
    * 只补 NULL，已有值一律不覆盖；
    * 正文必须真的出现该「月+日」，否则视为模型编造直接丢弃；
    * 年份必须落在 [今年-1, 今年+1]；只有月日时按「离今天最近」推断年份；
    * 解析不出「年-月-日」就写 NULL，绝不替模型补日。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import db

SHANGHAI = timezone(timedelta(hours=8))
DETAIL_DATE_FIELDS = ("contest_start_date", "contest_end_date")
DATE_TO_MONTH = {
    "contest_start_date": "contest_start_month",
    "contest_end_date": "contest_end_month",
}

# 年份合理窗口：比赛日不会比今天早一年多，也不会晚一年多（届次换代）
MIN_YEAR_OFFSET = -1
MAX_YEAR_OFFSET = 1

# 只有月日时的年份推断窗口：落在 [今天-330 天, 今天+400 天] 内的年份优先
PAST_WINDOW_DAYS = 330
FUTURE_WINDOW_DAYS = 400

log = logging.getLogger(__name__)

_RE_CN_DATE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def evidence(text: str, iso: str) -> bool:
    """正文里是否真的写了这个「月+日」（防模型编造）。

    支持 ``11月14日`` / ``11-14`` / ``11/14`` / ``11.14`` / ``2026年11月14日``。
    """
    parts = str(iso or "").split("-")
    if len(parts) != 3:
        return False
    try:
        month, day = int(parts[1]), int(parts[2])
    except ValueError:
        return False
    flat = _flat(text)
    if "%d月%d日" % (month, day) in flat or "%d月%d号" % (month, day) in flat:
        return True
    patterns = (
        "%02d-%02d" % (month, day),
        "%d-%02d" % (month, day),
        "%02d/%02d" % (month, day),
        "%d/%d" % (month, day),
        "%02d.%02d" % (month, day),
    )
    return any(p in flat for p in patterns)


def infer_year(month: int, day: int, *, today: Optional[date] = None) -> int:
    """只有「月日」时推断年份：优先今年，其次明年，最后去年（取第一个落在窗口内的）。

    优先今年而非「离今天最近」，是因为库里绝大多数是当届/刚结束的赛事：
    11 月抓到的「4月11日决赛」应记 2026（已结束），而不是 2027（凭空出现倒计时）。
    """
    today = today or datetime.now(SHANGHAI).date()
    for year in (today.year, today.year + 1, today.year - 1):
        try:
            gap = (date(year, month, day) - today).days
        except ValueError:
            continue
        if -PAST_WINDOW_DAYS <= gap <= FUTURE_WINDOW_DAYS:
            return year
    return today.year


def _year_in_text(text: str, iso: str) -> bool:
    """正文里是否出现了该年份（『2026年』『2026-』『2026/』等）。"""
    year = str(iso)[:4]
    return bool(re.search(r"(?<!\d)%s(?!\d)" % year, _flat(text)))


def _normalize(value, *, today: Optional[date] = None) -> Optional[tuple]:
    """模型给的值 -> (iso, month)；解析不出完整日期返回 None。"""
    iso = db._date_or_none(value)
    if not iso:
        return None
    if len(iso) == 5:  # 'MM-DD'：原文没给年份，按窗口推断
        month, day = int(iso[:2]), int(iso[3:])
        iso = "%04d-%s" % (infer_year(month, day, today=today), iso)
    parts = iso.split("-")
    if len(parts) != 3:
        return None
    year = int(parts[0])
    today = today or datetime.now(SHANGHAI).date()
    if not (today.year + MIN_YEAR_OFFSET <= year <= today.year + MAX_YEAR_OFFSET):
        return None
    return iso, int(parts[1])


def _year_plausible(iso: str, *, today: Optional[date] = None) -> bool:
    today = today or datetime.now(SHANGHAI).date()
    try:
        year = int(str(iso)[:4])
    except ValueError:
        return False
    return today.year + MIN_YEAR_OFFSET <= year <= today.year + MAX_YEAR_OFFSET


def plan_date_writes(current: dict, dates: dict, raw_text: str, *,
                     today: Optional[date] = None) -> dict:
    """把模型输出的日期过一遍护栏，返回可写入主表的 patch（只含要补的列）。

    参数:
        current: 当前行（含 contest_*_date / contest_*_month）
        dates: ``{"contest_start_date": "...", "contest_end_date": "..."}``
        raw_text: 用于佐证的原始正文（考试安排原文，不是 AI 改写句）

    返回:
        ``{"contest_start_date": "2026-11-14", "contest_start_month": 11}``；
        没通过校验的列不会出现。
    """
    today = today or datetime.now(SHANGHAI).date()
    parsed = {}
    for field in DETAIL_DATE_FIELDS:
        value = dates.get(field)
        if not value:
            continue
        if str(current.get(field) or "").strip():
            continue  # 已有日期，绝不覆盖
        item = _normalize(value, today=today)
        if not item:
            continue
        iso = item[0]
        if not evidence(raw_text, iso):
            log.warning("[日期] 丢弃 %s=%s：正文没有这一天的佐证", field, iso)
            continue
        if not _year_in_text(raw_text, iso):
            # 正文没写年份，模型却给了年份：不信任模型的猜测，按「优先今年」重新推断
            month, day = int(iso[5:7]), int(iso[8:10])
            fixed = "%04d-%02d-%02d" % (infer_year(month, day, today=today), month, day)
            if fixed != iso:
                log.info("[日期] %s=%s 的年份不在正文里，改为 %s", field, iso, fixed)
                iso = fixed
        parsed[field] = iso

    start = parsed.get("contest_start_date")
    end = parsed.get("contest_end_date")
    if start and end and end < start:
        log.warning("[日期] 丢弃 end=%s：早于 start=%s", end, start)
        parsed.pop("contest_end_date", None)

    patch = {}
    for field, iso in parsed.items():
        patch[field] = iso
        month_field = DATE_TO_MONTH[field]
        if current.get(month_field) in (None, ""):
            patch[month_field] = int(iso[5:7])
    return patch


def plan_from_db(db_path: str, competition_id: int, dates: dict, raw_text: str, *,
                 today: Optional[date] = None) -> dict:
    """读当前行 -> 过护栏，返回「将会写入」的 patch（不写库，dry-run 用）。"""
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT contest_start_date, contest_end_date, "
            "contest_start_month, contest_end_month "
            "FROM competitions WHERE id = ?", (int(competition_id),),
        ).fetchone()
    if not row:
        return {}
    return plan_date_writes(dict(row), dates or {}, raw_text, today=today)


def apply(db_path: str, competition_id: int, dates: dict, raw_text: str, *,
          today: Optional[date] = None) -> dict:
    """过护栏后只补空写回。返回实际写入的 patch。"""
    patch = plan_from_db(db_path, competition_id, dates, raw_text, today=today)
    if patch:
        db.update_competition(int(competition_id), patch, db_path=db_path)
    return patch


# ============================================================
# AI 抽取：正文 -> {"contest_start_date": ..., "contest_end_date": ...}
# ============================================================

EXTRACT_SYSTEM = (
    "你是严谨的赛事时间抽取器。只从给定文本中抽取比赛（初赛/复赛/决赛/决赛周等）"
    "的开始与结束日期，绝不猜测、绝不编造。只输出 JSON。"
)

EXTRACT_SCHEMA_HINT = (
    '{"contest_start_date": "2026-11-14", "contest_end_date": null, '
    '"evidence": "决赛于11月14日举行"}'
)


def build_extract_prompt(name: str, text: str) -> str:
    return "\n".join([
        "从下面的赛事文本中抽取「比赛时间」（不是报名时间）。",
        "",
        "赛事名称：%s" % (name or "（未提供）"),
        "",
        "规则：",
        "1. 只抽「比赛/初赛/复赛/决赛/决赛周/答辩/评审」等比赛环节的日期；"
        "报名开始/截止、作品提交截止、缴费截止都不算比赛时间。",
        "2. 输出 YYYY-MM-DD。原文只写了「11月14日」这种不带年份的，按赛程推断："
        "报名在 2025 年、决赛在次年春天，就写 2026；无法判断时按离报名结束最近的年份写。",
        "3. 只有月份、没有具体日（如「7月至8月」）就输出 null——不要编造某一天。",
        "4. 找不到任何比赛日期就两个字段都输出 null。",
        "5. evidence 必须是原文里出现过的片段（不超过 40 字），用于人工复核。",
        "",
        "只输出 JSON，不要任何解释。格式：%s" % EXTRACT_SCHEMA_HINT,
        "",
        "原文：",
        str(text or "")[:2500],
    ])


def extract_dates(name: str, text: str) -> dict:
    """调一次模型抽取比赛日期；失败抛 llm.LLMError（调用方保留 NULL）。"""
    import llm

    data = llm.chat_json(
        build_extract_prompt(name, text),
        system=EXTRACT_SYSTEM,
        schema_hint=EXTRACT_SCHEMA_HINT,
        temperature=0.0,
        max_tokens=2000,
        retries=1,
    )
    if not isinstance(data, dict):
        raise llm.LLMError("日期抽取返回的不是 JSON 对象：%r" % type(data).__name__)
    if isinstance(data.get("fields"), dict):
        data = data["fields"]
    result = {}
    for field in DETAIL_DATE_FIELDS:
        value = data.get(field)
        if value in (None, "", "null", "None"):
            continue
        result[field] = str(value).strip()
    return result