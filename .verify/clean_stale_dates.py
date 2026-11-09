# -*- coding: utf-8 -*-
"""清理「往年届次」误写：把 2022 年的日期从候选池撤掉（当年页面的陈旧通知）。"""
import sqlite3

DB = r"D:\agent-lab\KuAo-Platform\backend\competitions.db"
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT id, norm_name, reg_start_date, reg_end_date, contest_start_date, contest_end_date "
    "FROM competition_candidates WHERE "
    "(reg_start_date LIKE '2022%' OR reg_end_date LIKE '2022%' "
    " OR contest_start_date LIKE '2022%' OR contest_end_date LIKE '2022%')").fetchall()
print("待清理：", [dict(r) for r in rows])

for r in rows:
    c.execute(
        "UPDATE competition_candidates SET reg_start_date=NULL, reg_end_date=NULL, "
        "contest_start_date=NULL, contest_end_date=NULL WHERE id=?", (r["id"],))
c.commit()
print("已清理", len(rows), "条；剩余 2022 日期：", c.execute(
    "SELECT COUNT(*) FROM competition_candidates WHERE "
    "(reg_start_date LIKE '2022%' OR contest_start_date LIKE '2022%')").fetchone()[0])
