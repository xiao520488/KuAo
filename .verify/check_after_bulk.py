# -*- coding: utf-8 -*-
"""批量批准后的排序/热门验证（在 .verify/_approve_test.db 副本上跑，不碰生产库）。"""
import os
import sys
from datetime import date

BACKEND = r"D:\agent-lab\KuAo-Platform\backend"
sys.path.insert(0, BACKEND)

import db  # noqa: E402

db.DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_approve_test.db")
today = date.today()
print("today:", today, "| 库:", db.DB_PATH)

rows = db.list_competitions()
print("主表总数:", len(rows))

from collections import Counter  # noqa: E402


def bucket(r):
    s, e = r.get("reg_start_date"), r.get("reg_end_date")
    ok = lambda v: isinstance(v, str) and len(v) == 10 and v[4] == "-"  # noqa: E731
    if ok(e) and today.isoformat() > e:
        return "已截止"
    if ok(s) and today.isoformat() < s:
        return "尚未开始"
    if ok(s) or ok(e):
        return "报名中(日期)"
    if db._month_in_range(today.month, r.get("reg_start_month"),
                          r.get("reg_end_month"), r.get("reg_cross_year")):
        return "报名中(月份)"
    return "无日期"


print(Counter(bucket(r) for r in rows))
print("\n列表前 12 条：")
for i, r in enumerate(rows[:12], 1):
    print("  %2d. %-9s %s..%s %s" % (
        i, bucket(r), r.get("reg_start_date").__str__().ljust(10),
        r.get("reg_end_date").__str__().ljust(10), r["name"][:26]))

hot = db.list_hot_competitions(limit=8)
print("\n热门 %d 条：" % len(hot))
for i, r in enumerate(hot, 1):
    print("  %d. %-9s %s..%s %s" % (
        i, bucket(r), r.get("reg_start_date"), r.get("reg_end_date"),
        r["name"][:26]))

assert all(bucket(r) in ("报名中(日期)", "尚未开始") for r in hot), "热门混入已截止/无日期"
assert len(hot) == 8, "热门不足 8 条：%d" % len(hot)
assert all((r.get("level") or "national") == "national" for r in hot), "热门混入非国家级"
print("\n通过：热门满 8 条、全为国家级、无已截止/无日期混入")
os.remove(db.DB_PATH)
print("已删除临时副本")