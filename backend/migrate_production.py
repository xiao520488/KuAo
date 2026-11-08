# -*- coding: utf-8 -*-
"""
migrate_production.py —— 生产库体检 + 备份 + 补列迁移（先备份，后动手）

用途
----
把 backend/competitions.db（生产库）迁移到最新表结构：
    1) 自动备份 -> competitions.db.bak（WAL 安全，用 SQLite backup API）；
    2) 校验备份可用（integrity_check + 行数比对）；
    3) 备份通过后才调用 db.init_db() 幂等补列（image_url / contest_*_month 等）；
    4) 迁移后复检：新列是否齐全、行数是否保持、SHA-256 变化、备份文件路径。

安全设计（与 CRAWLER_HANDOVER.md 第四节红线对齐）
--------------------------------------------------
* 默认 **只体检不迁移**：不带 --apply 时全程只读（以 mode=ro 打开），
  不建 -wal 文件、不执行任何写操作。
* 真正迁移必须显式 --apply；备份失败或校验不通过则立即终止，绝不硬上。
* 迁移只做 ADD COLUMN / CREATE TABLE IF NOT EXISTS / 索引，
  db.init_db() 额外会把空 summary 由 intro 回填、空 tech_stack 置 '[]'，
  脚本会在动手前打印这两类回填影响的行数。
* 迁移前后打印行数与 SHA-256，便于事后核对；不删除、不重建任何表。

用法（在 backend 目录、激活 venv 后）
------------------------------------
    # 1. 只体检（只读，推荐先跑）
    .\\venv\\Scripts\\python.exe migrate_production.py

    # 2. 真正迁移（自动备份 -> 校验 -> 补列）
    .\\venv\\Scripts\\python.exe migrate_production.py --apply

    # 3. 指定别的库 / 备份路径（例如先在测试副本上演练）
    .\\venv\\Scripts\\python.exe migrate_production.py --db-path .\\test_crawl.db --apply
    .\\venv\\Scripts\\python.exe migrate_production.py --apply --backup-path .\\competitions.db.bak

建议
----
* 迁移前先停掉后端（避免 WAL 未 checkpoint / 迁移期间并发写）。
* 备份文件不要提交进仓库；确认迁移无误后再决定是否保留。
"""

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")
DEFAULT_BACKUP_PATH = os.path.join(BASE_DIR, "competitions.db.bak")

# 本次迁移关心的新列（体检 & 迁移后复检都用它）
# 2026-11-08 追加：攻略生成溯源 4 列（配合 ai_jobs 表，见 AI_AUTOMATION_PLAN.md）
# 2026-11-08 追加：赛氪详情补充 3 列（enrich_saikr_details.py 写入）
TARGET_COLUMNS = (
    "image_url", "contest_start_month", "contest_end_month",
    "guide_generated_at", "guide_model", "guide_prompt_ver", "guide_input_hash",
    "participants", "content_scope", "exam_schedule",
)

# 需要比对行数的核心表
COUNTED_TABLES = ("competitions", "awards")


# ===========================================================================
# 一、基础工具
# ===========================================================================

def log(step: str, message: str) -> None:
    print(f"[{step}] {message}")


def _setup_console() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 避免中文日志乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老环境 / 已被重定向
            pass


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def sqlite_uri(path: str, mode: str = "ro") -> str:
    """把 Windows 路径转成合法的 SQLite URI（自动处理盘符与空格）。"""
    from pathlib import Path

    return Path(path).resolve().as_uri() + f"?mode={mode}"


def connect_readonly(path: str) -> sqlite3.Connection:
    """只读连接：不创建 -wal/-shm，不产生任何写副作用。"""
    try:
        conn = sqlite3.connect(sqlite_uri(path), uri=True, timeout=10)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"无法以只读方式打开 {path}：{exc}\n"
            "  常见原因：后端正在运行且 WAL 未 checkpoint。请先停止后端服务再重试。"
        ) from exc
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> list:
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def table_names(conn: sqlite3.Connection) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return {row[0] for row in rows}


def row_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return -1


