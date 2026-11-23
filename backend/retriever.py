# -*- coding: utf-8 -*-
"""检索内核 (retriever.py) —— 竞赛语义检索

职责（只做一件事）：
    把 competitions 表里的竞赛做成向量索引，回答「我想参加 XX 类比赛」这种自然语言查询。

三级打分（最终分 = 0.65 * 向量 + 0.35 * 关键词，命中名称再 +0.15 封顶 1.0）：
    L1 向量召回   —— Chroma cosine 相似度，目录只有 85~100 条，全量召回后统一排序
    L2 关键词融合 —— 名称 / 简称 / 类别 / 摘要 的模糊命中，保证「数学建模」这类精确词不被向量稀释
    L3 真值校验   —— 回 SQLite 取真值，已删除的竞赛直接丢弃

设计约定：
    - 真值源永远是 SQLite；Chroma 只是可重建的向量索引
    - 索引条数或指纹与库不一致时自动重建（ensure_index），不依赖任何定时任务
    - 向量库不可用时由调用方降级到纯关键词检索（见 routers/ai.py），接口永远有结果
"""

import os

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"   # 需在任何 HF 库之前

import json
import logging
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

import chromadb
from chromadb.config import Settings

import config
import db
import embedder

# ------------------------------------------------------------
# 配置
# ------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHROMA_DIR = os.getenv("KB_CHROMA_DIR", config.CHROMA_DIR or os.path.join(BASE_DIR, "chroma_db"))
COLLECTION_NAME = os.getenv("KB_COLLECTION", "competitions")
LEGACY_COLLECTION = "awards"          # 旧的奖项级索引，已废弃，重建时顺手删掉

MIN_SIM = float(os.getenv("KB_MIN_SIM", "0.30"))      # 向量分下限（低于它且关键词也没命中就丢弃）
VEC_WEIGHT = float(os.getenv("KB_VEC_WEIGHT", "0.65"))
KW_WEIGHT = float(os.getenv("KB_KW_WEIGHT", "0.35"))
NAME_BONUS = 0.15                                      # 名称精确/包含命中加成

_ID_PREFIX = "comp:"

_client = None
_collection = None
_lock = threading.Lock()

# Embedding 接口故障后的冷却期：期间直接走关键词，避免每次搜索都卡 10 秒做重试
_COOLDOWN_SEC = 60.0
_down_until = 0.0
logger = logging.getLogger(__name__)


def _embed_down() -> bool:
    return time.time() < _down_until


def _mark_embed_down(exc: Exception) -> None:
    global _down_until
    _down_until = time.time() + _COOLDOWN_SEC
    logger.warning("Embedding 不可用，%.0fs 内降级为关键词检索：%r", _COOLDOWN_SEC, exc)


# ============================================================
# 一、SQLite 小工具（真值源）
# ============================================================

def _query(sql: str, params: tuple = ()) -> List[dict]:
    """查询并返回 dict 列表（行工厂临时切换，不污染全模块连接）。"""
    with db.get_conn() as conn:
        prev = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.row_factory = prev


