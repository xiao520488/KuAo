"""AI 能力路由 (routers/ai.py)

提供两个基于大模型的接口：

  * POST /api/ai/plan     备赛教练（渐进式多轮对话：先反问 -> 信息足够后再生成计划）
  * POST /api/ai/analyze  赛事获奖作品聚合分析（多作品→共性特征与获奖套路）

设计原则：
  - 依赖 llm.py 的薄封装，不在此处直接触碰 OpenAI SDK
  - 竞赛上下文一律从 db 取真实数据，避免模型凭空编造赛制/时间
  - LLM 异常统一转换为 502，保证前端拿到结构化错误而非裸 500
  - 单作品解析 / 赛事共性分析的结果按目标全局缓存（ai_analysis_cache），
    命中即直接返回，不再调用大模型；prompt 指纹变化时自动重新生成
"""

import hashlib
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import llm
from db import (
    get_ai_analysis,
    get_award_by_id,
    get_competition,
    list_awards_by_competition,
    list_ideas_by_competition,
    list_insights_by_competition,
    save_ai_analysis,
)
from security import current_ai_quota, require_ai_quota
from models.schemas import Competition

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class PlanRequest(BaseModel):
    """备赛计划请求。

    三种业务场景（由后端自动判定）：
      1. 「收集信息」：current_plan 为空 & 用户背景不足以制定计划
                      -> 返回反问文本，前端展示给用户作答
      2. 「生成计划」：current_plan 为空 & 信息已足够（或用户明确要求直接生成）
                      -> 返回完整备赛计划
      3. 「增量修改」：current_plan 有值
                      -> 在既有计划上修改，返回整合后的完整计划
    """

    competition_id: Optional[int] = Field(
        None, description="竞赛 ID，用于从数据库拉取真实赛制信息作为上下文"
    )
    user_input: str = Field(
        "",
        max_length=8000,
        description=(
            "用户最新一轮输入。多轮对话时，前端应把『历史问答 + 本轮回答』"
            "拼接后放入本字段，以保证模型能看到完整背景。"
        ),
    )
    current_plan: Optional[str] = Field(
        None,
        max_length=32000,
        description="已有计划（Markdown）。为空则走「反问 / 生成」流程，非空则增量修改",
    )


class AnalyzeRequest(BaseModel):
    """赛事获奖作品聚合分析请求。

    前端只需传 competition_id，后端自行去 awards 表取该赛事下的获奖作品
    （最多 5 条），拼进 prompt 让模型总结「共性特征与获奖套路」。
    """

    competition_id: int = Field(..., gt=0, description="竞赛 ID，用于检索该赛事的获奖作品")


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------
class PlanResponse(BaseModel):
    """备赛教练响应。

    status 是前端分流渲染的依据：
      * ask     -> 内容为反问文本，以对话气泡展示，等待用户作答
      * plan    -> 内容为完整备赛计划，以 Markdown 渲染
      * refine  -> 内容为修改后的完整计划
    """

    status: str = Field(
        ...,
        description="ask=反问待补充信息；plan=首次生成计划；refine=增量修改后的计划",
    )
    reply: str = Field(..., description="模型回复正文（反问文本或 Markdown 计划）")
    mode: str = Field(
        ..., description="create=新建流程（含 ask / plan 两种结果），refine=增量修改"
    )
    competition_id: Optional[int] = Field(None, description="关联的竞赛 ID")


