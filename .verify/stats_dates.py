# -*- coding: utf-8 -*-
"""最终盘点：日期轨覆盖情况。"""
import sqlite3

c = sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
c.row_factory = sqlite3.Row

NODATE = ("reg_start_date IS NULL AND reg_end_date IS NULL "
          "AND contest_start_date IS NULL AND contest_end_date IS NULL")


def has(col):
    return "SUM(CASE WHEN %s IS NOT NULL THEN 1 ELSE 0 END)" % col


print("== competitions ==")
print("总数", c.execute("SELECT COUNT(*) FROM competitions").fetchone()[0])
for col in ("reg_start_date", "reg_end_date", "contest_start_date", "contest_end_date"):
    print("  %-20s 有值 %d" % (col, c.execute(
        "SELECT %s FROM competitions" % has(col)).fetchone()[0]))
print("  完全无日期 %d" % c.execute(
    "SELECT COUNT(*) FROM competitions WHERE %s" % NODATE).fetchone()[0])

print("== competition_candidates ==")
print("总数", c.execute("SELECT COUNT(*) FROM competition_candidates").fetchone()[0])
for col in ("reg_start_date", "reg_end_date", "contest_start_date", "contest_end_date"):
    print("  %-20s 有值 %d" % (col, c.execute(
        "SELECT %s FROM competition_candidates" % has(col)).fetchone()[0]))
print("  完全无日期 %d" % c.execute(
    "SELECT COUNT(*) FROM competition_candidates WHERE %s" % NODATE).fetchone()[0])
print("  pending 且有日期 %d" % c.execute(
    "SELECT COUNT(*) FROM competition_candidates WHERE status='pending' AND NOT (%s)" % NODATE).fetchone()[0])

print("== 主表样例（有日期） ==")
for r in c.execute(
        "SELECT id, name, reg_start_date, reg_end_date, contest_start_date, contest_end_date "
        "FROM competitions WHERE NOT (%s) ORDER BY id" % NODATE):
    print(" ", dict(r))
