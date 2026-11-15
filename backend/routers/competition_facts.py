# -*- coding: utf-8 -*-
"""赛事事实核对接口 (routers/competition_facts.py)

Routes（全部要求管理员）
------------------------
GET    /api/admin/competition-facts                  待确认队列（默认 status=pending）
POST   /api/admin/competition-facts/fetch            抓官网 + 抽取，写入队列（最多 5 条）
POST   /api/admin/competition-facts/{sid}/apply      采纳队列里的字段 → 写主表
POST   /api/admin/competition-facts/{sid}/reject     忽略一条（不改主表）
POST   /api/admin/competition-facts/{sid}/unknown    标记「官网未公布」（不清空现值）
GET    /api/admin/competitions/{cid}/facts           某赛事的台账（字段/值/来源/核对时间）
PUT    /api/admin/competitions/{cid}/facts           手工校正日期（可清空）

设计要点
--------
  * 抽取结果永远先进 staging，管理员点「采纳」才改 competitions；
  * 采纳/手工校正会写 competition_fact_ledger，前台据此显示"信息核对"；
  * 「官网未公布」只在台账里记 NULL，不覆盖已有日期（避免误清数据）；
  * fetch 同步执行且限制最多 5 条：单条 30-60 秒，后台按钮等得起，
    也不至于把接口拖到超时（要全量批跑用 CLI：python competition_facts.py）。
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

import competition_facts
import db
from security import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["competition-facts"])

MAX_FETCH_PER_REQUEST = 5


def _serialize_staging(item: dict) -> dict:
    """队列出参：把 payload 里的 facts/diffs/evidence 摊平给前端。"""
    payload = item.get("payload") or {}
    return {
        "id": item["id"],
        "competition_id": item["competition_id"],
        "competition_name": item.get("competition_name"),
        "official_url": item.get("official_url") or item.get("source_url"),
        "source_url": item.get("source_url"),
        "status": item.get("status"),
        "note": item.get("note"),
        "fetched_at": item.get("fetched_at"),
        "facts": payload.get("facts") or {},
        "diffs": payload.get("diffs") or [],
        "evidence": payload.get("evidence") or "",
        "current": {
            field: item.get(field)
            for field in competition_facts.FACT_FIELDS
        },
    }


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------
@router.get("/admin/competition-facts", summary="赛事事实待确认队列")
def list_facts_queue(
    status: str = Query("pending", description="pending / applied / rejected / all"),
    limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(get_current_admin),
) -> dict:
    """列出抽取结果队列；status=all 时返回全部状态。"""
    if status == "all":
        status = ""
    elif status not in ("pending", "applied", "rejected"):
        raise HTTPException(status_code=422, detail="status 只能是 pending/applied/rejected/all")
    rows = db.list_fact_staging(status=status, limit=limit)
    return {
        "code": 0,
        "total": len(rows),
        "items": [_serialize_staging(r) for r in rows],
    }


class FetchRequest(BaseModel):
    """触发抽取的请求体（都可省略）。"""

    competition_ids: Optional[List[int]] = Field(
        None, description="指定赛事 ID；省略则自动挑最久没核对的"
    )
    limit: int = Field(
        MAX_FETCH_PER_REQUEST, ge=1, le=MAX_FETCH_PER_REQUEST,
        description="本次最多处理几条",
    )
    source_url: Optional[str] = Field(
        None, max_length=500,
        description="官网首页没有日期时，可指定具体通知页/赛程页链接",
    )


@router.post("/admin/competition-facts/fetch", summary="抓官网抽日期入队")
def fetch_facts(
    body: Optional[FetchRequest] = None,
    admin: dict = Depends(get_current_admin),
) -> dict:
    """同步抓取 + 抽取（最多 5 条），结果进队列等人工确认。"""
    ids = [int(i) for i in ((body.competition_ids if body else None) or [])]
    ids = ids[:MAX_FETCH_PER_REQUEST]
    limit = min((body.limit if body else MAX_FETCH_PER_REQUEST), MAX_FETCH_PER_REQUEST)
    try:
        result = competition_facts.stage_batch(
            ids or None, limit=limit,
            source_url=(body.source_url if body else None),
        )
    except Exception as exc:  # noqa: BLE001 - 外部站点/模型都可能炸
        logger.exception("[事实] 批量抽取失败")
        raise HTTPException(status_code=502, detail="抽取失败：%s" % str(exc)[:200])
    logger.info("[事实] 管理员 %s 触发抽取：%s 条入队",
                admin["username"], result.get("queued"))
    return {"code": 0, "message": "已入队 %d 条" % result.get("queued"), **result}


# ---------------------------------------------------------------------------
# 处理单条：采纳 / 忽略 / 标记未公布
# ---------------------------------------------------------------------------
class ApplyRequest(BaseModel):
    """采纳哪些字段；省略 = 采纳全部有差异的字段。"""

    fields: Optional[List[str]] = Field(None, description="要采纳的字段名列表")


@router.post("/admin/competition-facts/{sid}/apply", summary="采纳抽取结果")
def apply_facts(
    sid: int = Path(..., description="队列记录 id"),
    body: ApplyRequest = ApplyRequest(),
    admin: dict = Depends(get_current_admin),
) -> dict:
    """把队列里的日期写进主表，并记台账。"""
    item = db.get_fact_staging(sid)
    if not item:
        raise HTTPException(status_code=404, detail="队列记录不存在")
    if item.get("status") != "pending":
        raise HTTPException(status_code=409, detail="这条记录已处理过")

    facts = (item.get("payload") or {}).get("facts") or {}
    want = [f for f in (body.fields or list(facts.keys())) if f in facts]
    if not want:
        raise HTTPException(status_code=422, detail="没有可采纳的字段")
    patch = {field: facts[field] for field in want}
    try:
        written = db.set_competition_facts(
            item["competition_id"], patch,
            source_url=item.get("source_url"),
            verified_by=admin["username"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    db.decide_fact_staging(sid, "applied", note=None,
                           decided_by=admin["username"])
    logger.info("[事实] %s 采纳 staging=%s 字段=%s",
                admin["username"], sid, sorted(written))
    return {"code": 0, "message": "已采纳", "written": written}


@router.post("/admin/competition-facts/{sid}/reject", summary="忽略抽取结果")
def reject_facts(
    sid: int = Path(..., description="队列记录 id"),
    note: Optional[str] = Query(None, max_length=200),
    admin: dict = Depends(get_current_admin),
) -> dict:
    """忽略一条：不改主表，只把队列标为 rejected。"""
    item = db.get_fact_staging(sid)
    if not item:
        raise HTTPException(status_code=404, detail="队列记录不存在")
    db.decide_fact_staging(sid, "rejected", note=note, decided_by=admin["username"])
    return {"code": 0, "message": "已忽略"}


class UnknownRequest(BaseModel):
    """标记官网未公布哪些字段。"""

    fields: Optional[List[str]] = Field(None, description="字段名列表；省略=全部")


@router.post("/admin/competition-facts/{sid}/unknown", summary="标记官网未公布")
def unknown_facts(
    sid: int = Path(..., description="队列记录 id"),
    body: UnknownRequest = UnknownRequest(),
    admin: dict = Depends(get_current_admin),
) -> dict:
    """台账记 NULL（前台显示"以官网为准"），主表保持原样。"""
    item = db.get_fact_staging(sid)
    if not item:
        raise HTTPException(status_code=404, detail="队列记录不存在")
    fields = body.fields or list(competition_facts.FACT_FIELDS)
    try:
        db.mark_fact_unknown(
            item["competition_id"], fields,
            source_url=item.get("source_url"), verified_by=admin["username"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    db.decide_fact_staging(sid, "applied", note="官网未公布",
                           decided_by=admin["username"])
    return {"code": 0, "message": "已标记为官网未公布"}


# ---------------------------------------------------------------------------
# 手工校正
# ---------------------------------------------------------------------------
class FactsPatch(BaseModel):
    """手工校正的四个日期（空值 = 清空）与来源。"""

    reg_start_date: Optional[str] = None
    reg_end_date: Optional[str] = None
    contest_start_date: Optional[str] = None
    contest_end_date: Optional[str] = None
    source_url: Optional[str] = Field(None, max_length=500)


@router.get("/admin/competitions/{cid}/facts", summary="赛事事实台账")
def get_competition_facts(
    cid: int = Path(..., description="赛事 id"),
    admin: dict = Depends(get_current_admin),
) -> dict:
    """返回台账明细，前端在详情页展示"信息核对"。"""
    row = db.get_competition(cid)
    if not row:
        raise HTTPException(status_code=404, detail="赛事不存在")
    return {
        "code": 0,
        "competition_id": cid,
        "facts_checked_at": row.get("facts_checked_at"),
        "current": {f: row.get(f) for f in competition_facts.FACT_FIELDS},
        "ledger": db.list_competition_facts(cid),
    }


@router.put("/admin/competitions/{cid}/facts", summary="手工校正赛事日期")
def save_competition_facts(
    body: FactsPatch,
    cid: int = Path(..., description="赛事 id"),
    admin: dict = Depends(get_current_admin),
) -> dict:
    """管理员直接改日期（来源 URL 可选），同时写台账。"""
    payload = {k: v for k, v in body.model_dump().items()
               if k != "source_url" and v is not None}
    if body.source_url is not None:
        payload_source = body.source_url
    else:
        payload_source = None
    try:
        written = db.set_competition_facts(
            cid, payload, source_url=payload_source, verified_by=admin["username"],
        )
    except ValueError as exc:
        status = 404 if "不存在" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc))
    logger.info("[事实] %s 手工校正 cid=%s 字段=%s",
                admin["username"], cid, sorted(written))
    return {"code": 0, "message": "已保存", "written": written}