# ===========================================================================
# 二、体检（只读）
# ===========================================================================

def inspect(db_path: str) -> dict:
    """只读体检：表、列、行数、待补列、回填影响面。"""
    conn = connect_readonly(db_path)
    try:
        tables = table_names(conn)
        if "competitions" not in tables:
            raise RuntimeError(f"{db_path} 里没有 competitions 表，拒绝继续（可能指错了库）")

        comp_cols = table_columns(conn, "competitions")
        award_cols = table_columns(conn, "awards")
        counts = {t: row_count(conn, t) for t in COUNTED_TABLES}
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]

        # 依赖 db.py 的迁移字典，避免脚本和代码各维护一份列清单
        import db  # 延迟导入：确保 --help / 报错时不引入副作用

        missing_comp = [
            col for col in db._MIGRATION_COLUMNS
            if col not in comp_cols
        ]
        missing_award = [
            col for col in db._AWARD_MIGRATION_COLUMNS
            if col not in award_cols
        ]

        backfill_summary = _safe_scalar(
            conn,
            "SELECT COUNT(*) FROM competitions "
            "WHERE (summary IS NULL OR summary = '') "
            "AND intro IS NOT NULL AND intro <> ''",
        )
        backfill_tech = _safe_scalar(
            conn,
            "SELECT COUNT(*) FROM competitions "
            "WHERE tech_stack IS NULL OR TRIM(tech_stack) = '' "
            "OR LOWER(TRIM(tech_stack)) = 'null'",
        )
    finally:
        conn.close()

    return {
        "db_path": db_path,
        "integrity": integrity,
        "tables": sorted(tables),
        "counts": counts,
        "comp_columns": comp_cols,
        "missing_comp": missing_comp,
        "missing_award": missing_award,
        "target_missing": [c for c in TARGET_COLUMNS if c not in comp_cols],
        "has_ai_jobs": "ai_jobs" in tables,
        "backfill_summary": backfill_summary,
        "backfill_tech": backfill_tech,
        "sha256": sha256_file(db_path),
        "size": os.path.getsize(db_path),
    }


def _safe_scalar(conn: sqlite3.Connection, sql: str) -> int:
    try:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return -1


def print_inspection(info: dict, db_path: str) -> None:
    log("体检", f"目标库：{db_path}")
    log("体检", f"文件大小 {human_size(info['size'])}；SHA-256 {info['sha256']}")
    log("体检", f"integrity_check = {info['integrity']}")
    log("体检", f"表：{', '.join(info['tables'])}")
    log(
        "体检",
        "行数：" + "，".join(f"{t}={n}" for t, n in info["counts"].items()),
    )

    if info["missing_comp"]:
        log("体检", f"competitions 待补列（{len(info['missing_comp'])}）：{', '.join(info['missing_comp'])}")
    else:
        log("体检", "competitions 列已是最新，无需补列")

    if info["missing_award"]:
        log("体检", f"awards 待补列（{len(info['missing_award'])}）：{', '.join(info['missing_award'])}")
    else:
        log("体检", "awards 列已是最新，无需补列")

    total_missing = len(info["missing_comp"]) + len(info["missing_award"])
    if total_missing == 0 and info["has_ai_jobs"]:
        log("体检", "结论：库结构已对齐，迁移可跳过（--apply 也只会重复执行幂等 DDL）")
    else:
        log(
            "体检",
            f"结论：共 {total_missing} 列待补"
            f"{'' if info['has_ai_jobs'] else ' + 缺 ai_jobs 表'}，"
            "执行 --apply 会自动先备份再补列",
        )

    log(
        "体检",
        "ai_jobs（AI 生成任务表）：" + ("已存在" if info["has_ai_jobs"] else "缺失，需建表"),
    )

    log(
        "体检",
        f"回填影响面：summary 为空将被 intro 回填 {info['backfill_summary']} 行；"
        f"tech_stack 为空将被置 '[]' {info['backfill_tech']} 行",
    )


# ===========================================================================
# 三、备份
# ===========================================================================

