# -*- coding: utf-8 -*-
"""管理后台接口 (routers/admin.py)

所有接口统一挂载 `get_current_admin` 依赖：
  * 未登录 / Token 失效 -> 401
  * 已登录但不是管理员   -> 403

Routes
------
GET  /api/admin/candidates                 待审候选（status='pending'）
POST /api/admin/candidates/{id}/approve    通过：候选 -> competitions，标记 approved
POST /api/admin/candidates/{id}/reject     拒绝：标记 rejected，不动主表
GET  /api/admin/feedback                   全部用户反馈（按时间倒序）
PUT  /api/admin/feedback/{id}/read         标记单条反馈已读

设计要点：
  - 复用 db 层已有的 promote_candidate / update_candidate_status，与 CLI
    (review_candidates.py) 走同一条入库路径，避免两套逻辑分叉；
  - 例外：CLI 里的「封面重新过闸门」需要联网 HEAD 图片，管理后台点击即响应
    优先，这里直接沿用候选采集阶段已通过 image_guard 的 image_url；
  - 通过后不自动生成简称 / 不自动抓详情（CLI 的慢操作），需要时跑脚本补。
"""

import logging
import re
import unicodedata
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

import db
from security import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_admin)],
)

# 新入库赛事默认排序值：与 review_candidates.py 一致，首页不曝光
DEFAULT_SORT_ORDER = 9999


