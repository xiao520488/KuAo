# -*- coding: utf-8 -*-
"""生产库在线备份（SQLite backup API，WAL 安全）。

为什么不用 cp / scp：
    ``competitions.db`` 处于 WAL 模式时，最近提交的事务可能还在
    ``competitions.db-wal`` 里。直接复制 ``.db`` 会得到一个缺少最近写入的
    「旧快照」；只复制 ``.db`` 而漏掉 ``-wal`` 更糟。

本脚本用 ``sqlite3.Connection.backup()`` 在同一个一致性读事务里把
「主库 + 已提交的 WAL」一起写进**单个**备份文件，因此：

* 不需要停 uvicorn / crawler；
* 备份文件是自包含的，恢复时不需要再带 ``-wal`` / ``-shm``；
* 备份完成后立即做 ``PRAGMA integrity_check`` 并统计核心表行数。

用法（在 backend 目录、venv 下执行）::

    ./venv/bin/python backup_db.py
    ./venv/bin/python backup_db.py --db competitions.db --backup-dir backups --keep-days 30
    ./venv/bin/python backup_db.py --label daily --json

退出码：
    0  备份成功且校验通过
    2  参数错误 / 源库不存在 / 备份失败 / 校验失败
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "competitions.db")
DEFAULT_BACKUP_DIR = os.path.join(BASE_DIR, "backups")

# 备份里要统计行数的核心表（不存在的表自动跳过，兼容老库）
CORE_TABLES = ("competitions", "awards", "competition_candidates", "ai_jobs")


def _sha256(path: str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_uri(path: str, mode: str = "ro") -> str:
    """把本地路径转成 sqlite3 能识别的 file: URI（兼容 Windows 盘符）。"""
    from pathlib import Path

    uri = Path(path).resolve().as_uri()
    return "%s?mode=%s" % (uri, mode)


def _table_counts(conn: sqlite3.Connection) -> dict:
    counts = {}
    for table in CORE_TABLES:
        try:
            counts[table] = conn.execute(
                "SELECT COUNT(*) FROM %s" % table
            ).fetchone()[0]
        except sqlite3.Error:
            continue
    return counts


def _prune(backup_dir: str, prefix: str, keep_days: int, keep_count: int,
           keep_path: str, log) -> list:
    """只清理本脚本生成的备份，绝不碰目录里的其它文件。"""
    removed = []
    if keep_days <= 0 and keep_count <= 0:
        return removed

    candidates = []
    for name in os.listdir(backup_dir):
        if not name.startswith(prefix):
            continue
        path = os.path.join(backup_dir, name)
        if not os.path.isfile(path) or os.path.abspath(path) == os.path.abspath(keep_path):
            continue
        candidates.append(path)

    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    for index, path in enumerate(candidates):
        too_old = False
        if keep_days > 0:
            cutoff = datetime.now() - timedelta(days=keep_days)
            too_old = datetime.fromtimestamp(os.path.getmtime(path)) < cutoff
        # keep_path（本次刚生成的备份）不在 candidates 里，所以要 +2：
        # keep_count=1 时不应再保留任何历史备份。
        over_count = keep_count > 0 and (index + 2) > keep_count
        if too_old or over_count:
            try:
                os.remove(path)
                removed.append(path)
                log("[清理] 已删除旧备份：%s" % path)
            except OSError as exc:
                log("[清理] 删除失败（跳过）：%s -> %s" % (path, exc))
    return removed


def make_backup(db_path: str, backup_dir: str, label: str = "",
                keep_days: int = 30, keep_count: int = 0,
                verify: bool = True, log=print) -> dict:
    db_path = os.path.abspath(os.path.expanduser(db_path))
    backup_dir = os.path.abspath(os.path.expanduser(backup_dir))

    if not os.path.isfile(db_path):
        raise RuntimeError("源库不存在：%s" % db_path)

    os.makedirs(backup_dir, exist_ok=True)
    try:
        os.chmod(backup_dir, 0o700)
    except OSError:
        pass  # Windows / 无权限时忽略

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(db_path)
    name = "%s.bak-%s" % (base, stamp)
    if label:
        name += "-" + label
    target = os.path.join(backup_dir, name)

    # 同一秒重复执行时追加序号，永不覆盖已有备份
    suffix = 1
    while os.path.exists(target):
        target = os.path.join(backup_dir, "%s-%d" % (name, suffix))
        suffix += 1

    log("[备份] 源库：%s" % db_path)
    log("[备份] 目标：%s" % target)

    src = None
    dst = None
    try:
        src = sqlite3.connect(_sqlite_uri(db_path, "ro"), uri=True, timeout=30)
        src.execute("PRAGMA busy_timeout = 10000")
        # 只读连接下 backup 会取一致性快照，已提交的 WAL 内容一并纳入
        dst = sqlite3.connect(target)
        src.backup(dst, pages=256, sleep=0.05)
        # 备份文件落成 DELETE 模式：恢复时是单文件，不依赖 -wal / -shm
        dst.execute("PRAGMA journal_mode = DELETE")
        dst.commit()
    except Exception:
        for conn in (dst, src):
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        if os.path.exists(target):
            os.remove(target)
        raise
    finally:
        for conn in (dst, src):
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    result = {
        "db": db_path,
        "backup": target,
        "bytes": os.path.getsize(target),
        "sha256": _sha256(target),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "integrity": "skipped",
        "counts": {},
    }

    if verify:
        conn = sqlite3.connect(_sqlite_uri(target, "ro"), uri=True, timeout=30)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            result["integrity"] = integrity
            result["counts"] = _table_counts(conn)
        finally:
            conn.close()
        if result["integrity"] != "ok":
            os.remove(target)
            raise RuntimeError("备份完整性校验失败：%s" % result["integrity"])

    result["removed"] = _prune(
        backup_dir, base + ".bak-", keep_days, keep_count, target, log
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SQLite 在线备份（WAL 安全；默认保留 30 天）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="源数据库（默认 %s）" % DEFAULT_DB)
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR,
                        help="备份目录（默认 %s）" % DEFAULT_BACKUP_DIR)
    parser.add_argument("--label", default="",
                        help="备份文件名后缀，如 daily / pre-migrate")
    parser.add_argument("--keep-days", type=int, default=30,
                        help="保留天数，0 = 不按时间清理（默认 30）")
    parser.add_argument("--keep-count", type=int, default=0,
                        help="最多保留份数，0 = 不限制（默认 0）")
    parser.add_argument("--no-verify", action="store_true",
                        help="跳过 integrity_check 与行数统计（不建议）")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 输出结果，便于 cron / 监控采集")
    parser.add_argument("--quiet", action="store_true", help="只输出最终结果")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    quiet = args.quiet or args.json

    def log(message: str) -> None:
        if not quiet:
            print("%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))

    try:
        result = make_backup(
            db_path=args.db,
            backup_dir=args.backup_dir,
            label=args.label,
            keep_days=args.keep_days,
            keep_count=args.keep_count,
            verify=not args.no_verify,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001 - cron 需要明确的非 0 退出码
        print("[备份失败] %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("[备份完成] %s（%d 字节，integrity=%s）"
              % (result["backup"], result["bytes"], result["integrity"]))
        if result["counts"]:
            print("[行数] " + " / ".join(
                "%s=%d" % (k, v) for k, v in result["counts"].items()
            ))
        print("[SHA-256] %s" % result["sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
