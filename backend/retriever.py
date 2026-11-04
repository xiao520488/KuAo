# -*- coding: utf-8 -*-
"""
检索内核 (retriever.py) —— S1

职责：
    1. Chroma 封装：集合管理 / upsert / delete / 计数（索引可重建）
    2. 两级检索：
         L1  向量召回   —— Chroma cosine 相似度，多召回（topK * over_fetch）
         L2  结构化重排 —— 回 SQLite 取 awards 真值，硬过滤 + 软加权
    3. 索引同步：sync_awards_to_chroma() 从 SQLite 全量重建/增量补齐

设计约定（与架构一致）：
    - 真值源永远是 SQLite；Chroma 只是可重建的向量索引
    - 向量库里的 id 使用 "award:{id}"，metadata 冗余少量筛选用字段
    - L2 以 SQLite 为准：Chroma 命中的行若已删除/未审核，直接丢弃
"""

import os

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"   # 需在任何 HF 库之前

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

import chromadb
from chromadb.config import Settings

import embedder
import kb

# ------------------------------------------------------------
# 配置
# ------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHROMA_DIR = os.getenv("KB_CHROMA_DIR", os.path.join(BASE_DIR, "chroma_db"))
COLLECTION_NAME = os.getenv("KB_COLLECTION", "awards")
RECALL_MULTIPLIER = int(os.getenv("KB_RECALL_MULT", "3"))   # L1 多召回倍数
MIN_SCORE = float(os.getenv("KB_MIN_SCORE", "0.0"))        # L1 最低相似度

_ID_PREFIX = "award:"

_client = None
_collection = None


# ============================================================
# 一、Chroma 封装
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
    """获取/创建 awards 集合（cosine 空间）。"""
    global _collection
    if _collection is None:
        _collection = get_client().get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def _doc_id(award_id: int) -> str:
    return f"{_ID_PREFIX}{award_id}"


def _parse_id(doc_id: str) -> Optional[int]:
    if isinstance(doc_id, str) and doc_id.startswith(_ID_PREFIX):
        try:
            return int(doc_id[len(_ID_PREFIX):])
        except ValueError:
            return None
    return None


def _award_to_text(a: dict) -> str:
    """拼接用于向量化的文档文本（标题权重靠前）。"""
    parts = [
        a.get("title") or "",
        a.get("competition_raw") or "",
        a.get("category") or "",
        a.get("track") or "",
        a.get("summary") or "",
        a.get("highlights") or "",
    ]
    # tech_stack 可能是 JSON 字符串
    ts = a.get("tech_stack")
    if ts:
        try:
            ts_list = json.loads(ts) if isinstance(ts, str) else ts
            if isinstance(ts_list, (list, tuple)):
                parts.append(" ".join(str(x) for x in ts_list))
            else:
                parts.append(str(ts_list))
        except (json.JSONDecodeError, TypeError):
            parts.append(str(ts))
    return "\n".join(p.strip() for p in parts if p and str(p).strip())


def _award_to_metadata(a: dict) -> dict:
    """Chroma metadata 只支持 str/int/float/bool，None 需剔除。"""
    meta = {
        "award_id": int(a["id"]),
        "competition_id": int(a["competition_id"]) if a.get("competition_id") is not None else -1,
        "year": int(a["year"]) if a.get("year") is not None else -1,
        "review_status": a.get("review_status") or "pending",
        "embedding_state": a.get("embedding_state") or "none",
    }
    for key in ("category", "level", "award_grade", "competition_raw", "title"):
        val = a.get(key)
        if val:
            meta[key] = str(val)
    return meta


def upsert_awards(awards: Sequence[dict]) -> int:
    """把 awards 行写入/更新到 Chroma。返回写入条数。"""
    awards = [a for a in awards if a.get("id") is not None]
    if not awards:
        return 0

    ids = [_doc_id(int(a["id"])) for a in awards]
    docs = [_award_to_text(a) for a in awards]
    metas = [_award_to_metadata(a) for a in awards]
    embs = embedder.encode_docs(docs)

    get_collection().upsert(ids=ids, documents=docs, metadatas=metas, embeddings=embs)
    return len(ids)


