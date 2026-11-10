# -*- coding: utf-8 -*-
"""打印 competition_candidates 表结构与 pending 样本。"""
import os
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = os.path.join(BASE, "backend", "competitions.db")
con = sqlite3.connect(path)
con.row_factory = sqlite3.Row

print("schema:")
for r in con.execute("select sql from sqlite_master where name='competition_candidates'"):
    print(r[0])

print("\nstatus 分布:")
for r in con.execute("select status, count(*) c from competition_candidates group by status"):
    print(" ", tuple(r))

print("\npending 样本（按 id 倒序 8 条）:")
cols = [d[1] for d in con.execute("pragma table_info(competition_candidates)")]
print(" columns:", cols)
for r in con.execute("select * from competition_candidates where status='pending' "
                     "order by id desc limit 8"):
    d = dict(r)
    print(" ", {k: d.get(k) for k in ("id", "source", "source_key", "raw_name",
                                     "category_hint", "level_label", "image_url")})