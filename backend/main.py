"""FastAPI 应用入口。

启动方式：
    uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import ai, auth, competitions, preparations

app = FastAPI(
    title="Agent Lab API",
    description="FastAPI 后端服务",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# CORS 跨域配置
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 开发阶段放开，上线前请替换为具体前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------
app.include_router(competitions.router)
app.include_router(ai.router)
app.include_router(auth.router)
app.include_router(preparations.router)


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
