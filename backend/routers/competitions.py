"""竞赛信息相关路由。

数据来源：db 层（SQLite 真实数据）。

提供：
  * GET /api/competitions        竞赛列表（支持筛选）
  * GET /api/competitions/{id}   竞赛详情
  * GET /api/categories          分类列表
"""

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Path, Query

from db import get_categories as db_get_categories
from db import get_competition as db_get_competition
from db import list_awards_by_competition as db_list_awards_by_competition
from db import list_competitions as db_list_competitions
from models.schemas import (
    Award,
    AwardList,
    CategoryList,
    Competition,
    CompetitionDetail,
)

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
    （如 discover_source / last_checked_at / is_active），
    避免未来 DB 加列时污染响应结构。
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
        official_url=row.get("official_url"),
        intro=row.get("intro"),
        summary=row.get("summary"),
        tech_stack=_parse_tech_stack(row.get("tech_stack")),
        full_process_guide=row.get("full_process_guide") or "",
        sort_order=row.get("sort_order"),
        created_at=row.get("created_at"),
    )


def _row_to_detail(row: Dict[str, Any]) -> CompetitionDetail:
    """把 db 返回的一行 dict 转换为 CompetitionDetail 模型（详情）。

    详情继承列表全部字段（含 summary），字段映射与列表保持一致。
    """
    base = _row_to_competition(row)
    return CompetitionDetail(**base.model_dump())


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
) -> List[Competition]:
    """获取竞赛列表（真实数据）。"""
    rows = db_list_competitions(category=category, month=month, keyword=keyword)
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
      * 按标题归一化去重（保留“改进版”等信息，不合并近似标题）
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
        "已排除被拒绝的脏数据，并按标题去重（近似标题如“…（改进版）”予以保留）。"
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
