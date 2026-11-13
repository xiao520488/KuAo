"""竞赛信息相关路由。

数据来源：db 层（SQLite 真实数据）。

提供：
  * GET  /api/competitions                       竞赛列表（支持筛选，按报名紧迫度智能排序）
  * GET  /api/competitions/hot                   首页热门（国家级 + 报名中/未开始，前 8 条）
  * GET  /api/competitions/{id}                  竞赛详情（含攻略全文）
  * GET  /api/competitions/{id}/awards           获奖作品
  * POST /api/competitions/{id}/generate_guide   按需生成攻略（登录 + 异步）
  * GET  /api/competitions/{id}/generate_guide   查询攻略生成状态
  * POST /api/competitions/{id}/generate_inspirations  按需生成专属灵感（登录 + 异步）
  * GET  /api/competitions/{id}/generate_inspirations  查询灵感生成状态
  * GET  /api/categories                         分类列表

注意（2026-11-08）：full_process_guide 只在详情接口返回，列表接口不再带长文。
"""

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

import ai_jobs_runner
import stage_schedule
from db import get_categories as db_get_categories
from db import get_competition as db_get_competition
from db import get_guide_state as db_get_guide_state
from db import list_awards_by_competition as db_list_awards_by_competition
from db import list_competitions as db_list_competitions
from db import list_hot_competitions as db_list_hot_competitions
from models.schemas import (
    Award,
    AwardList,
    CategoryList,
    Competition,
    CompetitionDetail,
)
from security import get_current_user

router = APIRouter(tags=["competitions"])


