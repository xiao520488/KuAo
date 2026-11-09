# -*- coding: utf-8 -*-
"""看看主表里没日期的行，能否用候选池（赛氪 raw_json）里的 contest_id 补齐。"""
import json
import sqlite3

c = sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
c.row_factory = sqlite3.Row

print("--- 主表无日期且 official_url 是赛氪的行 ---")
rows = c.execute(
    "SELECT id, name, discover_source, official_url FROM competitions "
    "WHERE reg_start_date IS NULL AND contest_start_date IS NULL").fetchall()
for r in rows:
    print(dict(r))
