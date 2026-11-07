"""集中配置层 (config.py)

职责：把 .env 里的 DEEPSEEK_* 环境变量映射为 llm.py 期望的 LLM_* 属性，
并提供完整的默认值，保证 llm.py 在缺少某项配置时也能安全启动。

约定：
    - 所有配置在此处一次性读取，其它模块不再直接 os.getenv
    - 数值/布尔项做容错转换，非法值回落默认值（不让脏配置炸掉整个服务）
"""

import os

from dotenv import load_dotenv

# 显式指定 .env 路径，避免因工作目录不同而读不到
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_PATH)


# ------------------------------------------------------------
# 类型安全的读取工具
# ------------------------------------------------------------
def _get_str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _get_int(key: str, default: int) -> int:
    try:
        return int(_get_str(key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_float(key: str, default: float) -> float:
    try:
        return float(_get_str(key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_bool(key: str, default: bool) -> bool:
    raw = _get_str(key, "").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


# ------------------------------------------------------------
# LLM 配置（供 llm.py 使用）
# ------------------------------------------------------------
# 兼容命名：优先读 DEEPSEEK_*，也接受已写成 LLM_* 的情况
LLM_API_KEY = _get_str("DEEPSEEK_API_KEY", _get_str("LLM_API_KEY", ""))
LLM_BASE_URL = _get_str("DEEPSEEK_BASE_URL", _get_str("LLM_BASE_URL", "https://api.deepseek.com"))
LLM_MODEL = _get_str("DEEPSEEK_MODEL", _get_str("LLM_MODEL", "deepseek-chat"))

# 重试策略
LLM_MAX_RETRIES = _get_int("LLM_MAX_RETRIES", 3)
LLM_RETRY_BASE_DELAY = _get_float("LLM_RETRY_BASE_DELAY", 1.5)

# 超时：由 llm.py 统一设置为 connect=5s / read=45s，不再使用单一 LLM_TIMEOUT。

# 单次补全的 max_tokens 上限（推理模型放大重试时以此封顶）
LLM_MAX_TOKENS_CEILING = _get_int("LLM_MAX_TOKENS_CEILING", 8192)

# 是否启用 response_format={"type": "json_object"}
LLM_JSON_MODE = _get_bool("LLM_JSON_MODE", True)

# 是否启用 json_repair 容错修复
JSON_REPAIR_ENABLED = _get_bool("JSON_REPAIR_ENABLED", True)


# ------------------------------------------------------------
# 数据库 / 向量库（预留，其它模块可复用）
# ------------------------------------------------------------
DB_PATH = _get_str("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "competitions.db"))
CHROMA_DIR = _get_str("CHROMA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_db"))


# ------------------------------------------------------------
# CORS（白名单，禁止通配符）
# ------------------------------------------------------------
# 多个来源用英文逗号分隔；默认仅覆盖本地 Vite 开发/预览端口。
# 生产部署务必通过环境变量改成真实前端域名，例如：
#   CORS_ALLOW_ORIGINS=https://kuao.example.com
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in _get_str(
        "CORS_ALLOW_ORIGINS",
        (
            "http://localhost:5173,"
            "http://127.0.0.1:5173,"
            "http://localhost:4173,"
            "http://127.0.0.1:4173"
        ),
    ).split(",")
    if origin.strip()
]


# ------------------------------------------------------------
# 鉴权 / JWT（用户系统）
# ------------------------------------------------------------
# 生产环境必须通过 .env / 部署环境覆盖 JWT_SECRET_KEY。
# 启动时强校验：使用仓库内置的默认 dev 密钥，或密钥过短，直接拒绝启动。
_DEV_JWT_SECRET = "dev-only-secret-key-please-change-in-prod-4f8a2c9e1b7d"

JWT_SECRET_KEY = _get_str("JWT_SECRET_KEY", _DEV_JWT_SECRET)

if JWT_SECRET_KEY == _DEV_JWT_SECRET:
    raise RuntimeError(
        "检测到正在使用默认的开发 JWT_SECRET_KEY，服务拒绝启动。"
        "请在 .env 中配置 JWT_SECRET_KEY（至少 32 位，建议 64 位随机字符串）。"
    )

if len(JWT_SECRET_KEY) < 32:
    raise RuntimeError(
        "JWT_SECRET_KEY 长度不足（当前 %d 位），至少需要 32 位，建议 64 位随机字符串。"
        % len(JWT_SECRET_KEY)
    )

JWT_ALGORITHM = _get_str("JWT_ALGORITHM", "HS256")
# Access Token 默认 24 小时（不再使用 7 天）；如需更短，可在 .env 覆盖。
JWT_EXPIRE_MINUTES = _get_int("JWT_EXPIRE_MINUTES", 60 * 24)

# ------------------------------------------------------------
# AI daily quota (per user, resets at local midnight)
# ------------------------------------------------------------
# Max AI calls per user per day. Set to 0 for unlimited (dev only).
AI_DAILY_QUOTA = _get_int("AI_DAILY_QUOTA", 30)
