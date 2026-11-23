# -*- coding: utf-8 -*-
"""应用 meta-fill 子代理产出的补丁（out/meta_patch.json）。

规则：
  * 只写目标字段当前为空的行（NULL / 空串 / 空白）；
  * patch 里 replace=true 的才允许覆盖已有值（占位日期这类脏值）；
  * 应用前先备份 JSON 到 out/（源库由 db 层自己再备份一次）。

用法：
    python apply_meta_patch.py            # dry-run，打印将要写的字段
    python apply_meta_patch.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH = os.path.join(ROOT, "out", "meta_patch.json")


def is_empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description="应用赛事信息补丁")
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--patch", default=PATCH, help="补丁 JSON 路径")
    args = ap.parse_args()

    if not os.path.exists(args.patch):
        print(f"找不到补丁：{args.patch}")
        return 1

    data = json.load(open(args.patch, encoding="utf-8"))
    patches = data.get("patches") or data
    print(f"补丁 {len(patches)} 条")

    written = skipped = 0
    with db.get_conn() as conn:
        for item in patches:
            cid = int(item["competition_id"])
            row = conn.execute("SELECT * FROM competitions WHERE id=?", (cid,)).fetchone()
            if row is None:
                print(f"  cid={cid} 不存在，跳过")
                skipped += 1
                continue
            fields = dict(item.get("fields") or {})
            replace = bool(item.get("replace"))
            todo = {}
            for key, value in fields.items():
                if key not in row.keys():
                    print(f"  cid={cid} 字段 {key} 不在表里，跳过该字段")
                    continue
                if is_empty(value):
                    continue
                if is_empty(row[key]) or replace:
                    todo[key] = value
            if not todo:
                print(f"  cid={cid} 无需写入（字段已填）")
                skipped += 1
                continue
            print(f"  cid={cid} {row['name'][:20]} ← {sorted(todo)}")
            if args.apply:
                set_sql = ", ".join(f"{k}=?" for k in todo)
                conn.execute(f"UPDATE competitions SET {set_sql} WHERE id=?",
                             (*todo.values(), cid))
            written += 1

    if args.apply:
        with db.get_conn() as conn:
            empty_url = conn.execute(
                "SELECT COUNT(*) n FROM competitions WHERE official_url IS NULL "
                "OR TRIM(official_url)=''").fetchone()["n"]
        print(f"\n已写入 {written} 条；官网为空剩 {empty_url} 个")
    else:
        print(f"\n可写 {written} 条；加 --apply 生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
