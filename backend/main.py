"""FastAPI 应用入口。

启动方式：
    uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config
import db
from routers import ai, auth, competitions, feedback, preparations

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
    allow_credentials=False,       # 鉴权使用 Authorization: Bearer，不依赖 Cookie
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------
app.include_router(competitions.router)
app.include_router(ai.router)
app.include_router(auth.router)
app.include_router(preparations.router)
app.include_router(feedback.router)


# ---------------------------------------------------------------------------
# 启动初始化
# ---------------------------------------------------------------------------
@app.on_event("startup")
def startup_init_db() -> None:
    """启动时执行幂等建表/补列，确保老库自动拥有 insights.competition_id。"""
    db.init_db()


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
