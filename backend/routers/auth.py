# -*- coding: utf-8 -*-
"""用户鉴权路由 (routers/auth.py)

提供：
  * POST /api/auth/register  注册（用户名 + 密码 → 建号并直接返回 Token）
  * POST /api/auth/login     登录（校验密码 → 返回 JWT Token）
  * GET  /api/auth/me        获取当前登录用户（用于前端刷新页面后校验登录态）

安全约定：
  - 密码只以 bcrypt 哈希入库，任何时候不回显明文
  - 登录失败统一提示「用户名或密码错误」，不泄露账号是否存在
  - 注册重名返回 409
"""

import sqlite3
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

import config
from db import create_user, get_user_by_id, get_user_by_username
from security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, description="用户名（3-32 位）")
    password: str = Field(..., min_length=6, max_length=64, description="密码（6-64 位）")

    model_config = {
        "json_schema_extra": {
            "example": {"username": "alice", "password": "123456"}
        }
    }


class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")

    model_config = {
        "json_schema_extra": {
            "example": {"username": "alice", "password": "123456"}
        }
    }


class UserInfo(BaseModel):
    id: int = Field(..., description="用户 ID")
    username: str = Field(..., description="用户名")
    created_at: str = Field(..., description="注册时间")
    is_admin: bool = Field(False, description="是否管理员（决定前端是否显示管理后台入口）")


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="JWT 访问令牌")
    token_type: str = Field("bearer", description="令牌类型，固定为 bearer")
    expires_in: int = Field(..., description="有效期（秒）")
    user: UserInfo = Field(..., description="用户信息")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _normalize_username(username: str) -> str:
    """用户名归一化：去首尾空格 + 转小写，避免 Alice / alice 注册出两个账号。"""
    return username.strip().lower()


def _to_user_dict(user) -> dict:
    """把 db 层返回的用户记录统一转成普通 dict。

    db.get_user_by_id / get_user_by_username 返回的是 sqlite3.Row，
    它没有 .get() 方法（调用即 AttributeError），也不支持下标赋值，
    所以这里统一转成 dict，后续一律用 user["key"] 取值。
    """
    if isinstance(user, dict):
        return dict(user)
    return {key: user[key] for key in user.keys()}


def _is_admin(user) -> bool:
    """从用户记录里安全读 is_admin（老库无该列时按非管理员处理）。"""
    try:
        return bool(int(user["is_admin"] or 0))
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def _build_token_response(user) -> TokenResponse:
    user = _to_user_dict(user)
    token = create_access_token(user["id"], user["username"])
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=config.JWT_EXPIRE_MINUTES * 60,
        user=UserInfo(
            id=user["id"],
            username=user["username"],
            created_at=user["created_at"],
            is_admin=_is_admin(user),
        ),
    )


def _set_refresh_cookie(response: Response, user: dict) -> None:
    """把 refresh token 种进 httpOnly Cookie（滑动续期：每次刷新都重置 30 天）。"""
    token = create_refresh_token(user["id"], user["username"])
    response.set_cookie(
        key=config.REFRESH_COOKIE_NAME,
        value=token,
        max_age=config.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
        path=config.REFRESH_COOKIE_PATH,
        domain=config.COOKIE_DOMAIN,
        secure=config.COOKIE_SECURE,
        httponly=True,          # JS 读不到，XSS 偷不走
        samesite=config.COOKIE_SAMESITE,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=config.REFRESH_COOKIE_NAME,
        path=config.REFRESH_COOKIE_PATH,
        domain=config.COOKIE_DOMAIN,
        secure=config.COOKIE_SECURE,
        httponly=True,
        samesite=config.COOKIE_SAMESITE,
    )


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
@router.post(
    "/register",
    response_model=TokenResponse,
    summary="注册账号",
    description="创建新用户。用户名重复返回 409；成功后直接返回 Token，前端可免二次登录。",
    responses={
        409: {"description": "用户名已被注册"},
    },
)
def register(req: RegisterRequest, response: Response) -> TokenResponse:
    """注册新用户并签发 Token。"""
    username = _normalize_username(req.username)
    if not username:
        raise HTTPException(status_code=422, detail="用户名不能为空")

    # 预检重名，给出友好提示（唯一索引兜底并发）
    if get_user_by_username(username) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已被注册")

    try:
        user_id = create_user(username, hash_password(req.password))
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="用户名已被注册"
        )

    user = get_user_by_username(username)
    if user is None:  # 理论上不会发生
        raise HTTPException(status_code=500, detail="注册失败，请稍后重试")

    # sqlite3.Row 不支持下标赋值，先转成 dict 才能改写 id；
    # 兼容个别情况下 lastrowid 与查询结果不一致
    user = _to_user_dict(user)
    user["id"] = user_id
    _set_refresh_cookie(response, user)
    return _build_token_response(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="登录",
    description="用户名 + 密码换取 JWT Token。前端需保存 token，并在调用 AI 接口时放入请求头：`Authorization: Bearer <token>`。",
    responses={
        401: {"description": "用户名或密码错误"},
    },
)
def login(req: LoginRequest, response: Response) -> TokenResponse:
    """校验账号密码，返回 JWT Token，并种下 refresh Cookie。"""
    username = _normalize_username(req.username)
    user = get_user_by_username(username)

    # 用户不存在 / 密码错误 → 统一文案，防账号枚举
    if user is None or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    _set_refresh_cookie(response, _to_user_dict(user))
    return _build_token_response(user)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="续期登录（刷新 access token）",
    description=(
        "无需 Authorization 头，浏览器自动携带 httpOnly Cookie `kuao_refresh`。"
        "用有效的 refresh token 换一个新的 access token，并顺延 Cookie 有效期（滑动续期）。"
    ),
    responses={401: {"description": "refresh token 缺失或已过期，需重新登录"}},
)
def refresh(
    response: Response,
    refresh_cookie: Optional[str] = Cookie(default=None, alias=config.REFRESH_COOKIE_NAME),
) -> TokenResponse:
    """用 httpOnly Cookie 里的 refresh token 静默换取新的 access token。"""
    if not refresh_cookie:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_refresh_token(refresh_cookie)
    try:
        uid = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的登录凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = get_user_by_id(uid)
    if user is None:
        _clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已被删除",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = _to_user_dict(user)
    _set_refresh_cookie(response, user)   # 滑动续期：再用 30 天
    return _build_token_response(user)


@router.post(
    "/logout",
    summary="退出登录",
    description="清除 refresh Cookie；前端同时清掉内存中的 access token。",
)
def logout(response: Response) -> dict:
    """清掉 refresh Cookie（access token 由前端自行丢弃）。"""
    _clear_refresh_cookie(response)
    return {"ok": True}


@router.get(
    "/me",
    response_model=UserInfo,
    summary="获取当前登录用户",
    description="需携带 `Authorization: Bearer <token>`。前端启动时用它校验本地 Token 是否仍有效。",
    responses={401: {"description": "未登录或 Token 失效"}},
)
def read_current_user(user: dict = Depends(get_current_user)) -> UserInfo:
    """返回当前 Token 对应的用户信息。"""
    user = _to_user_dict(user)
    return UserInfo(
        id=user["id"],
        username=user["username"],
        created_at=user["created_at"],
        is_admin=_is_admin(user),
    )