def delete_awards(award_ids: Iterable[int]) -> None:
    ids = [_doc_id(int(i)) for i in award_ids]
    if ids:
        get_collection().delete(ids=ids)


def count_indexed() -> int:
    """索引中的向量条数。"""
    return int(get_collection().count())


def reset_index() -> None:
    """清空并重建集合（仅重建索引，不动 SQLite 真值）。"""
    global _collection
    try:
        get_client().delete_collection(COLLECTION_NAME)
    except Exception:  # noqa: BLE001
        pass
    _collection = None
    get_collection()


# ============================================================
# 二、索引同步（SQLite -> Chroma）
# ============================================================

def sync_awards_to_chroma(
    *,
    only_approved: bool = True,
    rebuild: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """
    从 SQLite 同步 awards 到 Chroma。

    参数:
        only_approved: 只索引 review_status='approved' 的记录（默认 True）
        rebuild:       True 则先清空索引再全量重建
        limit:         限制同步条数（调试用）
    返回:
        {"total": 总数, "synced": 写入条数, "indexed": 索引内条数}
    """
    if rebuild:
        reset_index()

    conds = ["1=1"]
    params: List[Any] = []
    if only_approved:
        conds.append("review_status = ?")
        params.append("approved")

    where = " AND ".join(conds)
    sql = f"SELECT * FROM awards WHERE {where} ORDER BY id ASC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))

    rows = kb.query(sql, params)

    synced = 0
    for chunk_start in range(0, len(rows), 64):
        chunk = rows[chunk_start:chunk_start + 64]
        synced += upsert_awards(chunk)

    # 标记 embedding_state = 'ok'
    if rows:
        ids = [int(r["id"]) for r in rows]
        for i in range(0, len(ids), 200):
            batch = ids[i:i + 200]
            qs = ",".join("?" * len(batch))
            with kb.conn_ctx() as conn:
                conn.execute(
                    f"UPDATE awards SET embedding_state='ok' WHERE id IN ({qs})",
                    batch,
                )

    return {"total": len(rows), "synced": synced, "indexed": count_indexed()}


# ============================================================
# 三、两级检索
# ============================================================

def _l1_recall(query: str, recall_n: int, filters: Optional[dict] = None) -> List[dict]:
    """
    L1：Chroma 向量召回。

    返回 [{"award_id", "score", "document", "metadata"}, ...]（按 score 降序）
    filters 会转成 Chroma where 条件做粗过滤，降低无效召回。
    """
    if count_indexed() == 0:
        return []

    where = None
    if filters:
        clauses = []
        for key in ("category", "level", "award_grade"):
            val = filters.get(key)
            if val:
                clauses.append({key: {"$eq": str(val)}})
        # 注意：Chroma 的 where 要求每个表达式恰好一个算子，
        # 因此 year 区间必须拆成两条独立子句，再用 $and 组合。
        if filters.get("year_from") is not None:
            clauses.append({"year": {"$gte": int(filters["year_from"])}})
        if filters.get("year_to") is not None:
            clauses.append({"year": {"$lte": int(filters["year_to"])}})
        if len(clauses) == 1:
            where = clauses[0]
        elif len(clauses) > 1:
            where = {"$and": clauses}

    qvec = embedder.encode_query(query)
    res = get_collection().query(
        query_embeddings=[qvec],
        n_results=max(1, recall_n),
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    out: List[dict] = []
    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]

    for i, doc_id in enumerate(ids):
        aid = _parse_id(doc_id)
        if aid is None:
            continue
        dist = float(dists[i]) if i < len(dists) else 1.0
        # cosine distance ∈ [0, 2]（Chroma 未归一化时可 > 1），相似度可能为负。
        # L1 只做召回，不做分数截断：截断/加权统一交给 L2，避免误杀低分但合规的候选。
        score = 1.0 - dist
        out.append({
            "award_id": aid,
            "score": score,
            "document": docs[i] if i < len(docs) else "",
            "metadata": metas[i] if i < len(metas) else {},
        })

    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def _l2_rerank(
    candidates: Sequence[dict],
    *,
    filters: Optional[dict] = None,
    top_k: int = 8,
) -> List[dict]:
    """
    L2：回 SQLite 取真值，硬过滤 + 软加权重排。

    硬过滤：行不存在 / review_status 非 approved / embedding_state 非 ok -> 丢弃
    软加权：category、level、award_grade 精确匹配各 +0.05；命中年份区间 +0.03
    最终分 = L1 相似度 + 加成（上限 1.0），再截断 top_k
    """
    filters = filters or {}
    if not candidates:
        return []

    cand_map = {c["award_id"]: c for c in candidates}
    ids = list(cand_map.keys())

    rows: List[dict] = []
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        qs = ",".join("?" * len(batch))
        rows.extend(kb.query(f"SELECT * FROM awards WHERE id IN ({qs})", batch))
    row_map = {int(r["id"]): r for r in rows}

    results: List[dict] = []
    for aid, cand in cand_map.items():
        row = row_map.get(aid)
        if not row:
            continue                                   # 真值已被删除
        if row.get("review_status") != "approved":
            continue
        if row.get("embedding_state") != "ok":
            continue

        bonus = 0.0
        for key in ("category", "level", "award_grade"):
            want = filters.get(key)
            if want and str(row.get(key) or "") == str(want):
                bonus += 0.05
        yf, yt = filters.get("year_from"), filters.get("year_to")
        if yf is not None or yt is not None:
            y = row.get("year")
            if y is not None:
                if (yf is None or int(y) >= int(yf)) and (yt is None or int(y) <= int(yt)):
                    bonus += 0.03

        final = min(1.0, float(cand["score"]) + bonus)
        results.append({
            "award_id": aid,
            "score": round(float(cand["score"]), 4),
            "final_score": round(final, 4),
            "bonus": round(bonus, 4),
            "title": row.get("title"),
            "year": row.get("year"),
            "category": row.get("category"),
            "level": row.get("level"),
            "award_grade": row.get("award_grade"),
            "competition_raw": row.get("competition_raw"),
            "summary": row.get("summary"),
            "highlights": row.get("highlights"),
            "tech_stack": kb.json_loads(row.get("tech_stack"), []),
            "source_url": row.get("source_url"),
        })

    results.sort(key=lambda x: x["final_score"], reverse=True)
    return results[:top_k]


def retrieve(
    query: str,
    top_k: int = 8,
    *,
    filters: Optional[dict] = None,
    recall_multiplier: Optional[int] = None,
    with_debug: bool = False,
) -> List[dict]:
    """
    两级检索主入口。

    参数:
        query:  自然语言查询
        top_k:  最终返回条数
        filters: {"category","level","award_grade","year_from","year_to"}
        recall_multiplier: L1 召回倍数（默认 3）
        with_debug: 返回额外诊断字段（l1_count）
    返回:
        [{"award_id","score","final_score", ...awards 字段}, ...]
    """
    query = (query or "").strip()
    if not query:
        return []

    mult = recall_multiplier or RECALL_MULTIPLIER
    recall_n = max(top_k * mult, top_k)

    l1 = _l1_recall(query, recall_n, filters)
    l2 = _l2_rerank(l1, filters=filters, top_k=top_k)

    if with_debug:
        for item in l2:
            item["l1_count"] = len(l1)
    return l2


def stats() -> dict:
    """检索层自检信息（给页面/测试用）。"""
    return {
        "chroma_dir": CHROMA_DIR,
        "collection": COLLECTION_NAME,
        "indexed": count_indexed(),
        "embedder": embedder.self_test(),
    }


if __name__ == "__main__":
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