class AnalyzeResponse(BaseModel):
    """赛事获奖作品聚合分析响应。"""

    analysis: str = Field(..., description="共性特征与获奖套路分析（Markdown）")
    competition_id: int = Field(..., description="对应的竞赛 ID")
    competition_name: Optional[str] = Field(None, description="竞赛名称")
    work_count: int = Field(..., description="参与本次分析的作品条数")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _competition_context(competition_id: Optional[int]) -> str:
    """从数据库读取竞赛真实信息，拼成 prompt 上下文片段。

    竞赛不存在时返回空串，不阻断主流程（仅降级为无上下文生成）。
    """
    if competition_id is None:
        return ""

    try:
        row = get_competition(competition_id)
    except Exception as exc:  # 数据库异常不应让整个接口失败
        logger.warning("读取竞赛 %s 失败: %r", competition_id, exc)
        return ""

    if not row:
        logger.warning("竞赛 ID=%s 不存在，将以无上下文模式生成", competition_id)
        return ""

    start_m, end_m = row.get("reg_start_month"), row.get("reg_end_month")
    if start_m and end_m:
        cross = "（跨年赛事）" if row.get("reg_cross_year") else ""
        reg_window = f"{start_m} 月至 {end_m} 月{cross}"
    else:
        reg_window = "未收录"

    lines = [
        "【竞赛基本信息（来自数据库，请以此为准）】",
        f"- 名称：{row.get('name') or '未知'}",
        f"- 类别：{row.get('category') or '未知'}",
        f"- 级别：{row.get('level_label') or row.get('level') or '未知'}",
        f"- 报名窗口：{reg_window}",
        f"- 官网：{row.get('official_url') or '未收录'}",
    ]
    intro = (row.get("intro") or row.get("summary") or "").strip()
    if intro:
        lines.append(f"- 赛事简介：{intro}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AI 分析结果缓存（全局，按「作品 / 赛事」复用）
# ---------------------------------------------------------------------------
# 设计要点：
#   * 键 = (target_type, target_id)：同一个作品/赛事全局只保留一条最新结果；
#   * 有效性 = prompt 的 SHA-256：作品/赛事数据一旦变化，指纹不同即视为未命中，
#     重新生成并覆盖旧结果，避免返回过期分析；
#   * 读/写缓存均做异常兜底——缓存只是加速手段，故障时自动降级为直接调模型，
#     绝不让接口 500。
ANALYSIS_TARGET_WORK = "work"
ANALYSIS_TARGET_COMPETITION = "competition"


def _analysis_input_hash(prompt: str) -> str:
    """对实际送入大模型的 prompt 取 SHA-256，作为缓存有效性指纹。"""
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()


def _load_cached_analysis(target_type: str, target_id: int,
                          prompt: str) -> Optional[str]:
    """读取已缓存的分析正文；未命中/指纹不一致/异常均返回 None。"""
    try:
        row = get_ai_analysis(
            target_type, target_id, input_hash=_analysis_input_hash(prompt)
        )
    except Exception as exc:  # 缓存故障不应中断业务
        logger.warning("读取 AI 分析缓存异常，降级调用模型: %r", exc)
        return None

    if not row:
        return None
    content = (row.get("content_md") or "").strip()
    return content or None


def _store_analysis(target_type: str, target_id: int, prompt: str,
                    content: str) -> None:
    """把模型生成结果写入缓存；失败只记日志，不影响本次返回。"""
    text = (content or "").strip()
    if not text:
        return
    try:
        save_ai_analysis(
            target_type,
            target_id,
            _analysis_input_hash(prompt),
            text,
            model=getattr(llm, "MODEL", None),
        )
    except Exception as exc:  # 写缓存失败不影响接口成功返回
        logger.warning("写入 AI 分析缓存异常（忽略）: %r", exc)


# ---------------------------------------------------------------------------
# System Prompt —— 「先反问，后生成」的教练人格，约束写入此处强制生效
# ---------------------------------------------------------------------------
ASK_MARKER = "【反问】"
PLAN_MARKER = "【计划】"

PLAN_SYSTEM_PROMPT = (
    "你是一位资深的学科竞赛教练，采用「渐进式多轮对话」的方式指导学生，"
    "而不是一次性把计划丢给学生。\n"
    "\n"
    "【最核心规则：先反问，后生成】\n"
    "1. 在当前轮**没有【现有备赛计划】**、且你判断用户背景信息**不足以**制定一份"
    "有针对性、可执行的计划时，你**绝对不能直接输出计划**，"
    "必须改为向用户反问关键问题来收集信息。\n"
    "2. 反问时，请聚焦以下维度中尚不明确的项（通常一次问 3-5 个，不要超过 6 个）：\n"
    "   - 你是第一次参赛吗？（参赛经历）\n"
    "   - 每天 / 每周大约能投入多少时间？\n"
    "   - 目前掌握了哪些技能、工具或课程基础？\n"
    "   - 你的目标是什么？（如保底完赛 / 冲省奖 / 冲国奖）\n"
    "   - 是否已有队友、作品方向或选题想法？\n"
    "3. **反问轮的输出格式**：第一行必须严格是 `【反问】`，随后用简洁友好的 Markdown "
    "列出问题（可用有序列表）。此时**禁止**输出任何计划内容、禁止写“示例计划”。\n"
    "4. 当且仅当满足以下任一条件时，才输出计划：\n"
    "   a) 用户在历史对话中已提供了足够背景信息；或\n"
    "   b) 用户明确表示「可以了」「直接生成」「不用问了」「先给我一版」等，"
    "希望跳过提问。\n"
    "5. **生成计划轮的输出格式**：第一行必须严格是 `【计划】`，随后用 Markdown 输出"
    "完整计划。此时**禁止**再提出问题。\n"
    "6. 每轮回复只允许出现一个标记，且必须是全文第一行，不得有前置空行或说明文字。"
    "\n"
    "【增量修改场景】\n"
    "若用户提供了【现有备赛计划】，则不再反问，直接按要求修改，"
    "输出第一行同样使用 `【计划】` 标记。\n"
    "\n"
    "语气要求：专业、鼓励、简洁；反问轮不要长篇大论，把注意力放在提问上。"
)


def _build_plan_prompt(req: PlanRequest, ctx: str) -> str:
    """构造用户消息。是否反问由 System Prompt 约束模型，此处只提供事实与情境。"""
    parts: List[str] = []

    if ctx:
        parts.append(ctx)
        parts.append("")

    if req.current_plan and req.current_plan.strip():
        # ---- 增量修改场景 ----
        parts.append("【现有备赛计划】")
        parts.append("<<<PLAN_START")
        parts.append(req.current_plan.strip())
        parts.append("PLAN_END>>>")
        parts.append("")
        parts.append(
            "【本轮任务】\n"
            "用户已在下方给出【用户输入】。请在此现有计划基础上做**增量修改与补充**：\n"
            "1. 完整保留原有结构、已完成项与仍然有效的安排，不要推倒重写；\n"
            "2. 针对用户新增或调整的诉求，补充相应章节或条目；\n"
            "3. 可优化不合理处，但保持原有 Markdown 层级风格一致；\n"
            "4. 输出**修改整合后的完整计划**（而非 diff 或变更说明）；\n"
            "5. 第一行输出 `【计划】`，随后直接给出 Markdown 正文，不要用代码块包裹。"
        )
    else:
        # ---- 新建流程：反问 or 生成，交由模型依据 System Prompt 判定 ----
        parts.append(
            "【本轮任务】\n"
            "当前没有【现有备赛计划】。请你作为教练，先判断下方【用户输入】中"
            "所提供的背景信息是否**足以**制定一份有针对性的备赛计划：\n"
            "- 若**不足**：请严格按照 System Prompt 要求，只输出反问，"
            "第一行为 `【反问】`，不要输出任何计划内容；\n"
            "- 若**已足够**（或用户明确要求直接生成）：请输出完整计划，"
            "第一行为 `【计划】`。\n"
            "务必遵循「先反问，后生成」的核心规则，不要在信息不足时硬编计划。"
        )

    user_input = req.user_input.strip()
    if user_input:
        parts.append("")
        parts.append("【用户输入】")
        parts.append("<<<USER_INPUT_START")
        parts.append(user_input)
        parts.append("USER_INPUT_END>>>")
    else:
        parts.append("")
        parts.append("【用户输入】\n（用户尚未提供任何背景信息）")

    return "\n".join(parts)


_MARKER_RE = re.compile(r"^\s*【(反问|计划)】\s*", re.M)


def _parse_stage(text: str) -> str:
    """从模型输出中解析本轮属于「反问(ask)」还是「计划(plan)」。

    兜底策略：模型若未按要求给标记，则根据是否包含 Markdown 标题（`#`）
    粗判——有标题更像计划，无标题更像反问。解析后剥离标记行。
    """
    stripped = text.lstrip()
    m = re.match(r"【(反问|计划)】", stripped)
    if m:
        return "ask" if m.group(1) == "反问" else "plan"
    # 无标记兜底
    if re.search(r"^#{1,3}\s", text, re.M):
        return "plan"
    return "ask"


def _strip_marker(text: str) -> str:
    """去掉首行的【反问】/【计划】标记，返回可展示正文。"""
    out = re.sub(r"^\s*【(反问|计划)】[ \t]*\n?", "", text, count=1)
    return out.strip()


# ---------------------------------------------------------------------------
# POST /api/ai/plan
# ---------------------------------------------------------------------------
@router.post(
    "/plan",
    response_model=PlanResponse,
    summary="备赛教练（渐进式多轮对话）",
    description=(
        "渐进式多轮对话接口：\n"
        "- `current_plan` 为空且背景信息不足 → 返回 `status=ask` 的反问文本；\n"
        "- 前端带着用户回答再次请求，信息足够或用户要求直接生成 → 返回 `status=plan` 的完整计划；\n"
        "- `current_plan` 有值 → 在其基础上增量修改，返回 `status=refine`。"
    ),
    responses={
        404: {"description": "竞赛不存在"},
        502: {"description": "大模型调用失败"},
    },
)
def generate_plan(
    req: PlanRequest,
    _user: dict = Depends(require_ai_quota),  # 需登录
) -> PlanResponse:
    """备赛教练：先反问收集信息，信息足够后再生成计划。"""
    # 显式传了 competition_id 却查不到 -> 明确报错，便于前端定位
    if req.competition_id is not None:
        try:
            exists = get_competition(req.competition_id)
        except Exception as exc:
            logger.error("查询竞赛失败: %r", exc)
            raise HTTPException(status_code=500, detail="数据库查询失败") from exc
        if exists is None:
            raise HTTPException(
                status_code=404, detail=f"竞赛 ID={req.competition_id} 不存在"
            )

    ctx = _competition_context(req.competition_id)
    prompt = _build_plan_prompt(req, ctx)
    is_refine = bool(req.current_plan and req.current_plan.strip())

    try:
        text = llm.chat(
            prompt,
            system=PLAN_SYSTEM_PROMPT,
            temperature=0.6,
            max_tokens=4096,
        )
    except llm.LLMError as exc:
        logger.error("备赛教练调用失败: %r", exc)
        raise HTTPException(status_code=502, detail=f"大模型调用失败: {exc}") from exc
    except Exception as exc:  # 网络等未预期异常
        logger.exception("备赛教练出现未预期异常")
        raise HTTPException(status_code=502, detail=f"大模型调用异常: {exc}") from exc

    if not text or not text.strip():
        raise HTTPException(status_code=502, detail="大模型返回内容为空")

    stage = _parse_stage(text)
    reply = _strip_marker(text) or text.strip()

    # 增量修改场景下，即便模型误标为 ask，业务语义也应是 refine
    if is_refine:
        status, mode = "refine", "refine"
    elif stage == "ask":
        status, mode = "ask", "create"
    else:
        status, mode = "plan", "create"

    return PlanResponse(
        status=status, reply=reply, mode=mode, competition_id=req.competition_id
    )


# ---------------------------------------------------------------------------
# POST /api/ai/analyze
# ---------------------------------------------------------------------------
ANALYZE_SYSTEM_PROMPT = (
    "你是一位资深赛事评审与技术趋势分析师，擅长横向对比同一赛事的多件获奖作品，"
    "提炼出可复用的共性规律、获奖套路与评审偏好。你要基于给定作品事实进行归纳，"
    "不得凭空编造未出现的作品信息。输出使用 Markdown。"
)


_FIELD_PREFIXES = ("亮点", "创新点", "优势", "特色", "项目亮点")


def _clean_field(text: Optional[str]) -> str:
    """清洗爬取字段自带的前缀与多余空白。

    实际数据中 highlights 常以“亮点：”开头，直接拼接会变成
    “亮点：亮点：…”。这里反复剥离开头的“字段名：”直到不再命中。
    """
    if not text:
        return ""
    t = str(text).strip()
    changed = True
    while changed:
        changed = False
        for pref in _FIELD_PREFIXES:
            for sep in ("：", ":"):
                head = pref + sep
                if t.startswith(head):
                    t = t[len(head):].lstrip()
                    changed = True
    return t.strip()


def _fmt_works(works: List[Dict[str, Any]]) -> str:
    """把作品列表渲染成带编号的纯文本块，避免模型混淆作品边界。"""
    blocks = []
    for i, w in enumerate(works, 1):
        tech = w.get("tech_stack") or []
        tech_str = ", ".join(str(t) for t in tech) if tech else "未提供"
        award = w.get("award_grade") or "等级未标注"
        year = w.get("year")
        year_str = f"{year} 年" if year else "年份未标注"
        session = w.get("session")
        if session:
            year_str = f"{year_str}{session}"
        team = w.get("team")
        track = w.get("track")
        blocks.append(
            "\n".join([
                f"--- 作品 {i} ---",
                f"标题：{w.get('title') or '未提供'}",
                f"获奖等级：{award}（{year_str}）",
                *([f"团队：{team}"] if team else []),
                *([f"赛道：{track}"] if track else []),
                f"摘要：{(_clean_field(w.get('summary')) or '未提供')}",
                f"技术栈：{tech_str}",
                f"亮点：{_clean_field(w.get('highlights')) or '未提供'}",
            ])
        )
    return "\n\n".join(blocks)


def _build_analyze_prompt(
    competition_name: Optional[str],
    ctx: str,
    works: List[Dict[str, Any]],
) -> str:
    """构建「多作品聚合分析」prompt。"""
    parts = []

    if ctx:
        parts.append(ctx)
        parts.append("")

    parts.append(f"【参评赛事】{competition_name or '未知赛事'}")
    parts.append(f"【作品总数】{len(works)} 件（均为该赛事的获奖作品）")
    parts.append("")
    parts.append("【获奖作品列表】")
    parts.append("")
    parts.append(_fmt_works(works))
    parts.append("")
    parts.append("【任务要求】")
    parts.append(
        "以上是该赛事的多件获奖作品。请横向对比它们，输出一份"
        "「赛事共性特征与获奖套路」分析，要求："
    )
    parts.append("1. **选题共性**：这些获奖作品在选题方向、问题规模、落地场景上的共同倾向；")
    parts.append("2. **技术套路**：高频出现的技术栈组合与技术方案范式，指出哪些是「标配」；")
    parts.append("3. **创新点模式**：获奖作品惯用的创新切入方式（场景迁移、性能突破、成本优化等）；")
    parts.append("4. **工程与呈现**：完成度、可演示性、文档与答辩材料的共同特征；")
    parts.append("5. **获奖公式**：用一句话概括可复制的获奖方法论，再列出 5 条可执行建议；")
    parts.append("6. **风险提示**：指出容易被评审扣分的典型共性问题。")
    parts.append("")
    parts.append("格式约束：使用 Markdown，每个维度用二级标题，观点要具体、"
                 "能引用上面某件作品作为例证，避免空泛套话。")
    return "\n".join(parts)


@router.post(
    "/analyze",
    response_model=AnalyzeResponse,
    summary="赛事获奖作品聚合分析",
    description=(
        "接收 competition_id，从数据库检索该赛事最多 5 件获奖作品"
        "（title/summary/tech_stack/highlights），聚合产出共性特征与获奖套路（Markdown）。"
    ),
    responses={
        404: {"description": "竞赛不存在或该赛事暂无获奖作品"},
        502: {"description": "大模型调用失败"},
    },
)
def analyze_work(
    req: AnalyzeRequest,
    _user: dict = Depends(require_ai_quota),  # 需登录
) -> AnalyzeResponse:
    """赛事获奖作品聚合分析：从库里取前 5 件获奖作品，总结共性特征与获奖套路。"""
    # 1. 校验竞赛存在
    try:
        comp = get_competition(req.competition_id)
    except Exception as exc:
        logger.error("查询竞赛失败: %r", exc)
        raise HTTPException(status_code=500, detail="数据库查询失败") from exc
    if comp is None:
        raise HTTPException(
            status_code=404, detail=f"竞赛 ID={req.competition_id} 不存在"
        )

    # 2. 取该赛事的获奖作品（最多 5 条）
    try:
        works = list_awards_by_competition(req.competition_id, limit=5)
    except Exception as exc:
        logger.error("查询获奖作品失败: %r", exc)
        raise HTTPException(status_code=500, detail="数据库查询失败") from exc

    if not works:
        raise HTTPException(
            status_code=404,
            detail=f"竞赛 ID={req.competition_id} 暂无获奖作品数据，无法做聚合分析",
        )

    # 3. 拼 prompt；先查全局缓存，命中直接返回，不再调用大模型
    ctx = _competition_context(req.competition_id)
    comp_name = comp.get("name")
    prompt = _build_analyze_prompt(comp_name, ctx, works)

    cached = _load_cached_analysis(
        ANALYSIS_TARGET_COMPETITION, req.competition_id, prompt
    )
    if cached:
        logger.info(
            "赛事共性分析命中缓存 competition_id=%s，跳过大模型",
            req.competition_id,
        )
        return AnalyzeResponse(
            analysis=cached,
            competition_id=req.competition_id,
            competition_name=comp_name,
            work_count=len(works),
        )

    # 4. 未命中 -> 调用模型
    try:
        text = llm.chat(
            prompt,
            system=ANALYZE_SYSTEM_PROMPT,
            temperature=0.5,
            max_tokens=3072,
        )
    except llm.LLMError as exc:
        logger.error("作品聚合分析失败: %r", exc)
        raise HTTPException(status_code=502, detail=f"大模型调用失败: {exc}") from exc
    except Exception as exc:
        logger.exception("作品聚合分析出现未预期异常")
        raise HTTPException(status_code=502, detail=f"大模型调用异常: {exc}") from exc

    if not text or not text.strip():
        raise HTTPException(status_code=502, detail="大模型返回内容为空")

    analysis = text.strip()
    # 5. 生成成功 -> 落库，供后续请求全局复用
    _store_analysis(
        ANALYSIS_TARGET_COMPETITION, req.competition_id, prompt, analysis
    )

    return AnalyzeResponse(
        analysis=analysis,
        competition_id=req.competition_id,
        competition_name=comp_name,
        work_count=len(works),
    )


# ---------------------------------------------------------------------------
# 单作品深度解析
# ---------------------------------------------------------------------------
class WorkAnalyzeRequest(BaseModel):
    """单作品深度解析请求。"""

    work_id: int = Field(..., gt=0, description="获奖作品 ID，对应 awards 表主键")


class WorkAnalyzeResponse(BaseModel):
    """单作品深度解析响应。"""

    analysis: str = Field(..., description="针对该作品的深度解析（Markdown）")


def _build_work_prompt(work: Dict[str, Any]) -> str:
    """构建单作品深度解析 prompt。"""
    tech = work.get("tech_stack") or []
    tech_str = "、".join(str(t) for t in tech) if tech else "未提供"

    title = _clean_field(work.get("title")) or "未提供"
    summary = _clean_field(work.get("summary")) or "未提供"
    highlights = _clean_field(work.get("highlights")) or "未提供"

    # 来源链接：DB 字段为 source_url，对外 API 名为 origin_url，两者都兼容
    origin_url = (
        work.get("origin_url")
        or work.get("source_url")
        or ""
    )
    origin_url = str(origin_url).strip() or "未提供"

    year = work.get("year")
    year_str = f"{year} 年" if year else "年份未注明"
    session = work.get("session")
    if session:
        year_str = f"{year_str} {session}"
    grade = work.get("award_grade") or "等级未注明"
    team = work.get("team") or "未提供"
    track = work.get("track") or "未提供"

    return (
        "你是一位资深的学科竞赛指导专家，擅长拆解获奖作品并提炼可复用的经验。\n"
        "请针对下面这一件具体的获奖作品做深度解析，帮助学弟学妹理解它为什么能获奖、"
        "以及在哪些地方可以借鉴。\n\n"
        "【作品信息】\n"
        f"标题：{title}\n"
        f"赛事年份/赛段：{year_str}\n"
        f"获奖等级：{grade}\n"
        f"赛道/组别：{track}\n"
        f"团队：{team}\n"
        f"技术栈：{tech_str}\n"
        f"作品简介：{summary}\n"
        f"作品亮点：{highlights}\n"
        f"原始链接：{origin_url}\n\n"
        "【解析要求】\n"
        "请用 Markdown 输出，严格包含以下六个部分，标题层级用二级标题（##）：\n"
        "1. **作品概览**：用 2-3 句话说明这件作品解决了什么问题、面向什么场景。\n"
        "2. **技术亮点**：逐条拆解其技术亮点，说明每一项技术选择解决了什么难点、"
        "为什么在这个场景下是合理的。\n"
        "3. **获奖要素分析**：结合其获奖等级，分析评委可能看重的点"
        "（创新性、完成度、工程量、展示效果等）。\n"
        "4. **可借鉴之处**：给出具体、可操作的方法论，说明哪些思路可以直接迁移到"
        "其他项目或你正在准备的作品中。\n"
        "5. **潜在不足与提升空间**：客观指出该作品可能的短板，以及如果继续做可以怎么加强。\n"
        "6. **如果想模仿，从哪一步开始**：给出一个 3-5 步的落地行动建议。\n\n"
        "另外：若「原始链接」不为「未提供」，说明该作品有相关的源码／文档／演示等外部资料。请在「作品概览」末尾用一句话提示用户：该作品附有原始链接，可点击页面下方的按钮查看详情。\n"
        "【严禁】在任何位置输出、复述或改写具体的 URL 字符串（包括 http/https 链接、域名、GitHub 仓库地址等），一律只做文字引导，由前端按钮承载跳转。\n"
        "注意：只围绕这一件作品展开，不要泛泛而谈整个赛事；"
        "信息不足的部分请明确说明「作品信息中未提及」，不要编造。"
    )


@router.post(
    "/analyze-work",
    response_model=WorkAnalyzeResponse,
    summary="单作品深度解析",
    description=(
        "接收 work_id，从数据库检索该作品的详细信息"
        "（title/summary/tech_stack/highlights 等），"
        "让大模型针对这单个作品做深度解析（技术亮点、获奖要素、如何借鉴等）。"
    ),
    responses={
        404: {"description": "作品不存在"},
        502: {"description": "大模型调用失败"},
    },
)
def analyze_single_work(
    req: WorkAnalyzeRequest,
    _user: dict = Depends(require_ai_quota),  # 需登录
) -> WorkAnalyzeResponse:
    """单作品深度解析。"""
    # 1. 校验作品存在
    try:
        work = get_award_by_id(req.work_id)
    except Exception as exc:
        logger.error("查询作品失败: %r", exc)
        raise HTTPException(status_code=500, detail="查询作品失败")

    if work is None:
        raise HTTPException(status_code=404, detail=f"作品 ID={req.work_id} 不存在")

    # 2. 组装 prompt；先查全局缓存，命中直接返回，不再调用大模型
    prompt = _build_work_prompt(work)

    cached = _load_cached_analysis(ANALYSIS_TARGET_WORK, req.work_id, prompt)
    if cached:
        logger.info("单作品解析命中缓存 work_id=%s，跳过大模型", req.work_id)
        return WorkAnalyzeResponse(analysis=cached)

    # 3. 未命中 -> 调用模型
    try:
        raw = llm.chat(prompt)
    except Exception as exc:
        logger.error("单作品解析调用模型失败: %r", exc)
        raise HTTPException(status_code=502, detail="大模型调用失败")

    if not raw or not str(raw).strip():
        logger.error("单作品解析返回空内容, work_id=%s", req.work_id)
        raise HTTPException(status_code=502, detail="大模型返回空内容")

    analysis = str(raw).strip()
    # 4. 生成成功 -> 落库，供后续请求全局复用
    _store_analysis(ANALYSIS_TARGET_WORK, req.work_id, prompt, analysis)

    return WorkAnalyzeResponse(analysis=analysis)


# ---------------------------------------------------------------------------
# 自然语言检索（首页搜索框）
# ---------------------------------------------------------------------------
class SearchRequest(BaseModel):
    """自然语言检索请求。"""

    query: str = Field(
        ..., min_length=1, max_length=200, description="用户的自然语言描述"
    )
    top_k: int = Field(8, ge=1, le=20, description="返回竞赛条数上限（1-20）")


class SearchItem(BaseModel):
    """单条检索结果：竞赛基本信息 + 相关度。"""

    id: int = Field(..., description="竞赛 ID")
    name: str = Field(..., description="竞赛名称")
    category: Optional[str] = Field(None, description="竞赛类别")
    level: Optional[str] = Field(None, description="级别：national/provincial")
    level_label: Optional[str] = Field(None, description="级别中文标签")
    official_url: Optional[str] = Field(None, description="竞赛官网地址")
    summary: Optional[str] = Field(None, description="竞赛摘要")
    short_name: Optional[str] = Field(
        None, description="AI 提取的 2 字赛事简称（无图卡片 Logo 兜底）"
    )
    score: float = Field(0.0, description="相关度得分（越大越相关）")
    matched_awards: int = Field(0, description="命中的获奖作品条数")


class SearchResponse(BaseModel):
    """自然语言检索响应。"""

    query: str = Field(..., description="原始查询词")
    total: int = Field(0, description="返回结果条数")
    items: List[SearchItem] = Field(default_factory=list, description="竞赛列表")


def _keyword_search(query: str, list_comps_fn: Callable[..., List[Dict[str, Any]]]
                    ) -> Dict[int, float]:
    """语义检索不可用时的关键词兜底。

    把 query 拆成多个词元，分别对 name / category / summary 做模糊匹配，
    命中字段越精确（name 命中）得分越高，返回 {competition_id: score}。

    :param query: 用户原始查询
    :param list_comps_fn: db.list_competitions，注入以便测试替换
    :return: 竞赛 id -> 得分（0~1）
    """
    # 拆词：中英文标点、空白全部当分隔符；过滤单字符与纯符号
    tokens = [t for t in re.split(r"[\s,，、;；/|\+\-_:：()（）【】\[\]]+", query or "")
              if len(t) >= 1 and re.search(r"[0-9A-Za-z\u4e00-\u9fff]", t)]
    # 去掉被其他词元包含的短词（"数学建模" 已含 "建模"，避免重复计分）
    uniq: List[str] = []
    for t in sorted(set(tokens), key=len, reverse=True):
        if not any(t in u for u in uniq):
            uniq.append(t)
    tokens = uniq[:8]
    if not tokens:
        return {}

    # 一次取全量竞赛在内存中打分。
    # 注意：不能调 list_comps_fn(keyword=tok)，因为它的 SQL 是 name LIKE，
    # 返回的行必然 name 命中，会让 category/summary 分级永远失效。
    all_rows = list_comps_fn() or []

    result: Dict[int, float] = {}
    for row in all_rows:
        cid = row.get("id")
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            continue

        name = str(row.get("name") or "")
        category = str(row.get("category") or "")
        summary = str(row.get("summary") or "")

        best = 0.0       # 本竞赛在所有词元上的最高单次命中分
        multi = 0        # 命中的不同词元个数
        for tok in tokens:
            # 命中精度权重：名称全等 > 名称包含 > 类别包含 > 摘要包含
            if tok == name:
                base = 0.95
            elif tok in name:
                base = 0.75
            elif tok in category:
                base = 0.55
            elif tok in summary:
                base = 0.35
            else:
                continue
            multi += 1
            if base > best:
                best = base

        if best <= 0:
            continue
        # 多词命中叠加：每多命中一个词元 +0.05，上限 1.0
        result[cid] = round(min(1.0, best + 0.05 * (multi - 1)), 4)
    return result


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="自然语言检索竞赛",
    description=(
        "首页搜索框入口。把用户的自然语言描述做语义向量召回（retriever.retrieve），"
        "命中获奖作品后按 competition_id 聚集到竞赛级别，返回最相关的竞赛列表。"
        "向量库不可用时自动降级为关键词检索，保证接口始终有结果。"
    ),
)
def search_competitions(req: SearchRequest) -> SearchResponse:
    """自然语言检索竞赛。"""
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=422, detail="query 不能为空")

    # ---- 1. 语义召回：作品级别，多取一些便于聚集到竞赛 ----
    # 每个竞赛可能命中多个作品，因此召回条数放大，避免只覆盖到少数竞赛。
    award_hits: List[Dict[str, Any]] = []
    retrieval_degraded = False
    try:
        # 懒加载：retriever 顶层依赖 chromadb，缺依赖时不应影响整个路由模块导入
        import retriever as _retriever
        award_hits = _retriever.retrieve(
            query, top_k=max(req.top_k * 4, 20), with_debug=True
        )
    except Exception as exc:
        # 向量库/embedding 异常不应让前端白屏，降级为关键词检索
        logger.warning("语义检索失败，降级为关键词检索: %r", exc)
        retrieval_degraded = True
        award_hits = []

    # ---- 2. 上卷到竞赛：按 competition_id 聚合 ----
    scores: Dict[int, float] = {}
    hit_counts: Dict[int, int] = {}
    for hit in award_hits:
        cid = hit.get("competition_id")
        if cid is None:
            continue
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            continue

        # 优先用 final_score（L2 重排分），退化到 score
        raw_score = hit.get("final_score")
        if raw_score is None:
            raw_score = hit.get("score")
        try:
            s = float(raw_score)
        except (TypeError, ValueError):
            s = 0.0

        hit_counts[cid] = hit_counts.get(cid, 0) + 1
        # 同一竞赛取命中作品中的最高分作为代表分
        if cid not in scores or s > scores[cid]:
            scores[cid] = s

    # ---- 3. 兜底：语义检索无结果时走关键词检索 ----
    if not scores:
        try:
            from db import list_competitions as _list_comps
            kw_scores = _keyword_search(query, _list_comps)
            for cid, s in kw_scores.items():
                scores[cid] = s
                hit_counts.setdefault(cid, 0)
            retrieval_degraded = True
        except Exception as exc:
            logger.warning("关键词兜底检索失败: %r", exc)

    # ---- 4. 排序取 top_k ----
    ranked = sorted(
        scores.items(),
        key=lambda kv: (kv[1], hit_counts.get(kv[0], 0)),
        reverse=True,
    )[: req.top_k]

    # ---- 5. 补全竞赛基本信息 ----
    items: List[SearchItem] = []
    for cid, score in ranked:
        try:
            comp = get_competition(cid)
        except Exception as exc:
            logger.warning("查询竞赛 id=%s 失败: %r", cid, exc)
            comp = None
        if not comp:
            continue
        # 复用 Competition 模型做字段规范化，防止 level 等字段类型不符
        try:
            normalized = Competition(**comp)
        except Exception:
            normalized = None

        if normalized is not None:
            items.append(
                SearchItem(
                    id=normalized.id,
                    name=normalized.name,
                    category=normalized.category,
                    level=normalized.level,
                    level_label=normalized.level_label,
                    official_url=normalized.official_url,
                    summary=normalized.summary,
                    short_name=normalized.short_name,
                    score=round(float(score), 4),
                    matched_awards=hit_counts.get(cid, 0),
                )
            )
        else:
            items.append(
                SearchItem(
                    id=int(comp.get("id") or cid),
                    name=str(comp.get("name") or ""),
                    category=comp.get("category"),
                    level=comp.get("level"),
                    level_label=comp.get("level_label"),
                    official_url=comp.get("official_url"),
                    summary=comp.get("summary"),
                    short_name=comp.get("short_name"),
                    score=round(float(score), 4),
                    matched_awards=hit_counts.get(cid, 0),
                )
            )

    logger.info(
        "自然语言检索 query=%r top_k=%s -> %s 条%s",
        query, req.top_k, len(items), "（已降级）" if retrieval_degraded else "",
    )
    return SearchResponse(query=query, total=len(items), items=items)


