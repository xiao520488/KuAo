# -*- coding: utf-8 -*-
"""
向量化封装 (embedder.py) —— S1 检索内核

重构说明（v2）：
    - 【已移除】本地 sentence-transformers / BGE 模型加载（省云服务器内存）
    - 改为调用【在线 Embedding API】（OpenAI 兼容接口，走 openai 库）
    - 配置项（均在 .env 中读取，见 config.py）：
          EMBEDDING_API_KEY   必填，API 密钥
          EMBEDDING_BASE_URL  必填，兼容接口的 base_url
          EMBEDDING_MODEL     可选，默认 bge-m3

设计要点：
    - 接口保持不变：encode_docs / encode_query / dim / get_backend / self_test，
      retriever.py 无需改动调用方式
    - query / doc 使用同一模型；bge 系列官方建议 query 加指令前缀，这里保留
      QUERY_INSTRUCTION（可用环境变量 KB_EMBED_QUERY_INSTRUCTION 置空关闭）
    - 向量做 L2 归一化，配合 Chroma 的 cosine 空间
    - 批量分批 + 指数退避重试（网络抖动 / 429 / 5xx）
    - Mock 兜底：仅当 KB_EMBED_MODE=mock 时启用（确定性伪随机向量），
      用于离线跑通工程链路。API 调用失败【不再静默降级】——直接抛 EmbeddingError，
      避免把 mock 向量混进真实索引。
"""

import hashlib
import os
import random
import threading
import time
from typing import List, Optional, Sequence

from openai import OpenAI, APIError, APIConnectionError, APITimeoutError, RateLimitError

# ============================================================
# 配置
# ============================================================
# 密钥 / 地址读不到时不要直接炸 import（服务要能启动），首次使用时再报错
_API_KEY = os.getenv("EMBEDDING_API_KEY", "").strip()
_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "").strip()
MODEL_NAME = os.getenv("EMBEDDING_MODEL", "bge-m3").strip() or "bge-m3"

# 请求批大小 / 超时 / 重试
BATCH_SIZE = int(os.getenv("KB_EMBED_BATCH_SIZE", "32"))
TIMEOUT = float(os.getenv("KB_EMBED_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("KB_EMBED_MAX_RETRIES", "3"))
RETRY_BASE_DELAY = float(os.getenv("KB_EMBED_RETRY_BASE_DELAY", "1.5"))

# 预期维度（仅用于文档/mock 兜底；真实维度以 API 返回为准）
EXPECTED_DIM = int(os.getenv("KB_EMBED_DIM", "1024"))

# 检索指令前缀（仅用于 query，不用于 doc）。bge 系列官方推荐写法。
QUERY_INSTRUCTION = os.getenv(
    "KB_EMBED_QUERY_INSTRUCTION",
    "为这个句子生成表示以用于检索相关文章：",
)

# 模式：api = 只用在线接口；mock = 只用伪随机向量（离线调试）
MODE = os.getenv("KB_EMBED_MODE", "api").strip().lower()

_lock = threading.Lock()
_client: Optional[OpenAI] = None
_active_backend: Optional[str] = None
_cached_dim: Optional[int] = None


class EmbeddingError(RuntimeError):
    """Embedding API 调用失败（未配置 / 网络 / 鉴权 / 响应异常）。"""


# ============================================================
# 一、客户端（懒加载单例）
# ============================================================

def _get_client() -> OpenAI:
    """构建 OpenAI 客户端（兼容接口）。缺配置时抛 EmbeddingError。"""
    global _client
    if _client is not None:
        return _client

    with _lock:
        if _client is not None:
            return _client

        if not _API_KEY:
            raise EmbeddingError(
                "未配置 EMBEDDING_API_KEY，请在 .env 中填写在线 Embedding 服务的 API Key"
            )
        if not _BASE_URL:
            raise EmbeddingError(
                "未配置 EMBEDDING_BASE_URL，请在 .env 中填写兼容接口的 base_url"
            )

        _client = OpenAI(
            api_key=_API_KEY,
            base_url=_BASE_URL,
            timeout=TIMEOUT,
            max_retries=0,  # 重试由本模块控制，便于统一退避策略
        )
        return _client


# ============================================================
# 二、Mock 后端（仅离线调试，确定性伪随机向量）
# ============================================================

def _mock_vec(text: str) -> List[float]:
    seed = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:16], 16)
    rnd = random.Random(seed)
    vec = [rnd.uniform(-1.0, 1.0) for _ in range(EXPECTED_DIM)]
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def _mock_encode(texts: Sequence[str]) -> List[List[float]]:
    return [_mock_vec(t) for t in texts]


