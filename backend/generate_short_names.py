# -*- coding: utf-8 -*-
"""generate_short_names.py —— 批量生成「2 字赛事简称」CLI。

作用
----
遍历 competitions 主表，调用大模型为每条赛事提炼 2 个纯汉字简称，
写入 ``competitions.short_name``（如「中国大学生计算机设计大赛」->「计设」）。
该字段只用于无图卡片的 Logo 式兜底，**真实封面图永远优先**。

安全开关
--------
* 默认目标库为 ``test_crawl.db``（本地测试库），不碰生产库；
* 写生产库必须显式 ``--allow-production``；
* 任何写库前都先用 SQLite backup API 自动备份（WAL 安全）；
* ``--dry-run`` 只调用模型并打印结果，不建列、不写库（零副作用）；
* 默认只处理 short_name 为空的行，``--force`` 才会覆盖已有简称。

用法（backend 目录）：
    .\\venv\\Scripts\\python.exe generate_short_names.py --limit 5
    .\\venv\\Scripts\\python.exe generate_short_names.py --limit 5 --dry-run
    .\\venv\\Scripts\\python.exe generate_short_names.py --id 1,5,9 --force
    .\\venv\\Scripts\\python.exe generate_short_names.py `
        --db-path .\\competitions.db --allow-production

退出码：0 全部成功；2 有失败；4 参数/目标库错误。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

import db
import discover_common
import short_names

DEFAULT_DB_PATH = discover_common.DEFAULT_DB_PATH

# 外层兜底重试（llm 内部已有 3 次指数退避，这里只处理“重试耗尽”的抖动）
MAX_ATTEMPTS = 3
RETRY_DELAY = 2.0


def log(message: str) -> None:
    print(message, flush=True)


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


def extract_with_retry(name: str, *, max_tokens: int) -> str:
    """带外层重试的简称提取；重试耗尽后抛出最后一次异常。"""
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return short_names.extract_short_name(name, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整体
            last_exc = exc
            log("  [第 %d/%d 次失败] %s: %s" % (
                attempt, MAX_ATTEMPTS, type(exc).__name__, exc))
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY)
    assert last_exc is not None
    raise last_exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="批量生成 2 字赛事简称（competitions.short_name）"
    )
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH,
                        help="目标数据库（默认测试库 %s）" % DEFAULT_DB_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="只调用模型并打印结果，不写库（也不建列）")
    parser.add_argument("--force", action="store_true",
                        help="已有 short_name 也重新生成并覆盖")
    parser.add_argument("--id", dest="ids", default=None,
                        help="只处理指定比赛 ID，逗号分隔，如 1,5,9")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多处理多少条比赛（0 表示全部）")
    parser.add_argument("--sleep", type=float, default=0.3,
                        help="每条之间的间隔秒数（默认 0.3）")
    parser.add_argument("--max-tokens", type=int,
                        default=short_names.SHORT_NAME_MAX_TOKENS,
                        help="单次 max_tokens（默认 %d）"
                             % short_names.SHORT_NAME_MAX_TOKENS)
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
        log("[错误] 目标库不存在：%s" % db_path)
        return 4

    log("[目标库] %s" % db_path)
    is_prod = discover_common.is_production_db(db_path)
    if is_prod:
        log("[目标库] ⚠ 这是生产库 competitions.db")
        if not args.dry_run and not args.allow_production:
            log("[拒绝] 写生产库必须同时传 --allow-production")
            return 4

    # 关键：db 模块内部所有 get_conn() 都走 DB_PATH
    db.DB_PATH = db_path

    if not args.dry_run:
        # 先备份再改库结构：init_db() 会补 short_name 列，属于写操作
        try:
            log("[备份] %s" % discover_common.backup_db(db_path))
        except Exception as exc:  # noqa: BLE001 - 备份失败必须停下
            log("[错误] 备份失败，已终止，未修改目标库：%s" % exc)
            return 4
        db.init_db()
        log("[结构] 已确保 competitions.short_name 列存在")

    try:
        comps = db.list_competitions_for_short_names(
            only_missing=not args.force, db_path=db_path
        )
    except Exception as exc:  # noqa: BLE001
        log("[错误] 读取比赛列表失败：%s: %s" % (type(exc).__name__, exc))
        return 4

    wanted_ids = parse_ids(args.ids)
    if wanted_ids:
        wanted = set(wanted_ids)
        comps = [c for c in comps if c.get("id") in wanted]
        missing = wanted - {c.get("id") for c in comps}
        if missing:
            log("[警告] 以下 ID 未找到或已有简称（可加 --force）：%s"
                % sorted(missing))

    if args.limit is not None and args.limit > 0:
        comps = comps[: args.limit]

    total = len(comps)
    if total == 0:
        log("[完成] 没有需要处理的比赛（已有简称可加 --force 重刷）")
        return 0

    log("[开始] 待处理 %d 条%s" % (
        total, "（dry-run，不写库）" if args.dry_run else ""))

    ok, failed = 0, 0
    for idx, comp in enumerate(comps, 1):
        cid = comp.get("id")
        name = comp.get("name") or "ID=%s" % cid
        log("[%d/%d] %s (id=%s)" % (idx, total, name, cid))
        try:
            short_name = extract_with_retry(name, max_tokens=args.max_tokens)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log("  [失败] %s: %s" % (type(exc).__name__, exc))
            continue

        if args.dry_run:
            log("  [dry-run] -> %s（未写入）" % short_name)
        else:
            try:
                db.save_short_name(cid, short_name, db_path=db_path)
                ok += 1
                log("  [已写入] -> %s" % short_name)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log("  [写库失败] %s: %s" % (type(exc).__name__, exc))
                continue

        if args.sleep and idx < total:
            time.sleep(args.sleep)

    log("[完成] 成功 %d 条，失败 %d 条，共 %d 条"
        % (ok if not args.dry_run else total - failed, failed, total))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
