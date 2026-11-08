# -*- coding: utf-8 -*-
"""AI 内容生成核心（攻略 + 灵感）。

本模块只做「读数据 -> 调大模型 -> 落库」，**不负责**进程管理、HTTP、锁和
任务状态；那些由下面三处调用方各自处理：

  * ``routers/competitions.py`` + ``ai_jobs_runner.py``：按需生成（登录 + 异步）
  * ``daily_ai_worker.py``：每天定时慢刷
  * ``generate_guides.py``：保留的兼容 CLI（内部改为调用本模块）

为什么抽出来：
    原 ``generate_guides.py`` 把 Prompt、重试、写库、CLI 混在一起，既不能指定
    数据库，也不能被接口复用。抽成纯函数后，三处调用共享同一套 Prompt 与
    输入指纹，换 Prompt 只需改一个地方。

约定：
    * 数据库路径沿用仓库既有约定 —— 调用方先 ``db.DB_PATH = path``；
      本模块内部只用 db 的默认库，不再自己拼连接。
    * 本模块函数要么返回结构化 dict，要么抛异常，**不 print**（日志归调用方）。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
from typing import Optional

import db
import llm

# Prompt 版本：改动 Prompt 结构时 +1，便于批量识别旧攻略（guide_prompt_ver）
GUIDE_PROMPT_VER = "v2"

# 攻略单次生成的 max_tokens：
# deepseek-flash 的 reasoning 也吃 max_tokens，给太小会出现「正文为空 ->
# llm 层自动翻倍重试」，等于同一篇攻略付费两次。6144 是实测的安全值。
GUIDE_MAX_TOKENS = 6144

# 输入瘦身：获奖作品最多 12 条，单字段截断 120 字
GUIDE_MAX_AWARDS = 12
AWARD_FIELD_CLIP = 120
COMP_FIELD_CLIP = 240

# 低于这个长度视为生成失败（防止截断/占位文案入库）
GUIDE_MIN_CHARS = 200

_FENCE_RE = re.compile(r"```(?:markdown|md)?\s*(.*?)```", re.DOTALL)

GUIDE_SYSTEM_PROMPT = (
    "你是资深的学科竞赛指导老师，为大学生竞赛平台撰写「参赛全过程攻略」。\n"
    "硬性规则：\n"
    "1. 只能使用用户消息【赛事事实】里给出的信息，没有给出的内容一律不得编造。\n"
    "2. 时间只能引用【赛事事实】中的「报名月份 / 比赛月份」；若事实里写「未公布」，"
    "必须明确写「官方尚未公布，以官网通知为准」，禁止推测具体日期、截止时间、"
    "奖项名额、评分权重等细节。\n"
    "3. 获奖作品只用于归纳「什么样的作品更容易获奖」，不得当作官方赛制引用。\n"
    "4. 只输出 Markdown 正文，不要用代码块包裹整篇内容，不要开场白和结尾寒暄。\n"
    "5. 篇幅 800-1500 字，使用二级标题分节，语气务实、可执行。"
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _clip(value, limit: int) -> str:
    """压成单行并截断，控制 prompt token。"""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _month_range(start, end, cross_year) -> str:
    """把月份整数拼成「3 月 - 7 月」「11 月 - 次年 3 月」「未公布」。"""
    def _ok(v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if 1 <= n <= 12 else None

    s, e = _ok(start), _ok(end)
    if s and e:
        if cross_year and e < s:
            return f"{s} 月 - 次年 {e} 月"
        if s == e:
            return f"{s} 月"
        return f"{s} 月 - {e} 月"
    if s:
        return f"{s} 月起"
    if e:
        return f"截止 {e} 月"
    return "未公布"


def _slim_award(row: dict) -> dict:
    """只保留对「获奖画像」有价值的字段。"""
    tech = row.get("tech_stack")
    if tech is None:
        tech = row.get("tech")
    if isinstance(tech, (list, tuple)):
        tech = "、".join(str(t) for t in tech if str(t).strip())
    return {
        "year": row.get("year"),
        "title": _clip(row.get("title"), AWARD_FIELD_CLIP),
        "award": _clip(
            row.get("award_grade") or row.get("award_level") or row.get("award"), 40
        ),
        "tech": _clip(tech, AWARD_FIELD_CLIP),
    }


def _strip_fence(text: str) -> str:
    """模型偶尔仍会用 ```markdown 包整篇，剥掉。"""
    if not text:
        return ""
    match = _FENCE_RE.search(text)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return text.strip()


def _usage_delta(before: dict, after: dict) -> int:
    return max(int(after.get("total_tokens", 0)) - int(before.get("total_tokens", 0)), 0)


# ---------------------------------------------------------------------------
# Prompt 构造
# ---------------------------------------------------------------------------
def build_guide_prompt(comp: dict, awards: Optional[list] = None,
                       year: Optional[int] = None) -> str:
    """把赛事事实（含月份与获奖作品）压成一份防编造的 Prompt。"""
    import datetime as _dt

    year = year or _dt.datetime.now().year
    name = (comp.get("name") or "未知比赛").strip()
    # 兼容两种入参：db 原始行（award_grade/tech_stack）与已瘦身的 dict
    awards = [_slim_award(a) for a in (awards or [])]

    lines = [
        "【赛事事实】",
        f"比赛名称：{name}",
        f"当前年份：{year}",
        f"类别：{(comp.get('category') or '未分类').strip()}",
        f"级别：{(comp.get('level_label') or comp.get('level') or '未标注').strip()}",
        "官网：" + ((comp.get("official_url") or "").strip() or "未提供"),
        "报名月份：" + _month_range(
            comp.get("reg_start_month"), comp.get("reg_end_month"),
            comp.get("reg_cross_year"),
        ),
        "比赛月份：" + _month_range(
            comp.get("contest_start_month"), comp.get("contest_end_month"), False
        ),
    ]

    intro = _clip(comp.get("intro"), COMP_FIELD_CLIP)
    summary = _clip(comp.get("summary"), COMP_FIELD_CLIP)
    if intro:
        lines.append(f"已知简介：{intro}")
    if summary and summary != intro:
        lines.append(f"平台摘要：{summary}")

    if awards:
        lines.append("")
        lines.append("近年获奖作品（最多 12 条，只用于归纳获奖画像，不是官方赛制）：")
        for item in awards[:GUIDE_MAX_AWARDS]:
            parts = [str(item.get("year") or "年份未知")]
            if item.get("title"):
                parts.append(item["title"])
            if item.get("award"):
                parts.append(item["award"])
            if item.get("tech"):
                parts.append("技术栈：" + item["tech"])
            lines.append("- " + "｜".join(parts))
    else:
        lines.append("")
        lines.append("近年获奖作品：暂无平台数据（不要编造往届获奖情况）。")

    lines.extend([
        "",
        f"请为【{name}】写一份参赛全过程攻略，严格按下面六节输出：",
        "## 一、赛前准备（需要准备的材料、技能、工具）",
        "## 二、报名与材料（结合上面给出的报名月份；未公布就写以官网为准）",
        "## 三、比赛形式（只写事实支持的形式；不清楚就提示核对官网）",
        "## 四、展示与答辩（形式未公布时给出通用准备清单，并注明待官方确认）",
        "## 五、常见坑（可执行、不要空话）",
        "## 六、官方信息核对清单（告诉读者去官网核对哪几项）",
        "",
        "再次强调：不要出现事实里没有的具体日期、奖项名额、评分比例。",
    ])
    return "\n".join(lines)


def guide_input_hash(comp: dict, awards: Optional[list] = None) -> str:
    """攻略输入指纹：官网/月份/简介/获奖集合变化时指纹变化，可用于失效判定。"""
    awards = awards or []
    payload = {
        "v": GUIDE_PROMPT_VER,
        "name": _clip(comp.get("name"), COMP_FIELD_CLIP),
        "category": _clip(comp.get("category"), 80),
        "level": comp.get("level"),
        "level_label": comp.get("level_label"),
        "official_url": _clip(comp.get("official_url"), 300),
        "reg": [comp.get("reg_start_month"), comp.get("reg_end_month"),
                bool(comp.get("reg_cross_year"))],
        "contest": [comp.get("contest_start_month"), comp.get("contest_end_month")],
        "intro": _clip(comp.get("intro"), COMP_FIELD_CLIP),
        "summary": _clip(comp.get("summary"), COMP_FIELD_CLIP),
        "awards": sorted(
            (int(a.get("id") or 0), _clip(a.get("title"), AWARD_FIELD_CLIP))
            for a in awards
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 攻略生成
# ---------------------------------------------------------------------------
def generate_guide(comp: dict, awards: Optional[list] = None, *,
                   max_tokens: int = GUIDE_MAX_TOKENS) -> dict:
    """调一次大模型生成攻略，返回 {markdown, tokens, model, prompt_ver, input_hash}。"""
    awards = awards if awards is not None else []
    prompt = build_guide_prompt(comp, awards)
    before = llm.get_usage()
    text = llm.chat(
        prompt,
        system=GUIDE_SYSTEM_PROMPT,
        temperature=0.3,
        max_tokens=int(max_tokens),
    )
    after = llm.get_usage()

    markdown = _strip_fence(text or "")
    if len(markdown) < GUIDE_MIN_CHARS:
        raise llm.LLMError(
            "攻略过短（%d 字），疑似截断或占位文案" % len(markdown)
        )
    return {
        "markdown": markdown,
        "tokens": _usage_delta(before, after),
        "model": getattr(llm, "MODEL", None),
        "prompt_ver": GUIDE_PROMPT_VER,
        "input_hash": guide_input_hash(comp, awards),
    }


def generate_guide_for(competition_id: int, *, force: bool = False,
                       max_tokens: int = GUIDE_MAX_TOKENS) -> dict:
    """生成并落库（调用方负责 db.DB_PATH 与任务状态）。

    返回::

        {"status": "exists",    "guide_length": 4300}          # 已有攻略且未 force
        {"status": "generated", "guide_length": 3800, "tokens": 3120,
         "model": "...", "prompt_ver": "v2"}
    """
    comp = db.get_competition(int(competition_id))
    if comp is None:
        raise LookupError("竞赛 ID=%s 不存在" % competition_id)

    existing = (comp.get("full_process_guide") or "").strip()
    if existing and not force:
        return {"status": "exists", "guide_length": len(existing)}

    awards = db.list_awards_by_competition(int(competition_id)) or []
    result = generate_guide(comp, awards, max_tokens=max_tokens)
    db.save_generated_guide(
        int(competition_id),
        result["markdown"],
        model=result["model"],
        prompt_ver=result["prompt_ver"],
        input_hash=result["input_hash"],
    )
    return {
        "status": "generated",
        "guide_length": len(result["markdown"]),
        "tokens": result["tokens"],
        "model": result["model"],
        "prompt_ver": result["prompt_ver"],
    }


# ---------------------------------------------------------------------------
# 灵感生成（insights + ideas，复用 analyzer.py 的成熟实现）
# ---------------------------------------------------------------------------
def generate_inspiration(competition_id: int, *, idea_n: int = 4,
                         force: bool = False, max_tokens: int = 8000,
                         quiet: bool = True) -> dict:
    """生成该赛事的 insights + ideas，返回新增条数与 token 用量。

    `analyzer.py` 仍是唯一实现（3 次 insight + 按批 ideas、容错解析都在里面），
    这里只做「统计前后差异 + 静音 + token 对账」，避免复制一份 Prompt 出来。
    """
    import analyzer

    cid = int(competition_id)
    before_ins = len(db.list_insights_by_competition(cid))
    before_ideas = len(db.list_ideas_by_competition(cid))
    before_usage = llm.get_usage()

    buffer = io.StringIO()
    if quiet:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = analyzer.generate(
                cid, only="all", idea_n=int(idea_n), force=bool(force),
                max_tokens=int(max_tokens),
            )
    else:
        code = analyzer.generate(
            cid, only="all", idea_n=int(idea_n), force=bool(force),
            max_tokens=int(max_tokens),
        )

    after_usage = llm.get_usage()
    insights_new = max(len(db.list_insights_by_competition(cid)) - before_ins, 0)
    ideas_new = max(len(db.list_ideas_by_competition(cid)) - before_ideas, 0)
    return {
        "exit_code": int(code),
        "insights_new": insights_new,
        "ideas_new": ideas_new,
        "tokens": _usage_delta(before_usage, after_usage),
        "log": buffer.getvalue()[-2000:] if quiet else "",
    }
