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
# 「生成更多选题灵感」每次新增的条数（用户点击是无限叠加，不是固定 4 条死水）
MORE_IDEA_N = 3
# ---- 跨赛道抽样（2026-11-14 重构）----
# 旧逻辑按「奖项等级」分层（金奖 10 / 银奖 8 / 铜奖 6），结果 5-24 条样本很可能
# 全落在同一个赛道（例如「先进制造」），模型只看到局部就下全局结论，必然片面。
# 新逻辑按「赛道」配额：每个主要赛道 3-5 条，合计 20-30 条，覆盖全部主要赛道。
PER_TRACK_MIN = 3
PER_TRACK_MAX = 5
MAX_TRACKS = 8             # 最多覆盖 8 个主要赛道（11 个赛道的赛事取 Top 8，每个 3 条）
MAX_AWARDS = 26           # 跨赛道样本总上限（落在 20-30 区间，成本与数据量无关）
SAMPLE_TARGET_MIN = 20    # 赛道少时的补样下限（3 个赛道 × 5 条 = 15，需要补到 20）
# 赛道内部仍按奖项档位优先取样：金奖 > 银奖 > 铜奖 > 其它，
# 保证「金奖凭什么赢」有样本支撑，又不至于让某一档占满整个赛道配额。
GRADE_TIERS = (
    ("金奖", ("冠军", "亚军", "季军", "特等", "金奖", "一等奖")),
    ("银奖", ("银奖", "二等奖")),
    ("铜奖", ("铜奖", "三等奖", "优秀奖", "优胜奖", "创新潜力奖")),
)
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
# Prompt 版本号：改动 Prompt 后递增，使旧缓存指纹失效（v6：跨赛道抽样 + 双 Prompt 隔离）。
PROMPT_VER = "v6"

# 「灵感工作台 · 赛事洞察」的三条口径：商业趋势与选题规划。
# 注意：技术栈/架构/工程实现属于「获奖作品共性分析」（routers/ai.py 的
# _build_analyze_prompt），两者必须完全不重合，否则页面上两块内容看起来一样。
KIND_LABEL = {
    "experience": "红海赛道：竞争过热与同质化",
    "trend": "产业趋势与选题风向",
    "gap": "蓝海机会与组队参赛策略",
}

