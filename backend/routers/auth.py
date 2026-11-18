# -*- coding: utf-8 -*-
"""用户鉴权路由 (routers/auth.py)

提供：
  * GET  /api/auth/captcha   图形验证码（登录 / 注册前必须先取一张）
  * POST /api/auth/register  注册（用户名 + 密码 + 验证码 → 建号并直接返回 Token）
  * POST /api/auth/login     登录（校验验证码与密码 → 返回 JWT Token）
  * GET  /api/auth/me        获取当前登录用户（用于前端刷新页面后校验登录态）

安全约定：
  - 密码只以 bcrypt 哈希入库，任何时候不回显明文
  - 登录失败统一提示「用户名或密码错误」，不泄露账号是否存在
  - 注册重名返回 409
  - 验证码**一次性**：校验过就作废（成功失败都作废），失败返回 400
  - 同一用户名 / IP 连续输错 5 次密码锁定 15 分钟，返回 429（见 login_guard.py）
"""

import base64
import secrets
import sqlite3
from typing import Optional

from captcha.image import ImageCaptcha
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

import config
from captcha_store import captcha_store
from db import create_user, get_user_by_id, get_user_by_username
from login_guard import login_guard
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
    captcha_key: str = Field(..., min_length=1, description="验证码标识（取自 /api/auth/captcha）")
    captcha_code: str = Field(..., min_length=1, max_length=8, description="用户输入的验证码")

    model_config = {
        "json_schema_extra": {
            "example": {
                "username": "alice",
                "password": "123456",
                "captcha_key": "2f1c...",
                "captcha_code": "1234",
            }
        }
    }


class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")
    captcha_key: str = Field(..., min_length=1, description="验证码标识（取自 /api/auth/captcha）")
    captcha_code: str = Field(..., min_length=1, max_length=8, description="用户输入的验证码")

    model_config = {
        "json_schema_extra": {
            "example": {
                "username": "alice",
                "password": "123456",
                "captcha_key": "2f1c...",
                "captcha_code": "1234",
            }
        }
    }


class CaptchaResponse(BaseModel):
    captcha_key: str = Field(..., description="验证码标识，提交登录/注册时原样回传")
    captcha_image: str = Field(..., description="PNG 图片的 data URL（可直接塞进 <img :src>）")
    expires_in: int = Field(..., description="有效期（秒）")


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
# 图形验证码
# ---------------------------------------------------------------------------
# 复用同一个 ImageCaptcha 实例：它每次 generate 都会新建 Image 并重新挑字体、加噪点，
# 单实例即可，无需每请求 new 一个。
_CAPTCHA_RENDERER = ImageCaptcha(width=150, height=50)


def _random_captcha_code() -> str:
    """生成指定位数的纯数字验证码（用 secrets，不用可预测的 random）。"""
    length = max(1, int(config.CAPTCHA_LENGTH))
    return "".join(secrets.choice("0123456789") for _ in range(length))


def _client_ip(request: Request) -> str:
    """真实客户端 IP。

    uvicorn 以 ``--proxy-headers --forwarded-allow-ips=127.0.0.1`` 启动，
    nginx 会带上 X-Forwarded-For，所以 request.client.host 已经是用户真实 IP。
    """
    return request.client.host if request.client else ""


def _require_captcha(captcha_key: str, captcha_code: str) -> None:
    """校验验证码；不通过直接抛 400（key 一次性，校验即作废）。"""
    if not captcha_store.consume(captcha_key, captcha_code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="验证码错误或已过期",
        )


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
@router.get(
    "/captcha",
    response_model=CaptchaResponse,
    summary="获取图形验证码",
    description=(
        "返回 PNG 图片的 Base64 data URL 与唯一 captcha_key，有效期 5 分钟。"
        "登录 / 注册时必须同时回传 captcha_key 与用户输入的 captcha_code；"
        "一个验证码只能用一次（成功失败都会作废）。"
    ),
)
def get_captcha() -> CaptchaResponse:
    """生成一张验证码图片，并把「key → 文本」登记进一次性缓存。"""
    code = _random_captcha_code()
    png = _CAPTCHA_RENDERER.generate(code).getvalue()
    key = captcha_store.issue(code)
    return CaptchaResponse(
        captcha_key=key,
        captcha_image="data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        expires_in=config.CAPTCHA_TTL_SECONDS,
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    summary="注册账号",
    description="创建新用户。用户名重复返回 409；成功后直接返回 Token，前端可免二次登录。",
    responses={
        400: {"description": "验证码错误或已过期"},
        409: {"description": "用户名已被注册"},
    },
)
def register(req: RegisterRequest, request: Request, response: Response) -> TokenResponse:
    """注册新用户并签发 Token。"""
    # 验证码先校验：机器人连「用户名是否重复」这一步都不该走到
    _require_captcha(req.captcha_key, req.captcha_code)

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
        400: {"description": "验证码错误或已过期"},
        401: {"description": "用户名或密码错误"},
        429: {"description": "连续登录失败过多，已临时锁定"},
    },
)
def login(req: LoginRequest, request: Request, response: Response) -> TokenResponse:
    """校验验证码与账号密码，返回 JWT Token，并种下 refresh Cookie。"""
    username = _normalize_username(req.username)
    ip = _client_ip(request)

    # 1) 锁定期内直接挡掉：连验证码校验和 bcrypt 都不必付
    remaining = login_guard.locked_remaining(username, ip)
    if remaining > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请稍后再试",
            headers={"Retry-After": str(remaining)},
        )

    # 2) 验证码：一次性，错了就作废，防脚本复用同一张图穷举
    _require_captcha(req.captcha_key, req.captcha_code)

    user = get_user_by_username(username)

    # 3) 用户不存在 / 密码错误 → 统一文案，防账号枚举
    if user is None or not verify_password(req.password, user["password_hash"]):
        login_guard.record_failure(username, ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 4) 登录成功：清零失败计数，避免历史错误把正常用户越锁越久
    login_guard.reset(username, ip)
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
