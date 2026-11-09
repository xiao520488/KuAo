# -*- coding: utf-8 -*-
"""在数据库副本上验证「候选 -> 正式赛事」是否带得动日期轨字段。"""
import os
import shutil
import sqlite3
import sys

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

import db
from routers import admin

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend", "competitions.db")
COPY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "promote_test.db")

shutil.copyfile(SRC, COPY)
db.init_db(COPY)

conn = sqlite3.connect(COPY)
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT * FROM competition_candidates WHERE status='pending' LIMIT 1").fetchone()
cand = dict(row)
# 给候选补上日期轨，模拟新 Prompt 抓到的候选
conn.execute(
    "UPDATE competition_candidates SET reg_start_date='2026-03-24', reg_end_date='2026-06-30', "
    "contest_start_date='2026-04-11', contest_end_date='2026-04-25' WHERE id=?", (cand["id"],))
conn.commit()
cand = dict(conn.execute("SELECT * FROM competition_candidates WHERE id=?", (cand["id"],)).fetchone())
conn.close()

data = admin._build_competition_data(cand)
print("候选 id=%s -> 组装数据中的日期：%s" % (cand["id"], {k: v for k, v in data.items() if k.endswith("_date")}))
new_id = db.promote_candidate(cand["id"], data, db_path=COPY)
print("新赛事 id=%s" % new_id)

conn = sqlite3.connect(COPY)
conn.row_factory = sqlite3.Row
new = dict(conn.execute("SELECT * FROM competitions WHERE id=?", (new_id,)).fetchone())
print("主表回读：月份 %s-%s / %s-%s ｜ 日期 %s ~ %s / %s ~ %s" % (
    new["reg_start_month"], new["reg_end_month"], new["contest_start_month"], new["contest_end_month"],
    new["reg_start_date"], new["reg_end_date"],
    new["contest_start_date"], new["contest_end_date"]))
brief = admin._candidate_brief(dict(conn.execute(
    "SELECT * FROM competition_candidates WHERE id=?", (cand["id"],)).fetchone()))
print("候选列表接口字段日期：", {k: v for k, v in brief.items() if k.endswith("_date")})
conn.close()

ok = (new["reg_start_date"] == "2026-03-24" and new["contest_end_date"] == "2026-04-25"
      and new["reg_start_month"] == cand["reg_start_month"])
print("PASS" if ok else "FAIL")
os.remove(COPY)
