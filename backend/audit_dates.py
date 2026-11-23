# -*- coding: utf-8 -*-
"""数据体检：报名/比赛日期自相矛盾的地方逐条列出来。

判定规则（都是「只可能错、不可能对」的组合，不需要看原文）：
  R1 报名窗口与比赛窗口四项日期完全相同，且报名月份为空
     —— 说明日期是从「比赛时间」复制进报名列的（世界技能大赛即此类）；
  R2 报名截止早于报名开始 / 比赛结束早于比赛开始；
  R3 只有比赛日期、报名日期却是同一段（同上，重复检查）；
  R4 reg_start_month 为空但 reg_start_date 有值（粒度不一致，前端会只显示日期）。

用法：
  python audit_dates.py            # 只报
  python audit_dates.py --apply    # 修 R1：把抄来的报名日期清成 NULL（报名改为待核实）
"""
from __future__ import annotations

import argparse

import db


def find_issues(conn) -> dict:
    same = conn.execute(
        "SELECT id, name, reg_start_date, reg_end_date, "
        "       contest_start_date, contest_end_date, reg_start_month "
        "FROM competitions "
        "WHERE reg_start_date IS NOT NULL AND contest_start_date IS NOT NULL "
        "  AND reg_start_date = contest_start_date "
        "  AND IFNULL(reg_end_date,'') = IFNULL(contest_end_date,'')"
    ).fetchall()
    r1 = [dict(r) for r in same if r["reg_start_month"] is None]
    r1_ok = [dict(r) for r in same if r["reg_start_month"] is not None]

    backwards = conn.execute(
        "SELECT id, name, reg_start_date, reg_end_date, "
        "       contest_start_date, contest_end_date "
        "FROM competitions WHERE "
        "(reg_start_date IS NOT NULL AND reg_end_date IS NOT NULL "
        " AND reg_start_date > reg_end_date) OR "
        "(contest_start_date IS NOT NULL AND contest_end_date IS NOT NULL "
        " AND contest_start_date > contest_end_date)"
    ).fetchall()

    grain = conn.execute(
        "SELECT id, name FROM competitions "
        "WHERE reg_start_month IS NULL AND reg_start_date IS NOT NULL "
        "  AND contest_start_month IS NOT NULL"
    ).fetchall()

    # R4 月份列与精确日期不同月：精确日期来自原文，月份是早期补全留下的，
    #    以日期为准（例：cid=10 报名月份 4 但报名日期 2026-03-01）。
    drift = conn.execute(
        "SELECT id, name, reg_start_month, reg_start_date, "
        "       contest_start_month, contest_start_date "
        "FROM competitions WHERE "
        "(reg_start_month IS NOT NULL AND reg_start_date IS NOT NULL "
        " AND CAST(strftime('%m', reg_start_date) AS INTEGER) <> reg_start_month) OR "
        "(contest_start_month IS NOT NULL AND contest_start_date IS NOT NULL "
        " AND CAST(strftime('%m', contest_start_date) AS INTEGER) <> contest_start_month)"
    ).fetchall()

    return {
        "copied_reg_dates": r1,
        "same_window_but_has_month": r1_ok,
        "backwards": [dict(r) for r in backwards],
        "grain_mismatch": [dict(r) for r in grain],
        "month_drift": [dict(r) for r in drift],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="报名/比赛日期一致性体检")
    ap.add_argument("--apply", action="store_true",
                    help="修复 R1：把抄来的报名日期清成 NULL")
    ap.add_argument("--include-same-window", action="store_true",
                    help="连同 R1b 一起处理：窗口相同且原文只有报名语境时，清比赛日期")
    ap.add_argument("--sync-months", action="store_true",
                    help="R4：月份列按精确日期纠正（日期来自原文，月份是早期补全的）")
    args = ap.parse_args()

    with db.get_conn() as conn:
        issues = find_issues(conn)
        print(f"R1 报名日期疑似抄比赛日期（且无报名月份）：{len(issues['copied_reg_dates'])} 条")
        for r in issues["copied_reg_dates"]:
            print(f"  cid={r['id']:<4} reg={r['reg_start_date']}~{r['reg_end_date']} "
                  f"contest={r['contest_start_date']}~{r['contest_end_date']}  {r['name'][:30]}")
        print(f"\nR1b 窗口相同但报名月份有值（可能本来就是同期，需看原文）："
              f"{len(issues['same_window_but_has_month'])} 条")
        for r in issues["same_window_but_has_month"]:
            print(f"  cid={r['id']:<4} {r['reg_start_date']}~{r['reg_end_date']}  {r['name'][:30]}")
        print(f"\nR2 起止倒挂：{len(issues['backwards'])} 条")
        for r in issues["backwards"]:
            print(f"  cid={r['id']:<4} {r['name'][:30]}")
        print(f"\nR3 有精确报名日期但缺报名月份：{len(issues['grain_mismatch'])} 条"
              "（前端只显示日期，不算错，仅提示）")
        print(f"\nR4 月份列与精确日期不同月：{len(issues['month_drift'])} 条")
        for r in issues["month_drift"]:
            print(f"  cid={r['id']:<4} 报名月份={r['reg_start_month']} 日期={r['reg_start_date']} / "
                  f"比赛月份={r['contest_start_month']} 日期={r['contest_start_date']}  {r['name'][:24]}")

        if args.apply:
            fixed = 0
            for r in issues["copied_reg_dates"]:
                conn.execute(
                    "UPDATE competitions SET reg_start_date=NULL, reg_end_date=NULL, "
                    "last_checked_at=datetime('now','localtime') WHERE id=?",
                    (r["id"],),
                )
                fixed += 1
            print(f"\n已修复 {fixed} 条：报名日期清空（前端显示「以官网通知为准」待核实）")

            if args.include_same_window:
                cleared = 0
                for r in issues["same_window_but_has_month"]:
                    conn.execute(
                        "UPDATE competitions SET contest_start_date=NULL, "
                        "contest_end_date=NULL, "
                        "last_checked_at=datetime('now','localtime') WHERE id=?",
                        (r["id"],),
                    )
                    cleared += 1
                print(f"已清 {cleared} 条 R1b 的比赛日期"
                      "（原文只写了报名窗口、没写比赛日期，赛段细节仍在 stage_schedule）")

            if args.sync_months:
                fixed = 0
                for r in issues["month_drift"]:
                    sets, vals = [], []
                    for month_col, date_col in (("reg_start_month", "reg_start_date"),
                                                ("contest_start_month", "contest_start_date")):
                        month, date = r[month_col], r[date_col]
                        if month is not None and date:
                            real = int(str(date)[5:7])
                            if real != month:
                                sets.append(f"{month_col}=?")
                                vals.append(real)
                    if sets:
                        conn.execute(
                            "UPDATE competitions SET " + ", ".join(sets) + " WHERE id=?",
                            (*vals, r["id"]),
                        )
                        fixed += 1
                        print(f"  cid={r['id']} 月份已按日期纠正：{sets}")
                print(f"已同步 {fixed} 条赛事的月份列")
        elif issues["copied_reg_dates"]:
            print("\n加 --apply 可把上面 R1 的报名日期清成 NULL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
