# -*- coding: utf-8 -*-
"""generate_guides.py —— 「参赛全过程攻略」批量生成 CLI（兼容入口）。

2026-11-08 起：
    * 真正的 Prompt / 调模型 / 写库逻辑已抽到 ``ai_content.py``（v2 Prompt，
      会喂报名月份、比赛月份、最多 12 条获奖作品，禁止编造时间）；
    * 本脚本只保留命令行参数与批量循环，**老命令仍然可用**：
          python generate_guides.py --only-missing
          python generate_guides.py --id 1,5,9 --force
          python generate_guides.py --limit 10 --dry-run
    * 日常自动生成请用 ``daily_ai_worker.py``（有预算、冷却、任务表与报告）；
      本脚本适合人工补跑 / 单条重刷。

新增安全开关（与仓库红线一致）：
    * ``--db-path``：以前写死默认库，现在可指向测试库；
    * 写生产库必须 ``--allow-production``，且写前用 SQLite backup API 自动备份；
    * ``--dry-run`` 不再执行 init_db()（旧版会顺手改库文件）。

退出码：0 全部成功；2 有失败；4 参数/目标库错误。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

import ai_content
import db
import discover_common

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "competitions.db")

# 外层兜底重试（llm 内部已有 3 次指数退避，这里只处理“重试耗尽”的抖动）
MAX_ATTEMPTS = 3
RETRY_DELAY = 3.0


def parse_ids(raw: Optional[str]) -> Optional[List[int]]:
    """解析 --id 参数，如 "1,5,9" -> [1, 5, 9]。"""
    if not raw:
        return None
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise SystemExit("非法的比赛 ID：%r" % part)
    return ids or None


def generate_with_retry(comp: dict, *, max_tokens: int) -> dict:
    """带外层重试的生成（返回 ai_content.generate_guide 的结构）。"""
    cid = comp.get("id")
    last_exc: Optional[Exception] = None
    awards = db.list_awards_by_competition(cid) if cid else []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return ai_content.generate_guide(
                comp, awards,
                max_tokens=max_tokens or ai_content.GUIDE_MAX_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整体
            last_exc = exc
            print("  [id=%s] 第 %d/%d 次失败：%s: %s"
                  % (cid, attempt, MAX_ATTEMPTS, type(exc).__name__, exc))
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY)
    assert last_exc is not None
    raise last_exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="批量生成比赛参赛全过程攻略并回写数据库（v2 Prompt）"
    )
    parser.add_argument("--only-missing", action="store_true",
                        help="只处理 full_process_guide 为空的比赛（断点续跑）")
    parser.add_argument("--force", action="store_true",
                        help="即使已有攻略也重新生成并覆盖")
    parser.add_argument("--id", dest="ids", default=None,
                        help="只处理指定比赛 ID，逗号分隔，如 1,5,9")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多处理多少条比赛")
    parser.add_argument("--sleep", type=float, default=0.5,
                        help="每条之间的间隔秒数（默认 0.5）")
    parser.add_argument("--max-tokens", type=int, default=0,
                        help="单篇 max_tokens（0 = ai_content 默认 6144）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只生成并打印长度，不写库（也不执行 init_db）")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH,
                        help="目标数据库（默认 %s）" % DEFAULT_DB_PATH)
    parser.add_argument("--allow-production", action="store_true",
                        help="写生产库 competitions.db 时必须显式声明")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    args = build_parser().parse_args(argv)
    db_path = os.path.abspath(os.path.expanduser(args.db_path))
    if not os.path.exists(db_path):
        print("[错误] 目标库不存在：%s" % db_path)
        return 4

    print("[目标库] %s" % db_path)
    is_prod = discover_common.is_production_db(db_path)
    if is_prod:
        print("[目标库] ⚠ 这是生产库 competitions.db")
        if not args.dry_run and not args.allow_production:
            print("[拒绝] 写生产库必须同时传 --allow-production")
            return 4

    db.DB_PATH = db_path
    if not args.dry_run:
        # 先备份再改库结构：init_db() 本身会补列/建表，属于写操作
        if is_prod:
            print("[备份] %s" % discover_common.backup_db(db_path))
        # 补列 + 建 ai_jobs；dry-run 保持只读语义
        db.init_db()

    only_missing = args.only_missing and not args.force
    try:
        comps = db.list_competitions_for_guide(only_missing=only_missing)
    except Exception as exc:  # noqa: BLE001
        print("[错误] 读取比赛列表失败：%s: %s" % (type(exc).__name__, exc))
        return 4

    wanted_ids = parse_ids(args.ids)
    if wanted_ids:
        wanted = set(wanted_ids)
        comps = [c for c in comps if c.get("id") in wanted]
        missing = wanted - {c.get("id") for c in comps}
        if missing:
            print("[警告] 以下 ID 未找到或已被过滤：%s" % sorted(missing))

    if not args.force:
        comps = [c for c in comps
                 if not (c.get("full_process_guide") or "").strip()]

    if args.limit is not None and args.limit >= 0:
        comps = comps[: args.limit]

    total = len(comps)
    if total == 0:
        print("[完成] 没有需要处理的比赛")
        return 0

    print("[开始] 待处理 %d 条%s"
          % (total, "（dry-run，不写库）" if args.dry_run else ""))

    ok, failed = 0, 0
    for idx, comp in enumerate(comps, 1):
        cid = comp.get("id")
        name = comp.get("name") or "ID=%s" % cid
        print("[%d/%d] 生成：%s (id=%s)" % (idx, total, name, cid))
        try:
            result = generate_with_retry(comp, max_tokens=args.max_tokens)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("  [id=%s] 失败：%s: %s" % (cid, type(exc).__name__, exc))
            continue

        if args.dry_run:
            print("  [id=%s] [dry-run] 生成 %d 字，未写入" % (cid, len(result["markdown"])))
        else:
            try:
                db.save_generated_guide(
                    cid, result["markdown"], model=result["model"],
                    prompt_ver=result["prompt_ver"], input_hash=result["input_hash"],
                )
                ok += 1
                print("  [id=%s] 已写入（%d 字，%d tokens）"
                      % (cid, len(result["markdown"]), result["tokens"]))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("  [id=%s] 写库失败：%s: %s" % (cid, type(exc).__name__, exc))
                continue

        if args.sleep and idx < total:
            time.sleep(args.sleep)

    print("[完成] 成功 %d 条，失败 %d 条，共 %d 条" % (ok, failed, total))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
