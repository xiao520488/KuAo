#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把已生成的封面接进数据库：image_url 指向 /covers/<id>.webp。

用法（仓库根目录）：
  python tools/apply_covers.py --dry-run
  python tools/apply_covers.py            # 只处理已有封面文件的赛事
  python tools/apply_covers.py --ids 120,10
  python tools/apply_covers.py --revert   # 还原成原来的 image_url（从备份表读）
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "competitions.db"
COVERS = ROOT / "frontend" / "public" / "covers"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    if not args.dry_run and not args.revert:
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = DB.with_suffix(f".db.bak-covers-{ts}")
        shutil.copy2(DB, bak)
        print(f"备份：{bak.name}")

    if args.revert:
        con.execute(
            "UPDATE competitions SET image_url = "
            "(SELECT image_url FROM _cover_prev WHERE _cover_prev.id = competitions.id) "
            "WHERE id IN (SELECT id FROM _cover_prev)"
        )
        con.execute("DELETE FROM _cover_prev")
        con.commit()
        print("已还原", con.total_changes, "行")
        con.close()
        return 0

    ids = [int(x) for x in re.split(r"[,，\s]+", args.ids) if x.strip().isdigit()]
    rows = [dict(r) for r in con.execute("SELECT id, image_url FROM competitions")]
    plan = []
    for r in rows:
        f = COVERS / f"{r['id']}.webp"
        if not f.exists():
            continue
        if ids and r["id"] not in ids:
            continue
        plan.append((r["id"], f"/covers/{r['id']}.webp", r["image_url"]))

    if args.dry_run:
        for cid, new, old in plan[:50]:
            print(f"{cid:>4}  {str(old)[:44]:<46} -> {new}")
        print(f"共 {len(plan)} 个赛事会改成 /covers/<id>.webp")
        con.close()
        return 0

    # 首次改之前先记下原图，便于回滚
    con.execute("CREATE TABLE IF NOT EXISTS _cover_prev (id INTEGER PRIMARY KEY, image_url TEXT)")
    for cid, new, old in plan:
        con.execute(
            "INSERT INTO _cover_prev(id, image_url) VALUES(?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (cid, old),
        )
        con.execute("UPDATE competitions SET image_url = ? WHERE id = ?", (new, cid))
    con.commit()
    con.close()
    print(f"已更新 {len(plan)} 个赛事的 image_url（原图记录在 _cover_prev 表）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
