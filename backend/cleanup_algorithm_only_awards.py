"""清理「纯算法/知识类赛事」的假作品与假灵感（幂等，可反复执行）。

适用场景：
    蓝桥杯、ACM-ICPC 这类赛事只公示「获奖名单」，没有项目作品。历史上种子
    数据/演示数据曾给它们塞过项目型 awards（如「多模态大模型智能助手」），
    AI 据此生成了完全错误的灵感结论（如「AI 赛道主导」）。

本脚本做四件事（仅针对 competitions.has_project_awards = 0 的赛事）：
    1. 物理删除 awards 中 source_type != 'crawl' 的行（mock / manual 的假作品）；
    2. 物理删除 insights、ideas（历史假灵感）；
    3. 清掉 ai_analysis_cache 里 inspiration 类型的缓存，避免旧结论被复用；
    4. 打印清理后保留下来的真实名单条数与 integrity_check 结果。

用法：
    python cleanup_algorithm_only_awards.py                       # dry-run
    python cleanup_algorithm_only_awards.py --apply                # 真正删除（先自动备份）
    python cleanup_algorithm_only_awards.py --db-path competitions.db --apply

删除前会把整库备份到 <db-path>.bak-cleanup-<时间戳>，并把将被删除的行导出到
workspace/deleted-algo-awards-<时间戳>.{json,csv}，需要时可回填。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "competitions.db")


def _backup(db_path: str, out_path: str) -> None:
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(out_path)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def _algo_ids(con: sqlite3.Connection) -> list[int]:
    rows = con.execute(
        "SELECT id, name FROM competitions WHERE COALESCE(has_project_awards, 1) = 0 ORDER BY id"
    ).fetchall()
    return [(r["id"], r["name"]) for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理纯算法类赛事的假作品与假灵感")
    parser.add_argument("--db-path", default=DEFAULT_DB, help="数据库路径（默认 backend/competitions.db）")
    parser.add_argument("--apply", action="store_true", help="真正删除；不加则只做 dry-run")
    parser.add_argument("--out-dir", default=os.path.join(BASE_DIR, "workspace"), help="导出目录")
    args = parser.parse_args(argv)

    db_path = os.path.abspath(args.db_path)
    if not os.path.exists(db_path):
        print(f"[error] 数据库不存在：{db_path}")
        return 1
    os.makedirs(args.out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    algo = _algo_ids(con)
    if not algo:
        print("[info] 没有 has_project_awards=0 的赛事，无需清理。")
        con.close()
        return 0
    print(f"[scan] 纯算法/知识类赛事 {len(algo)} 条：")
    for cid, name in algo:
        print(f"       id={cid:<5} {name}")

    ids = [cid for cid, _ in algo]
    placeholders = ",".join("?" * len(ids))
    bad = con.execute(
        f"SELECT * FROM awards WHERE competition_id IN ({placeholders}) "
        "AND COALESCE(source_type, '') != 'crawl' ORDER BY competition_id, year, id",
        ids,
    ).fetchall()
    ins = con.execute(
        f"SELECT * FROM insights WHERE competition_id IN ({placeholders}) ORDER BY competition_id, id", ids
    ).fetchall()
    ideas = con.execute(
        f"SELECT * FROM ideas WHERE competition_id IN ({placeholders}) ORDER BY competition_id, id", ids
    ).fetchall()
    cache = con.execute(
        f"SELECT * FROM ai_analysis_cache WHERE target_type = 'inspiration' "
        f"AND target_id IN ({placeholders})",
        ids,
    ).fetchall()

    print(f"\n[found] 假作品 awards={len(bad)}  insights={len(ins)}  ideas={len(ideas)}  cache={len(cache)}")
    for r in bad:
        print(f"        - comp={r['competition_id']} id={r['id']} {r['year']} [{r['source_type']}] {r['title']}")

    if not any((bad, ins, ideas, cache)):
        print("\n[done] 已经是干净的，无需删除。")
        con.close()
        return 0

    if not args.apply:
        print("\n[dry-run] 未写入。加 --apply 执行删除。")
        con.close()
        return 0

    backup_path = f"{db_path}.bak-cleanup-{ts}"
    _backup(db_path, backup_path)
    print(f"\n[backup] {backup_path}  ({os.path.getsize(backup_path) / 1048576:.1f} MB)")

    json_path = os.path.join(args.out_dir, f"deleted-algo-awards-{ts}.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "competitions": [{"id": cid, "name": name} for cid, name in algo],
                "awards": [dict(r) for r in bad],
                "insights": [dict(r) for r in ins],
                "ideas": [dict(r) for r in ideas],
                "cache": [{k: v for k, v in dict(r).items() if k != "content_md"} for r in cache],
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
    csv_path = os.path.join(args.out_dir, f"deleted-algo-awards-{ts}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["competition_id", "id", "year", "title", "summary", "source_type"])
        for r in bad:
            writer.writerow([r["competition_id"], r["id"], r["year"], r["title"], r["summary"], r["source_type"]])
    print(f"[export] {json_path}\n[export] {csv_path}")

    with con:
        d1 = con.execute(
            f"DELETE FROM awards WHERE competition_id IN ({placeholders}) "
            "AND COALESCE(source_type, '') != 'crawl'",
            ids,
        ).rowcount
        d2 = con.execute(f"DELETE FROM insights WHERE competition_id IN ({placeholders})", ids).rowcount
        d3 = con.execute(f"DELETE FROM ideas WHERE competition_id IN ({placeholders})", ids).rowcount
        d4 = con.execute(
            f"DELETE FROM ai_analysis_cache WHERE target_type = 'inspiration' "
            f"AND target_id IN ({placeholders})",
            ids,
        ).rowcount
    print(f"[delete] awards={d1} insights={d2} ideas={d3} cache={d4}")

    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print("\n[verify] 保留的真实名单条数：")
    for cid, name in algo:
        total = con.execute("SELECT COUNT(*) FROM awards WHERE competition_id = ?", (cid,)).fetchone()[0]
        crawl = con.execute(
            "SELECT COUNT(*) FROM awards WHERE competition_id = ? AND COALESCE(source_type,'') = 'crawl'", (cid,)
        ).fetchone()[0]
        print(f"       id={cid:<5} 合计 {total:>6} 条（crawl {crawl:>6}）  {name}")
    leftover = con.execute(
        f"SELECT COUNT(*) FROM awards WHERE competition_id IN ({placeholders}) "
        "AND COALESCE(source_type, '') != 'crawl'",
        ids,
    ).fetchone()[0]
    print(f"       残留非 crawl 假作品：{leftover}")
    print(f"       integrity_check：{con.execute('PRAGMA integrity_check').fetchone()[0]}")
    con.close()
    print("\n[done] 清理完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())