# ============================================================
# 三、在线 API 后端
# ============================================================

def _normalize(vec: Sequence[float]) -> List[float]:
    norm = sum(float(v) * float(v) for v in vec) ** 0.5 or 1.0
    return [float(v) / norm for v in vec]


def _request_batch(batch: Sequence[str]) -> List[List[float]]:
    """单批请求（含指数退避重试），返回与 batch 等长、顺序一致、已归一化的向量。"""
    client = _get_client()
    last_exc: Optional[Exception] = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.embeddings.create(
                model=MODEL_NAME,
                input=list(batch),
                encoding_format="float",
            )
            # OpenAI 兼容接口不保证顺序，按 index 排序后再校验
            items = sorted(resp.data, key=lambda d: d.index)
            if len(items) != len(batch):
                raise EmbeddingError(
                    f"向量数量不匹配：期望 {len(batch)}，实际 {len(items)}"
                )
            return [_normalize(it.embedding) for it in items]

        except (RateLimitError, APITimeoutError, APIConnectionError, APIError) as exc:
            last_exc = exc
            if attempt >= MAX_RETRIES:
                break
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            print(
                f"[embedder] API 调用失败（第 {attempt + 1} 次），{delay:.1f}s 后重试："
                f"{type(exc).__name__}: {exc}"
            )
            time.sleep(delay)

    raise EmbeddingError(
        f"Embedding API 调用失败（model={MODEL_NAME}, base_url={_BASE_URL}）："
        f"{type(last_exc).__name__}: {last_exc}"
    )


def _api_encode(texts: Sequence[str]) -> List[List[float]]:
    global _cached_dim
    out: List[List[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = list(texts[start:start + BATCH_SIZE])
        vecs = _request_batch(batch)
        if vecs and _cached_dim is None:
            _cached_dim = len(vecs[0])
        out.extend(vecs)
    return out


# ============================================================
# 四、对外接口
# ============================================================

def get_backend() -> str:
    """当前生效后端：'api' 或 'mock'。"""
    global _active_backend
    if _active_backend is not None:
        return _active_backend
    with _lock:
        if _active_backend is None:
            _active_backend = "mock" if MODE == "mock" else "api"
    print(f"[embedder] backend = {_active_backend} (model={MODEL_NAME})")
    return _active_backend


def dim() -> int:
    """向量维度：已知（已调用过 API / mock）返回实际值，否则返回配置预期值。"""
    if _cached_dim is not None:
        return _cached_dim
    if get_backend() == "mock":
        return EXPECTED_DIM
    return EXPECTED_DIM


def encode_docs(texts: Sequence[str]) -> List[List[float]]:
    """文档向量化（入库用，不加指令前缀）。"""
    texts = list(texts)
    if not texts:
        return []
    if get_backend() == "mock":
        return _mock_encode(texts)
    return _api_encode(texts)


def encode_query(text: str) -> List[float]:
    """查询向量化（bge 系列加检索指令前缀，可用环境变量置空关闭）。"""
    vecs = encode_docs([QUERY_INSTRUCTION + text])
    return vecs[0]


def self_test() -> dict:
    """自检：返回后端、模型、维度、query/doc 一致性（会真实调用一次 API）。"""
    be = get_backend()
    v1 = encode_query("智能车竞赛 视觉识别")
    v2 = encode_docs(["智能车竞赛 视觉识别"])[0]
    return {
        "backend": be,
        "model": MODEL_NAME if be == "api" else "mock-md5",
        "base_url": _BASE_URL if be == "api" else "-",
        "dim": len(v1),
        "query_dim_matches_doc": len(v1) == len(v2),
        "normalized": abs(sum(x * x for x in v1) - 1.0) < 1e-3,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(self_test(), ensure_ascii=False, indent=2))