# ---------------------------------------------------------------------------
# 内部工具：候选 -> competitions 的数据组装
# ---------------------------------------------------------------------------
def _clean_name(raw_name) -> str:
    """候选名 -> 主表展示名：去营销标签 / 年份 / 届次，保留官方全称。"""
    text = unicodedata.normalize("NFKC", str(raw_name or "")).strip()
    text = re.sub(r"^(?:【[^】]*】|\[[^\]]*\])\s*", "", text)
    text = re.sub(r"\s*(?:【[^】]*】|\[[^\]]*\])\s*$", "", text)
    text = re.sub(r"^(?:19|20)\d{2}\s*年?\s*", "", text)
    text = re.sub(r"^第[一二三四五六七八九十百零\d]+届\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _infer_level(candidate: dict) -> tuple:
    """返回 (level, level_label)；现有枚举只有 national / provincial。"""
    label = str(candidate.get("level_label") or "").strip()
    text = label + " " + str(candidate.get("raw_name") or "")
    provincial_words = (
        "省级", "省赛", "区域级", "区域赛", "赛区", "分赛区",
        "华南", "华东", "华北", "华中", "西南", "西北", "东北",
    )
    if any(word in text for word in provincial_words):
        return "provincial", label or "省级"
    return "national", label or "国家级"


def _build_competition_data(candidate: dict) -> dict:
    """组装 promote_candidate 需要的字段（与 review_candidates 同口径）。"""
    level, level_label = _infer_level(candidate)
    source = str(candidate.get("source") or "").strip()
    if source == "saikr":
        discover_source = "saikr:list"
    elif source == "edu-roster":
        discover_source = "edu:roster"
    else:
        discover_source = source or None
    return {
        "name": _clean_name(candidate.get("raw_name")),
        "category": candidate.get("category_hint") or "待确认",
        "reg_start_month": candidate.get("reg_start_month"),
        "reg_end_month": candidate.get("reg_end_month"),
        "reg_cross_year": candidate.get("reg_cross_year") or 0,
        "official_url": candidate.get("official_url"),
        "image_url": (candidate.get("image_url") or "").strip() or None,
        "contest_start_month": candidate.get("contest_start_month"),
        "contest_end_month": candidate.get("contest_end_month"),
        # 双轨制：候选里若有精确日期一并带进正式库，避免审核通过时丢掉
        "reg_start_date": candidate.get("reg_start_date"),
        "reg_end_date": candidate.get("reg_end_date"),
        "contest_start_date": candidate.get("contest_start_date"),
        "contest_end_date": candidate.get("contest_end_date"),
        "discover_source": discover_source,
        "last_checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_active": 1,
        "level": level,
        "level_label": level_label,
        "sort_order": DEFAULT_SORT_ORDER,
        "tech_stack": "[]",
    }


def _candidate_brief(row: dict) -> dict:
    """候选列表返回字段（不暴露 raw_json，避免响应过大）。"""
    return {
        "id": row.get("id"),
        "name": _clean_name(row.get("raw_name")),
        "raw_name": row.get("raw_name"),
        "level_label": row.get("level_label") or _infer_level(row)[1],
        "source": row.get("source"),
        "source_url": row.get("source_url") or row.get("official_url"),
        "official_url": row.get("official_url"),
        "image_url": row.get("image_url"),
        "reg_start_month": row.get("reg_start_month"),
        "reg_end_month": row.get("reg_end_month"),
        "reg_start_date": row.get("reg_start_date"),
        "reg_end_date": row.get("reg_end_date"),
        "contest_start_date": row.get("contest_start_date"),
        "contest_end_date": row.get("contest_end_date"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
    }


# ---------------------------------------------------------------------------
# 候选赛事
# ---------------------------------------------------------------------------
@router.get(
    "/candidates",
    summary="待审候选赛事列表",
    responses={
        401: {"description": "未登录"},
        403: {"description": "需要管理员权限"},
    },
)
def list_pending_candidates(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """按 id 倒序返回 status='pending' 的候选赛事。"""
    rows = db.query_candidates(status="pending", limit=limit, offset=offset)
    return {
        "code": 0,
        "total": db.count_candidates(status="pending"),
        "items": [_candidate_brief(row) for row in rows],
    }


@router.post(
    "/candidates/{candidate_id}/approve",
    summary="通过候选（转入正式赛事）",
    responses={
        400: {"description": "候选状态不是 pending"},
        401: {"description": "未登录"},
        403: {"description": "需要管理员权限"},
        404: {"description": "候选不存在"},
    },
)
def approve_candidate(candidate_id: int) -> dict:
    """把候选提拔进 competitions，并在同一事务里标记 approved。"""
    candidate = db.get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="候选不存在")
    if candidate.get("status") != "pending":
        raise HTTPException(
            status_code=400,
            detail="候选当前状态为 %s，只有 pending 可以审核" % candidate.get("status"),
        )

    data = _build_competition_data(candidate)
    try:
        competition_id = db.promote_candidate(candidate_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "管理后台通过候选 candidate_id=%s -> competition_id=%s",
        candidate_id, competition_id,
    )
    return {
        "code": 0,
        "candidate_id": candidate_id,
        "competition_id": competition_id,
        "status": "approved",
        "message": "已通过并入库：%s" % data["name"],
    }


@router.post(
    "/candidates/{candidate_id}/reject",
    summary="拒绝候选",
    responses={
        400: {"description": "候选状态不是 pending"},
        401: {"description": "未登录"},
        403: {"description": "需要管理员权限"},
        404: {"description": "候选不存在"},
    },
)
def reject_candidate(candidate_id: int) -> dict:
    """标记候选为 rejected，主表不动。"""
    candidate = db.get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="候选不存在")
    if candidate.get("status") != "pending":
        raise HTTPException(
            status_code=400,
            detail="候选当前状态为 %s，只有 pending 可以审核" % candidate.get("status"),
        )

    db.update_candidate_status(candidate_id, "rejected")
    logger.info("管理后台拒绝候选 candidate_id=%s", candidate_id)
    return {
        "code": 0,
        "candidate_id": candidate_id,
        "status": "rejected",
        "message": "已拒绝",
    }


# ---------------------------------------------------------------------------
# 用户反馈
# ---------------------------------------------------------------------------
@router.get(
    "/feedback",
    summary="全部用户反馈",
    responses={
        401: {"description": "未登录"},
        403: {"description": "需要管理员权限"},
    },
)
def list_feedback(
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """按时间倒序返回所有反馈，附带提交人用户名与已读状态。"""
    items = db.list_feedbacks(limit=limit, offset=offset)
    return {
        "code": 0,
        "items": [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "username": row["username"],
                "content": row["content"],
                "created_at": row["created_at"],
                "is_read": bool(row["is_read"]),
            }
            for row in items
        ],
        "unread": db.count_unread_feedbacks(),
    }


@router.put(
    "/feedback/{feedback_id}/read",
    summary="标记反馈已读",
    responses={
        401: {"description": "未登录"},
        403: {"description": "需要管理员权限"},
        404: {"description": "反馈不存在"},
    },
)
def mark_feedback_read(feedback_id: int) -> dict:
    """把单条反馈置为已读（幂等）。"""
    if not db.mark_feedback_read(feedback_id, 1):
        raise HTTPException(status_code=404, detail="反馈不存在")
    return {"code": 0, "id": feedback_id, "is_read": True}
