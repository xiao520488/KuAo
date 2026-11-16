"""FastAPI 应用入口。

启动方式：
    uvicorn main:app --reload --port 8000
"""

import threading

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

import config
import db
from routers import (
    admin,
    ai,
    auth,
    changelogs,
    competition_facts,
    competitions,
    feedback,
    preparations,
)

app = FastAPI(
    title="Agent Lab API",
    description="FastAPI 后端服务",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# CORS 跨域配置（白名单模式）
# ---------------------------------------------------------------------------
# 允许的前端来源统一由 CORS_ALLOW_ORIGINS 配置（逗号分隔），不再使用通配符。
# 默认值仅覆盖本地开发端口；生产环境必须在 .env / 部署环境中改为真实前端域名。
if "*" in config.CORS_ALLOW_ORIGINS:
    raise RuntimeError(
        "CORS_ALLOW_ORIGINS 不允许使用通配符 *，请配置明确的前端来源白名单"
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_credentials=True,        # refresh token 走 httpOnly Cookie，需允许携带凭证
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)


# ---------------------------------------------------------------------------
# API 响应禁止共享缓存（P0 账号串号防线）
# ---------------------------------------------------------------------------
# 鉴权 / 用户数据接口如果被浏览器、Nginx、CDN 或运营商中间缓存，
# 就可能把 A 的响应（含 /api/auth/me 的身份）发给 B。这里统一对 /api/*
# 加 no-store，确保任何中间层都不得存储/复用 API 响应。
@app.middleware("http")
async def add_api_no_store_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, private, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------
app.include_router(competitions.router)
app.include_router(ai.router)
app.include_router(auth.router)
app.include_router(preparations.router)
app.include_router(feedback.router)
app.include_router(changelogs.router)
app.include_router(competition_facts.router)
app.include_router(admin.router)


# ---------------------------------------------------------------------------
# 启动初始化
# ---------------------------------------------------------------------------
@app.on_event("startup")
def startup_init_db() -> None:
    """启动时执行幂等建表/补列，确保老库自动拥有 insights.competition_id。"""
    db.init_db()
    _warm_search_index()


def _warm_search_index() -> None:
    """后台预热语义检索索引（目录只有百来条，1~2 秒）。

    embedding 接口不可用时静默失败：搜索会自动降级为关键词检索，
    不能让向量库的问题拖垮整个 Api 启动。
    """
    def _worker() -> None:
        try:
            import retriever
            retriever.ensure_index()
        except Exception as exc:  # noqa: BLE001
            print(f"[startup] 语义检索索引预热跳过: {exc!r}")

    threading.Thread(target=_worker, daemon=True, name="warm-search-index").start()


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["system"])
def health_check():
    """服务健康检查接口。"""
    return {"status": "ok", "service": "agent-lab-api", "version": "0.1.0"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
