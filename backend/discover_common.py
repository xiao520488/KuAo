# -*- coding: utf-8 -*-
"""discover_common.py —— 新赛事发现的公共工具（候选表专用）

被 discover_university.py / discover_saikr.py 复用：
  * 目标库保护：默认 test_crawl.db；写生产库必须 --apply --allow-production；
  * 写前自动备份（SQLite backup API + integrity_check）；
  * 候选表建表（只建 competition_candidates，不跑全量 schema）；
  * 统一入库循环 + 去重统计 + 预览 JSON。

红线（与 CRAWLER_HANDOVER.md 第四/九节一致）：
  * 发现脚本默认 dry-run，只读不写；
  * 只写候选表，绝不自动 INSERT competitions；
  * 生产库写入必须备份成功后才继续。
"""
import json
import os
import sqlite3
from datetime import datetime

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "test_crawl.db")
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")


def log(message: str) -> None:
    print(message, flush=True)


def is_production_db(db_path: str) -> bool:
    return os.path.abspath(db_path) == os.path.abspath(PRODUCTION_DB_PATH)


def backup_db(db_path: str) -> str:
    """SQLite backup API 备份（WAL 安全），备份名带时间戳且不覆盖。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = "%s.bak-%s" % (db_path, stamp)
    if os.path.exists(backup_path):
        raise RuntimeError("备份文件已存在，拒绝覆盖：%s" % backup_path)

    src = sqlite3.connect(db_path)
    try:
        dest = sqlite3.connect(backup_path)
        try:
            src.backup(dest)
            dest.execute("PRAGMA journal_mode = DELETE")
        finally:
            dest.close()
    finally:
        src.close()

    check = sqlite3.connect(backup_path)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if integrity != "ok":
        raise RuntimeError("备份完整性校验失败：%s" % integrity)
    return backup_path


def prepare_target(db_path: str, apply: bool, allow_production: bool) -> str:
    """校验目标库；apply 时按需备份 + 建候选表。返回绝对路径。"""
    path = os.path.abspath(db_path)
    if not os.path.exists(path):
        raise SystemExit("目标库不存在：%s" % path)

    log("[目标库] %s" % path)
    if is_production_db(path):
        log("[目标库] ⚠ 这是生产库 competitions.db")
        if apply and not allow_production:
            raise SystemExit("拒绝写生产库：必须同时传 --allow-production")
        if apply:
            log("[备份] %s" % backup_db(path))

    if apply:
        created = db.ensure_candidates_table(path)
        log("[候选表] %s" % ("已新建 competition_candidates" if created else "已存在"))
    return path


def save_preview(filename: str, payload: dict) -> str:
    path = os.path.join(WORKSPACE_DIR, filename)
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


_CATEGORY_RULES = (
    ("数学建模", ("数学建模", "数学竞赛", "统计建模")),
    ("机器人", ("机器人", "robocup", "机械臂")),
    ("程序设计", ("程序设计", "编程", "软件测试", "算法设计", "acm", "icpc", "蓝桥")),
    ("安全与人工智能", ("信息安全", "网络安全", "人工智能", "算法", "智能", "大数据")),
    ("物联网/嵌入式/ICT", ("物联网", "嵌入式", "芯片", "集成电路", "ict", "电子设计", "通信")),
    ("计算机综合与软件", ("计算机", "软件", "信息技术", "数字化", "互联网")),
    ("创新创业", ("创新创业", "创业", "创客", "电子商务", "商业", "营销", "管理")),
)


def guess_category(name: str) -> str:
    """按名称关键词猜一个现有 7 类之一；猜不到落「待确认」。"""
    text = (name or "").lower()
    for category, keywords in _CATEGORY_RULES:
        if any(keyword in text for keyword in keywords):
            return category
    return "待确认"


def ingest(items: list, db_path: str, apply: bool) -> dict:
    """逐条写候选表（或 dry-run 预演），返回统计。

    apply=False 时仍会查询候选表/主表做去重判断，但不写任何数据。
    """
    stats = {
        "total": len(items),
        "inserted": 0,
        "duplicate": 0,
        "invalid": 0,
        "inserted_names": [],
        "duplicates": [],
    }
    for item in items:
        if not item.get("raw_name") or not item.get("norm_name"):
            stats["invalid"] += 1
            continue

        if apply:
            result = db.add_candidate(item, db_path=db_path)
        else:
            duplicate = db.find_duplicate_candidate(
                item["norm_name"], item.get("official_url"), db_path=db_path
            )
            result = ({"status": "duplicate", "existing": duplicate}
                      if duplicate else {"status": "inserted"})

        if result["status"] == "inserted":
            stats["inserted"] += 1
            stats["inserted_names"].append(item["raw_name"])
        elif result["status"] == "duplicate":
            stats["duplicate"] += 1
            if len(stats["duplicates"]) < 30:
                existing = result.get("existing") or {}
                stats["duplicates"].append({
                    "name": item["raw_name"],
                    "where": existing.get("where"),
                    "existing_name": existing.get("name"),
                    "reason": result.get("reason") or existing.get("reason"),
                })
    return stats


def print_ingest_summary(stats: dict, apply: bool, sample: int = 10) -> None:
    action = "已写入" if apply else "可插入"
    log("[候选] %s %d 条 / 重复跳过 %d / 无效 %d（共 %d）" % (
        action, stats["inserted"], stats["duplicate"],
        stats["invalid"], stats["total"],
    ))
    for name in stats["inserted_names"][:sample]:
        log("        + %s" % name)
    if len(stats["inserted_names"]) > sample:
        log("        ... 其余 %d 条见预览 JSON" %
            (len(stats["inserted_names"]) - sample))
    for item in stats["duplicates"][:5]:
        log("        = 跳过：%s（%s：%s）" % (
            item["name"], item.get("where"), item.get("existing_name"),
        ))
