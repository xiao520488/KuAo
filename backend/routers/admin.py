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
GET  /api/admin/competitions               正式赛事列表（分页，供「赛事管理」）
DELETE /api/admin/competitions/{id}        删除赛事 + 级联清理 awards/ideas/insights 等

设计要点：
  - 复用 db 层已有的 promote_candidate / update_candidate_status，与 CLI
    (review_candidates.py) 走同一条入库路径，避免两套逻辑分叉；
  - 例外：CLI 里的「封面重新过闸门」需要联网 HEAD 图片，管理后台点击即响应
    优先，这里直接沿用候选采集阶段已通过 image_guard 的 image_url；
  - 通过后与 CLI 一致，自动补全「2 字简称 + 赛氪详情（抓取 + AI 提纯）」。
    两者都是增强数据：失败只记日志并把原因回传给前端，绝不回滚已入库的赛事，
    避免「审批成功但详情永远为空」的静默空壳。
"""

import json
import logging
import re
import unicodedata
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

import db
import enrich_saikr_details as enrich
import short_names
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
# 审核后自动补全：2 字简称 + 赛氪详情（抓取 + AI 提纯）
# ---------------------------------------------------------------------------
# 与 CLI（review_candidates.cmd_approve）保持同一套增强逻辑，避免两条入库路径
# 产物不一致 —— 这是「通过审核后变成空壳」的根因所在。
# 扩展性数据缺失不算入库失败：异常只记 warning 并在响应里回传原因，
# 前端据此回退到名称首字 / 无图兜底，而不是静默留空。
def _candidate_contest_id(candidate: dict):
    """从 saikr 候选里取赛氪 contest_id；其它来源返回 None（走名称相似度兜底）。"""
    if str(candidate.get("source") or "").strip() != "saikr":
        return None

    match = re.search(r"(\d+)\s*$", str(candidate.get("source_key") or ""))
    if match:
        return int(match.group(1))

    raw = candidate.get("raw_json")
    if not raw:
        return None
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        contest_id = int(payload.get("contest_id") or 0)
    except (TypeError, ValueError):
        return None
    return contest_id if contest_id > 0 else None


def _auto_complete_after_approve(competition_id: int, name: str,
                                 candidate: dict) -> dict:
    """入库后补全简称与详情；返回结构化结果供接口回传（永不抛异常）。"""
    result = {"short_name": None, "short_name_error": None, "enrich": None}

    # 1) 2 字简称：失败时前端回退名称首字
    try:
        short_name = short_names.extract_short_name(name)
        db.save_short_name(competition_id, short_name)
        result["short_name"] = short_name
        logger.info("[审核补全] competition=%s 简称=%s", competition_id, short_name)
    except Exception as exc:  # noqa: BLE001 - 简称失败不影响已入库赛事
        result["short_name_error"] = str(exc)
        logger.warning(
            "[审核补全] competition=%s 简称提取失败（前端回退首字）：%s",
            competition_id, exc,
        )

    # 2) 赛氪详情抓取 + AI 提纯：只填空字段，不覆盖已有值
    #    名录类候选没有 contest_id，且 source_url 是共用的目录页 —— 严禁拿它当锚点，
    #    交给 enrich 内部的「名称 -> 本地赛氪索引」匹配（allow_name_match）。
    contest_id = _candidate_contest_id(candidate)
    logger.info(
        "[审核补全] competition=%s 开始抓取赛氪详情（contest_id=%s，约 20~60 秒）",
        competition_id, contest_id or "待按名称匹配",
    )
    try:
        enriched = enrich.enrich_competition(
            db.DB_PATH, competition_id, contest_id=contest_id,
            allow_stale=False, allow_name_match=True,
        )
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - 详情失败不影响主流程
        result["enrich"] = {"status": "failed", "error": str(exc)}
        logger.warning(
            "[审核补全] competition=%s 详情抓取失败（字段保持 NULL，赛事仍在库）：%s",
            competition_id, exc,
        )
        return result

    result["enrich"] = {
        key: enriched.get(key)
        for key in ("status", "contest_id", "written", "reason", "extra")
        if enriched.get(key) is not None
    }
    if enriched.get("status") == "updated":
        labels = [
            enrich.FIELD_LABELS.get(field, field)
            for field in enriched.get("written") or []
        ]
        logger.info(
            "[审核补全] competition=%s 详情已写入 %d 项：%s",
            competition_id, len(labels), "、".join(labels),
        )
    else:
        logger.info(
            "[审核补全] competition=%s 详情未写入（status=%s%s）",
            competition_id, enriched.get("status"),
            "：" + str(enriched["reason"]) if enriched.get("reason") else "",
        )
    return result


def _auto_complete_summary(result: dict) -> str:
    """把补全结果压成一句人话，回传给管理后台的 toast。"""
    parts = []
    if result.get("short_name"):
        parts.append("简称=%s" % result["short_name"])
    else:
        parts.append("简称未生成（回退首字）")

    enriched = result.get("enrich") or {}
    status = enriched.get("status")
    if status == "updated":
        detail = "详情补全 %d 项" % len(enriched.get("written") or [])
        extra = enriched.get("extra") or {}
        bonus = []
        if extra.get("dates"):
            bonus.append("日期 %d 项" % len(extra["dates"]))
        if extra.get("cover"):
            bonus.append("封面")
        if bonus:
            detail += "（另补%s）" % "、".join(bonus)
        parts.append(detail)
    elif status == "complete":
        parts.append("详情本就完整")
    elif status == "failed":
        parts.append("详情抓取失败，已入库待补")
    elif status:
        reason = str(enriched.get("reason") or status)
        parts.append("详情未补（%s），已入库待补" % reason[:40])
    else:
        parts.append("详情未补，已入库待补")
    return "；".join(parts)


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
    """把候选提拔进 competitions（同一事务标记 approved），随后自动补全简称与详情。

    补全失败不回滚、不抛 500：赛事已在库，缺失字段回退前端兜底，
    失败原因写日志并在响应的 `short_name` / `enrich` 里回传。
    """
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

    # 入库成功后立即补全简称与详情（同步执行，与 CLI 同口径）。
    # 该路由是 def（FastAPI 会放进线程池），阻塞期间不影响其他请求。
    completion = _auto_complete_after_approve(
        competition_id, data.get("name") or "", candidate
    )
    summary = _auto_complete_summary(completion)

    logger.info(
        "管理后台通过候选 candidate_id=%s -> competition_id=%s（%s）",
        candidate_id, competition_id, summary,
    )
    return {
        "code": 0,
        "candidate_id": candidate_id,
        "competition_id": competition_id,
        "status": "approved",
        "short_name": completion.get("short_name"),
        "enrich": completion.get("enrich"),
        "message": "已通过并入库：%s（%s）" % (data["name"], summary),
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
# 赛事管理（正式赛事库）
# ---------------------------------------------------------------------------
@router.get(
    "/competitions",
    summary="正式赛事列表（后台赛事管理）",
    responses={
        401: {"description": "未登录"},
        403: {"description": "需要管理员权限"},
    },
)
def list_competitions_admin(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    keyword: str = Query("", max_length=50),
) -> dict:
    """按 id 倒序分页返回主表比赛，供后台「赛事管理」列表使用。"""
    data = db.list_competitions_for_admin(
        limit=limit, offset=offset, keyword=(keyword or "").strip() or None
    )
    return {
        "code": 0,
        "total": data["total"],
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": row["id"],
                "name": row["name"],
                "short_name": row.get("short_name"),
                "level_label": row.get("level_label") or row.get("level"),
                "category": row.get("category"),
                "source": row.get("discover_source"),
                "official_url": row.get("official_url"),
                "image_url": row.get("image_url"),
                "is_active": bool(row.get("is_active")),
                "awards_count": row.get("awards_count") or 0,
                "created_at": row.get("created_at"),
            }
            for row in data["items"]
        ],
    }


@router.delete(
    "/competitions/{competition_id}",
    summary="删除赛事（级联清理关联数据）",
    responses={
        401: {"description": "未登录"},
        403: {"description": "需要管理员权限"},
        404: {"description": "赛事不存在"},
    },
)
def delete_competition(competition_id: int) -> dict:
    """永久删除一条赛事，并清理 awards / ideas / insights / 备赛计划 / AI 任务。

    单事务执行，任一步失败整体回滚；候选表的 competition_id 置 NULL 保留审核痕迹。
    """
    result = db.delete_competition_cascade(competition_id)
    if result is None:
        raise HTTPException(status_code=404, detail="赛事不存在")

    logger.info("[管理员操作] 删除了赛事：%s", result["name"])
    deleted = result.get("deleted") or {}
    detail = "、".join("%s %d" % (table, count) for table, count in deleted.items())
    return {
        "code": 0,
        "id": competition_id,
        "name": result["name"],
        "deleted": deleted,
        "candidates_unlinked": result.get("candidates_unlinked", 0),
        "message": "已删除《%s》%s" % (result["name"], "（连带清理 %s）" % detail if detail else ""),
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
