# -*- coding: utf-8 -*-
"""找出「应能补全成功」的 pending 候选：raw_json 带 contest_id，或名称能对上 saikr_map。"""
import json
import tempfile
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # 仓库根
BACKEND = os.path.join(BASE, "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import db  # noqa: E402
import enrich_saikr_details as enrich  # noqa: E402

DST = os.path.join(tempfile.gettempdir(), "kuo-verify", "approve_api_test.db")
db.DB_PATH = DST

mapping = enrich.load_map()
by_name = enrich._build_by_name(mapping)

con = sqlite3.connect(DST)
con.row_factory = sqlite3.Row
rows = [dict(r) for r in con.execute(
    "select id, source, source_key, raw_name, raw_json from competition_candidates "
    "where status = 'pending' order by id")]
con.close()

print("pending:", len(rows))
print("按来源:", {s: sum(1 for r in rows if r["source"] == s) for s in {r["source"] for r in rows}})

with_cid = []
for row in rows:
    raw = row.get("raw_json")
    payload = {}
    if raw:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
    cid = payload.get("contest_id") if isinstance(payload, dict) else None
    if cid:
        with_cid.append((row["id"], row["source"], payload.get("contest_id"),
                         row["raw_name"]))

print("\nraw_json 带 contest_id 的:", with_cid[:10])

print("\n名称能对上 saikr_map 的候选：")
hits = 0
for row in rows:
    norm = db.normalize_competition_name(row["raw_name"] or "")
    for name, cid in by_name:
        if norm and (norm == name or (len(name) > 8 and name in norm)):
            print(" 候选 %s《%s》 -> contest %s（%s）" % (
                row["id"], row["raw_name"], cid, name))
            hits += 1
            break
print("命中:", hits)

sample = next((r for r in rows if r["raw_json"]), None)
if sample:
    print("\nraw_json 示例（候选 %s）:" % sample["id"])
    print(" ", str(sample["raw_json"])[:400])