def _rotate_existing_backup(backup_path: str) -> str:
    """已有备份先改名保留，绝不静默覆盖历史备份。返回改名后的路径（无则空串）。"""
    if not os.path.exists(backup_path):
        return ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rotated = f"{backup_path}.{stamp}"
    os.replace(backup_path, rotated)
    return rotated


def make_backup(db_path: str, backup_path: str) -> str:
    """用 SQLite backup API 生成一致性备份（WAL 下有未 checkpoint 的写入也安全）。"""
    rotated = _rotate_existing_backup(backup_path)

    src = connect_readonly(db_path)
    try:
        dest_conn = sqlite3.connect(backup_path)
        try:
            src.backup(dest_conn)
            dest_conn.execute("PRAGMA journal_mode = DELETE")
        finally:
            dest_conn.close()
    except sqlite3.Error:
        # 备份 API 不可用时退回文件拷贝，但仅限没有 -wal 尾巴的情况
        if os.path.exists(db_path + "-wal") and os.path.getsize(db_path + "-wal") > 0:
            raise RuntimeError(
                "backup API 失败且存在未 checkpoint 的 WAL，拒绝用文件拷贝冒充一致备份。\n"
                "  请先停止后端服务，再重新执行。"
            )
        shutil.copy2(db_path, backup_path)
    finally:
        src.close()

    if not os.path.exists(backup_path):
        raise RuntimeError(f"备份文件未生成：{backup_path}")
    return rotated


def verify_backup(db_path: str, backup_path: str, expected_counts: dict) -> None:
    """备份必须能打开、完整、行数与源库一致，否则拒绝迁移。"""
    conn = sqlite3.connect(sqlite_uri(backup_path), uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"备份 integrity_check 未通过：{integrity}")

        for table, expected in expected_counts.items():
            if expected < 0:
                continue
            actual = row_count(conn, table)
            if actual != expected:
                raise RuntimeError(
                    f"备份行数不一致：{table} 源库 {expected} / 备份 {actual}"
                )
    finally:
        conn.close()

    backup_sha = sha256_file(backup_path)
    log("备份", f"备份校验通过：integrity_check=ok，行数一致，SHA-256 {backup_sha}")
    log("备份", f"备份文件：{backup_path}（{human_size(os.path.getsize(backup_path))}）")


# ===========================================================================
# 四、迁移
# ===========================================================================

def apply_migration(db_path: str) -> None:
    import db

    # 关键：把 db 模块的默认库指向目标库，init_db() 内部走 get_conn() -> DB_PATH
    db.DB_PATH = db_path
    db.init_db()


def post_check(db_path: str, before: dict) -> bool:
    """迁移后复检：目标列齐全、行数不变。返回是否全部通过。"""
    after = inspect(db_path)
    ok = True

    log("复检", f"目标列 {', '.join(TARGET_COLUMNS)}：")
    missing = [c for c in TARGET_COLUMNS if c not in after["comp_columns"]]
    if missing:
        ok = False
        log("复检", f"✗ 仍缺列：{', '.join(missing)}")
    else:
        log("复检", "✓ 目标列已全部存在")

    if after["missing_comp"] or after["missing_award"]:
        ok = False
        log("复检", f"✗ 仍有待补列：{after['missing_comp'] + after['missing_award']}")
    else:
        log("复检", "✓ competitions / awards 列已全部对齐")

    for table, expected in before["counts"].items():
        actual = after["counts"].get(table, -1)
        if actual != expected:
            ok = False
            log("复检", f"✗ {table} 行数变化：迁移前 {expected} -> 迁移后 {actual}")
        else:
            log("复检", f"✓ {table} 行数保持 {actual}")

    if after["integrity"] != "ok":
        ok = False
        log("复检", f"✗ integrity_check = {after['integrity']}")
    else:
        log("复检", "✓ integrity_check = ok")

    log("复检", f"迁移后 SHA-256 {after['sha256']}")

    if after["has_ai_jobs"]:
        log("复检", "✓ ai_jobs 表已存在")
    else:
        ok = False
        log("复检", "✗ 仍缺 ai_jobs 表")
    return ok


