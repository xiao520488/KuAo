# -*- coding: utf-8 -*-
"""清除「名单型」获奖记录（标题是 school·members 拼出来的，官方没有作品名）。

产品口径（2026-11-23）：名单型记录不作为作品——显示「暂未收入获奖作品」即可。

判定（两条同时成立，避免误删）：
  1. title 以「本行 team（学校）+ ·」开头；
  2. summary 里有「成员：」（说明这条记录的正文只有队员信息，没有作品描述）。

用法：
    python purge_roster_awards.py             # 只统计，导出备份到 out/
    python purge_roster_awards.py --apply     # 先备份 JSON，再删除
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

import db

SQL_WHERE = ("COALESCE(review_status,'pending')<>'rejected' "
             "AND team IS NOT NULL "
             "AND title LIKE team || '·%' "
             "AND summary LIKE '%成员：%'")

BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "out", "purged")


def main() -> int:
    ap = argparse.ArgumentParser(description="清除名单型获奖记录")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只统计+备份）")
    args = ap.parse_args()

    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM awards WHERE " + SQL_WHERE + " ORDER BY competition_id, id"
        ).fetchall()]
        per_comp = {}
        for r in rows:
            per_comp[r["competition_id"]] = per_comp.get(r["competition_id"], 0) + 1
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM competitions")}

        total_before = conn.execute(
            "SELECT COUNT(*) n FROM awards WHERE COALESCE(review_status,'pending')<>'rejected'"
        ).fetchone()["n"]
        print(f"命中名单型记录 {len(rows)} 条 / 当前可见 {total_before} 条")
        for cid, n in sorted(per_comp.items(), key=lambda kv: -kv[1]):
            print(f"  cid={cid:<4} {n:>6}  {names.get(cid, '?')}")

        if not rows:
            print("没有需要清理的记录")
            return 0

        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(BACKUP_DIR, f"roster-awards-{stamp}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"purged_at": stamp, "count": len(rows), "rows": rows},
                      fh, ensure_ascii=False, indent=1)
        print(f"已备份 → {path}")

        if not args.apply:
            print("加 --apply 才会真正删除")
            return 0

        ids = [r["id"] for r in rows]
        conn.executemany("DELETE FROM awards WHERE id = ?", [(i,) for i in ids])
        after = conn.execute(
            "SELECT COUNT(*) n FROM awards WHERE COALESCE(review_status,'pending')<>'rejected'"
        ).fetchone()["n"]
        comps = conn.execute(
            "SELECT COUNT(DISTINCT competition_id) n FROM awards "
            "WHERE competition_id IS NOT NULL "
            "  AND COALESCE(review_status,'pending')<>'rejected'"
        ).fetchone()["n"]
        print(f"已删除 {len(ids)} 条；可见奖项 {total_before} → {after} 条；"
              f"有作品赛事 {comps} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
