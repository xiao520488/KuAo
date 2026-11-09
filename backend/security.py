# -*- coding: utf-8 -*-
"""鉴权核心工具 (security.py)

职责：
  1. 密码哈希 / 校验（bcrypt，直接调用 bcrypt 库，不引入 passlib）
  2. JWT 签发 / 解析（PyJWT）
  3. FastAPI 依赖 `get_current_user`：从 Authorization: Bearer <token> 解析当前用户

设计原则：
  - 业务路由只需 `user: dict = Depends(get_current_user)` 一行即可挂上鉴权
  - 所有鉴权失败统一抛 401，并带 WWW-Authenticate: Bearer 头（符合 OAuth2 规范，
    Swagger 的 Authorize 按钮才能正确识别）
  - 不在此模块 import 任何 router，避免循环依赖
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import config
from db import get_user_by_id

# ---------------------------------------------------------------------------
# 密码哈希（bcrypt）
# ---------------------------------------------------------------------------
# bcrypt 算法只处理前 72 字节，超长密码先截断，避免部分版本抛 ValueError
_BCRYPT_MAX_BYTES = 72


def _to_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """生成 bcrypt 哈希（含随机 salt），返回可直接入库的字符串。"""
    return bcrypt.hashpw(_to_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与库中哈希是否匹配；任何异常一律视为校验失败。"""
    try:
        return bcrypt.checkpw(_to_bytes(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def create_access_token(user_id: int, username: str,
                        expires_minutes: Optional[int] = None) -> str:
    """签发访问令牌，payload 中 sub 存用户 id（JWT 规范要求 sub 为字符串）。"""
    minutes = expires_minutes or config.JWT_EXPIRE_MINUTES
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, config.JWT_SECRET_KEY, algorithm=config.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """解析并验证令牌，失败抛 401。"""
    try:
        # require：显式要求 exp / sub 存在。缺少任一 claim 的 token 一律 401，
        # 避免「payload 为空 / 只有 username」的畸形 token 进入用户解析流程。
        return jwt.decode(
            token,
            config.JWT_SECRET_KEY,
            algorithms=[config.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的登录凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# FastAPI 依赖：当前登录用户
# ---------------------------------------------------------------------------
# auto_error=False：自己控制 401 文案，且缺少请求头时也返回 401 而非 403
_bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <登录接口返回的 token>")


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    """
    从 `Authorization: Bearer <token>` 解析并校验 Token，返回当前用户 dict。

    返回：{"id": int, "username": str, "created_at": str}

    用法::

        @router.post("/plan")
        def generate_plan(req: PlanRequest, user: dict = Depends(get_current_user)):
            ...

    失败场景统一 401：未带 Token / Token 格式错 / 过期 / 用户已被删除。
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录后再使用该功能",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的登录凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的登录凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = get_user_by_id(uid)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已被删除",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# ---------------------------------------------------------------------------
# FastAPI 依赖：管理员
# ---------------------------------------------------------------------------
def get_current_admin(user: dict = Depends(get_current_user)) -> dict:
    """
    管理员专用依赖：在 get_current_user 之上再校验 users.is_admin。

    用法::

        @router.get("/admin/feedback")
        def list_feedback(admin: dict = Depends(get_current_admin)):
            ...

    失败场景：
      * 未登录 / Token 失效 / 用户已删除 -> 401（由 get_current_user 抛出）
      * 已登录但不是管理员           -> 403
    """
    try:
        is_admin = int(user["is_admin"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        # 老库尚未迁移出 is_admin 列时按非管理员处理，绝不默认放行
        is_admin = 0
    if not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return user


# 可选依赖：有 Token 就尝试解析，没有也放行（本期暂未使用，预留）
def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[dict]:
    """游客可用的「软鉴权」依赖：解析失败返回 None 而不是 401。"""
    if credentials is None or not credentials.credentials:
        return None
    try:
        payload = decode_access_token(credentials.credentials)
        return get_user_by_id(int(payload.get("sub")))
    except (HTTPException, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# AI 每日配额依赖
# ---------------------------------------------------------------------------
# 用法（挂在需要限流的 AI 路由上）：
#     user: dict = Depends(require_ai_quota)
# 依赖内部会完成「鉴权 + 扣减一次配额」两件事：
#   - 未登录           -> 401（由 get_current_user 抛出）
#   - 配额已用尽       -> 429，返回体包含 {detail, used, limit, remaining}
#   - 扣减成功         -> 返回 user dict（与 get_current_user 一致，路由无需改动）
# 注意：一旦扣减成功，即使后续 AI 调用失败也不回滚配额（简单、可预期，
#       避免并发回滚带来的额外竞态）。


def require_ai_quota(user: dict = Depends(get_current_user)) -> dict:
    """鉴权 + 原子扣减一次 AI 配额；超限时抛 429。"""
    from db import consume_ai_quota  # 延迟导入，避免模块级循环依赖

    result = consume_ai_quota(user["id"], config.AI_DAILY_QUOTA)
    if not result.get("allowed"):
        remaining = result.get("remaining", 0)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="今日 AI 使用次数已用完，请明天再试",
            headers={"Retry-After": "3600"},
        )
    return user


def current_ai_quota(user: dict = Depends(get_current_user)) -> dict:
    """只读查询当前用户今日配额状态（不扣减），用于前端展示剩余次数。"""
    from db import get_ai_quota

    info = get_ai_quota(user["id"], config.AI_DAILY_QUOTA)
    remaining = info.get("remaining")
    return {
        "used": info.get("used", 0),
        "limit": info.get("limit", 0),
        "remaining": None if remaining is None or remaining < 0 else remaining,
    }