# ---------------------------------------------------------------------------
# 数据转换工具
# ---------------------------------------------------------------------------
def _parse_tech_stack(raw: Any) -> List[str]:
    """把 db 中的 tech_stack 统一解析为 list[str]。

    数据库中该字段以 JSON 字符串存储（如 '["Python","MATLAB"]'），
    但也兼容已是 list 的情况，解析失败时降级为空列表。
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


def _row_to_competition(row: Dict[str, Any]) -> Competition:
    """把 db 返回的一行 dict 转换为 Competition 模型（列表项）。

    使用显式字段映射，忽略数据库中的额外字段
    （如 discover_source / last_checked_at / is_active / full_process_guide），
    避免未来 DB 加列时污染响应结构。

    2026-11-08：full_process_guide 已从列表模型移除（改由详情返回），
    否则 30+ 条攻略会让列表响应多出上百 KB。
    """
    return Competition(
        id=row["id"],
        name=row["name"],
        category=row.get("category"),
        level=row.get("level") or "national",
        level_label=row.get("level_label"),
        reg_start_month=row.get("reg_start_month"),
        reg_end_month=row.get("reg_end_month"),
        reg_cross_year=bool(row.get("reg_cross_year")),
        contest_start_month=row.get("contest_start_month"),
        contest_end_month=row.get("contest_end_month"),
        # 双轨制（2026-11-09）：精确日期，可为空；为空时前端降级用月份
        reg_start_date=row.get("reg_start_date"),
        reg_end_date=row.get("reg_end_date"),
        contest_start_date=row.get("contest_start_date"),
        contest_end_date=row.get("contest_end_date"),
        official_url=row.get("official_url"),
        image=row.get("image_url") or row.get("image"),
        short_name=row.get("short_name"),
        intro=row.get("intro"),
        summary=row.get("summary"),
        tech_stack=_parse_tech_stack(row.get("tech_stack")),
        sort_order=row.get("sort_order"),
        created_at=row.get("created_at"),
    )


def _row_to_detail(row: Dict[str, Any]) -> CompetitionDetail:
    """把 db 返回的一行 dict 转换为 CompetitionDetail 模型（详情）。

    详情在列表字段基础上补三样：
      * full_process_guide：攻略 Markdown 全文；
      * guide_status：idle/queued/running/ready/failed（前端按钮与轮询用）；
      * guide_generated_at：生成时间，便于提示“最近更新”。
    """
    base = _row_to_competition(row)
    state = db_get_guide_state(row["id"]) or {}
    return CompetitionDetail(
        **base.model_dump(),
        full_process_guide=row.get("full_process_guide") or "",
        guide_status=state.get("status", "idle"),
        guide_generated_at=row.get("guide_generated_at"),
        participants=row.get("participants") or "",
        content_scope=row.get("content_scope") or "",
        exam_schedule=row.get("exam_schedule") or "",
        stage_schedule=stage_schedule.loads(row.get("stage_schedule")),
    )


# ---------------------------------------------------------------------------
# GET /api/competitions  竞赛列表
# ---------------------------------------------------------------------------
@router.get(
    "/api/competitions",
    response_model=List[Competition],
    summary="获取竞赛列表",
    description=(
        "从数据库读取竞赛信息列表。"
        "支持按 category（类别模糊匹配）、month（报名月份）、"
        "keyword（名称模糊匹配）可选筛选。"
    ),
)
def list_competitions(
    category: Optional[str] = Query(None, description="竞赛类别，模糊匹配"),
    month: Optional[int] = Query(None, ge=1, le=12, description="报名包含该月份的竞赛"),
    keyword: Optional[str] = Query(None, description="竞赛名称关键词，模糊匹配"),
    limit: Optional[int] = Query(None, ge=1, le=500,
                                 description="最多返回条数；省略则返回全部"),
) -> List[Competition]:
    """获取竞赛列表（真实数据）。"""
    rows = db_list_competitions(category=category, month=month, keyword=keyword,
                                limit=limit)
    return [_row_to_competition(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/competitions/hot  首页热门（冷启动口径）
# 注意：必须声明在 /api/competitions/{id} 之前，否则 "hot" 会被当成 id 解析成 422
# ---------------------------------------------------------------------------
@router.get(
    "/api/competitions/hot",
    response_model=List[Competition],
    summary="获取首页热门竞赛",
    description=(
        "首页专属接口（冷启动阶段无用户行为数据时的「热门」口径）：\n\n"
        "* 只保留级别为「国家级」的赛事；\n"
        "* 只保留当前正在报名（今天在报名起止日之间）或尚未开始的赛事，"
        "已截止与无日期的直接排除；\n"
        "* 排序：正在报名的按报名截止日期升序（越早截止越靠前），"
        "其次是尚未开始的按开报日期升序；\n"
        "* 默认取前 8 条。\n\n"
        "全部由 SQL 基于服务器当前时间实时计算，刷新即反映最新截止状态。"
    ),
)
def list_hot_competitions(
    limit: int = Query(8, ge=1, le=50, description="返回条数，默认 8"),
) -> List[Competition]:
    """首页热门竞赛（国家级 + 报名中/未开始，按紧迫度排序）。"""
    rows = db_list_hot_competitions(limit=limit)
    return [_row_to_competition(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/competitions/{id}  竞赛详情
# ---------------------------------------------------------------------------
@router.get(
    "/api/competitions/{id}",
    response_model=CompetitionDetail,
    summary="获取竞赛详情",
    description="按 ID 获取单条竞赛的完整详情（含 summary 摘要等全部字段）。",
    responses={404: {"description": "竞赛不存在"}},
)
def get_competition(
    id: int = Path(..., description="竞赛 ID"),
) -> CompetitionDetail:
    """获取单条竞赛详情。"""
    row = db_get_competition(id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"竞赛 ID={id} 不存在")
    return _row_to_detail(row)


# ---------------------------------------------------------------------------
# 按需生成攻略（2026-11-08）
# ---------------------------------------------------------------------------
class GuideStatusResponse(BaseModel):
    """攻略生成状态查询的响应体。"""

    competition_id: int = Field(..., description="竞赛 ID")
    status: str = Field(
        ..., description="idle / queued / running / ready / failed"
    )
    job_id: Optional[int] = Field(None, description="最近一次 ai_jobs.id")
    attempts: int = Field(0, description="该任务已尝试次数")
    guide_length: int = Field(0, description="已落库攻略的字符数（未生成为 0）")
    generated_at: Optional[str] = Field(None, description="攻略生成时间")


class GuideRequestResponse(BaseModel):
    """POST 生成攻略的响应体（状态码随场景变化）。"""

    competition_id: int = Field(..., description="竞赛 ID")
    status: str = Field(
        ...,
        description=(
            "ready=已有攻略；queued=已入队；running=已在执行；"
            "throttled=被预算/队列限流"
        ),
    )
    job_id: Optional[int] = Field(None, description="入队后的任务 id，用于轮询")
    guide_length: Optional[int] = Field(None, description="已有攻略的字符数")
    reason: Optional[str] = Field(
        None, description="限流原因：daily_budget / queue_full / cooldown"
    )
    poll_after_ms: int = Field(
        0, description="建议前端轮询间隔（毫秒）；0 表示无需轮询"
    )


@router.post(
    "/api/competitions/{id}/generate_guide",
    response_model=GuideRequestResponse,
    status_code=202,
    summary="按需生成参赛攻略（登录 + 异步）",
    description=(
        "登录用户触发。已有攻略直接返回 200；否则写入 ai_jobs 后立即返回 202，"
        "由后台线程池串行生成（单条 20–60s）。前端用 GET 同路径轮询状态。\n\n"
        "幂等：同一赛事已有 queued/running 任务时返回同一个 job_id，不会重复生成。\n"
        "限流：全站每日任务上限 / 队列上限 / 失败冷却，命中时返回 429 或 409。"
    ),
    responses={
        401: {"description": "未登录或 Token 失效"},
        404: {"description": "竞赛不存在"},
        409: {"description": "该赛事刚失败过，冷却中"},
        429: {"description": "超出每日预算或队列已满"},
    },
)
def generate_guide(
    response: Response,
    id: int = Path(..., description="竞赛 ID"),
    user: dict = Depends(get_current_user),
) -> GuideRequestResponse:
    """触发一次攻略生成（幂等、异步）。"""
    try:
        result = ai_jobs_runner.request_guide(
            id,
            user_id=int(user["id"]),
            request_ip=None,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    payload = dict(result["payload"])
    payload.setdefault("poll_after_ms", result.get("poll_after_ms", 0))
    response.status_code = int(result["http"])
    retry_after = result.get("retry_after_sec")
    if retry_after:
        response.headers["Retry-After"] = str(int(retry_after))
    return GuideRequestResponse(**payload)


@router.get(
    "/api/competitions/{id}/generate_guide",
    response_model=GuideStatusResponse,
    summary="查询攻略生成状态（登录）",
    description="前端轮询用；只读，不触发任何生成，也不消耗配额。",
    responses={401: {"description": "未登录或 Token 失效"}, 404: {"description": "竞赛不存在"}},
)
def get_generate_guide_status(
    id: int = Path(..., description="竞赛 ID"),
    _user: dict = Depends(get_current_user),
) -> GuideStatusResponse:
    """返回攻略生成状态（与详情接口的 guide_status 同源）。"""
    if db_get_competition(id) is None:
        raise HTTPException(status_code=404, detail=f"竞赛 ID={id} 不存在")
    return GuideStatusResponse(**ai_jobs_runner.get_guide_status(id))


# ---------------------------------------------------------------------------
# 按需生成灵感（2026-11-13）
# ---------------------------------------------------------------------------
class InspirationStatusResponse(BaseModel):
    """灵感生成状态查询的响应体。"""

    competition_id: int = Field(..., description="竞赛 ID")
    status: str = Field(
        ..., description="idle / queued / running / ready / failed"
    )
    job_id: Optional[int] = Field(None, description="最近一次 ai_jobs.id")
    attempts: int = Field(0, description="该任务已尝试次数")
    awards_count: int = Field(0, description="该赛事获奖作品数（素材基础）")
    insights_count: int = Field(0, description="已生成的洞察条数")
    ideas_count: int = Field(0, description="已生成的选题灵感条数")
    error: Optional[str] = Field(None, description="最近一次失败原因")
    generated_at: Optional[str] = Field(None, description="最近一次生成时间")


class InspirationRequestResponse(BaseModel):
    """POST 生成灵感的响应体（状态码随场景变化）。"""

    competition_id: int = Field(..., description="竞赛 ID")
    status: str = Field(
        ...,
        description=(
            "ready=已生成；queued=已入队；running=已在执行；"
            "no_awards=无获奖作品不可生成；throttled=被限流"
        ),
    )
    job_id: Optional[int] = Field(None, description="入队后的任务 id，用于轮询")
    awards_count: Optional[int] = Field(None, description="获奖作品数")
    insights_count: Optional[int] = Field(None, description="已有洞察条数")
    ideas_count: Optional[int] = Field(None, description="已有选题条数")
    reason: Optional[str] = Field(
        None, description="拒绝原因：no_awards / daily_budget / queue_full / cooldown"
    )
    poll_after_ms: int = Field(
        0, description="建议前端轮询间隔（毫秒）；0 表示无需轮询"
    )


@router.post(
    "/api/competitions/{id}/generate_inspirations",
    response_model=InspirationRequestResponse,
    status_code=202,
    summary="按需生成专属灵感（登录 + 异步）",
    description=(
        "登录用户触发。灵感全部从该赛事的获奖作品提炼，因此没有获奖作品时"
        "直接 409（reason=no_awards），不会白烧额度。\n\n"
        "已有 insights/ideas 返回 200；否则写 ai_jobs 后立即 202，由后台线程池"
        "串行生成（约 60–180s）。前端用 GET 同路径轮询状态。\n\n"
        "幂等：同一赛事已有 queued/running 任务时返回同一个 job_id。\n"
        "限流：全站每日上限 / 队列上限 / 失败冷却，命中时返回 429 或 409。"
    ),
    responses={
        401: {"description": "未登录或 Token 失效"},
        404: {"description": "竞赛不存在"},
        409: {"description": "无获奖作品（no_awards）或刚失败冷却中"},
        429: {"description": "超出每日预算或队列已满"},
    },
)
def generate_inspirations(
    response: Response,
    id: int = Path(..., description="竞赛 ID"),
    user: dict = Depends(get_current_user),
) -> InspirationRequestResponse:
    """触发一次灵感生成（幂等、异步、无素材即拒绝）。"""
    try:
        result = ai_jobs_runner.request_inspiration(
            id,
            user_id=int(user["id"]),
            request_ip=None,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    payload = dict(result["payload"])
    payload.setdefault("poll_after_ms", result.get("poll_after_ms", 0))
    response.status_code = int(result["http"])
    retry_after = result.get("retry_after_sec")
    if retry_after:
        response.headers["Retry-After"] = str(int(retry_after))
    return InspirationRequestResponse(**payload)


@router.get(
    "/api/competitions/{id}/generate_inspirations",
    response_model=InspirationStatusResponse,
    summary="查询灵感生成状态（登录）",
    description="前端轮询用；只读，不触发任何生成，也不消耗配额。",
    responses={
        401: {"description": "未登录或 Token 失效"},
        404: {"description": "竞赛不存在"},
    },
)
def get_generate_inspirations_status(
    id: int = Path(..., description="竞赛 ID"),
    _user: dict = Depends(get_current_user),
) -> InspirationStatusResponse:
    """返回灵感生成状态（含 awards_count，供前端防呆）。"""
    if db_get_competition(id) is None:
        raise HTTPException(status_code=404, detail=f"竞赛 ID={id} 不存在")
    return InspirationStatusResponse(**ai_jobs_runner.get_inspiration_status(id))


# ---------------------------------------------------------------------------
# GET /api/categories  分类列表
# ---------------------------------------------------------------------------
@router.get(
    "/api/categories",
    response_model=CategoryList,
    summary="获取竞赛分类列表",
    description="从数据库提取所有不重复、非空的竞赛分类名称。",
)
def get_categories() -> CategoryList:
    """获取去重后的竞赛分类列表。"""
    categories = db_get_categories()
    return CategoryList(categories=categories, total=len(categories))


# ---------------------------------------------------------------------------
# GET /api/competitions/{id}/awards  获奖作品列表
# ---------------------------------------------------------------------------
def _row_to_award(row: Dict[str, Any]) -> Award:
    """把 db 返回的一行 awards dict 转换为 Award 模型。

    db 层（list_awards_by_competition）已完成：
      * 过滤 review_status='rejected'
      * tech_stack 的 JSON 字符串解析
    这里只做字段映射与类型兜底。
    """
    return Award(
        id=row["id"],
        title=row.get("title"),
        summary=row.get("summary"),
        highlights=row.get("highlights"),
        team=row.get("team"),
        track=row.get("track"),
        year=row.get("year"),
        session=row.get("session"),
        award_grade=row.get("award_grade"),
        level=row.get("level"),
        tech_stack=_parse_tech_stack(row.get("tech_stack")),
        origin_url=(row.get("source_url") or "").strip() or None,
    )


@router.get(
    "/api/competitions/{id}/awards",
    response_model=AwardList,
    summary="获取某竞赛的获奖作品列表",
    description=(
        "按竞赛 ID 返回数据库中的真实获奖作品（Tab 2 往年获奖作品数据源）。"
        "已排除被拒绝的脏数据。"
    ),
    responses={404: {"description": "竞赛不存在"}},
)
def list_competition_awards(
    id: int = Path(..., description="竞赛 ID"),
    limit: int = Query(20, ge=1, le=100, description="最多返回条数"),
) -> AwardList:
    """获取指定竞赛的获奖作品列表。"""
    if db_get_competition(id) is None:
        raise HTTPException(status_code=404, detail=f"竞赛 ID={id} 不存在")

    rows = db_list_awards_by_competition(id, limit=limit)
    items = [_row_to_award(r) for r in rows]
    return AwardList(competition_id=id, total=len(items), items=items)