KIND_TASK = {
    "experience": (
        "盘点该赛事的「红海」方向：哪类选题/场景已经扎堆、同质化严重、"
        "评委容易审美疲劳；给出 3 点避开正面对撞的差异化策略。"
    ),
    "trend": (
        "从产业与政策视角指出 2-3 个正在变热的选题方向（谁在推动、为什么现在热），"
        "并说明这会把明年的评审重心推向哪里。"
    ),
    "gap": (
        "指出 2-3 个尚未被充分占据的「蓝海」空白机会，并给出可执行的组队配置与参赛策略："
        "需要什么角色、先做哪一步验证、如何把项目讲成评委听得懂的价值。"
    ),
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


def _id_key(row: dict) -> int:
    """样本排序键：id 升序；缺失 id 的行排到最后（不影响确定性）。"""
    try:
        return int(row.get("id"))
    except (TypeError, ValueError):
        return 1 << 62


def _track_key(row: dict) -> str:
    """赛道名归一化：空赛道统一记为「未分赛道」，避免 None 与 '' 各成一桶。"""
    return str(row.get("track") or "").strip() or "未分赛道"


def _grade_tier(row: dict) -> str:
    """把各种奖项叫法归到三层：金奖档 / 银奖档 / 铜奖档（认不出归「其它」）。"""
    grade = str(row.get("award_grade") or row.get("award_level") or "")
    for tier, keys in GRADE_TIERS:
        if any(key in grade for key in keys):
            return tier
    return "其它"


def _pick_spread(rows: list, n: int) -> list:
    """在给定池子里按 id 均匀跨取 n 条（确定性、可复现，不用随机数）。"""
    if n <= 0 or not rows:
        return []
    ordered = sorted(rows, key=lambda r: (r.get("id") or 0))
    if len(ordered) <= n:
        return list(ordered)
    step = len(ordered) / n
    return [ordered[int(i * step)] for i in range(n)]


def _pick_within_track(rows: list, n: int) -> list:
    """同一条赛道内取 n 条：先按奖项档位（金 → 银 → 铜 → 其它）分名额，再各档均匀跨取。

    只在赛道内部做奖项分层：金/银/铜各给一部分名额，保证既有「金奖凭什么赢」
    的样本，也看得到中低档作品的普遍做法；不会让某一档吃掉整条赛道配额。
    """
    if n <= 0 or not rows:
        return []
    if len(rows) <= n:
        return list(rows)

    pools: dict[str, list] = {tier: [] for tier, _keys in GRADE_TIERS}
    pools["其它"] = []
    for row in rows:
        pools[_grade_tier(row)].append(row)

    picked: list = []
    used: set = set()
    per_tier = max(1, n // len(GRADE_TIERS))
    for tier, _keys in GRADE_TIERS:
        if len(picked) >= n:
            break
        got = _pick_spread(pools[tier], min(per_tier, n - len(picked)))
        picked.extend(got)
        used.update(id(r) for r in got)

    # 某档不够（小赛事没有铜奖等）：缺额按 金 → 银 → 铜 → 其它 补齐
    if len(picked) < n:
        for tier, _keys in list(GRADE_TIERS) + [("其它", ())]:
            if len(picked) >= n:
                break
            rest = [r for r in pools[tier] if id(r) not in used]
            for row in _pick_spread(rest, n - len(picked)):
                if len(picked) >= n:
                    break
                picked.append(row)
                used.add(id(row))
    return picked[:n]


def sample_cross_track(works: list, *, per_track: int = PER_TRACK_MAX,
                       min_per_track: int = PER_TRACK_MIN,
                       max_tracks: int = MAX_TRACKS,
                       max_total: int = MAX_AWARDS) -> tuple:
    """跨赛道抽样：覆盖尽量多的主要赛道，每个赛道 3-5 条，合计 20-30 条。

    返回 ``(sampled_rows, track_meta)``，其中
    ``track_meta = [{"track": 赛道名, "total": 该赛道条数, "sampled": 本次抽了几条}]``。

    为什么按赛道抽：旧实现按「奖项等级」分层，样本极易全部落在同一个赛道
    （例如「先进制造」），模型只看到局部就总结全局，结论必然片面。
    单赛道赛事（或个人赛没有赛道字段）退回「赛道内按奖项分层」，行为与旧版一致。
    """
    # 先按 id 升序规范化输入：数据库返回顺序（年份倒序 / 分组）不能影响抽样结果，
    # 否则同一条赛事每次请求的样本顺序都不同 → prompt 指纹跳动 → 缓存永远不命中，
    # 「终身只生成一次」就退化成「每次刷新都重烧一次」。
    works = sorted(works, key=lambda r: _id_key(r))
    groups: dict[str, list] = {}
    for row in works:
        groups.setdefault(_track_key(row), []).append(row)

    # 单赛道 / 无赛道：退化为赛道内奖项分层，总条数仍受 max_total 约束
    if len(groups) <= 1:
        only = next(iter(groups.values()), [])
        sampled = _pick_within_track(only, min(max_total, len(only)))
        meta = []
        if only:
            meta.append({"track": _track_key(only[0]), "total": len(only),
                         "sampled": len(sampled)})
        return sampled, meta

    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:max_tracks]
    # 每个赛道的基础名额：总预算 / 赛道数，夹在 [min_per_track, per_track]
    quota = max(min_per_track, min(per_track, max_total // max(len(ranked), 1)))

    sampled: list = []
    meta: list = []
    used: set = set()
    for track, rows in ranked:
        picked = _pick_within_track(rows, min(quota, len(rows)))
        sampled.extend(picked)
        used.update(id(r) for r in picked)
        meta.append({"track": track, "total": len(rows), "sampled": len(picked)})

    # 赛道少时（3 个赛道 × 5 条 = 15）够不到 20 条下限：轮流补样直到达标或数据用尽
    target = min(SAMPLE_TARGET_MIN, len(works))
    if len(sampled) < target:
        for track, rows in ranked:
            if len(sampled) >= target:
                break
            rest = [r for r in rows if id(r) not in used]
            need = target - len(sampled)
            for row in _pick_spread(rest, min(need, 8)):
                if len(sampled) >= target:
                    break
                sampled.append(row)
                used.add(id(row))
                for item in meta:
                    if item["track"] == track:
                        item["sampled"] += 1
    return sampled[:max_total], meta


# ---------------------------------------------------------------------------
# 「AI 深度赛事研判报告」抽样：年份均衡 + 奖项顺延（2026-11-14 重构）
# ---------------------------------------------------------------------------
# 规则（与产品定稿一致）：
#   1. 年份均衡：按年份分组平均分配名额（两届 -> 每年 50 条）；
#   2. 奖项顺延：每年名额内先取金奖/一等奖，不足再用银奖/二等奖补齐；
#   3. 异常兜底：全库金银奖合计不足 100 条 -> 全量使用，不再下探铜奖；
#   4. 上限 100 条，样本按 id 升序，保证 prompt 指纹稳定（缓存能命中）。
REPORT_SAMPLE_CAP = 100
_GOLD_TIER = "金奖"
_SILVER_TIER = "银奖"
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _year_key(row: dict) -> Optional[int]:
    """取年份整数；取不到返回 None（这类行排最后，只在名额有剩时补）。"""
    raw = row.get("year")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    m = _YEAR_RE.search(str(raw))
    if m:
        return int(m.group(0))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def sample_year_balanced(
    works: list, *, cap: int = REPORT_SAMPLE_CAP
) -> tuple:
    """年份均衡 + 奖项顺延抽样。

    返回 ``(sampled_rows, meta)``：
      * ``sampled_rows``：按 id 升序的样本（≤ cap 条）；
      * ``meta``：``{"cap", "total_awards", "gold_total", "silver_total",
        "sampled", "fallback_all", "years": [{year, quota, gold_avail,
        silver_avail, gold_picked, silver_picked, picked}]}``，
        供前端/日志核对抽样是否真的「按年均衡、金银优先」。
    """
    rows = sorted(works, key=_id_key)
    cap = max(1, int(cap))

    gold_all = [r for r in rows if _grade_tier(r) == _GOLD_TIER]
    silver_all = [r for r in rows if _grade_tier(r) == _SILVER_TIER]
    high_pool = gold_all + silver_all          # 只在金银档里抽（规则 3：不下探铜奖）

    meta = {
        "cap": cap,
        "total_awards": len(rows),
        "gold_total": len(gold_all),
        "silver_total": len(silver_all),
        "sampled": 0,
        "fallback_all": False,
        "years": [],
    }

    # 规则 3：金银奖全库都不够 cap -> 全量使用
    if len(high_pool) <= cap:
        meta["fallback_all"] = True
        meta["sampled"] = len(high_pool)
        meta["years"] = _year_stats(high_pool, high_pool)
        return high_pool, meta

    # 按年份分桶（None 年份单独一桶，排在最后）
    buckets: dict = {}
    for row in high_pool:
        buckets.setdefault(_year_key(row), []).append(row)
    years = sorted([y for y in buckets if y is not None], reverse=True)
    if None in buckets:
        years.append(None)

    picked: list = []
    year_meta = []
    n_years = len(years)
    base, extra = divmod(cap, max(n_years, 1))
    for idx, year in enumerate(years):
        quota = base + (1 if idx < extra else 0)
        bucket = buckets.get(year) or []
        gold_rows = [r for r in bucket if _grade_tier(r) == _GOLD_TIER]
        silver_rows = [r for r in bucket if _grade_tier(r) == _SILVER_TIER]

        # 规则 2：先金奖，不足用银奖补
        got_gold = _pick_spread(gold_rows, min(quota, len(gold_rows)))
        picked_gold_ids = {id(r) for r in got_gold}
        need = quota - len(got_gold)
        got_silver = _pick_spread(
            [r for r in silver_rows if id(r) not in picked_gold_ids],
            min(need, len(silver_rows)),
        )
        got = got_gold + got_silver
        picked.extend(got)
        year_meta.append(
            {
                "year": year,
                "quota": quota,
                "gold_avail": len(gold_rows),
                "silver_avail": len(silver_rows),
                "gold_picked": len(got_gold),
                "silver_picked": len(got_silver),
                "picked": len(got),
            }
        )

    # 某些年份金银奖不足额（小赛事）：剩余名额从其它年份的剩余金银奖里补齐
    used = {id(r) for r in picked}
    if len(picked) < cap:
        rest_gold = [r for r in gold_all if id(r) not in used]
        rest_silver = [r for r in silver_all if id(r) not in used]
        need = cap - len(picked)
        fill = _pick_spread(rest_gold, min(need, len(rest_gold)))
        need -= len(fill)
        if need > 0:
            fill += _pick_spread(
                [r for r in rest_silver if id(r) not in fill], need
            )
        picked.extend(fill)
        # 把补来的样本记到各自年份的 picked 上，保持 meta 与样本一致
        for row in fill:
            y = _year_key(row)
            for item in year_meta:
                if item["year"] == y:
                    item["picked"] += 1
                    if _grade_tier(row) == _GOLD_TIER:
                        item["gold_picked"] += 1
                    else:
                        item["silver_picked"] += 1
                    break
            else:  # 该年份原本不在 meta（理论上不会发生）
                year_meta.append(
                    {
                        "year": y,
                        "quota": 0,
                        "gold_avail": 0,
                        "silver_avail": 0,
                        "gold_picked": 1 if _grade_tier(row) == _GOLD_TIER else 0,
                        "silver_picked": 1 if _grade_tier(row) == _SILVER_TIER else 0,
                        "picked": 1,
                    }
                )

    picked = sorted(picked, key=_id_key)[:cap]
    meta["sampled"] = len(picked)
    meta["years"] = sorted(
        (m for m in year_meta if m["picked"] > 0),
        key=lambda m: (m["year"] is None, -(m["year"] or 0)),
    )
    return picked, meta


def _year_stats(selected: list, high_pool: list) -> list:
    """全量兜底时也给出分年统计（quota 记为实际条数）。"""
    rows = selected
    gold = [r for r in high_pool if _grade_tier(r) == _GOLD_TIER]
    silver = [r for r in high_pool if _grade_tier(r) == _SILVER_TIER]
    out = []
    for year in sorted({_year_key(r) for r in rows}, key=lambda y: (y is None, -(y or 0))):
        ys = [r for r in rows if _year_key(r) == year]
        out.append(
            {
                "year": year,
                "quota": len(ys),
                "gold_avail": len([r for r in gold if _year_key(r) == year]),
                "silver_avail": len([r for r in silver if _year_key(r) == year]),
                "gold_picked": len([r for r in ys if _grade_tier(r) == _GOLD_TIER]),
                "silver_picked": len([r for r in ys if _grade_tier(r) == _SILVER_TIER]),
                "picked": len(ys),
            }
        )
    return out


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
def _context_block(competition: dict, works: list, year_from: int, year_to: int) -> tuple:
    """公共上下文：赛事信息 + 跨赛道样本 + 硬性约束。

    返回 (context, sampled_ids)。sampled_ids 只有 20-30 个，是模型唯一可引用的 id 集合；
    历史上这里塞过全量 id（13631 个 → 39 万 token），是成本事故的根因。

    2026-11-14 重构：
      * 抽样改为「按赛道配额」（每个主要赛道 3-5 条，合计 20-30 条），
        修掉「只抽到一个赛道就总结全局」的片面结论；
      * 彻底删除全量 SQL 统计（条数/占比/年度分布）。百分比既不参与归纳，
        又会被模型直接抄进正文形成误导，还要多烧 300-500 token。
    """
    sampled, track_meta = sample_cross_track(works)
    payload = [_slim_award(w) for w in sampled]
    sampled_ids = [int(r["id"]) for r in sampled if r.get("id") is not None]
    name = competition.get("name") or f"赛事 #{competition.get('id')}"
    category = competition.get("category") or "未分类"

    # 个人赛名单型赛事（蓝桥杯等）：没有作品名称/技术栈，只有科目与奖项，
    # 必须明确告诉模型，否则它会凭空编造「技术栈转型」。
    form = "个人赛获奖名单（按赛道/科目 + 奖项记录，无作品简介）" \
        if competition.get("category") and "软件" in str(competition.get("category")) \
        else "获奖记录（含赛道/科目与奖项）"

    track_lines = "；".join(
        f"{m['track']} {m['sampled']} 条" for m in track_meta
    ) or "未分赛道"

    context = f"""你是一位资深赛事评审与备赛教练。下面是「{name}」（ID={competition.get('id')}，
类别：{category}）在 {year_from}-{year_to} 年获奖作品中抽取的跨赛道样本。

【样本说明】
- 形态：{form}
- 覆盖 {len(track_meta)} 个赛道、共 {len(payload)} 条样本：{track_lines}
- 这是抽样（每个赛道 3-5 条，含各奖项档位代表），不是全量名单

【获奖作品样本】
{json.dumps(payload, ensure_ascii=False)}

【硬性约束】
1. 只基于以上样本归纳，禁止混入其它赛事、其它类别或样本中不存在的作品与事实。
2. 必须跨赛道分析：先给 2-3 条「所有赛道共有」的整体规律，再逐赛道写 1 行差异结论。
   严禁只看一个赛道就下全局结论，也不要把某个赛道的个别做法说成整个赛事的共性。
3. 禁止输出任何百分比、占比、条数排名或「Top N」式统计（也不要写「某类占多数」这种推断）；
   不要罗列样本 id / 编号；需要交代来源时最多写一句「基于跨赛道抽样归纳」。
4. 信息不足时写「数据中未提及」，不要编造。
5. 输出精简、可执行，不要长篇大论。

（可用样本 id 仅为：{sampled_ids[:6]}，正文中不要写出来。）"""
    if len(context) > MAX_PROMPT_CHARS:
        raise ValueError(
            f"prompt 过大（{len(context)} 字符 > {MAX_PROMPT_CHARS}），已拦截以免烧 token"
        )
    return context, sampled_ids


def _insight_prompt(context: str, kind: str) -> str:
    return f"""{context}

【本次只生成 1 条：{KIND_LABEL[kind]}】
任务：{KIND_TASK[kind]}

口径（必须遵守，与「获奖作品共性分析」严格区分）：
- 本条属于「灵感工作台 · 赛事洞察」，只谈商业趋势与选题规划：
  市场机会、竞争密度、红海/蓝海、组队与参赛策略。
- 禁止写技术栈清单、系统架构、模块划分、工程实现、代码方案——这些属于
  「获奖作品共性分析」（另一块功能），两边内容重复会被用户判为无用。

输出要求：
- 只输出 Markdown 正文，不要 JSON、不要代码围栏、不要任何解释性开场白。
- 必须精简：正文控制在 {INSIGHT_MAX_CHARS} 字以内（最多 3 个要点，每点一句话）。
- 禁止出现 id、编号、条数、百分比、占比等统计口径；不要写「引用作品」「参考 id」。
"""


def _ideas_prompt(context: str, batch_n: int, valid_ids: list,
                  avoid_titles: list, *, round_no: int = 1) -> str:
    """构思新的选题 idea。

    avoid_titles = 用户已经看过的全部选题（历史生成 + 前端当前列表）。
    「换一批」的核心就在这里：把已看过的完整清单摆给模型，明确要求换视角，
    而不是靠它自己猜哪些算重复。
    """
    avoid = ""
    if avoid_titles:
        listed = "\n".join(
            f"  {i}. {str(t)[:60]}" for i, t in enumerate(avoid_titles[:40], 1)
        )
        avoid = f"""

【以下是用户已看过的 {len(avoid_titles)} 个主题——必须全部避开】
{listed}

请避开这些主题，从全新角度再生成 {batch_n} 条差异化灵感：
- 不许与上面任何一条重复、近义，或只是「换个行业名词 / 换个表述」；
- 不许把同一个方案拆成两个赛道各写一遍；
- 请从「尚未覆盖的行业场景」「未组合过的技术栈」「没被服务的人群」三个方向入手。"""
    return f"""{context}

【本次生成 {batch_n} 条面向下一届比赛的选题 idea（第 {round_no} 轮）】
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
    "你是赛事选题与商业趋势顾问，只从市场机会、竞争格局、组队与参赛策略的角度给建议。"
    "你只依据给定样本归纳，不编造、不跨赛事混用；不讨论技术栈清单或系统架构。"
    "只输出 Markdown 正文，不要输出 JSON、代码围栏或解释性文字。"
)

_SYSTEM_JSON = (
    "你是严谨的赛事选题顾问，擅长从市场空白与人群需求找差异化选题。"
    "只依据给定作品事实构思，不编造、不跨赛事混用；严格遵守「必须避开」清单。"
    "只输出合法 JSON 对象，不要输出代码围栏或解释性文字。"
)


def _chat_markdown(prompt: str, max_tokens: int) -> str:
    return llm.chat(
        prompt, system=_SYSTEM_MD, temperature=0.3, max_tokens=max_tokens
    )


def _chat_ideas(context: str, batch_n: int, valid_ids: list,
                avoid_titles: list, max_tokens: int,
                *, round_no: int = 1) -> list:
    """请求一批 ideas；解析失败时补一次更严格的 JSON 纠错请求。"""
    prompt = _ideas_prompt(context, batch_n, valid_ids, avoid_titles,
                           round_no=round_no)
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
    """逐 kind 调用模型，单条失败不影响其它两条。

    终身一次：库内已有该 kind 的记录就跳过，绝不重复烧钱。
    洞察是「赛事级结论」，一旦生成即长期有效，重生成只会让成本翻倍。
    """
    saved, failed = 0, 0
    try:
        existing_kinds = {
            str(r.get("kind") or "")
            for r in db.list_insights_by_competition(competition_id)
        }
    except Exception as exc:  # noqa: BLE001 - 读库失败退化为全部生成
        print(f"[!] 读取已有 insights 失败，按无记录处理：{exc}", file=sys.stderr)
        existing_kinds = set()

    evidence = _pick_evidence(list(valid))
    for kind in ("experience", "trend", "gap"):
        if kind in existing_kinds:
            print(f"[i] {KIND_LABEL[kind]} 已存在，终身只生成一次，跳过")
            continue
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


def _save_idea_item(competition_id: int, item: dict) -> tuple:
    """落库一条 idea，返回 (id, action, title)；失败时 action="failed"。"""
    title = _sanitize_md(item.get("title") or "")[:60] or str(item.get("title") or "")[:60]
    if not title:
        return None, "failed", ""
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
        return None, "failed", title
    return new_id, action, title


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
                context, want, sorted(valid), titles, IDEAS_MAX_TOKENS,
                round_no=start + 1,
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
            new_id, action, title = _save_idea_item(competition_id, item)
            if action == "failed":
                continue
            titles.append(title)
            if action == "inserted":
                saved += 1
                print(f"[idea] {title} -> id={new_id}")
            else:
                skipped += 1
                print(f"[idea] 已存在，跳过：{title}")
    return saved, skipped, failed


def _idea_row(row: dict) -> dict:
    """DB 行 -> 前端直接可用的 idea 结构（字段名与 /api/ai/inspirations 保持一致）。"""
    refs = row.get("ref_awards")
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except (ValueError, TypeError):
            refs = []
    return {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "pitch": row.get("pitch") or "",
        "detail_md": row.get("detail_md") or "",
        "novelty": row.get("novelty") or "",
        "risk": row.get("risk") or "",
        "ref_awards": refs or [],
    }


def generate_more_ideas(competition_id: int, *, n: int = MORE_IDEA_N,
                        avoid_titles: Optional[list] = None,
                        avoid_ids: Optional[list] = None) -> dict:
    """「生成更多选题灵感」：追加生成 n 条新 idea，不覆盖历史。

    * n 默认 3（用户点一次追加 3 条）；
    * avoid_titles = 用户已经看过的选题（库内已有 + 前端当前列表），
      完整清单会写进 prompt 的「必须避开」清单，要求模型换全新视角；
    * 结果写入 ideas 表并追加返回：用户点得越多，灵感库越厚；
    * 返回 {"saved", "skipped", "total", "avoided", "ideas": [新生成的 idea]}。
    """
    cid = int(competition_id)
    competition = db.get_competition(cid)
    if not competition:
        raise LookupError(f"竞赛 ID={cid} 不存在")
    if not competition.get("has_project_awards", 1):
        raise ValueError("algorithm_only")
    works = [w for w in db.list_awards_by_competition(cid)
             if str(w.get("source_type") or "").strip().lower() != "mock"]
    if not works:
        raise ValueError("no_awards")

    year_from, year_to = _year_range(works)
    context, valid_ids = _context_block(competition, works, year_from, year_to)

    existing = db.list_ideas_by_competition(cid)
    seen: list = []
    for row in existing:
        title = str(row.get("title") or "").strip()
        if title and title not in seen:
            seen.append(title)
    for title in (avoid_titles or []):
        title = str(title or "").strip()
        if title and title not in seen:
            seen.append(title)

    saved_ids: list = []
    skipped = 0
    batch_n = IDEA_BATCH_SIZE
    for start in range(0, max(int(n), 1), batch_n):
        want = min(batch_n, int(n) - start)
        print(f"[idea+] 第 {start + 1}-{start + want} 条：避开 {len(seen)} 条已看过的选题…")
        items = _chat_ideas(context, want, sorted(valid_ids), seen,
                            IDEAS_MAX_TOKENS, round_no=len(seen) + 1)
        if not items:
            print(f"[!] 第 {start + 1}-{start + want} 条生成失败，跳过本批", file=sys.stderr)
            continue
        for item in items:
            new_id, action, title = _save_idea_item(cid, item)
            if action == "failed":
                continue
            if action == "inserted":
                saved_ids.append(int(new_id))
                seen.append(title)
            else:
                skipped += 1
                print(f"[idea+] 与已有选题重复，跳过：{title}")

    rows = {int(r["id"]): r for r in db.list_ideas_by_competition(cid)
            if r.get("id") is not None}
    new_ideas = [_idea_row(rows[i]) for i in saved_ids if i in rows]
    return {
        "saved": len(new_ideas),
        "skipped": skipped,
        "total": len(rows),
        "avoided": len(seen) - len(existing),
        "ideas": new_ideas,
    }


def generate(competition_id: int, *, only: str = "all", idea_n: int = DEFAULT_IDEA_N,
             force: bool = False, dry_run: bool = False,
             reset_insights: bool = False,
             max_tokens: int = DEFAULT_MAX_TOKENS) -> int:
    """执行一次该赛事的灵感生成，返回进程退出码。

    insights 是「终身一次」的赛事级结论：库内已有记录就不再生成，
    --force 只覆盖 ideas（选题可以无限追加），只有显式 --reset-insights
    才会删掉旧洞察重写（运维修复用，正常链路永远不会走到）。
    """
    competition = db.get_competition(competition_id)
    if not competition:
        print(f"[!] 竞赛 ID={competition_id} 不存在", file=sys.stderr)
        return 2

    # 纯算法/知识类赛事（蓝桥杯、ACM-ICPC…）：历年公示只有获奖名单，
    # 没有任何项目作品，基于名单提炼「选题灵感/技术趋势」既没价值又烧钱。
    # 这里兜底拦住，防止绕过 API 直接跑脚本时白花一次调用。
    if not competition.get("has_project_awards", 1):
        print(
            f"[!] 竞赛「{competition.get('name')}」(ID={competition_id}) 是纯算法/"
            "知识类赛事，获奖数据只有名单、没有项目作品，不提供灵感分析。",
            file=sys.stderr,
        )
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
    print(f"[i] prompt 版本 {PROMPT_VER}；喂给模型的跨赛道样本 ≤{MAX_AWARDS} 条"
          f"（每赛道 {PER_TRACK_MIN}-{PER_TRACK_MAX} 条，已剔除全量统计）；"
          f"insights 3 次 + ideas 每 {IDEA_BATCH_SIZE} 条一批")

    existing_insights = db.list_insights_by_competition(competition_id)
    existing_ideas = db.list_ideas_by_competition(competition_id)

    # 洞察终身一次：只要已有 3 条，任何参数组合都不再触碰大模型
    insights_done = len({str(r.get("kind") or "") for r in existing_insights}) >= 3
    if insights_done and only in ("all", "insight"):
        print("[i] 该赛事已有 3 条 insights，终身只生成一次，跳过洞察生成")
        if only == "insight":
            return 0

    if not force:
        if only == "idea" and len(existing_ideas) >= idea_n:
            print(f"[i] 该赛事已有 {len(existing_ideas)} 条 ideas；如需重新生成请加 --force")
            return 0
        if only == "all" and insights_done and len(existing_ideas) >= idea_n:
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

    if reset_insights:
        removed_i = db.delete_insights_by_competition(competition_id)
        print(f"[i] --reset-insights 已清空 insights {removed_i} 条，将重新生成")

    if force:
        removed_d = db.delete_ideas_by_competition(competition_id) \
            if only in ("all", "idea") else 0
        print(f"[i] --force 清理 ideas {removed_d} 条（insights 终身一次，不受 --force 影响）")

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
        help="删除该赛事已有 ideas 后重新生成（insights 终生一次，不受影响）",
    )
    parser.add_argument(
        "--reset-insights",
        dest="reset_insights",
        action="store_true",
        help="运维专用：强制清空并重写 insights（正常链路不会用到）",
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
            reset_insights=args.reset_insights,
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
