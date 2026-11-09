# -*- coding: utf-8 -*-
"""智能排序回归测试：用临时库构造各阶段边界数据，断言 SQL 排序结果。

覆盖：精确日期三阶段 + 月份兜底 + 脏值沉底 + 精度优先级 + 热门筛选 + limit 收敛。
不碰生产库：复制 competitions.db 到 .verify/_order_test.db，清空 competitions 后插合成行。
"""
import os
import shutil
import sys
from datetime import date, timedelta

BACKEND = r"D:\agent-lab\KuAo-Platform\backend"
sys.path.insert(0, BACKEND)

import db  # noqa: E402

SRC = os.path.join(BACKEND, "competitions.db")
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_order_test.db")
if os.path.exists(TMP):
    os.remove(TMP)
shutil.copyfile(SRC, TMP)

db.DB_PATH = TMP
today = date.today()
CUR_MONTH = today.month


def d(offset):
    return (today + timedelta(days=offset)).isoformat()


def m(offset):
    """相对当前月偏移，收敛到 1..12。"""
    return ((CUR_MONTH - 1 + offset) % 12) + 1


# name, level, reg_start, reg_end, contest_start, start_month, end_month, cross_year
CASES = [
    # 阶段 0：精确日期，报名中（按 reg_end_date 升序）
    ("A_today_deadline", "national", d(-70), d(0), None, None, None, 0),
    ("B_open_3d", "national", d(-40), d(3), None, None, None, 0),
    ("C_open_11d", "provincial", d(-10), d(11), None, None, None, 0),
    ("K_only_end", "national", None, d(40), None, None, None, 0),
    # 阶段 0：月份兜底（无日期 + 本月落在区间），排在精确日期之后
    ("N_month_open", "national", None, None, None, 1, 12, 0),
    ("Q_month_single", "national", None, None, None, CUR_MONTH, CUR_MONTH, 0),
    ("P_month_cross", "national", None, None, None, CUR_MONTH, m(2), 1),
    # 阶段 1：尚未开始
    ("I_tomorrow", "national", d(1), d(30), None, None, None, 0),
    ("E_soon6", "national", d(6), d(40), None, None, None, 0),
    ("D_soon22", "provincial", d(22), d(60), None, None, None, 0),
    # 阶段 2：已截止（按 reg_end_date 倒序）
    ("F_over_y", "national", None, d(-1), None, None, None, 0),
    ("J_over_25", "national", None, d(-25), None, None, None, 0),
    ("G_over_39", "national", d(-90), d(-39), None, None, None, 0),
    # 阶段 3：无日期 / 月份不覆盖本月
    ("L_contest_only", "national", None, None, d(26), None, None, 0),
    ("H_nothing", "national", None, None, None, None, None, 0),
    ("M_dirty_date", "national", "2026-11", "2026-13-40", None, None, None, 0),
    ("O_month_closed", "national", None, None, None, m(3), m(5), 0),
]

with db.get_conn() as conn:
    conn.execute("DELETE FROM competitions")
    for name, level, rs, re_, cs, sm, em, cross in CASES:
        conn.execute(
            "INSERT INTO competitions (name, level, category, reg_start_date, "
            "reg_end_date, contest_start_date, reg_start_month, reg_end_month, "
            "reg_cross_year) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, level, "计算机", rs, re_, cs, sm, em, cross),
        )

# 月度值越界由表结构 CHECK 兜住（写不进去），这里断言这道防线还在
_check_failed = False
try:
    with db.get_conn() as conn:
        conn.execute("INSERT INTO competitions (name, reg_start_month) "
                     "VALUES ('R_month_bad', 13)")
except Exception as exc:  # noqa: BLE001
    _check_failed = "CHECK" in str(exc)
assert _check_failed, "reg_start_month=13 竟能写入，表结构 CHECK 丢了"
print(" месячный CHECK 生效：reg_start_month=13 被拒"[:0] + "月度越界被表结构 CHECK 拦截（符合预期）")

rows = db.list_competitions()
order = [r["name"] for r in rows]
print("\n实际顺序（当前月=%d）：" % CUR_MONTH)
for i, r in enumerate(rows, 1):
    print("  %2d. %-16s %s..%s  月=%s-%s" % (
        i, r["name"], r.get("reg_start_date"), r.get("reg_end_date"),
        r.get("reg_start_month"), r.get("reg_end_month")))

EXPECT = [
    "A_today_deadline", "B_open_3d", "C_open_11d", "K_only_end",
    "Q_month_single", "N_month_open", "P_month_cross",
    "I_tomorrow", "E_soon6", "D_soon22",
    "F_over_y", "J_over_25", "G_over_39",
    "L_contest_only", "H_nothing", "M_dirty_date", "O_month_closed",
]

assert len(order) == len(EXPECT), "row count mismatch: %d" % len(order)
assert order == EXPECT, "order mismatch:\nactual %s\nexpect %s" % (order, EXPECT)

for r in rows:
    assert "_phase" not in r and "_prec" not in r, "临时列泄漏：%s" % r["name"]

hot = db.list_hot_competitions(limit=8)
hot_names = [r["name"] for r in hot]
print("\n热门顺序：", hot_names)
EXPECT_HOT = ["A_today_deadline", "B_open_3d", "K_only_end",
              "Q_month_single", "N_month_open", "P_month_cross",
              "I_tomorrow", "E_soon6"]
assert hot_names == EXPECT_HOT, "hot mismatch: %s" % hot_names
for r in hot:
    assert (r.get("level") or "national") == "national", "热门混入非国家级：%s" % r["name"]
    assert r["name"] not in ("F_over_y", "J_over_25", "G_over_39"), "热门混入已截止"
    assert r["name"] not in ("O_month_closed", "H_nothing"), "热门混入无报名信息"

assert len(db.list_hot_competitions(limit=2)) == 2, "limit 未生效"
assert len(db.list_hot_competitions(limit=999)) <= 50, "limit 上限未收敛"

print("\n全部通过：%d 行排序（含月份兜底）+ 热门筛选 + limit 收敛" % len(EXPECT))
os.remove(TMP)