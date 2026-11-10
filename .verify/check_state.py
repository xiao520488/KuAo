# -*- coding: utf-8 -*-
"""服务停掉后直接查库：确认批准后的数据量 + 跨零点（今天 2026-11-10）的阶段翻转。"""
import sys
from datetime import date

BACKEND = r"D:\agent-lab\KuAo-Platform\backend"
sys.path.insert(0, BACKEND)

import db  # noqa: E402

today = date.today()
print("今天:", today, "| 库:", db.DB_PATH)

rows = db.list_competitions()
print("主表总数:", len(rows))


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


from collections import Counter  # noqa: E402
print(Counter(bucket(r) for r in rows))

hot = db.list_hot_competitions(limit=8)
print("\n热门 %d 条：" % len(hot))
for i, r in enumerate(hot, 1):
    print("  %d. %-9s %s..%s %s" % (i, bucket(r), r.get("reg_start_date"),
                                    r.get("reg_end_date"), r["name"][:24]))

print("\n列表前 5：")
for i, r in enumerate(rows[:5], 1):
    print("  %d. %-9s %s..%s %s" % (i, bucket(r), r.get("reg_start_date"),
                                    r.get("reg_end_date"), r["name"][:24]))

# 昨天截止的行，今天必须已被判为已截止（跨零点翻转）
y = (today.toordinal() - 1)
yesterday = date.fromordinal(y).isoformat()
flipped = [r["name"] for r in rows
           if r.get("reg_end_date") == yesterday and bucket(r) == "已截止"]
print("\n截止日=昨天(%s) 且已判为已截止：%d 条 %s" % (yesterday, len(flipped), flipped[:3]))