def _json_list(value) -> list:
    """tech_stack 等 JSON 字符串字段解析失败时返回空列表。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        parsed = json.loads(value)
        return list(parsed) if isinstance(parsed, (list, tuple)) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ============================================================
# 二、Chroma 封装
# ============================================================

def get_client():
    """持久化 Chroma 客户端（单例，匿名化遥测）。"""
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(
            path=CHROMA_DIR,
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
    return _client


def get_collection():
    """获取/创建竞赛集合（cosine 空间）。"""
    global _collection
    if _collection is None:
        _collection = get_client().get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def count_indexed() -> int:
    """索引中的向量条数。"""
    return int(get_collection().count())


def _doc_id(cid: int) -> str:
    return f"{_ID_PREFIX}{cid}"


def _parse_id(doc_id: str) -> Optional[int]:
    if isinstance(doc_id, str) and doc_id.startswith(_ID_PREFIX):
        try:
            return int(doc_id[len(_ID_PREFIX):])
        except ValueError:
            return None
    return None


def _competition_doc(c: dict) -> str:
    """拼接用于向量化的文本。名称、简称、类别放最前面，权重最高。"""
    parts = [
        c.get("name") or "",
        c.get("short_name") or "",
        c.get("category") or "",
        c.get("level_label") or "",
        c.get("summary") or "",
        c.get("track") or "",
    ]
    return "\n".join(str(p).strip() for p in parts if p and str(p).strip())


def _competition_meta(c: dict) -> dict:
    """Chroma metadata 只支持 str/int/float/bool，None 需剔除。"""
    meta: Dict[str, Any] = {"competition_id": int(c["id"])}
    for key in ("name", "category", "level", "short_name", "level_label"):
        val = c.get(key)
        if val:
            meta[key] = str(val)
    return meta


# ============================================================
# 三、索引同步（SQLite -> Chroma）
# ============================================================

def _fingerprint() -> str:
    """目录指纹：条数 + 最大 id + 最新更新时间。任何增删改都会变，用于判断是否需要重建。"""
    row = _query(
        "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS max_id, "
        "COALESCE(MAX(last_checked_at), '') AS ts, "
        "COALESCE(SUM(LENGTH(name) + LENGTH(COALESCE(summary, ''))), 0) AS txt FROM competitions"
    )
    if not row:
        return "0:0::0"
    r = row[0]
    return f"{r['n']}:{r['max_id']}:{r['ts'] or ''}:{r['txt']}"


def _read_fingerprint() -> str:
    try:
        meta = get_collection().metadata or {}
    except Exception:  # noqa: BLE001
        return ""
    return str(meta.get("fingerprint") or "")


def sync_index(*, rebuild: bool = False) -> dict:
    """把 competitions 全量写入 Chroma。目录很小（<200 条），全量重建最稳。"""
    global _collection
    if rebuild:
        try:
            get_client().delete_collection(COLLECTION_NAME)
        except Exception:  # noqa: BLE001
            pass
        try:
            get_client().delete_collection(LEGACY_COLLECTION)   # 旧奖项索引不再使用
        except Exception:  # noqa: BLE001
            pass
        _collection = None

    rows = _query(
        "SELECT id, name, short_name, category, level, level_label, summary "
        "FROM competitions ORDER BY id"
    )
    rows = [r for r in rows if r.get("name")]
    if not rows:
        return {"total": 0, "synced": 0, "indexed": 0}

    ids = [_doc_id(int(r["id"])) for r in rows]
    docs = [_competition_doc(r) for r in rows]
    metas = [_competition_meta(r) for r in rows]

    synced = 0
    for start in range(0, len(rows), 64):
        chunk_ids = ids[start:start + 64]
        chunk_docs = docs[start:start + 64]
        chunk_metas = metas[start:start + 64]
        embs = embedder.encode_docs(chunk_docs)
        get_collection().upsert(
            ids=chunk_ids, documents=chunk_docs, metadatas=chunk_metas, embeddings=embs
        )
        synced += len(chunk_ids)

    # 指纹写进集合 metadata，下次直接比对，避免无谓重建
    try:
        col = get_collection()
        meta = dict(col.metadata or {})
        meta.update({"hnsw:space": "cosine", "fingerprint": _fingerprint()})
        col.modify(metadata=meta)
    except Exception:  # noqa: BLE001
        pass

    return {"total": len(rows), "synced": synced, "indexed": count_indexed()}


def ensure_index() -> dict:
    """索引缺失 / 条数不符 / 指纹变化时自动重建（搜索时调用，无定时任务依赖）。"""
    with _lock:
        if _embed_down():
            return {"error": "embedding cooldown", "indexed": count_indexed()}
        try:
            want = _fingerprint()
            if count_indexed() > 0 and _read_fingerprint() == want:
                return {"total": -1, "synced": 0, "indexed": count_indexed()}
            return sync_index(rebuild=True)
        except Exception as exc:  # noqa: BLE001
            try:
                _mark_embed_down(exc)
            except Exception:  # noqa: BLE001
                pass
            return {"error": repr(exc), "indexed": 0}


# ============================================================
# 四、检索
# ============================================================

_TOKEN_SPLIT = re.compile(r"[\s,，、;；/|+\-_:：()（）【】\[\]]+")
_SIGNIFICANT = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


def keyword_scores(query: str, rows: Optional[List[dict]] = None) -> Dict[int, float]:
    """关键词打分（0~1）：名称全等 > 名称包含 > 简称/类别 > 摘要，多词命中叠加。"""
    tokens = [t for t in _TOKEN_SPLIT.split(query or "") if t and _SIGNIFICANT.search(t)]
    uniq: List[str] = []
    for t in sorted({t for t in tokens}, key=len, reverse=True):
        if not any(t in u for u in uniq):
            uniq.append(t)
    tokens = uniq[:8]
    if not tokens:
        return {}

    if rows is None:
        rows = _query("SELECT id, name, short_name, category, summary FROM competitions")

    # 别称表（大挑/小挑/大广赛…）：正式名里没有这些俗称
    import search_alias
    amap = search_alias.alias_map(rows)

    out: Dict[int, float] = {}
    for row in rows:
        try:
            cid = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(row.get("name") or "")
        short = str(row.get("short_name") or "")
        category = str(row.get("category") or "")
        summary = str(row.get("summary") or "")

        best = 0.0
        multi = 0
        for tok in tokens:
            alias = search_alias.alias_score(cid, tok, amap)
            if tok == name:
                base = 1.0
            elif alias >= search_alias.ALIAS_EXACT:
                base = alias
            elif tok in name:
                base = 0.80
            elif alias > 0:
                base = alias
            elif tok in short:
                base = 0.65
            elif tok in category:
                base = 0.55
            elif tok in summary:
                base = 0.35
            else:
                continue
            multi += 1
            best = max(best, base)
        if best <= 0:
            continue
        out[cid] = round(min(1.0, best + 0.05 * (multi - 1)), 4)
    return out


_QUERY_CACHE: "OrderedDict[str, List[float]]" = OrderedDict()
_QUERY_CACHE_MAX = 256


def _query_vector(query: str) -> List[float]:
    """查询向量缓存：同一个词反复搜索（联想输入）不重复调用 embedding 接口。"""
    qvec = _QUERY_CACHE.get(query)
    if qvec is not None:
        _QUERY_CACHE.move_to_end(query)
        return qvec
    qvec = embedder.encode_query(query)
    _QUERY_CACHE[query] = qvec
    while len(_QUERY_CACHE) > _QUERY_CACHE_MAX:
        _QUERY_CACHE.popitem(last=False)
    return qvec


def _vector_scores(query: str) -> Dict[int, float]:
    """向量召回：目录小，全量召回后返回 {competition_id: 相似度}。"""
    total = count_indexed()
    if total == 0:
        return {}
    qvec = _query_vector(query)
    res = get_collection().query(
        query_embeddings=[qvec],
        n_results=total,
        include=["metadatas", "distances"],
    )
    ids = (res.get("ids") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]

    out: Dict[int, float] = {}
    for i, doc_id in enumerate(ids):
        cid = _parse_id(doc_id)
        if cid is None and i < len(metas) and metas[i]:
            try:
                cid = int(metas[i].get("competition_id"))
            except (TypeError, ValueError):
                cid = None
        if cid is None:
            continue
        dist = float(dists[i]) if i < len(dists) else 2.0
        out[cid] = 1.0 - dist          # cosine distance -> 相似度，可能为负
    return out


def retrieve(
    query: str,
    top_k: int = 8,
    *,
    with_debug: bool = False,
) -> List[dict]:
    """
    竞赛级语义检索主入口。

    :param query: 自然语言查询
    :param top_k: 返回条数
    :param with_debug: 额外返回 l1_score / kw_score 诊断字段
    :return: [{"competition_id", "score", "name", "category", ...}, ...] 按相关性降序
    """
    query = (query or "").strip()
    if not query:
        return []

    ensure_index()

    try:
        vec = _vector_scores(query) if not _embed_down() else {}
    except Exception as exc:  # noqa: BLE001
        _mark_embed_down(exc)
        vec = {}                      # 向量不可用 → 纯关键词，由调用方决定是否降级

    rows = _query("SELECT id, name, short_name, category, level, level_label, summary FROM competitions")
    row_map = {int(r["id"]): r for r in rows}
    kw = keyword_scores(query, rows)
    q_lower = query.lower()

    scored: List[dict] = []
    for cid, row in row_map.items():
        v = max(0.0, float(vec.get(cid, 0.0)))
        k = float(kw.get(cid, 0.0))
        if v < MIN_SIM and k <= 0:
            continue                                  # 既不相似也不沾关键词 → 丢弃

        name = str(row.get("name") or "")
        bonus = NAME_BONUS if (name and (name in query or query in name or q_lower in name.lower())) else 0.0
        final = min(1.0, VEC_WEIGHT * v + KW_WEIGHT * k + bonus)

        scored.append({
            "competition_id": cid,
            "score": round(final, 4),
            "final_score": round(final, 4),
            "name": name,
            "category": row.get("category"),
            "level": row.get("level"),
            "level_label": row.get("level_label"),
            "summary": row.get("summary"),
            "short_name": row.get("short_name"),
            **({"l1_score": round(v, 4), "kw_score": round(k, 4)} if with_debug else {}),
        })

    scored.sort(key=lambda x: (x["final_score"], x.get("l1_score", 0)), reverse=True)
    return scored[:top_k]


def stats() -> dict:
    """检索层自检信息（给页面/测试用）。"""
    return {
        "chroma_dir": CHROMA_DIR,
        "collection": COLLECTION_NAME,
        "indexed": count_indexed(),
        "fingerprint": _read_fingerprint(),
        "embedder": embedder.self_test(),
    }


if __name__ == "__main__":
    print(json.dumps(stats(), ensure_ascii=False, indent=2))