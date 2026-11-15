"""更新日志接口 (routers/changelogs.py)

Routes
------
GET    /api/changelogs              —— 公开：按发布日期倒序返回全部更新日志
POST   /api/admin/changelogs        —— 管理员：发布一条新日志
PUT    /api/admin/changelogs/{id}   —— 管理员：修改一条日志
DELETE /api/admin/changelogs/{id}   —— 管理员：删除一条日志

设计要点
--------
  - 前台「更新日志」弹窗只读 GET，未登录也能看，不消耗任何 AI 配额；
  - 三个写接口都挂 get_current_admin：未登录 401、非管理员 403，权限在服务端；
  - 版本号在库里有唯一索引，重复发布 / 改成已存在的版本号返回 409；
  - 改 / 删不存在的 id 返回 404；
  - 正文按原始 Markdown 存库，渲染交给前端 marked + DOMPurify，
    服务端不做任何 HTML 净化假设（避免前端重复转义）。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import db
from security import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["changelogs"])

# 单条日志正文长度上限（字符数）：足够写一整版更新说明，又能防灌库
MAX_CONTENT_LENGTH = 20000


class ChangelogRequest(BaseModel):
    """发布更新日志的请求体。"""

    version: str = Field(
        ...,
        min_length=1,
        max_length=40,
        description="版本号，如 v1.0.0（唯一）",
    )
    release_date: str = Field(
        ...,
        min_length=10,
        max_length=10,
        description="发布日期，格式 YYYY-MM-DD",
    )
    content_markdown: str = Field(
        ...,
        min_length=1,
        max_length=MAX_CONTENT_LENGTH,
        description="Markdown 正文",
    )


def _serialize(row: dict) -> dict:
    """统一出参：字段名与前端约定保持一致。"""
    return {
        "id": row.get("id"),
        "version": row.get("version") or "",
        "release_date": row.get("release_date") or "",
        "content_markdown": row.get("content_markdown") or "",
        "created_at": row.get("created_at") or "",
    }


@router.get(
    "/changelogs",
    summary="获取更新日志列表",
    description="按发布日期倒序返回全部更新日志（公开接口，无需登录）。",
)
def get_changelogs(
    limit: int = Query(50, ge=1, le=200, description="返回条数上限"),
) -> dict:
    """返回更新日志列表：{code, total, items}。"""
    try:
        rows = db.list_changelogs(limit=limit)
    except Exception as exc:  # 读库失败不应把弹窗搞崩，返回空列表 + 500 文案
        logger.exception("读取更新日志失败")
        raise HTTPException(status_code=500, detail="更新日志加载失败") from exc

    items = [_serialize(r) for r in rows]
    return {"code": 0, "total": len(items), "items": items}


@router.post(
    "/admin/changelogs",
    summary="发布更新日志（管理员）",
    description="管理员发布一条更新日志，版本号重复时返回 409。",
    responses={
        401: {"description": "未登录或登录凭证无效"},
        403: {"description": "当前账号不是管理员"},
        409: {"description": "版本号已存在"},
        422: {"description": "字段为空或超出长度上限"},
    },
)
def create_changelog(
    req: ChangelogRequest,
    admin: dict = Depends(get_current_admin),
) -> dict:
    """发布更新日志：校验后落库，返回新记录 id。"""
    version = req.version.strip()
    release_date = req.release_date.strip()
    content = req.content_markdown.strip()

    if not version or not release_date or not content:
        raise HTTPException(status_code=422, detail="版本号、发布日期、更新内容都不能为空")

    try:
        changelog_id = db.create_changelog(version, release_date, content)
    except ValueError as exc:
        # 唯一的 ValueError 来源是版本号重复（参数已在上面校验过）
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("发布更新日志失败 version=%s", version)
        raise HTTPException(status_code=500, detail="发布失败，请稍后重试") from exc

    logger.info(
        "发布更新日志 admin_id=%s version=%s id=%s",
        admin["id"], version, changelog_id,
    )
    return {
        "code": 0,
        "id": changelog_id,
        "message": "发布成功",
    }


@router.put(
    "/admin/changelogs/{changelog_id}",
    summary="修改更新日志（管理员）",
    description="按 id 修改一条更新日志，版本号改成已存在的版本时返回 409。",
    responses={
        401: {"description": "未登录或登录凭证无效"},
        403: {"description": "当前账号不是管理员"},
        404: {"description": "日志不存在"},
        409: {"description": "版本号已被其他日志占用"},
        422: {"description": "字段为空或超出长度上限"},
    },
)
def update_changelog(
    changelog_id: int,
    req: ChangelogRequest,
    admin: dict = Depends(get_current_admin),
) -> dict:
    """修改更新日志，返回 {code, message}。"""
    version = req.version.strip()
    release_date = req.release_date.strip()
    content = req.content_markdown.strip()

    if not version or not release_date or not content:
        raise HTTPException(status_code=422, detail="版本号、发布日期、更新内容都不能为空")

    try:
        db.update_changelog(changelog_id, version, release_date, content)
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if "不存在" in message else 409
        raise HTTPException(status_code=status_code, detail=message) from exc
    except Exception as exc:
        logger.exception("修改更新日志失败 id=%s", changelog_id)
        raise HTTPException(status_code=500, detail="修改失败，请稍后重试") from exc

    logger.info(
        "修改更新日志 admin_id=%s id=%s version=%s",
        admin["id"], changelog_id, version,
    )
    return {"code": 0, "id": changelog_id, "message": "已保存"}


@router.delete(
    "/admin/changelogs/{changelog_id}",
    summary="删除更新日志（管理员）",
    description="按 id 删除一条更新日志，删除后前台弹窗立即不再展示。",
    responses={
        401: {"description": "未登录或登录凭证无效"},
        403: {"description": "当前账号不是管理员"},
        404: {"description": "日志不存在"},
    },
)
def remove_changelog(
    changelog_id: int,
    admin: dict = Depends(get_current_admin),
) -> dict:
    """删除更新日志，返回 {code, message}。"""
    try:
        db.delete_changelog(changelog_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("删除更新日志失败 id=%s", changelog_id)
        raise HTTPException(status_code=500, detail="删除失败，请稍后重试") from exc

    logger.info("删除更新日志 admin_id=%s id=%s", admin["id"], changelog_id)
    return {"code": 0, "id": changelog_id, "message": "已删除"}