# ===========================================================================
# 五、入口
# ===========================================================================

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="生产库迁移：先自动备份 competitions.db，再执行 db.init_db() 补列。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="默认只体检（只读）；真正迁移必须显式 --apply。",
    )
    parser.add_argument(
        "--db-path",
        default=PRODUCTION_DB_PATH,
        help=f"待迁移的数据库（默认生产库 {PRODUCTION_DB_PATH}）",
    )
    parser.add_argument(
        "--backup-path",
        default=None,
        help=f"备份文件路径（默认 <db-path>.bak，即 {DEFAULT_BACKUP_PATH}）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正执行：先备份并校验，再调用 db.init_db() 补列",
    )
    parser.add_argument(
        "--skip-backup-check",
        action="store_true",
        help="跳过备份完整性校验（不推荐，仅在备份 API 受限时使用）",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    _setup_console()
    args = parse_args(argv)
    db_path = os.path.abspath(args.db_path)
    backup_path = os.path.abspath(
        args.backup_path
        or (
            DEFAULT_BACKUP_PATH
            if db_path == os.path.abspath(PRODUCTION_DB_PATH)
            else db_path + ".bak"
        )
    )

    print("=" * 72)
    print("生产库迁移脚本（先备份，后补列）")
    print("=" * 72)

    if not os.path.exists(db_path):
        log("错误", f"数据库不存在：{db_path}")
        return 2
    if os.path.isdir(db_path):
        log("错误", f"--db-path 指向的是目录：{db_path}")
        return 2
    if os.path.abspath(db_path) == os.path.abspath(backup_path):
        log("错误", "备份路径不能与目标库相同")
        return 2

    try:
        info = inspect(db_path)
    except RuntimeError as exc:
        log("错误", str(exc))
        return 3

    print_inspection(info, db_path)

    if not args.apply:
        print()
        print("-" * 72)
        log("预检", "当前为只读预检模式，未做任何修改。")
        log("预检", "确认无误后执行：python migrate_production.py --apply")
        print("-" * 72)
        return 0

    print()
    print("-" * 72)
    log("备份", f"准备备份 -> {backup_path}")

    rotated = ""
    try:
        rotated = make_backup(db_path, backup_path)
    except Exception as exc:  # noqa: BLE001 - 备份失败必须停下
        log("错误", f"备份失败，已终止，未对目标库做任何修改：{exc}")
        return 4
    if rotated:
        log("备份", f"已存在旧备份，先改名保留为：{rotated}")
    log("备份", "备份完成，开始校验")

    if args.skip_backup_check:
        log("备份", "已按 --skip-backup-check 跳过校验（不推荐）")
    else:
        try:
            verify_backup(db_path, backup_path, info["counts"])
        except Exception as exc:  # noqa: BLE001 - 校验失败必须停下
            log("错误", f"备份校验失败，已终止，未对目标库做任何修改：{exc}")
            log("错误", f"请人工检查备份文件：{backup_path}")
            return 5

    print("-" * 72)
    log("迁移", f"开始执行 db.init_db()（目标库：{db_path}）")
    try:
        apply_migration(db_path)
    except Exception as exc:  # noqa: BLE001 - 迁移失败提示回滚方式
        log("错误", f"迁移失败：{exc}")
        log("回滚", f"先删除 '{db_path}-wal' / '{db_path}-shm'，再执行：")
        log("回滚", f"       Copy-Item '{backup_path}' '{db_path}' -Force")
        return 6

    print("-" * 72)
    ok = post_check(db_path, info)
    print("-" * 72)
    if ok:
        log("完成", "迁移成功，行数未变，目标列已补齐")
        log("完成", "如需回滚：先删除目标库旁的 -wal / -shm，再用备份覆盖：")
        log("完成", f"       Copy-Item '{backup_path}' '{db_path}' -Force")
        return 0
    log("完成", f"迁移已执行但复检未全通过，请检查；备份在 {backup_path}")
    return 7


if __name__ == "__main__":
    sys.exit(main())
