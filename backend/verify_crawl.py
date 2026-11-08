# -*- coding: utf-8 -*-
"""
verify_crawl.py —— 从指定数据库读回爬虫写入的 award，并校验关键字段。

用途：
    真实联调后，用独立查询代码证明 source_url / award_grade / competition_id
    等字段确实落库，而不是只看写入函数的返回值。

用法（在 backend 目录）：
    python verify_crawl.py --db-path test_crawl.db --award-id 86 87
    python verify_crawl.py --db-path test_crawl.db          # 看最近 10 条 crawl 记录

红线：
    默认拒绝查询生产库 competitions.db，请指向测试副本。
"""

import argparse
import json
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROD_DB = os.path.join(BASE_DIR, "competitions.db")

SQL = """
SELECT
    a.id,
    a.competition_id,
    a.competition_raw,
    a.title,
    a.year,
    a.award_level,
    a.award_grade,
    a.team,
    a.summary,
    a.source_url,
    a.source_type,
    a.confidence,
    a.review_status,
    a.embedding_state,
    a.content_hash,
    c.name              AS competition_name,
    c.category          AS competition_category,
    c.level             AS competition_level,
    c.official_url      AS competition_official_url
FROM awards a
LEFT JOIN competitions c ON c.id = a.competition_id
"""


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def fetch_rows(db_path: str, award_ids=None, limit: int = 10) -> list:
    if award_ids:
        placeholders = ", ".join("?" for _ in award_ids)
        sql = f"{SQL} WHERE a.id IN ({placeholders}) ORDER BY a.id"
        params = list(award_ids)
    else:
        sql = f"{SQL} WHERE a.source_type = 'crawl' ORDER BY a.id DESC LIMIT ?"
        params = [limit]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def check_row(row: dict) -> list:
    """返回失败项列表；空列表代表全部通过。"""
    failures = []
    if not (row.get("source_url") or "").startswith(("http://", "https://")):
        failures.append("source_url 不是 http(s) 链接")
    if not row.get("award_grade"):
        failures.append("award_grade 为空（前端奖项等级会空白）")
    if row.get("award_grade") != row.get("award_level"):
        failures.append("award_grade 与 award_level 不一致")
    if row.get("competition_id") is None:
        failures.append("competition_id 为 NULL（孤立记录）")
    if row.get("competition_id") is not None and not row.get("competition_name"):
        failures.append("competition_id 指向的 competitions 记录不存在")
    if not row.get("title"):
        failures.append("title 为空")
    if not row.get("year"):
        failures.append("year 为空")
    return failures


def main(argv=None) -> int:
    _setup_console()
    parser = argparse.ArgumentParser(description="读回并校验 crawler_news 写入的获奖记录")
    parser.add_argument("--db-path", required=True, help="测试库路径（不要指向生产 competitions.db）")
    parser.add_argument("--award-id", type=int, nargs="+", default=None, help="要查询的 award id")
    parser.add_argument("--limit", type=int, default=10, help="未指定 award-id 时显示最近 N 条 crawl 记录")
    args = parser.parse_args(argv)

    db_path = os.path.abspath(os.path.expanduser(args.db_path))
    if db_path == os.path.abspath(PROD_DB):
        print("拒绝查询生产库 competitions.db，请使用测试副本（如 test_crawl.db）。")
        return 1
    if not os.path.exists(db_path):
        print(f"数据库不存在：{db_path}")
        return 1

    rows = fetch_rows(db_path, args.award_id, args.limit)
    if not rows:
        print("没有查询到记录。")
        return 1

    print(f"数据库：{db_path}")
    print(f"命中记录：{len(rows)} 条\n")

    failed = 0
    for row in rows:
        print("=" * 78)
        print(json.dumps(row, ensure_ascii=False, indent=2))
        problems = check_row(row)
        if problems:
            failed += 1
            print("校验：FAIL")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print("校验：PASS（source_url / award_grade / competition_id / year 均正确）")
    print("=" * 78)
    print(f"汇总：{len(rows) - failed}/{len(rows)} 条通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
