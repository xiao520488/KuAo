# -*- coding: utf-8 -*-
"""cleanup_bad_covers.py —— 清理存量赛事封面里的垃圾图。

做什么
------
遍历 ``competitions.image_url``，逐张重新过 ``image_guard`` 严格闸门：

    * URL 含 qr/qrcode/erweima/logo/icon/banner 等 -> 置 NULL；
    * 404/410/不是图片/尺寸过小/比例畸形 -> 置 NULL；
    * 能扫出二维码/条码 -> 置 NULL；
    * 网络超时/5xx 等暂时性问题 -> 保留，标记「待复查」，不误删好图。

置 NULL 后前端卡片/详情页会自动走两字简称兜底，不会再出现拉伸的垃圾图。

安全约定
--------
* 默认 **dry-run**，只打印报告，不写库；
* 写生产库 ``competitions.db`` 必须显式 ``--allow-production``；
* ``--apply`` 写库前自动用 SQLite backup API 备份（WAL 安全）。

用法（backend 目录）
--------------------
    # 1. 只体检，不写库
    .\\venv\\Scripts\\python.exe cleanup_bad_covers.py --db-path .\\competitions.db --allow-production

    # 2. 确认报告后真正清理（自动备份）
    .\\venv\\Scripts\\python.exe cleanup_bad_covers.py --db-path .\\competitions.db --allow-production --apply

    # 3. 只清理指定 id
    .\\venv\\Scripts\\python.exe cleanup_bad_covers.py --db-path .\\competitions.db --allow-production --ids 34 --apply
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

import image_guard as guard

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老环境 / 被重定向
            pass


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}")


def backup_database(db_path: str) -> str:
    """用 SQLite backup API 备份，WAL 模式下也能得到一致快照。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{db_path}.bak-covers-{stamp}"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup_path


def load_rows(db_path: str, ids=None) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT id, name, image_url FROM competitions "
            "WHERE image_url IS NOT NULL AND TRIM(image_url) <> ''"
        )
        params = []
        if ids:
            sql += " AND id IN (%s)" % ",".join("?" for _ in ids)
            params.extend(ids)
        sql += " ORDER BY id"
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def classify(row: dict) -> dict:
    """返回 {action: keep/null/review, reason, info}。"""
    url = (row.get("image_url") or "").strip()
    if guard.is_bad_image_url(url):
        return {"action": "null", "reason": "URL 命中图片黑名单", "info": {}}

    ok, reason, info = guard.inspect_image(url)
    if ok:
        return {"action": "keep", "reason": "通过严格闸门", "info": info}
    if info.get("verdict") == "transient":
        return {"action": "review", "reason": reason, "info": info}
    return {"action": "null", "reason": reason, "info": info}


def apply_nulls(db_path: str, ids: list) -> int:
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.executemany(
                "UPDATE competitions SET image_url = NULL WHERE id = ?",
                [(i,) for i in ids],
            )
    finally:
        conn.close()
    return len(ids)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="清理存量赛事封面垃圾图（二维码 / logo / 比例畸形 / 死链）",
    )
    parser.add_argument("--db-path", default=PRODUCTION_DB_PATH,
                        help=f"目标数据库（默认 {PRODUCTION_DB_PATH}）")
    parser.add_argument("--allow-production", action="store_true",
                        help="写生产库 competitions.db 时必须显式加上")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认演练）")
    parser.add_argument("--no-backup", action="store_true",
                        help="跳过写库前备份（不推荐）")
    parser.add_argument("--ids", default=None, help="只处理指定 id，逗号分隔")
    parser.add_argument("--report", default=None, help="报告 JSON 落盘路径（可选）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    _setup_console()
    args = parse_args(argv)

    db_path = os.path.abspath(args.db_path)
    if not os.path.exists(db_path):
        log("错误", f"数据库不存在：{db_path}")
        return 2

    is_production = os.path.abspath(db_path) == os.path.abspath(PRODUCTION_DB_PATH)
    if is_production and not args.allow_production:
        log("错误", "目标库是生产库 competitions.db，必须显式加 --allow-production")
        return 2

    ids = None
    if args.ids:
        try:
            ids = [int(x) for x in args.ids.split(",") if x.strip()]
        except ValueError:
            log("错误", "--ids 需要逗号分隔的整数，例如 34,35")
            return 2

    rows = load_rows(db_path, ids=ids)
    print("=" * 74)
    print("赛事封面图清理（image_guard 严格闸门）")
    print("=" * 74)
    log("目标库", db_path)
    log("模式", "真实写库（先备份）" if args.apply else "演练：只体检，不写库")
    log("待检", f"{len(rows)} 张非空封面")
    if not rows:
        log("结论", "没有需要检查的封面图")
        return 0

    results = []
    for row in rows:
        outcome = classify(row)
        record = {**row, **outcome}
        results.append(record)
        mark = {"keep": "保留", "null": "置空", "review": "待复查"}[outcome["action"]]
        print(
            f"  [{mark}] id={row['id']:<3} {row['name'][:24]:<26} "
            f"{outcome['reason']}"
        )

    keep = [r for r in results if r["action"] == "keep"]
    nulls = [r for r in results if r["action"] == "null"]
    review = [r for r in results if r["action"] == "review"]

    print("-" * 74)
    log("汇总", f"保留 {len(keep)} / 置空 {len(nulls)} / 待复查 {len(review)}")

    backup_path = ""
    if nulls and args.apply:
        if args.no_backup:
            log("备份", "已按 --no-backup 跳过（不推荐）")
        else:
            try:
                backup_path = backup_database(db_path)
            except Exception as exc:  # noqa: BLE001 - 备份失败必须停
                log("错误", f"备份失败，已终止，未写入任何数据：{exc}")
                return 4
            log("备份", backup_path)

        apply_nulls(db_path, [r["id"] for r in nulls])
        log("写库", f"已把 {len(nulls)} 条比赛的 image_url 置为 NULL")

        remaining = load_rows(db_path, ids=[r["id"] for r in nulls])
        if remaining:
            log("警告", f"仍有 {len(remaining)} 条未清理，请复查数据库")
            return 5
        log("校验", "置空结果已确认：目标行 image_url 均为 NULL")
    elif nulls:
        log("提示", "本次未写库；确认报告后加 --apply 重跑")
    else:
        log("提示", "没有需要置空的封面图")

    report = {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db_path": db_path,
        "apply": bool(args.apply),
        "backup": backup_path,
        "stats": {
            "total": len(results),
            "keep": len(keep),
            "null": len(nulls),
            "review": len(review),
        },
        "results": [
            {
                "id": r["id"],
                "name": r["name"],
                "image_url": r["image_url"],
                "action": r["action"],
                "reason": r["reason"],
                "width": r["info"].get("width"),
                "height": r["info"].get("height"),
                "barcode": r["info"].get("barcode"),
            }
            for r in results
        ],
    }
    report_path = args.report or os.path.join(
        WORKSPACE_DIR,
        f"cover_cleanup_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json",
    )
    try:
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        log("报告", report_path)
    except OSError as exc:
        log("警告", f"报告写入失败：{exc}")

    if review:
        log("待复查", "以下图片本次网络未打通、已保留，建议稍后重跑：")
        for r in review:
            log("待复查", f"id={r['id']} {r['name'][:24]} —— {r['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
