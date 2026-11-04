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

# 超时（秒）
LLM_TIMEOUT = _get_float("LLM_TIMEOUT", 120.0)

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