# ---------------------------------------------------------------------------
# GET /api/ai/quota  查询今日 AI 配额（只读，不扣减）
# ---------------------------------------------------------------------------
@router.get("/quota", summary="查询今日 AI 配额")
def get_quota(quota: Dict[str, Any] = Depends(current_ai_quota)) -> Dict[str, Any]:
    """返回当前用户今日 AI 用量：{used, limit, remaining}。

    remaining 为 None 表示不限次数（AI_DAILY_QUOTA=0）。
    本接口不消耗配额，可在页面加载时安全调用。
    """
    return quota


# ---------------------------------------------------------------------------
# GET /api/ai/inspirations  灵感工作台数据（按赛事强隔离）
# ---------------------------------------------------------------------------
class InspirationInsight(BaseModel):
    """一条赛事洞察。"""

    id: int
    kind: str = Field(..., description="experience / trend / gap")
    content_md: str = Field("", description="Markdown 正文")
    evidence: List[int] = Field(default_factory=list, description="引用作品 ID")
    model: Optional[str] = None
    created_at: Optional[str] = None


class InspirationIdea(BaseModel):
    """一条赛事选题灵感。"""

    id: int
    title: str = ""
    pitch: Optional[str] = None
    detail_md: Optional[str] = None
    novelty: Optional[str] = None
    risk: Optional[str] = None
    ref_awards: List[int] = Field(default_factory=list, description="借鉴作品 ID")
    status: Optional[str] = None
    difficulty: Optional[str] = None
    metrics: Optional[str] = None
    created_at: Optional[str] = None


