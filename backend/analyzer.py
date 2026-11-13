# -*- coding: utf-8 -*-
"""离线灵感生成器（analyzer.py）。

用途
    读取指定竞赛的真实获奖作品（awards.competition_id = 参数），调用大模型生成
    该赛事专属的 insights（experience / trend / gap）与 ideas，写入 SQLite。
    页面通过 GET /api/ai/inspirations?competition_id=... 读取，严格按赛事隔离。

运行（在本 backend 目录下）
    python analyzer.py --competition_id 10
    python analyzer.py --competition_id 10 --idea-n 6
    python analyzer.py --competition_id 10 --only insight
    python analyzer.py --competition_id 10 --dry-run
    python analyzer.py --competition_id 10 --force

本次针对「completion_tokens 4096 被截断 -> JSON 解析失败」的优化
    1. insights 拆成 experience / trend / gap 三次独立请求；ideas 每 2 条一批请求。
    2. 所有请求 max_tokens 默认 8000，并在 Prompt 中强制「精简，每条不超过 100 字」。
    3. insights 直接要 Markdown 正文，不走 JSON；ideas 用 try-except + 正则 +
       json-repair + 逐段截断兜底解析，单条失败不会拖垮整次生成。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Optional

import db
import llm

DEFAULT_IDEA_N = 4
# 只把 24 条代表性样本喂给模型；全量数据只以「统计分布」形式出现（条数/占比），
# 既避免 prompt 爆炸，又保证趋势判断有真实分母。
MAX_AWARDS = 24
MAX_FIELD_CHARS = 400
# insights 单条 200 字 ≈ 300 token，给 400 足够；ideas JSON ≤1500 字 ≈ 1200 token。
INSIGHT_MAX_TOKENS = 600
IDEAS_MAX_TOKENS = 1800
DEFAULT_MAX_TOKENS = INSIGHT_MAX_TOKENS
# 单次请求 prompt 的硬上限（字符）。超过即报错，防止再出现「把全量 id 塞进 prompt」。
# 正常 prompt ≈ 8-12K 字符（≈5-7K token）。
MAX_PROMPT_CHARS = 24000
INSIGHT_MAX_CHARS = 200
IDEA_BATCH_SIZE = 2
MAX_IDEA_REFS = 2
# Prompt 版本号：改动 Prompt 后递增，使旧缓存指纹失效。
PROMPT_VER = "v4"

KIND_LABEL = {
    "experience": "获奖共性经验",
    "trend": "选题与技术趋势",
    "gap": "选题空白与差异化机会",
}

KIND_TASK = {
    "experience": "总结获奖共性经验：分 3 点写，每点包含「做法 + 为什么有效 + 可执行建议」。",
    "trend": "总结选题与技术趋势：说明 2-3 个上升/下降方向，并指出技术栈变化。",
    "gap": "指出尚未被充分占据的选题空白与差异化机会，并说明哪些方向竞争过热。",
}

_FENCE_RE = re.compile(r"```(?:json|JSON|markdown|md)?\s*(.*?)```", re.DOTALL)


# ---------------------------------------------------------------- 基础工具
def _clip(value, n: int = MAX_FIELD_CHARS) -> str:
    """截断长文本，控制 prompt token。"""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= n else text[:n] + "…"


def _slim_award(row: dict) -> dict:
    """只保留对分析有价值的获奖作品字段。

    个人赛名单里的 summary 基本是「准考证号 17515130；科目：…；赛事：…」，
    对提炼共性和趋势毫无信息量，却占掉样本体积的一半，直接丢弃；
    空字段也一律不输出，避免 prompt 里堆满空串。
    """
    tech = row.get("tech_stack") or []
    if isinstance(tech, (list, tuple)):
        tech = "、".join(str(t) for t in tech if str(t).strip())
    out = {
        "id": row.get("id"),
        "year": row.get("year"),
        "title": _clip(row.get("title"), 160),
        "award": row.get("award_grade") or row.get("award_level") or "",
        "track": _clip(row.get("track"), 100),
        "team": _clip(row.get("team"), 100),
    }
    summary = _clip(row.get("summary"))
    if summary and not summary.lstrip().startswith("准考证号"):
        out["summary"] = summary
    if row.get("highlights"):
        out["highlights"] = _clip(row.get("highlights"))
    tech = _clip(tech, 160)
    if tech:
        out["tech_stack"] = tech
    return {k: v for k, v in out.items() if v not in (None, "")}


def _fair_sample(rows: list, k: int) -> list:
    """按年份占比抽样（最大余数法），避免少数年份被过度放大。

    历史 bug：按年份「均匀」抽样，让 11 条 2021-2025 示例数据挤进 24 条样本
    （占比被放大到 40%+），模型于是编出「技术栈转向多模态 AI」的结论。
    现在严格按真实占比分配名额：占比 < 半条样本的年份自然拿到 0 个名额。
    """
    if len(rows) <= k:
        return list(rows)
    by_year: dict = {}
    for r in rows:
        by_year.setdefault(str(r.get("year") or "未知"), []).append(r)
    total = len(rows)
    quota = {y: len(items) * k / total for y, items in by_year.items()}
    base = {y: int(q) for y, q in quota.items()}
    rest = k - sum(base.values())
    order = sorted(quota, key=lambda y: (quota[y] - base[y], quota[y]), reverse=True)
    for y in order[:rest]:
        base[y] += 1

    picked = []
    for y, items in by_year.items():
        n = min(base.get(y, 0), len(items))
        if n <= 0:
            continue
        ordered = sorted(items, key=lambda r: r.get("id") or 0)
        step = len(ordered) / n
        picked.extend(ordered[int(i * step)] for i in range(n))
    return picked


def _scope(competition_id: int, year_from: int, year_to: int) -> str:
    return f"competition:{int(competition_id)}|year:{year_from}-{year_to}"


def _ids_of(rows: list) -> set:
    out = set()
    for r in rows:
        try:
            out.add(int(r.get("id")))
        except (TypeError, ValueError):
            continue
    return out


def _filter_ids(raw, valid: set) -> list:
    """只保留本次输入中真实存在的作品 ID，过滤模型编造 / 跨赛事引用。"""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for v in raw:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv in valid and iv not in out:
            out.append(iv)
    return out


def _year_range(works: list) -> tuple:
    years = []
    for w in works:
        try:
            years.append(int(w.get("year")))
        except (TypeError, ValueError):
            continue
    if not years:
        import datetime
        now = datetime.datetime.now().year
        return now, now
    return min(years), max(years)


# ---------------------------------------------------------------- 容错解析
def _strip_fence(text: str) -> str:
    """去掉 ```json / ```markdown 围栏，返回内部文本。"""
    if not text:
        return ""
    body = str(text).strip()
    match = _FENCE_RE.search(body)
    if match:
        return match.group(1).strip()
    # 只有开头围栏、结尾被截断的情况
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
    return body.strip()


def _json_candidates(text: str):
    """按「原文 -> 代码围栏 -> 第一个 { 或 [ 开始」的顺序给出候选 JSON 片段。"""
    if not text:
        return
    body = str(text).strip()
    yield body
    for match in _FENCE_RE.finditer(body):
        yield match.group(1).strip()
    first = re.search(r"[\[{]", body)
    if first:
        yield body[first.start():].strip()


def _json_from_text(text) -> Optional[object]:
    """从模型输出中尽力解析 JSON。

    依次尝试：
        1. json.loads
        2. llm.safe_json（内部使用 json_repair，可修截断）
        3. 直接调用 json_repair.repair_json
        4. 只截取第一个 { / [ 之后的文本再试
    全部失败返回 None，绝不抛异常。
    """
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str) or not text.strip():
        return None

    for candidate in _json_candidates(text):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, (dict, list)):
                return parsed
        except (TypeError, ValueError):
            pass

        try:
            parsed = llm.safe_json(candidate, fallback=None, log=False)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:  # noqa: BLE001 - 解析容错，不允许中断
            pass

        try:
            from json_repair import repair_json
            parsed = repair_json(candidate, return_objects=True)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:  # noqa: BLE001 - 依赖缺失/修复失败都继续
            pass

    return None


def _regex_json_string(text: str, field: str) -> str:
    """从可能被截断的 JSON 文本里正则提取某个字符串字段。"""
    if not text:
        return ""
    pattern = rf'"{re.escape(field)}"\s*:\s*"((?:\\.|[^"\\])*)"?'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return ""
    raw = match.group(1)
    try:
        return json.loads('"' + raw + '"')
    except (TypeError, ValueError):
        return raw


def _regex_json_strings(text: str, field: str) -> list:
    """提取多个同类字符串字段（用于 ideas 被截断时至少保住标题）。"""
    if not text:
        return []
    pattern = rf'"{re.escape(field)}"\s*:\s*"((?:\\.|[^"\\])*)"'
    out = []
    for match in re.finditer(pattern, text, re.DOTALL):
        raw = match.group(1)
        try:
            value = json.loads('"' + raw + '"')
        except (TypeError, ValueError):
            value = raw
        value = str(value).strip()
        if value and value not in out:
            out.append(value)
    return out


def _parse_insight_text(raw) -> str:
    """insights 请求优先返回 Markdown；同时兼容模型误输出 JSON 的情况。"""
    if not raw:
        return ""
    text = _strip_fence(str(raw))
    if not text:
        return ""

    data = _json_from_text(text)
    if isinstance(data, dict):
        for key in ("content_md", "content", "markdown", "text"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return _strip_fence(value).strip()
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                for key in ("content_md", "content", "markdown", "text"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        return _strip_fence(value).strip()

    # JSON 被截断：正则直接抓 content_md 字符串
    value = _regex_json_string(text, "content_md")
    if value.strip():
        return _strip_fence(value).strip()

    return text


def _parse_ideas(raw, valid: set, limit: int) -> list:
    """解析 ideas，兼容对象/数组/截断 JSON，并对字段做归属过滤。"""
    data = _json_from_text(raw)
    items = []
    if isinstance(data, dict):
        raw_items = data.get("ideas") or data.get("items") or data.get("data")
        if isinstance(raw_items, list):
            items = raw_items
        elif isinstance(raw_items, dict):
            items = [raw_items]
    elif isinstance(data, list):
        items = data

    if not items:
        # 最后兜底：JSON 被严重截断时，至少把 title 捞出来
        items = [{"title": t} for t in _regex_json_strings(str(raw), "title")]

    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "pitch": item.get("pitch") or item.get("summary") or "",
            "detail_md": item.get("detail_md") or item.get("detail") or "",
            "novelty": item.get("novelty") or "",
            "risk": item.get("risk") or "",
            "ref_awards": _filter_ids(item.get("ref_awards"), valid),
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- Prompt
def _distribution(works: list, top_n: int = 8) -> dict:
    """全量获奖数据的统计分布（用条数说话，而不是让模型自己猜趋势）。"""
    total = len(works)
    years: dict = {}
    grades: dict = {}
    tracks: dict = {}
    for w in works:
        y = str(w.get("year") or "未知")
        years[y] = years.get(y, 0) + 1
        g = str(w.get("award_grade") or w.get("award_level") or "未注明").strip() or "未注明"
        grades[g] = grades.get(g, 0) + 1
        t = str(w.get("track") or "").strip() or "未分赛道"
        tracks[t] = tracks.get(t, 0) + 1
    top = sorted(tracks.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return {
        "total": total,
        "years": dict(sorted(years.items())),
        "grades": dict(sorted(grades.items(), key=lambda kv: -kv[1])),
        "track_count": len(tracks),
        "top_tracks": top,
    }


def _pct(n: int, total: int) -> str:
    return f"{n * 100 / total:.1f}%" if total else "0%"


def _context_block(competition: dict, works: list, year_from: int, year_to: int) -> tuple:
    """公共上下文：赛事信息 + 全量统计 + 代表性样本 + 严格约束。

    返回 (context, sampled_ids)。sampled_ids 只有 24 个，是模型唯一可引用的 id 集合；
    历史上这里塞过全量 id（13631 个 → 39 万 token），是成本事故的根因。
    """
    sampled = _fair_sample(works, MAX_AWARDS)
    payload = [_slim_award(w) for w in sampled]
    sampled_ids = [int(r["id"]) for r in sampled if r.get("id") is not None]
    dist = _distribution(works)
    name = competition.get("name") or f"赛事 #{competition.get('id')}"
    category = competition.get("category") or "未分类"

    # 个人赛名单型赛事（蓝桥杯等）：没有作品名称/技术栈，只有科目与奖项，
    # 必须明确告诉模型，否则它会凭空编造「技术栈转型」。
    form = "个人赛获奖名单（按赛道/科目 + 奖项记录，无作品简介）" \
        if competition.get("category") and "软件" in str(competition.get("category")) \
        else "获奖记录（含赛道/科目与奖项）"

    year_lines = "；".join(f"{y} 年 {n} 条" for y, n in dist["years"].items())
    grade_lines = "；".join(
        f"{g} {n} 条（{_pct(n, dist['total'])}）" for g, n in dist["grades"].items()
    )
    track_lines = "；".join(f"{t} {n} 条（{_pct(n, dist['total'])}）"
                            for t, n in dist["top_tracks"])

    context = f"""你是一位资深赛事评审与备赛教练。下面是「{name}」（ID={competition.get('id')}，
类别：{category}）在 {year_from}-{year_to} 年的真实获奖数据。

【全量统计（这是判断趋势的唯一依据）】
- 记录总数：{dist['total']} 条；形态：{form}
- 年份：{year_lines}
- 奖项分布：{grade_lines}
- 赛道/科目分布（共 {dist['track_count']} 个，Top {len(dist['top_tracks'])}）：{track_lines}

【代表性样本（跨年份均匀抽样 {len(payload)} 条，仅用于举例）】
{json.dumps(payload, ensure_ascii=False)}

【硬性约束】
1. 只基于以上事实归纳，禁止混入其它赛事、其它类别或数据中不存在的作品与事实。
2. 该赛事包含 {dist['track_count']} 个赛道/科目，是多项比赛的集合。严禁把单个赛道的个别现象
   说成整个赛事的共性；某个小众方向出现一次，不等于「技术栈转为它」或「趋势上升」。
   任何趋势判断都必须引用上面的条数与占比作为依据（例如「X 组占 6.2%」）。
3. 提炼共性时优先看占比最高的部分：编程基本功、算法与数据结构能力、固定科目分组。
   占比很小的方向只能写「个别赛道出现」，不得作为整体结论。
4. 正文中禁止出现任何作品 id 或编号（如 [id:123]、id 12、16、24、纯数字列表）。
   需要交代数据来源时，最多写一句「基于 {dist['total']} 条获奖记录统计」，不要罗列数字。
5. 信息不足时写“数据中未提及”，不要编造。
6. 输出精简、可执行，不要长篇大论。

（如可用 id 仅为样本中的：{sampled_ids[:6]}，正文中不要写出来。）"""
    if len(context) > MAX_PROMPT_CHARS:
        raise ValueError(
            f"prompt 过大（{len(context)} 字符 > {MAX_PROMPT_CHARS}），已拦截以免烧 token"
        )
    return context, sampled_ids


def _insight_prompt(context: str, kind: str) -> str:
    return f"""{context}

【本次只生成 1 条：{KIND_LABEL[kind]}】
任务：{KIND_TASK[kind]}

输出要求：
- 只输出 Markdown 正文，不要 JSON、不要代码围栏、不要任何解释性开场白。
- 必须精简：正文控制在 {INSIGHT_MAX_CHARS} 字以内（2-3 个要点）。
- 禁止出现 id、编号、作品序号或任何数字列表；不要写「引用作品」「参考 id」。
- 每条结论后可用「（依据：某赛道 X 条 / 占比 Y%）」的形式给一行数据支撑。
"""


def _ideas_prompt(context: str, batch_n: int, valid_ids: list,
                  avoid_titles: list) -> str:
    avoid = ""
    if avoid_titles:
        avoid = "\n- 不要重复这些已生成的标题：" + "；".join(avoid_titles[:20])
    return f"""{context}

【本次生成 {batch_n} 条面向下一届比赛的选题 idea】
要求：
- 每条必须与已获奖方向有明确差异，不要照抄已有科目或题型。
- 方案要能在 3-6 个月内由 3-5 人小组完成。
- ref_awards 只能从样本 id 中挑 {MAX_IDEA_REFS} 个最有代表性的：{valid_ids[:MAX_IDEA_REFS]}{avoid}

【字段要求（务必精简）】
- title：选题名称，不超过 30 字
- pitch：一句话价值主张，不超过 50 字
- detail_md：Markdown 方案，只写「背景 / 做法 / 交付物」，不超过 300 字，禁止出现任何 id
- novelty：差异化说明，不超过 80 字
- risk：主要风险与规避，不超过 80 字
- ref_awards：作品 id 数组，最多 {MAX_IDEA_REFS} 个

只输出一个 JSON 对象，格式：
{{"ideas":[{{"title":"...","pitch":"...","detail_md":"...","novelty":"...","risk":"...","ref_awards":[1]}}]}}
不要输出代码围栏或任何解释，总字数控制在 1200 字以内。
"""


_SYSTEM_MD = (
    "你是严谨的赛事数据分析师。只依据给定作品事实归纳，不编造、不跨赛事混用。"
    "只输出 Markdown 正文，不要输出 JSON、代码围栏或解释性文字。"
)

_SYSTEM_JSON = (
    "你是严谨的赛事选题顾问。只依据给定作品事实构思，不编造、不跨赛事混用。"
    "只输出合法 JSON 对象，不要输出代码围栏或解释性文字。"
)


def _chat_markdown(prompt: str, max_tokens: int) -> str:
    return llm.chat(
        prompt, system=_SYSTEM_MD, temperature=0.3, max_tokens=max_tokens
    )


def _chat_ideas(context: str, batch_n: int, valid_ids: list,
                avoid_titles: list, max_tokens: int) -> list:
    """请求一批 ideas；解析失败时补一次更严格的 JSON 纠错请求。"""
    prompt = _ideas_prompt(context, batch_n, valid_ids, avoid_titles)
    last_raw = ""
    for attempt in range(2):
        if attempt == 1:
            prompt += (
                "\n\n【重要】上次输出无法解析为 JSON。请严格只输出一个合法 JSON 对象，"
                "不要任何前后缀、不要代码围栏，并确保数组完整闭合。"
            )
        try:
            last_raw = llm.chat(
                prompt, system=_SYSTEM_JSON, temperature=0.6,
                max_tokens=max_tokens,
            )
        except llm.LLMError as exc:
            print(f"[x] ideas 大模型调用失败：{exc}", file=sys.stderr)
            return []
        items = _parse_ideas(last_raw, set(valid_ids), batch_n)
        if items:
            return items
        print(
            f"[!] ideas 第 {attempt + 1} 次解析失败，返回片段："
            f"{str(last_raw)[:200]!r}",
            file=sys.stderr,
        )
    return []


# ---------------------------------------------------------------- 输出清洗
# 模型即使被要求“别写 id”，仍可能吐出 [id:1782]、id 12、16、24、（1782、2350、2918）。
# 这里在落库前统一清掉，保证缓存里存的就是干净文本；前端另有兜底。
_ID_CITE_RE = re.compile(
    r"[（(\[【]?\s*(?:作品|参考|样本)?\s*id\s*[:：]?\s*\d+"
    r"(?:\s*[、,，/]\s*\d+)*\s*[)）\]】]?",
    re.IGNORECASE,
)
_NUM_LIST_RE = re.compile(
    r"[（(\[【]\s*\d{2,6}(?:\s*[、,，/]\s*\d{2,6}){1,}\s*[)）\]】]"
)
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _sanitize_md(text: str) -> str:
    """剥离正文里的 id 引用与数字列表（UI 灾难的直接来源）。"""
    if not text:
        return ""
    cleaned = _ID_CITE_RE.sub("", text)
    cleaned = _NUM_LIST_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = _BLANK_LINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def _pick_evidence(sampled_ids: list, limit: int = 3) -> list:
    """证据只保留少量代表性样本 id（首/中/尾各一个），杜绝 1.3 万个 id 上屏。"""
    ids = [int(i) for i in sampled_ids if i is not None]
    if len(ids) <= limit:
        return ids
    step = len(ids) / limit
    return [ids[int(i * step)] for i in range(limit)]


def inspiration_fingerprint(competition_id: int) -> str:
    """灵感缓存指纹：获奖数据 + prompt 版本 + 模型。

    只依赖「条数/最大 id/最后更新时间/标题总长」这类聚合值，查询成本 <5ms；
    三者都没变时，任何用户再次打开页面都直接读缓存，不再调用大模型。
    """
    raw = (f"{PROMPT_VER}|{getattr(llm, 'MODEL', '')}|"
           f"{db.awards_fingerprint(int(competition_id))}")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _usage_delta(before: dict, after: dict) -> dict:
    keys = ("prompt_tokens", "completion_tokens", "total_tokens", "calls")
    return {k: int(after.get(k, 0)) - int(before.get(k, 0)) for k in keys}


def _log_prompt(label: str, text: str, max_tokens: int) -> None:
    """调用前打印真实 prompt 体量与 token 估算（≈1.6 字符/token，中文偏保守）。"""
    est_in = int(len(text) / 1.6)
    print(f"[prompt] {label}: {len(text)} 字符 ≈ {est_in} input tokens，"
          f"max_tokens={max_tokens}", file=sys.stderr)


# ---------------------------------------------------------------- 生成
def _generate_insights(competition: dict, works: list, context: str,
                       scope: str, competition_id: int, valid: set,
                       max_tokens: int) -> tuple:
    """逐 kind 调用模型，单条失败不影响其它两条。"""
    saved, failed = 0, 0
    evidence = _pick_evidence(list(valid))
    for kind in ("experience", "trend", "gap"):
        print(f"[insight] 正在生成：{KIND_LABEL[kind]}…")
        prompt = _insight_prompt(context, kind)
        _log_prompt(f"insight/{kind}", prompt, INSIGHT_MAX_TOKENS)
        used_before = llm.get_usage()
        try:
            raw = _chat_markdown(prompt, INSIGHT_MAX_TOKENS)
            content = _sanitize_md(_parse_insight_text(raw))
        except llm.LLMError as exc:
            print(f"[x] {kind} 调用失败：{exc}", file=sys.stderr)
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断
            print(f"[x] {kind} 生成异常：{type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
            continue

        if not content or len(content) < 10:
            print(f"[!] {kind} 返回内容为空或过短，跳过", file=sys.stderr)
            failed += 1
            continue

        used = _usage_delta(used_before, llm.get_usage())
        print(f"[usage] insight/{kind}: in={used.get('prompt_tokens')} "
              f"out={used.get('completion_tokens')}", file=sys.stderr)
        input_hash = hashlib.sha1(
            f"{PROMPT_VER}|{context}|{kind}".encode("utf-8")
        ).hexdigest()
        try:
            rid = db.save_insight(
                kind, scope, content,
                competition_id=competition_id,
                evidence=evidence,
                model=getattr(llm, "MODEL", None),
                input_hash=input_hash,
            )
        except Exception as exc:  # noqa: BLE001 - 写库失败也要继续其它 kind
            print(f"[x] {kind} 写库失败：{exc}", file=sys.stderr)
            failed += 1
            continue

        saved += 1
        print(f"[insight] {KIND_LABEL[kind]} -> id={rid}（{len(content)} 字）")
    return saved, failed


def _generate_ideas(competition: dict, context: str, competition_id: int,
                    valid: set, idea_n: int, max_tokens: int) -> tuple:
    """按每批 2 条生成，避免单次输出过长被截断。"""
    saved, skipped, failed = 0, 0, 0
    titles = []
    batch_n = IDEA_BATCH_SIZE

    for start in range(0, idea_n, batch_n):
        want = min(batch_n, idea_n - start)
        print(f"[idea] 正在生成第 {start + 1}-{start + want} 条…")
        used_before = llm.get_usage()
        try:
            items = _chat_ideas(
                context, want, sorted(valid), titles, IDEAS_MAX_TOKENS
            )
        except Exception as exc:  # noqa: BLE001 - 单批失败不阻断后续批次
            print(f"[x] 第 {start + 1}-{start + want} 条生成异常："
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
            continue
        if not items:
            failed += 1
            print(f"[!] 第 {start + 1}-{start + want} 条解析失败，继续下一批", file=sys.stderr)
            continue
        used = _usage_delta(used_before, llm.get_usage())
        print(f"[usage] ideas 第 {start + 1}-{start + want} 条: "
              f"in={used.get('prompt_tokens')} out={used.get('completion_tokens')}",
              file=sys.stderr)

        for item in items:
            title = _sanitize_md(item["title"])[:60] or item["title"][:60]
            try:
                new_id, action = db.save_idea(
                    title,
                    competition_id=competition_id,
                    pitch=_sanitize_md(_clip(item.get("pitch"), 120)),
                    detail_md=_sanitize_md(item.get("detail_md") or ""),
                    novelty=_sanitize_md(_clip(item.get("novelty"), 200)),
                    risk=_sanitize_md(_clip(item.get("risk"), 200)),
                    ref_awards=(item.get("ref_awards") or [])[:MAX_IDEA_REFS],
                    status="draft",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[x] idea 写库失败（{title}）：{exc}", file=sys.stderr)
                continue
            titles.append(title)
            if action == "inserted":
                saved += 1
                print(f"[idea] {title} -> id={new_id}")
            else:
                skipped += 1
                print(f"[idea] 已存在，跳过：{title}")
    return saved, skipped, failed


def generate(competition_id: int, *, only: str = "all", idea_n: int = DEFAULT_IDEA_N,
             force: bool = False, dry_run: bool = False,
             max_tokens: int = DEFAULT_MAX_TOKENS) -> int:
    """执行一次该赛事的灵感生成，返回进程退出码。"""
    competition = db.get_competition(competition_id)
    if not competition:
        print(f"[!] 竞赛 ID={competition_id} 不存在", file=sys.stderr)
        return 2

    works_all = db.list_awards_by_competition(competition_id)
    # 示例（mock）数据是早期演示种子，与真实获奖无关，绝不能进模型上下文。
    works = [w for w in works_all
             if str(w.get("source_type") or "").strip().lower() != "mock"]
    dropped = len(works_all) - len(works)
    if dropped:
        print(f"[i] 已剔除 {dropped} 条示例(mock)数据，不参与统计与抽样")
    if not works:
        print(
            f"[!] 竞赛「{competition.get('name')}」(ID={competition_id}) 暂无获奖作品，"
            "无法生成专属灵感。",
            file=sys.stderr,
        )
        return 2

    year_from, year_to = _year_range(works)
    scope = _scope(competition_id, year_from, year_to)
    print(f"[i] 赛事：{competition.get('name')}（ID={competition_id}）")
    print(f"[i] 该赛事获奖作品：{len(works)} 条，年份 {year_from}-{year_to}")
    print(f"[i] prompt 版本 {PROMPT_VER}；喂给模型的样本 ≤{MAX_AWARDS} 条 + 全量统计分布；"
          f"insights 3 次 + ideas 每 {IDEA_BATCH_SIZE} 条一批")

    existing_insights = db.list_insights_by_competition(competition_id)
    existing_ideas = db.list_ideas_by_competition(competition_id)
    if not force:
        if only == "insight" and len(existing_insights) >= 3:
            print("[i] 该赛事已有 3 条 insights；如需重新生成请加 --force")
            return 0
        if only == "idea" and len(existing_ideas) >= idea_n:
            print(f"[i] 该赛事已有 {len(existing_ideas)} 条 ideas；如需重新生成请加 --force")
            return 0
        if (only == "all" and len(existing_insights) >= 3
                and len(existing_ideas) >= idea_n):
            print("[i] 该赛事已有完整 insights + ideas；如需重新生成请加 --force")
            return 0

    context, valid_ids = _context_block(competition, works, year_from, year_to)

    if dry_run:
        print("[dry-run] 只预览，不调用大模型、不写库。")
        print(f"[dry-run] 作品样本：{json.dumps([_slim_award(w) for w in works[:2]], ensure_ascii=False, indent=2)}")
        for kind in ("experience", "trend", "gap"):
            print(f"\n[dry-run] {KIND_LABEL[kind]} prompt（前 500 字）：")
            print(_insight_prompt(context, kind)[:500] + "…")
        print("\n[dry-run] ideas prompt（前 800 字）：")
        print(_ideas_prompt(context, min(IDEA_BATCH_SIZE, idea_n), valid_ids, [])[:800] + "…")
        return 0

    if force:
        removed_i = db.delete_insights_by_competition(competition_id) \
            if only in ("all", "insight") else 0
        removed_d = db.delete_ideas_by_competition(competition_id) \
            if only in ("all", "idea") else 0
        print(f"[i] --force 清理：insights {removed_i} 条，ideas {removed_d} 条")

    saved_insights = saved_ideas = skipped_ideas = failed_insights = failed_batches = 0

    if only in ("all", "insight"):
        saved_insights, failed_insights = _generate_insights(
            competition, works, context, scope, competition_id, set(valid_ids),
            max_tokens
        )

    if only in ("all", "idea"):
        saved_ideas, skipped_ideas, failed_batches = _generate_ideas(
            competition, context, competition_id, set(valid_ids), idea_n, max_tokens
        )

    print("\n================ 汇总 ================")
    print(f"竞赛：{competition.get('name')}（ID={competition_id}）")
    if only in ("all", "insight"):
        print(f"insights 写入：{saved_insights} 条（失败 {failed_insights} 条）")
    if only in ("all", "idea"):
        print(f"ideas 写入：{saved_ideas} 条（已存在 {skipped_ideas} 条，失败批次 {failed_batches}）")
    usage = llm.get_usage()
    print(f"token 用量：in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')} "
          f"total={usage.get('total_tokens')}")
    if saved_insights or saved_ideas:
        # 记录本次生成时的输入指纹：获奖数据或 prompt 版本变化才会失效，
        # 之后任何用户再次请求都直接读缓存，不再扣费。
        try:
            db.save_ai_analysis(
                "inspiration", competition_id,
                inspiration_fingerprint(competition_id),
                f"insights={saved_insights};ideas={saved_ideas};"
                f"models={usage.get('calls')};ver={PROMPT_VER}",
                model=getattr(llm, "MODEL", None),
            )
        except Exception as exc:  # noqa: BLE001 - 缓存写失败不影响结果
            print(f"[!] 灵感缓存标记写入失败：{exc}", file=sys.stderr)
    print("\n下一步：刷新页面，或请求")
    print(f"  GET /api/ai/inspirations?competition_id={competition_id}")

    if only == "insight":
        return 0 if saved_insights > 0 else 3
    if only == "idea":
        return 0 if saved_ideas > 0 else 3
    return 0 if (saved_insights > 0 or saved_ideas > 0) else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按竞赛生成专属 insights + ideas（离线脚本）"
    )
    parser.add_argument(
        "--competition-id", "--competition_id",
        dest="competition_id",
        type=int,
        required=True,
        help="竞赛 ID（必填），只读取并写入该赛事的数据",
    )
    parser.add_argument(
        "--only",
        choices=["all", "insight", "idea"],
        default="all",
        help="只生成 insights / 只生成 ideas / 两者都生成（默认）",
    )
    parser.add_argument(
        "--idea-n",
        type=int,
        default=DEFAULT_IDEA_N,
        help=f"生成的 idea 条数（默认 {DEFAULT_IDEA_N}）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="删除该赛事已有 insights/ideas 后重新生成",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印统计与 prompt 预览，不调用大模型、不写库",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"单次请求最大输出 token（默认 {DEFAULT_MAX_TOKENS}）",
    )
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.idea_n <= 0:
        print("[!] --idea-n 必须大于 0", file=sys.stderr)
        return 2
    if args.max_tokens <= 0:
        print("[!] --max-tokens 必须大于 0", file=sys.stderr)
        return 2
    try:
        db.init_db()
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 数据库初始化失败：{exc}", file=sys.stderr)
        return 1
    try:
        return generate(
            args.competition_id,
            only=args.only,
            idea_n=args.idea_n,
            force=args.force,
            dry_run=args.dry_run,
            max_tokens=args.max_tokens,
        )
    except ValueError as exc:
        print(f"[!] 参数/数据校验失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 未预期异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
