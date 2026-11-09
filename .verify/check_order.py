# -*- coding: utf-8 -*-
"""核对智能排序：阶段分布 + 「按月兜底」能补多少条。"""
import sys
from datetime import date

BACKEND = r"D:\agent-lab\KuAo-Platform\backend"
sys.path.insert(0, BACKEND)

import db  # noqa: E402

today = date.today()
print("today(本地) =", today, "| sqlite 判定 =", end=" ")
with db.get_conn() as conn:
    print(conn.execute("SELECT date('now','+8 hours'), date('now')").fetchone()[:])

rows = db.list_competitions()
print("总数", len(rows))

def bucket(r):
    s, e = r.get("reg_start_date"), r.get("reg_end_date")
    ok = lambda v: isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-"
    if ok(e) and today.isoformat() > e:
        return "已截止"
    if ok(s) and today.isoformat() < s:
        return "尚未开始"
    if ok(s) or ok(e):
        return "正在报名"
    return "无日期"

from collections import Counter
print(Counter(bucket(r) for r in rows))

def month_open(r):
    return db._month_in_range(today.month, r.get("reg_start_month"),
                              r.get("reg_end_month"), r.get("reg_cross_year"))

nodate = [r for r in rows if bucket(r) == "无日期"]
mo = [r for r in nodate if month_open(r)]
print("无日期行里，按月口径「本月在报名区间」的：", len(mo))
for r in mo:
    print("   id=%s %s  月份=%s-%s" % (r["id"], r["name"], r.get("reg_start_month"),
                                       r.get("reg_end_month")))

nat = [r for r in rows if (r.get("level") or "national") == "national"]
print("国家级总数", len(nat),
      "| 其中按月在本月报名区间的（含已有日期的）",
      len([r for r in nat if month_open(r) or bucket(r) == "正在报名"]))
