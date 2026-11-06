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
MAX_AWARDS = 24
MAX_FIELD_CHARS = 400
DEFAULT_MAX_TOKENS = 8000
INSIGHT_MAX_CHARS = 200
IDEA_BATCH_SIZE = 2

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
    """只保留对分析有价值的获奖作品字段。"""
    tech = row.get("tech_stack") or []
    if isinstance(tech, (list, tuple)):
        tech = "、".join(str(t) for t in tech if str(t).strip())
    return {
        "id": row.get("id"),
        "year": row.get("year"),
        "title": _clip(row.get("title"), 160),
        "award": row.get("award_grade") or row.get("award_level") or "",
        "track": _clip(row.get("track"), 100),
        "team": _clip(row.get("team"), 100),
        "summary": _clip(row.get("summary")),
        "highlights": _clip(row.get("highlights")),
        "tech_stack": _clip(tech, 160),
    }


def _fair_sample(rows: list, k: int) -> list:
    """按年份均匀抽样，避免只喂某一年的作品。"""
    if len(rows) <= k:
        return rows
    ordered = sorted(rows, key=lambda r: (r.get("year") or 0, r.get("id") or 0))
    step = len(ordered) / k
    return [ordered[int(i * step)] for i in range(k)]


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
    """公共上下文：赛事信息 + 作品事实 + 严格约束。"""
    payload = [_slim_award(w) for w in _fair_sample(works, MAX_AWARDS)]
    name = competition.get("name") or f"赛事 #{competition.get('id')}"
    category = competition.get("category") or "未分类"
    valid_ids = sorted(_ids_of(works))
    context = f"""你是一位资深赛事评审与备赛教练。下面是「{name}」（ID={competition.get('id')}，
类别：{category}）在 {year_from}-{year_to} 年的真实获奖作品数据：

{json.dumps(payload, ensure_ascii=False)}

【硬性约束】
1. 这些数据已经按赛事严格筛选，只属于「{name}」。你只能基于这些作品归纳，
   禁止混入其它赛事、其它类别、或数据中不存在的作品/事实。
2. 引用作品时只能使用上面出现过的 id，可选范围：{valid_ids}。
3. 信息不足时明确写“数据中未提及”，不要编造。
4. 输出务必精简，避免长篇大论。"""
    return context, valid_ids


def _insight_prompt(context: str, kind: str) -> str:
    return f"""{context}

【本次只生成 1 条：{KIND_LABEL[kind]}】
任务：{KIND_TASK[kind]}

输出要求：
- 只输出 Markdown 正文，不要 JSON、不要代码围栏、不要任何解释性开场白。
- 必须精简：正文控制在 {INSIGHT_MAX_CHARS} 字以内（约 3 个要点）。
- 每个结论都要能追溯到上面出现的作品特征。
"""


def _ideas_prompt(context: str, batch_n: int, valid_ids: list,
                  avoid_titles: list) -> str:
    avoid = ""
    if avoid_titles:
        avoid = "\n- 不要重复这些已生成的标题：" + "；".join(avoid_titles[:20])
    return f"""{context}

【本次生成 {batch_n} 条面向下一届比赛的选题 idea】
要求：
- 每条必须与上面已有获奖作品有明确差异，不要照抄已有题目。
- 方案要能在 3-6 个月内由 3-5 人小组完成。
- ref_awards 只能填上面出现过的作品 id，可选范围：{valid_ids}{avoid}

【字段要求（务必精简）】
- title：选题名称，不超过 30 字
- pitch：一句话价值主张，不超过 50 字
- detail_md：Markdown 方案，只写「背景 / 做法 / 交付物」，不超过 300 字
- novelty：差异化说明，不超过 80 字
- risk：主要风险与规避，不超过 80 字
- ref_awards：作品 id 数组

只输出一个 JSON 对象，格式：
{{"ideas":[{{"title":"...","pitch":"...","detail_md":"...","novelty":"...","risk":"...","ref_awards":[1,2]}}]}}
不要输出代码围栏或任何解释，总字数控制在 1500 字以内。
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


# ---------------------------------------------------------------- 生成
def _generate_insights(competition: dict, works: list, context: str,
                       scope: str, competition_id: int, valid: set,
                       max_tokens: int) -> tuple:
    """逐 kind 调用模型，单条失败不影响其它两条。"""
    saved, failed = 0, 0
    for kind in ("experience", "trend", "gap"):
        print(f"[insight] 正在生成：{KIND_LABEL[kind]}…")
        try:
            raw = _chat_markdown(_insight_prompt(context, kind), max_tokens)
            content = _parse_insight_text(raw)
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

        evidence = sorted(valid)
        input_hash = hashlib.sha1(
            f"{context}|{kind}".encode("utf-8")
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
        try:
            items = _chat_ideas(
                context, want, sorted(valid), titles, max_tokens
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

        for item in items:
            title = item["title"]
            try:
                new_id, action = db.save_idea(
                    title,
                    competition_id=competition_id,
                    pitch=_clip(item.get("pitch"), 120),
                    detail_md=item.get("detail_md") or "",
                    novelty=_clip(item.get("novelty"), 200),
                    risk=_clip(item.get("risk"), 200),
                    ref_awards=item.get("ref_awards") or [],
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

    works = db.list_awards_by_competition(competition_id)
    if not works:
        print(
            f"[!] 竞赛「{competition.get('name')}」(ID={competition_id}) 暂无获奖作品，"
            "无法生成专属灵感。",
            file=sys.stderr,
        )
        return 2

    year_from, year_to = _year_range(works)
    scope = _scope(competition_id, year_from, year_to)
    valid = _ids_of(works)
    print(f"[i] 赛事：{competition.get('name')}（ID={competition_id}）")
    print(f"[i] 该赛事获奖作品：{len(works)} 条，年份 {year_from}-{year_to}")
    print(f"[i] max_tokens={max_tokens}，insights 分 3 次请求，ideas 每 {IDEA_BATCH_SIZE} 条一批")

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
            competition, works, context, scope, competition_id, valid, max_tokens
        )

    if only in ("all", "idea"):
        saved_ideas, skipped_ideas, failed_batches = _generate_ideas(
            competition, context, competition_id, valid, idea_n, max_tokens
        )

    print("\n================ 汇总 ================")
    print(f"竞赛：{competition.get('name')}（ID={competition_id}）")
    if only in ("all", "insight"):
        print(f"insights 写入：{saved_insights} 条（失败 {failed_insights} 条）")
    if only in ("all", "idea"):
        print(f"ideas 写入：{saved_ideas} 条（已存在 {skipped_ideas} 条，失败批次 {failed_batches}）")
    print(f"token 用量：{llm.get_usage()}")
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
