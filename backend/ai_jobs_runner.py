# -*- coding: utf-8 -*-
"""按需攻略生成的任务编排层。

位置：``routers/competitions.py``（HTTP） -> 本模块（预算 / 抢占 / 线程池）
-> ``ai_content.py``（Prompt / 调模型 / 写库） -> ``db.ai_jobs``（状态）。

为什么单独一层：
    * 路由只关心 HTTP 语义（202/409/429），不掺线程与预算；
    * 定时任务 ``daily_ai_worker.py`` 复用同一套 ``run_guide_job``，保证
      失败处理、token 记账与按需接口完全一致。

并发模型（2026-11-08 定）：
    * 抢占用 ``db.claim_ai_job``（唯一部分索引 + INSERT OR IGNORE），
      跨进程/跨 worker 都不会重复生成；
    * 进程内线程池固定 1 个 worker，避免一次点开多个详情页同时烧 token；
    * 进程被杀后，下次抢占用 15 分钟租约回收“僵尸 running”。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

import ai_content
import config
import db
import llm

logger = logging.getLogger(__name__)

# 串行执行：一篇攻略 20-60s，串行足够且能把并发成本锁死
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="guide-gen")


# ---------------------------------------------------------------------------
# 预算 / 冷却辅助
# ---------------------------------------------------------------------------
def _seconds_until_midnight() -> int:
    """距离本地零点还有多少秒（用于 429 的 Retry-After）。"""
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(int((tomorrow - now).total_seconds()), 60)


def _tokens_delta(before: dict, after: dict) -> int:
    return max(
        int(after.get("total_tokens", 0)) - int(before.get("total_tokens", 0)), 0
    )


def _job_status_payload(competition_id: int) -> dict:
    """给 GET 状态接口 / 前端轮询用的稳定结构。"""
    state = db.get_guide_state(competition_id) or {}
    return {
        "competition_id": int(competition_id),
        "status": state.get("status", "idle"),
        "job_id": state.get("job_id"),
        "attempts": state.get("attempts", 0),
        "guide_length": state.get("guide_length", 0),
        "generated_at": state.get("generated_at"),
    }


# ---------------------------------------------------------------------------
# 执行体（按需接口与定时任务共用）
# ---------------------------------------------------------------------------
def run_guide_job(job_id: int, competition_id: int, *,
                  max_tokens: int = 0) -> dict:
    """执行一个已抢占的攻略任务；任何异常都落 failed，不向外抛。"""
    if not db.start_ai_job(job_id):
        # 已被别的执行体抢走 / 已被回收：静默退出，不重复生成
        return {"job_id": job_id, "status": "skipped"}

    before = llm.get_usage()
    try:
        result = ai_content.generate_guide_for(
            competition_id,
            max_tokens=int(max_tokens) if max_tokens else ai_content.GUIDE_MAX_TOKENS,
        )
        tokens = _tokens_delta(before, llm.get_usage())
        if result.get("status") == "exists":
            # 抢占后发现有别的进程已经写入（极小概率）：记 skipped，不算失败
            db.skip_ai_job(job_id, "攻略已存在，跳过生成")
            return {"job_id": job_id, "status": "skipped", "tokens": tokens}
        db.finish_ai_job(job_id, tokens=tokens)
        logger.info(
            "攻略生成完成 competition_id=%s job=%s %s字 tokens=%s",
            competition_id, job_id, result.get("guide_length"), tokens,
        )
        # 注意：不能 **result —— 它自带 status="generated"，会覆盖这里的 "done"
        return {
            "job_id": job_id,
            "status": "done",
            "tokens": tokens,
            "guide_length": result.get("guide_length"),
            "model": result.get("model"),
            "prompt_ver": result.get("prompt_ver"),
        }
    except Exception as exc:  # noqa: BLE001 - 后台任务必须自己兜住
        tokens = _tokens_delta(before, llm.get_usage())
        message = "%s: %s" % (type(exc).__name__, exc)
        db.fail_ai_job(job_id, message, tokens=tokens)
        logger.warning(
            "攻略生成失败 competition_id=%s job=%s tokens=%s %s",
            competition_id, job_id, tokens, message,
        )
        return {"job_id": job_id, "status": "failed", "error": message}


def run_inspiration_job(job_id: int, competition_id: int, *,
                        idea_n: int = 4, max_tokens: int = 8000) -> dict:
    """执行一个已抢占的灵感任务（insights + ideas），异常同样不向外抛。"""
    if not db.start_ai_job(job_id):
        return {"job_id": job_id, "status": "skipped"}

    before = llm.get_usage()
    try:
        result = ai_content.generate_inspiration(
            competition_id, idea_n=idea_n, max_tokens=max_tokens
        )
        tokens = _tokens_delta(before, llm.get_usage())
        if result.get("exit_code") != 0:
            db.fail_ai_job(
                job_id,
                "analyzer exit=%s（可能无获奖作品或模型调用失败）"
                % result.get("exit_code"),
                tokens=tokens,
            )
            return {"job_id": job_id, "status": "failed", "tokens": tokens,
                    "detail": result}
        if not result.get("insights_new") and not result.get("ideas_new"):
            # 已有完整 insights + ideas 时 analyzer 会直接跳过，不算失败
            db.skip_ai_job(job_id, "insights/ideas 已存在，未重复生成")
            return {"job_id": job_id, "status": "skipped", "tokens": tokens,
                    "detail": result}
        db.finish_ai_job(job_id, tokens=tokens)
        logger.info(
            "灵感生成完成 competition_id=%s job=%s insights+%s ideas+%s tokens=%s",
            competition_id, job_id, result.get("insights_new"),
            result.get("ideas_new"), tokens,
        )
        return {"job_id": job_id, "status": "done", "tokens": tokens,
                "detail": result}
    except Exception as exc:  # noqa: BLE001 - 后台任务必须自己兜住
        tokens = _tokens_delta(before, llm.get_usage())
        message = "%s: %s" % (type(exc).__name__, exc)
        db.fail_ai_job(job_id, message, tokens=tokens)
        logger.warning(
            "灵感生成失败 competition_id=%s job=%s tokens=%s %s",
            competition_id, job_id, tokens, message,
        )
        return {"job_id": job_id, "status": "failed", "error": message,
                "tokens": tokens}


# ---------------------------------------------------------------------------
# 按需入口
# ---------------------------------------------------------------------------
def request_guide(competition_id: int, *, user_id: Optional[int] = None,
                  request_ip: Optional[str] = None,
                  force: bool = False) -> dict:
    """处理一次「生成攻略」请求，返回 HTTP 语义结果。

    返回 ``{"http": 200/202/409/429, "payload": {...}}``：

      * 200 已有攻略（fe force=False）
      * 202 已入队 / 已在跑（幂等，返回同一个 job_id）
      * 409 失败冷却中（retry_after_sec）
      * 429 当日预算或队列已满
    """
    cid = int(competition_id)
    comp = db.get_competition(cid)
    if comp is None:
        raise LookupError("竞赛 ID=%s 不存在" % cid)

    existing = (comp.get("full_process_guide") or "").strip()
    if existing and not force:
        return {
            "http": 200,
            "payload": {
                "competition_id": cid,
                "status": "ready",
                "guide_length": len(existing),
            },
        }

    if db.count_active_ai_jobs("guide") >= max(config.AI_GUIDE_QUEUE_MAX, 1):
        return {
            "http": 429,
            "payload": {
                "competition_id": cid,
                "status": "throttled",
                "reason": "queue_full",
            },
            "retry_after_sec": 60,
        }

    if db.count_ai_jobs_today("guide") >= max(config.AI_GUIDE_DAILY_LIMIT, 1):
        return {
            "http": 429,
            "payload": {
                "competition_id": cid,
                "status": "throttled",
                "reason": "daily_budget",
            },
            "retry_after_sec": _seconds_until_midnight(),
        }

    claim = db.claim_ai_job(
        "guide", cid,
        trigger="on_demand",
        requested_by=user_id,
        request_ip=request_ip,
        cooldown_minutes=max(config.AI_GUIDE_FAIL_COOLDOWN_MIN, 0),
        allow_existing=bool(force),
    )

    if not claim.get("claimed"):
        if claim.get("reason") == "exists":
            # 抢占锁内发现攻略已被别的请求写完：按“已有攻略”返回
            latest = db.get_competition(cid) or {}
            length = len((latest.get("full_process_guide") or "").strip())
            return {
                "http": 200,
                "payload": {
                    "competition_id": cid,
                    "status": "ready",
                    "guide_length": length,
                },
            }
        if claim.get("reason") == "cooldown":
            return {
                "http": 409,
                "payload": {
                    "competition_id": cid,
                    "status": "failed",
                    "reason": "cooldown",
                },
                "retry_after_sec": int(claim.get("retry_after_sec") or 60),
            }
        job_id = claim.get("job_id")
        return {
            "http": 202,
            "payload": {
                "competition_id": cid,
                "status": "running",
                "job_id": job_id,
            },
            "poll_after_ms": 3000,
        }

    job_id = int(claim["job_id"])
    _EXECUTOR.submit(run_guide_job, job_id, cid)
    return {
        "http": 202,
        "payload": {
            "competition_id": cid,
            "status": "queued",
            "job_id": job_id,
        },
        "poll_after_ms": 3000,
    }


def get_guide_status(competition_id: int) -> dict:
    """GET 状态：直接复用详情接口的同一份计算，避免两处口径漂移。"""
    return _job_status_payload(competition_id)
