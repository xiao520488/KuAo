"""用户反馈接口 (routers/feedback.py)

Routes
------
POST /api/feedback —— 登录用户提交一条反馈，写入 feedbacks 表。

设计要点：
  - 强制登录（get_current_user）：未登录 / 凭证无效 / 账号已删除一律 401，
    从入口杜绝匿名刷屏；
  - 只写入，不提供任何查询接口（需要时直接查库）；
  - 不消耗 AI 配额，与 /api/ai/* 的调用额度互不影响；
  - user_id 取自 Token 解析结果，客户端无法伪造；
  - 内容去首尾空白后不得为空，长度上限 2000 字（防超长灌库）。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import db
from security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["feedback"])

# 单条反馈正文长度上限（字符数）
MAX_FEEDBACK_LENGTH = 2000


class FeedbackRequest(BaseModel):
    """提交反馈的请求体。"""

    content: str = Field(
        ...,
        min_length=1,
        max_length=MAX_FEEDBACK_LENGTH,
        description="反馈内容（1-2000 字，首尾空白会被自动去除）",
    )


@router.post(
    "/feedback",
    summary="提交用户反馈",
    description=(
        "登录用户提交反馈内容，写入 feedbacks 表。"
        "接口只负责收集，不提供查询功能。"
    ),
    responses={
        401: {"description": "未登录或登录凭证无效"},
        422: {"description": "反馈内容为空或超出长度上限"},
        500: {"description": "反馈写入失败"},
    },
)
def submit_feedback(
    req: FeedbackRequest,
    current_user: dict = Depends(get_current_user),  # 需登录
) -> dict:
    """提交反馈：校验内容后落库，返回新记录 id。"""
    content = req.content.strip()
    if not content:
        raise HTTPException(
            status_code=422,
            detail="反馈内容不能为空",
        )

    try:
        feedback_id = db.create_feedback(content, user_id=current_user["id"])
    except Exception as exc:
        logger.exception(
            "写入用户反馈失败 user_id=%s", current_user["id"]
        )
        raise HTTPException(
            status_code=500,
            detail="反馈提交失败，请稍后重试",
        ) from exc

    logger.info(
        "收到用户反馈 user_id=%s feedback_id=%s length=%s",
        current_user["id"], feedback_id, len(content),
    )
    return {
        "code": 0,
        "id": feedback_id,
        "message": "反馈提交成功",
    }
