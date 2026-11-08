# -*- coding: utf-8 -*-
"""daily_ai_worker.py —— 每天定时慢刷「参赛攻略 + 备赛灵感」。

设计（见 AI_AUTOMATION_PLAN.md 2.4）：
    * 每天只挑 3-5 条「国家级 + 有官网 + 攻略为空」的热门赛事；
    * 排序：获奖作品数 DESC -> sort_order（NULL 最后）-> id ASC；
    * 攻略先生成；同一批里再挑 1-2 条「有 >=3 条获奖作品」的生成灵感；
    * 失败冷却 3 天、单次预算熔断、token 全量记账；
    * 默认 dry-run，写生产库必须 `--apply --allow-production`（复用
      discover_common.prepare_target：自动备份 + integrity_check）。

用法::

    # 演练：只打印候选与预估成本，不写库、不调模型
    python daily_ai_worker.py --db-path competitions.db

    # 测试库真跑（2 条攻略 + 1 条灵感）
    python daily_ai_worker.py --db-path test_crawl.db --limit 2 --idea-limit 1 --apply

    # 生产库（cron 用）
    python daily_ai_worker.py --db-path competitions.db --apply --allow-production

退出码：0 全部成功/跳过；2 有失败；3 预算在开始前就耗尽；4 配置/参数错误。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime
from typing import List, Optional

import ai_content
import ai_jobs_runner
import db
import discover_common
import llm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "competitions.db")

# 预估 token（只用于 dry-run 展示与预算判断，真实用量以 llm.get_usage 为准）
EST_GUIDE_TOKENS = 4000
EST_INSPIRATION_TOKENS = 27000

logger = logging.getLogger("daily_ai_worker")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def setup_logging(verbose: bool = False) -> None:
    # 显式写 stdout：cron 里统一 `>> logs/xxx.log 2>&1`，但 PowerShell 下
    # stderr 会被当成 NativeCommandError，混在一起反而干扰排查。
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def _setup_console() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 避免中文日志乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老环境 / 已被重定向
            pass


def used_tokens() -> int:
    return int(llm.get_usage().get("total_tokens", 0))


def save_report(payload: dict) -> str:
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(WORKSPACE_DIR, "ai_worker_%s.json" % stamp)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="每天慢刷攻略 + 灵感（默认 dry-run，不写库、不调模型）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db-path", default=DEFAULT_DB_PATH,
        help="目标数据库（默认 %s）" % DEFAULT_DB_PATH,
    )
    parser.add_argument(
        "--limit", type=int, default=4,
        help="每天生成攻略的条数（夹在 1-5，默认 4）",
    )
    parser.add_argument(
        "--idea-limit", type=int, default=2,
        help="每天生成灵感的赛事数（默认 2；灵感一条约等于 5 次调用）",
    )
    parser.add_argument(
        "--min-awards", type=int, default=3,
        help="生成灵感所需的最少获奖作品数（默认 3）",
    )
    parser.add_argument(
        "--sleep", type=float, default=5.0,
        help="条间基础间隔秒数（会上下浮动 30%%，默认 5）",
    )
    parser.add_argument(
        "--budget-tokens", type=int, default=300000,
        help="本次运行 token 熔断上限（默认 300000）",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=0,
        help="单篇攻略 max_tokens（0 = 用 ai_content 默认 6144）",
    )
    parser.add_argument(
        "--idea-n", type=int, default=4,
        help="每条赛事生成多少条 idea（默认 4）",
    )
    parser.add_argument(
        "--cooldown-days", type=int, default=3,
        help="失败赛事的冷却天数（默认 3，避免天天撞同一条）",
    )
    parser.add_argument(
        "--only", choices=["all", "guide", "inspiration"], default="all",
        help="只跑攻略 / 只跑灵感 / 都跑（默认 all）",
    )
    parser.add_argument(
        "--include-provincial", action="store_true",
        help="放宽到省级赛事（默认只做国家级 + 有官网）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="显式演练（默认就是演练；保留该开关让 cron 误配也不写库）",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真正执行：写生产库必须同时传 --allow-production",
    )
    parser.add_argument(
        "--allow-production", action="store_true",
        help="解除生产库写入拦截（必须与 --apply 同时使用）",
    )
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(args) -> int:  # noqa: C901 - 主流程分支多但线性
    db_path = os.path.abspath(os.path.expanduser(args.db_path))
    apply = bool(args.apply) and not bool(args.dry_run)

    # 目标库保护 + 生产库备份（apply 时才备份）
    try:
        db_path = discover_common.prepare_target(db_path, apply, args.allow_production)
    except SystemExit as exc:  # 生产库缺少 --allow-production
        logger.error("%s", exc)
        return 4

    db.DB_PATH = db_path
    if apply:
        # 幂等补列 / 建 ai_jobs；dry-run 不碰库结构
        db.init_db()

    llm.reset_usage()
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    coverage_before = db.guide_coverage()
    guide_candidates: List[dict] = []
    inspiration_candidates: List[dict] = []

    if args.only in ("all", "guide"):
        guide_candidates = db.select_guide_candidates(
            max(1, min(int(args.limit), 5)),
            cooldown_days=args.cooldown_days,
            include_provincial=args.include_provincial,
        )
    if args.only in ("all", "inspiration"):
        inspiration_candidates = db.select_inspiration_candidates(
            max(1, int(args.idea_limit)),
            min_awards=args.min_awards,
            cooldown_days=args.cooldown_days,
            include_provincial=args.include_provincial,
        )

    logger.info("目标库：%s", db_path)
    logger.info(
        "攻略覆盖：%s/%s（缺口 %s）",
        coverage_before["filled"], coverage_before["total"], coverage_before["missing"],
    )
    logger.info("本次候选：攻略 %d 条 / 灵感 %d 条%s",
                len(guide_candidates), len(inspiration_candidates),
                "" if apply else "（dry-run，不写库、不调模型）")

    est_tokens = (len(guide_candidates) * EST_GUIDE_TOKENS
                  + len(inspiration_candidates) * EST_INSPIRATION_TOKENS)
    logger.info("预估 token：约 %s（预算上限 %s）", est_tokens, args.budget_tokens)

    for item in guide_candidates:
        logger.info(
            "  [攻略候选] id=%s %s（获奖 %s 条）",
            item.get("id"), item.get("name"), item.get("awards_count"),
        )
    for item in inspiration_candidates:
        logger.info(
            "  [灵感候选] id=%s %s（获奖 %s 条，已有 insights=%s / ideas=%s）",
            item.get("id"), item.get("name"), item.get("awards_count"),
            item.get("insights_count"), item.get("ideas_count"),
        )

    if not guide_candidates and not inspiration_candidates:
        logger.info("今天没有需要生成的赛事（攻略已齐 / 无足够获奖数据的灵感候选）。")

    report = {
        "started_at": started_at,
        "finished_at": None,
        "db_path": db_path,
        "apply": apply,
        "args": {
            "limit": args.limit,
            "idea_limit": args.idea_limit,
            "min_awards": args.min_awards,
            "budget_tokens": args.budget_tokens,
            "cooldown_days": args.cooldown_days,
            "only": args.only,
            "include_provincial": args.include_provincial,
        },
        "coverage_before": coverage_before,
        "selection": {
            "guide": guide_candidates,
            "inspiration": inspiration_candidates,
        },
        "items": [],
        "summary": {
            "guide_done": 0, "guide_failed": 0, "guide_skipped": 0,
            "inspiration_done": 0, "inspiration_failed": 0,
            "inspiration_skipped": 0,
            "tokens": 0, "budget_exhausted": False,
        },
    }

    if not apply:
        report["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        path = save_report(report)
        logger.info("[dry-run] 预计消耗约 %s tokens；报告：%s", est_tokens, path)
        return 0

    summary = report["summary"]
    failed_any = False

    def budget_left() -> bool:
        if used_tokens() >= args.budget_tokens:
            summary["budget_exhausted"] = True
            logger.warning(
                "token 预算已用尽（%s >= %s），停止后续任务",
                used_tokens(), args.budget_tokens,
            )
            return False
        return True

    # ---- 1) 攻略 ----
    for item in guide_candidates:
        if not budget_left():
            break
        cid = int(item["id"])
        claim = db.claim_ai_job(
            "guide", cid, trigger="daily",
            cooldown_minutes=max(args.cooldown_days, 0) * 24 * 60,
        )
        if not claim.get("claimed"):
            summary["guide_skipped"] += 1
            report["items"].append({
                "kind": "guide", "competition_id": cid, "name": item.get("name"),
                "status": "skipped", "reason": claim.get("reason"),
            })
            logger.info("  [攻略] id=%s 跳过（%s）", cid, claim.get("reason"))
            continue

        max_tokens = args.max_tokens or ai_content.GUIDE_MAX_TOKENS
        result = ai_jobs_runner.run_guide_job(
            claim["job_id"], cid, max_tokens=max_tokens,
        )
        entry = {
            "kind": "guide", "competition_id": cid, "name": item.get("name"),
            "job_id": claim["job_id"], "status": result.get("status"),
            "tokens": result.get("tokens", 0),
            "guide_length": result.get("guide_length"),
            "error": result.get("error"),
        }
        report["items"].append(entry)

        if result.get("status") == "done":
            summary["guide_done"] += 1
            logger.info(
                "  [攻略] id=%s 完成：%s 字，%s tokens",
                cid, result.get("guide_length"), result.get("tokens"),
            )
        elif result.get("status") == "skipped":
            summary["guide_skipped"] += 1
            logger.info("  [攻略] id=%s 跳过（已有内容）", cid)
        else:
            summary["guide_failed"] += 1
            failed_any = True
            logger.error("  [攻略] id=%s 失败：%s", cid, result.get("error"))

        _sleep_between(args.sleep)

    # ---- 2) 灵感 ----
    for item in inspiration_candidates:
        if not budget_left():
            break
        cid = int(item["id"])
        claim = db.claim_ai_job(
            "inspiration", cid, trigger="daily",
            cooldown_minutes=max(args.cooldown_days, 0) * 24 * 60,
        )
        if not claim.get("claimed"):
            summary["inspiration_skipped"] += 1
            report["items"].append({
                "kind": "inspiration", "competition_id": cid,
                "name": item.get("name"), "status": "skipped",
                "reason": claim.get("reason"),
            })
            logger.info("  [灵感] id=%s 跳过（%s）", cid, claim.get("reason"))
            continue

        result = ai_jobs_runner.run_inspiration_job(
            claim["job_id"], cid, idea_n=max(int(args.idea_n), 1),
        )
        report["items"].append({
            "kind": "inspiration", "competition_id": cid, "name": item.get("name"),
            "job_id": claim["job_id"], "status": result.get("status"),
            "tokens": result.get("tokens", 0),
            "insights_new": (result.get("detail") or {}).get("insights_new"),
            "ideas_new": (result.get("detail") or {}).get("ideas_new"),
            "error": result.get("error"),
        })

        if result.get("status") == "done":
            summary["inspiration_done"] += 1
            logger.info(
                "  [灵感] id=%s 完成：insights +%s / ideas +%s，%s tokens",
                cid, (result.get("detail") or {}).get("insights_new"),
                (result.get("detail") or {}).get("ideas_new"),
                result.get("tokens"),
            )
        elif result.get("status") == "skipped":
            summary["inspiration_skipped"] += 1
            logger.info("  [灵感] id=%s 跳过（已存在）", cid)
        else:
            summary["inspiration_failed"] += 1
            failed_any = True
            logger.error("  [灵感] id=%s 失败：%s", cid, result.get("error"))

        _sleep_between(args.sleep)

    summary["tokens"] = used_tokens()
    report["coverage_after"] = db.guide_coverage()
    report["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report["llm_usage"] = llm.get_usage()
    path = save_report(report)

    logger.info(
        "完成：攻略 %s 成功 / %s 失败 / %s 跳过；灵感 %s 成功 / %s 失败 / %s 跳过；"
        "共 %s tokens",
        summary["guide_done"], summary["guide_failed"], summary["guide_skipped"],
        summary["inspiration_done"], summary["inspiration_failed"],
        summary["inspiration_skipped"], summary["tokens"],
    )
    logger.info("报告：%s", path)

    if failed_any:
        return 2
    if summary["budget_exhausted"] and not (
        summary["guide_done"] or summary["inspiration_done"]
    ):
        return 3
    return 0


def _sleep_between(base_seconds: float) -> None:
    """条间随机延迟，避免固定节奏触发风控。"""
    if not base_seconds or base_seconds <= 0:
        return
    time.sleep(random.uniform(base_seconds * 0.7, base_seconds * 1.3))


def main(argv: Optional[list] = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    if args.limit < 1 or args.idea_limit < 0:
        logger.error("参数非法：--limit 必须 >= 1，--idea-limit 必须 >= 0")
        return 4
    if args.apply and args.dry_run:
        logger.error("--apply 与 --dry-run 互斥")
        return 4
    if args.budget_tokens <= 0:
        logger.error("--budget-tokens 必须 > 0")
        return 4

    try:
        return run(args)
    except KeyboardInterrupt:
        logger.warning("收到 Ctrl+C，已中断（已写入的数据保留）")
        return 130
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，避免 cron 只看到堆栈
        logger.exception("未预期异常：%s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
