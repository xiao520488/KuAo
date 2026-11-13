# -*- coding: utf-8 -*-
"""stage_schedule.py —— 把赛程正文拆成「阶段级」结构化时间表。

背景（2026-11-13 重构）：
    主表原来只有 ``contest_start_date`` / ``contest_end_date`` 一个区间，
    无法表达「报名 9/10-11/30、初赛 12/1-12/20、复赛 2027/1/1-3/5、决赛 2027/5」
    这种多阶段赛程；详情页只能把 ``exam_schedule`` 当纯文本段落渲染。

本模块产出 ``stage_schedule``：JSON 数组，元素形如::

    {"stage": "初赛", "start_date": "2026-12-01",
     "end_date": "2026-12-20", "note": "作品申报"}

护栏（与 ``contest_dates`` 同一套口径）：
    1. 日期必须在原文里出现「月+日」佐证，否则置 NULL（绝不编造某一天）；
    2. 年份必须落在 [今年-1, 今年+1]；正文没写年份时按「优先今年」重算；
    3. ``end_date < start_date`` 时丢弃 end_date；
    4. 只补空：已有 ``stage_schedule`` 的行不覆盖；
    5. 阶段名为空、且没有任何日期与备注的条目直接丢弃。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import contest_dates
import db

SHANGHAI = timezone(timedelta(hours=8))
log = logging.getLogger(__name__)

COLUMN = "stage_schedule"

# 同义阶段名 -> 标准阶段名（用于排序与前端高亮）
STAGE_ALIASES = (
    ("报名", ("报名", "网上报名", "报名时间", "报名阶段", "参赛报名", "注册")),
    ("校赛", ("校内赛", "校赛")),
    ("省赛", ("省赛", "省级选拔赛", "区域赛", "分区赛", "选拔赛")),
    ("初赛", ("初赛", "预赛", "初评", "网评", "资格赛")),
    ("复赛", ("复赛", "半决赛", "复评", "复审", "复赛选拔")),
    ("决赛", ("决赛", "总决赛", "全国总决赛", "终审", "答辩", "现场决赛")),
)
STAGE_ORDER = ("报名", "校赛", "省赛", "初赛", "复赛", "决赛")

# 顶部「比赛时间」总跨度只取比赛阶段，报名不算（报名起止是另一条时间线）
SPAN_EXCLUDE = ("报名",)

MAX_STAGE_CHARS = 10
MAX_NOTE_CHARS = 40
MAX_STAGES = 8

# note 里出现这些字样才算「有信息量」，否则「具体时间以官网为准」这类空话不单独占一行
_NOTE_HAS_TIME = re.compile(r"年|月|日|上旬|中旬|下旬|预计")
# note 里写的年份超出窗口 = 往年届次，整条阶段丢弃（如「2020年完成全国选拔」）
_NOTE_YEAR = re.compile(r"(20\d{2})\s*年")
MIN_YEAR_OFFSET = -1
MAX_YEAR_OFFSET = 1


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def normalize_stage(name: str) -> str:
    """阶段名归一化：命中同义词返回标准名，否则保留原文（截断到 10 字）。"""
    plain = _flat(name)
    if not plain:
        return ""
    for canonical, aliases in STAGE_ALIASES:
        for alias in aliases:
            if alias in plain:
                return canonical
    return plain[:MAX_STAGE_CHARS]


def _guard_date(value, raw_text: str, *, today: Optional[date]) -> Optional[str]:
    """单个阶段日期过护栏：能解析 + 有原文佐证 + 年份合理，否则 None。"""
    if value in (None, "", "null", "None", "无"):
        return None
    item = contest_dates._normalize(value, today=today)
    if not item:
        return None
    iso = item[0]
    if not contest_dates.evidence(raw_text, iso):
        log.debug("[阶段] 丢弃日期 %s：正文无佐证", iso)
        return None
    if not contest_dates._year_in_text(raw_text, iso):
        month, day = int(iso[5:7]), int(iso[8:10])
        fixed = "%04d-%02d-%02d" % (
            contest_dates.infer_year(month, day, today=today), month, day)
        iso = fixed
    return iso


def guard_stages(items, raw_text: str, *, today: Optional[date] = None) -> list:
    """模型输出的阶段数组 -> 过护栏、去重、按时间排序的数组。"""
    today = today or datetime.now(SHANGHAI).date()
    out, seen = [], set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        stage = normalize_stage(item.get("stage") or item.get("name") or "")
        if not stage or stage in seen:
            continue
        start = _guard_date(item.get("start_date"), raw_text, today=today)
        end = _guard_date(item.get("end_date"), raw_text, today=today)
        if start and end and end < start:
            log.debug("[阶段] %s 的 end=%s 早于 start=%s，丢弃 end", stage, end, start)
            end = None
        note = _flat(item.get("note") or "")[:MAX_NOTE_CHARS]
        if not (start or end or note):
            continue
        if not (start or end):
            # 只有备注的阶段：备注里没时间信息（「以官网为准」）就没必要占一行
            if not _NOTE_HAS_TIME.search(note):
                continue
            years = [int(y) for y in _NOTE_YEAR.findall(note)]
            if years and all(
                    not (today.year + MIN_YEAR_OFFSET <= y <= today.year + MAX_YEAR_OFFSET)
                    for y in years):
                log.debug("[阶段] 丢弃 %s：备注年份 %s 越界", stage, years)
                continue
        out.append({
            "stage": stage,
            "start_date": start,
            "end_date": end,
            "note": note,
        })
        seen.add(stage)
    # 时间轴按日期排（报名永远在最前，其余有日期的按时间、无日期的沉底）
    out.sort(key=lambda row: (
        0 if row["stage"] == "报名" else 1,
        row["start_date"] or row["end_date"] or "9999-12-31",
        STAGE_ORDER.index(row["stage"])
        if row["stage"] in STAGE_ORDER else len(STAGE_ORDER),
    ))
    return out[:MAX_STAGES]


def span(stages: list) -> tuple:
    """比赛阶段（不含报名）的最早开始 / 最晚结束，用于顶部总跨度。

    若最晚结束早于最早开始（例如「名单公布 6/10」+「决赛 7/12」这种前置节点），
    结束日不可信，只返回开始日。
    """
    starts, ends = [], []
    for row in stages:
        if row.get("stage") in SPAN_EXCLUDE:
            continue
        if row.get("start_date"):
            starts.append(row["start_date"])
        if row.get("end_date"):
            ends.append(row["end_date"])
    start = min(starts) if starts else None
    end = max(ends) if ends else None
    if start and end and end < start:
        end = None
    return start, end


# note 里「2025年11月」「3月」这类只到月的表述，用来补顶部月份轨
_MONTH_WITH_YEAR = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")
_MONTH_ONLY = re.compile(r"(?<!\d)(\d{1,2})\s*月")
_DAY_IN_NOTE = re.compile(r"\d+\s*[日号]")


def span_candidates(stages: list, *, today: Optional[date] = None) -> list:
    """顶部跨度的全部候选：[(排序键, 精度, 类型, 值)]。

    精度 0=精确到日（来自 start_date/end_date），1=只到月（来自 note 的「YYYY年M月」）；
    同一时间点优先取更精确的那个。报名阶段不参与（报名是另一条时间线）。
    """
    today = today or datetime.now(SHANGHAI).date()
    lo, hi = today.year + MIN_YEAR_OFFSET, today.year + MAX_YEAR_OFFSET
    items = []
    for row in stages or []:
        if row.get("stage") in SPAN_EXCLUDE:
            continue
        for key in ("start_date", "end_date"):
            value = str(row.get(key) or "").strip()
            if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
                items.append((value, 0, "date", value, key))
        note = _flat(row.get("note"))
        if not note or _DAY_IN_NOTE.search(note):
            continue  # 备注里写到了日，说明日期本该在 start/end 上，别从备注猜
        hit = _MONTH_WITH_YEAR.search(note)
        if hit:
            year, month = int(hit.group(1)), int(hit.group(2))
        else:
            hit = _MONTH_ONLY.search(note)
            if not hit:
                continue
            year, month = today.year, int(hit.group(1))
        if not (1 <= month <= 12) or not (lo <= year <= hi):
            continue
        items.append(("%04d-%02d-01" % (year, month), 1, "month", (year, month), "note"))
    return items


def derive_span(stages: list, *, today: Optional[date] = None) -> dict:
    """顶部总跨度：精确到日就写 date 列，只到月就写 month 列（只给有值的一侧）。"""
    items = span_candidates(stages, today=today)
    if not items:
        return {}
    items.sort(key=lambda item: (item[0], item[1]))
    patch = {}

    def _put(candidate, side: str) -> None:
        _, _, kind, value, _ = candidate
        if kind == "date":
            patch["contest_%s_date" % side] = value
        else:
            patch["contest_%s_month" % side] = value[1]

    first, last = items[0], items[-1]
    if first is last:
        # 只有一个时间点：知道的是「结束」就只写结束，否则只写开始（单日赛事）
        _put(first, "end" if str(first[4]).startswith("end") else "start")
        return patch
    _put(first, "start")
    _put(last, "end")
    start, end = patch.get("contest_start_date"), patch.get("contest_end_date")
    if start and end and end < start:
        patch.pop("contest_end_date", None)
        patch.pop("contest_end_month", None)
    return patch


def dumps(stages: list) -> str:
    """数组 -> 入库字符串（ensure_ascii=False，便于人工排查）。"""
    return json.dumps(stages or [], ensure_ascii=False, separators=(",", ":"))


def loads(raw) -> list:
    """入库字符串 -> 数组；坏数据返回空数组，绝不抛异常。"""
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def plan_writes(current: dict, stages: list, raw_text: str, *,
                today: Optional[date] = None) -> dict:
    """返回可写入的 patch：stage_schedule + 顶部总跨度（只补空）。

    顶部跨度双轨：抽到精确日期就写 ``contest_*_date``；阶段只写到月（备注里的
    「2025年11月」）就写 ``contest_*_month``，避免详情页出现「暂未收录」。
    """
    patch = {}
    if not loads(current.get(COLUMN)) and stages:
        patch[COLUMN] = dumps(stages)

    wanted = derive_span(stages or [], today=today)
    dates = {k: v for k, v in wanted.items() if k.endswith("_date")}
    months = {k: v for k, v in wanted.items() if k.endswith("_month")}
    if dates:
        patch.update(contest_dates.plan_date_writes(
            current, dates, raw_text, today=today))
    for field, value in months.items():
        if current.get(field) in (None, ""):
            patch.setdefault(field, value)
    return patch


def plan_from_db(db_path: str, competition_id: int, stages: list,
                 raw_text: str, *, today: Optional[date] = None) -> dict:
    """dry-run：读当前行，返回「将会写入」的 patch。"""
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT contest_start_date, contest_end_date, contest_start_month, "
            "contest_end_month, %s FROM competitions WHERE id = ?" % COLUMN,
            (int(competition_id),),
        ).fetchone()
    if not row:
        return {}
    return plan_writes(dict(row), stages, raw_text, today=today)


def apply(db_path: str, competition_id: int, stages: list, raw_text: str, *,
          today: Optional[date] = None) -> dict:
    """过护栏后只补空写回，返回实际写入的 patch。"""
    patch = plan_from_db(db_path, competition_id, stages, raw_text, today=today)
    if patch:
        db.update_competition(int(competition_id), patch, db_path=db_path)
    return patch


# ============================================================
# AI 抽取：正文 -> [{"stage", "start_date", "end_date", "note"}]
# ============================================================

EXTRACT_SYSTEM = (
    "你是严谨的赛事赛程抽取器。把赛事通知里的阶段时间节点拆成结构化数组，"
    "只使用原文明确写出的日期，绝不猜测、绝不编造。只输出 JSON。"
)

EXTRACT_SCHEMA_HINT = (
    '{"stage_schedule": ['
    '{"stage": "报名", "start_date": "2026-09-10", "end_date": "2026-11-30", "note": ""},'
    '{"stage": "初赛", "start_date": "2026-12-01", "end_date": "2026-12-20", "note": "作品申报"},'
    '{"stage": "决赛", "start_date": null, "end_date": null, "note": "预计2027年5月"}'
    ']}'
)


def build_extract_prompt(name: str, text: str) -> str:
    return "\n".join([
        "从下面的赛事通知中抽取「赛程阶段时间表」，按阶段输出数组。",
        "",
        "赛事名称：%s" % (name or "（未提供）"),
        "",
        "规则：",
        "1. stage 用标准阶段名：报名 / 初赛 / 复赛 / 决赛；"
        "不属于这四类的（如作品申报、省赛、校内赛、公示、集训）保留原文 10 字以内的说法。",
        "2. start_date / end_date 输出 YYYY-MM-DD，必须来自原文；"
        "原文只写「2026年12月1日～12月20日」就输出 2026-12-01 与 2026-12-20。"
        "**原文没写年份时输出 MM-DD（如 05-12），系统会按赛程自动补年份，不要因此丢掉日期。**",
        "3. **原文写了具体日期的阶段必须填日期**，不要把日期塞进 note："
        "note 只写「网络评审」「作品申报」这类说明，不要重复日期。",
        "4. 原文只给月份（如「预计2027年5月」）时，start_date / end_date 都输出 null，"
        "把原话写进 note；**绝不能替原文编一个具体日子**。",
        "5. 原文说「各年时间略有不同」但后面又给了某一年的具体日期时，照那个年份抽。",
        "6. 原文没提到的阶段不要输出；没有日期也没有说明的阶段才省略。",
        "7. 阶段按时间先后排列，最多 %d 个；note 不超过 40 字。" % MAX_STAGES,
        "8. 找不到任何阶段信息就输出空数组 []。",
        "",
        "只输出 JSON，不要任何解释。格式：%s" % EXTRACT_SCHEMA_HINT,
        "",
        "原文：",
        str(text or "")[:3000],
    ])


def extract_raw(name: str, text: str) -> list:
    """调一次模型，返回原始阶段数组（**未过护栏**，调用方必须 guard_stages）。"""
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
        raise llm.LLMError("赛程抽取返回的不是 JSON 对象：%r" % type(data).__name__)
    items = data.get("stage_schedule")
    if not isinstance(items, list):
        return []
    return [row for row in items if isinstance(row, dict)]


def extract_stages(name: str, text: str, *, raw_text: Optional[str] = None) -> list:
    """抽阶段并直接过护栏；失败抛 llm.LLMError。"""
    items = extract_raw(name, text)
    return guard_stages(items, raw_text if raw_text is not None else text)