class InspirationResponse(BaseModel):
    """灵感工作台响应：只包含指定赛事的 insights / ideas。"""

    competition_id: int = Field(..., description="竞赛 ID")
    competition_name: Optional[str] = Field(None, description="竞赛名称")
    work_count: int = Field(0, description="该赛事获奖作品数（用于提示数据基础）")
    has_project_awards: bool = Field(
        True,
        description=(
            "False=纯算法/知识类赛事（只有获奖名单、没有项目作品）："
            "历年洞察/选题均为空，前端展示「去刷题」提示"
        ),
    )
    insights: List[InspirationInsight] = Field(default_factory=list)
    ideas: List[InspirationIdea] = Field(default_factory=list)


def _to_int_list(raw: Any) -> List[int]:
    """evidence / ref_awards（JSON 字符串或 list）-> list[int]，脏数据忽略。"""
    if raw is None:
        return []
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            data = [p for p in re.split(r"[,\s]+", text) if p]
    if isinstance(data, (int, float)):
        data = [data]
    if not isinstance(data, (list, tuple)):
        return []
    out: List[int] = []
    for v in data:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv not in out:
            out.append(iv)
    return out


@router.get(
    "/inspirations",
    response_model=InspirationResponse,
    summary="获取指定赛事的灵感数据",
    description=(
        "必须传 competition_id；只返回 insights / ideas 中 competition_id 与该赛事"
        "完全一致的数据。evidence / ref_awards 还会再做一次归属校验，"
        "任何其它赛事的作品 ID 都会被剔除，绝不做全库混合返回。"
    ),
    responses={
        404: {"description": "竞赛不存在"},
        422: {"description": "缺少或非法的 competition_id"},
    },
)
def get_inspirations(
    competition_id: int = Query(
        ..., gt=0, description="竞赛 ID（必填，严禁不传或传 0）"
    ),
) -> InspirationResponse:
    """按赛事读取灵感工作台数据（严格过滤，接口自身不调用大模型）。"""
    try:
        comp = get_competition(competition_id)
    except Exception as exc:
        logger.error("查询竞赛失败: %r", exc)
        raise HTTPException(status_code=500, detail="数据库查询失败") from exc
    if comp is None:
        raise HTTPException(
            status_code=404, detail=f"竞赛 ID={competition_id} 不存在"
        )

    try:
        works = list_awards_by_competition(competition_id)
        insight_rows = list_insights_by_competition(competition_id)
        idea_rows = list_ideas_by_competition(competition_id)
    except ValueError as exc:  # competition_id 校验失败
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("查询灵感数据失败: %r", exc)
        raise HTTPException(status_code=500, detail="数据库查询失败") from exc

    # 纯算法/知识类赛事：只有获奖名单、没有项目作品，任何历史 insights/ideas
    # 都不具备备赛参考价值，统一返回空列表，前端据此展示「直接去刷题」提示。
    if not comp.get("has_project_awards", 1):
        return InspirationResponse(
            competition_id=competition_id,
            competition_name=comp.get("name"),
            work_count=len(works),
            has_project_awards=False,
            insights=[],
            ideas=[],
        )

    valid_ids = set()
    for w in works:
        try:
            valid_ids.add(int(w["id"]))
        except (KeyError, TypeError, ValueError):
            continue

    insights = [
        InspirationInsight(
            id=int(r["id"]),
            kind=str(r.get("kind") or ""),
            content_md=str(r.get("content_md") or ""),
            evidence=[i for i in _to_int_list(r.get("evidence")) if i in valid_ids],
            model=r.get("model"),
            created_at=r.get("created_at"),
        )
        for r in insight_rows
    ]

    ideas = [
        InspirationIdea(
            id=int(r["id"]),
            title=str(r.get("title") or ""),
            pitch=r.get("pitch"),
            detail_md=r.get("detail_md"),
            novelty=r.get("novelty"),
            risk=r.get("risk"),
            ref_awards=[
                i for i in _to_int_list(r.get("ref_awards")) if i in valid_ids
            ],
            status=r.get("status"),
            difficulty=r.get("difficulty"),
            metrics=r.get("metrics"),
            created_at=r.get("created_at"),
        )
        for r in idea_rows
    ]

    return InspirationResponse(
        competition_id=competition_id,
        competition_name=comp.get("name"),
        work_count=len(works),
        has_project_awards=True,
        insights=insights,
        ideas=ideas,
    )
