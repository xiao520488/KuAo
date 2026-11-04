# -*- coding: utf-8 -*-
"""
向量化封装 (embedder.py) —— S1 检索内核

设计要点：
    - 【必须在 import 任何 HF 相关库之前】设置 HF_ENDPOINT 走国内镜像
    - 模型：BAAI/bge-small-zh-v1.5（512 维，中文短文检索，CPU 可跑）
    - bge 系列检索时 query 建议加指令前缀（官方推荐），doc 不加
    - Mock 兜底：当模型不可用 / 下载卡住 / 环境变量 KB_EMBED_MOCK=1 时，
      使用【确定性伪随机向量】（基于 md5 播种），保证 Chroma / 检索逻辑可先跑通，
      且同一条文本每次得到同一向量（可复现）。
"""

import os

# ---- 镜像必须在 import huggingface / transformers 之前设置 ----
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "20")
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "20")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
# Xet 存储后端在镜像站会 401，强制走传统 HTTP 下载
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import hashlib
import threading
from typing import List, Optional, Sequence

MODEL_NAME = os.getenv("KB_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
EMBED_DIM = int(os.getenv("KB_EMBED_DIM", "512"))

# bge 检索指令前缀（仅用于 query，不用于 doc）
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

# 模式： auto = 先试真模型，失败自动转 mock；mock = 直接用伪随机向量
MODE = os.getenv("KB_EMBED_MODE", "auto").strip().lower()

_lock = threading.Lock()
_st_model = None          # SentenceTransformer 实例
_active_backend: Optional[str] = None   # "st" | "mock"


# ============================================================
# 一、Mock 后端（确定性伪随机向量）
# ============================================================

def _mock_vec(text: str) -> List[float]:
    """
    由文本 md5 播种，生成确定性单位向量。
    注意：这不是语义向量，只用于跑通工程链路；真模型就绪后自动切换。
    """
    import random

    seed = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:16], 16)
    rnd = random.Random(seed)
    vec = [rnd.uniform(-1.0, 1.0) for _ in range(EMBED_DIM)]

    # L2 归一化（Chroma cosine 空间下等价于内积）
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def _mock_encode(texts: Sequence[str]) -> List[List[float]]:
    return [_mock_vec(t) for t in texts]


# ============================================================
# 二、真模型后端（懒加载 + 失败降级）
# ============================================================

def _load_st():
    """懒加载 SentenceTransformer；失败返回 None（不抛异常）。"""
    global _st_model
    if _st_model is not None:
        return _st_model
    try:
        from sentence_transformers import SentenceTransformer

        _st_model = SentenceTransformer(MODEL_NAME, device="cpu")
        return _st_model
    except Exception as exc:  # noqa: BLE001
        print(f"[embedder] 模型加载失败，将使用 Mock 向量: {type(exc).__name__}: {exc}")
        return None


def _st_encode(texts: Sequence[str]) -> List[List[float]]:
    model = _load_st()
    if model is None:
        raise RuntimeError("model unavailable")
    vecs = model.encode(
        list(texts),
        normalize_embeddings=True,
        batch_size=32,
        show_progress_bar=False,
    )
    return [v.tolist() for v in vecs]


# ============================================================
# 三、对外接口
# ============================================================

def get_backend() -> str:
    """当前生效后端：'st' 或 'mock'（首次调用会触发探测）。"""
    global _active_backend
    if _active_backend is not None:
        return _active_backend

    with _lock:
        if _active_backend is not None:
            return _active_backend

        if MODE == "mock":
            _active_backend = "mock"
        else:
            # auto：探一次真模型
            _active_backend = "st" if _load_st() is not None else "mock"

    print(f"[embedder] backend = {_active_backend} (model={MODEL_NAME}, dim={dim()})")
    return _active_backend


def dim() -> int:
    """向量维度。真模型加载成功后以实际输出为准，否则用配置值。"""
    if _st_model is not None:
        try:
            return int(_st_model.get_sentence_embedding_dimension())
        except Exception:  # noqa: BLE001
            pass
    return EMBED_DIM


def encode_docs(texts: Sequence[str]) -> List[List[float]]:
    """文档向量化（入库用，不加指令前缀）。"""
    texts = list(texts)
    if not texts:
        return []
    if get_backend() == "st":
        try:
            return _st_encode(texts)
        except Exception:  # noqa: BLE001
            pass
    return _mock_encode(texts)


def encode_query(text: str) -> List[float]:
    """查询向量化（加 bge 检索指令前缀）。"""
    return encode_docs([QUERY_INSTRUCTION + text])[0]


def self_test() -> dict:
    """自检：返回后端、维度、以及一致性抽查结果。"""
    be = get_backend()
    v1 = encode_query("智能车竞赛 视觉识别")
    v2 = encode_docs(["智能车竞赛 视觉识别"])[0]
    return {
        "backend": be,
        "model": MODEL_NAME if be == "st" else "mock-md5",
        "dim": len(v1),
        "query_dim_matches_doc": len(v1) == len(v2),
        "deterministic": encode_query("同一句话") == encode_query("同一句话"),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(self_test(), ensure_ascii=False, indent=2))
