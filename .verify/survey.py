# -*- coding: utf-8 -*-
"""只读盘点：主表/候选表的时间字段覆盖情况。"""
import sqlite3

c = sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
c.row_factory = sqlite3.Row
NODATE = ("reg_start_date IS NULL AND reg_end_date IS NULL "
          "AND contest_start_date IS NULL AND contest_end_date IS NULL")

print("比赛总数", c.execute("SELECT COUNT(*) FROM competitions").fetchone()[0])
print("--- competitions by source ---")
for r in c.execute(
        "SELECT COALESCE(discover_source,'NULL') s, COUNT(*) n, "
        "SUM(CASE WHEN official_url IS NOT NULL AND official_url<>'' THEN 1 ELSE 0 END) url, "
        "SUM(CASE WHEN %s THEN 1 ELSE 0 END) nodate "
        "FROM competitions GROUP BY s ORDER BY n DESC" % NODATE):
    print(dict(r))

print("--- candidates by status ---")
print("候选总数", c.execute("SELECT COUNT(*) FROM competition_candidates").fetchone()[0])
for r in c.execute(
        "SELECT status, COUNT(*) n, "
        "SUM(CASE WHEN %s THEN 1 ELSE 0 END) nodate, "
        "SUM(CASE WHEN source_url IS NOT NULL AND source_url<>'' THEN 1 ELSE 0 END) has_src, "
        "SUM(CASE WHEN official_url IS NOT NULL AND official_url<>'' THEN 1 ELSE 0 END) has_off "
        "FROM competition_candidates GROUP BY status" % NODATE):
    print(dict(r))

print("--- candidate columns ---")
print([r[1] for r in c.execute("PRAGMA table_info(competition_candidates)")])
print("--- sample candidate ---")
row = c.execute("SELECT * FROM competition_candidates ORDER BY id DESC LIMIT 1").fetchone()
print({k: row[k] for k in row.keys()})
