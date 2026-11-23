# -*- coding: utf-8 -*-
"""全库数据健康度体检（只读，不改数据）。查不出来源的误导性组合，逐项给条数。

设计原则：每一条规则都是「几乎必然错」的组合，不靠人工逐条看原文；
命中只报数 + 前几条样本，修不修、怎么修由人决定。

用法：
    python audit_data_health.py            # 本地库
    python audit_data_health.py --limit 8  # 每项多列几条样本
"""
from __future__ import annotations

import argparse
from datetime import datetime

import db

SHANGHAI_TODAY = datetime.now().date().isoformat()


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def checks(conn, sample: int) -> list:
    out = []

    def add(title, why, sql, params=()):
        rows = _rows(conn, sql, params)
        out.append((title, why, rows))

    # 1. 月份与精确日期互相矛盾（前端会两处显示不一致）
    add("月份与日期不自洽",
        "有精确日期时月份列应与之同月；不同月说明其中一个是脏值",
        "SELECT id, name, reg_start_month, reg_start_date, contest_start_month, "
        "contest_start_date FROM competitions "
        "WHERE (reg_start_month IS NOT NULL AND reg_start_date IS NOT NULL "
        "       AND CAST(strftime('%m', reg_start_date) AS INTEGER) <> reg_start_month) "
        "   OR (contest_start_month IS NOT NULL AND contest_start_date IS NOT NULL "
        "       AND CAST(strftime('%m', contest_start_date) AS INTEGER) <> contest_start_month)")

    # 2. 报名截止早于今天但比赛还没开始，却仍标 is_active=1 且没有历史届次数据
    add("报名已截止且无比赛日期",
        "只剩报名窗口、比赛时间全空，详情页会显示「以官网通知为准」——常用于占位赛事",
        "SELECT id, name, reg_end_date FROM competitions "
        "WHERE reg_end_date IS NOT NULL AND reg_end_date < ? "
        "  AND contest_start_date IS NULL AND contest_end_date IS NULL "
        "  AND contest_start_month IS NULL AND contest_end_month IS NULL "
        "  AND is_active = 1", (SHANGHAI_TODAY,))

    # 3. 日期看点像占位（1 月 1 日 / 12 月 31 日）
    add("疑似占位日期（01-01 / 12-31）",
        "补全脚本常见的默认值，不是官方原文",
        "SELECT id, name, reg_start_date, reg_end_date, contest_start_date, contest_end_date "
        "FROM competitions WHERE reg_start_date LIKE '%-01-01' "
        "   OR contest_start_date LIKE '%-01-01' OR reg_end_date LIKE '%-12-31'")

    # 4. 获奖年份离群（比该赛事其他记录早 5 年以上）
    add("获奖年份离群",
        "同一赛事里混进远古年份，多为解析串行或表头年份没换",
        "SELECT competition_id, MIN(year) mn, MAX(year) mx, COUNT(*) n FROM awards "
        "WHERE year IS NOT NULL AND review_status<>'rejected' "
        "GROUP BY competition_id HAVING mx - mn >= 5")

    # 5. 奖项等级缺失（AI/筛选都会失效）
    add("奖项等级为空或过短",
        "等于没有等级，前端筛选与优先级排序都会掉到最后",
        "SELECT competition_id, COUNT(*) n FROM awards "
        "WHERE review_status<>'rejected' "
        "  AND (award_grade IS NULL OR TRIM(award_grade) IN ('', '奖', '-')) "
        "GROUP BY competition_id")

    # 6. 标题为空 / 极短（多半是表头行或合并单元格残留）
    #    注意：队名可能就是 2 个字（『智音』『文迹』『c3』），所以只报空或单字。
    add("标题为空或仅 1 字",
        "表头行、序号列误当标题（2 字队名是合法的，不算）",
        "SELECT competition_id, COUNT(*) n FROM awards "
        "WHERE review_status<>'rejected' AND (title IS NULL OR LENGTH(TRIM(title)) < 2) "
        "GROUP BY competition_id")

    # 7. 标题里还带着表头关键词（解析把表头当数据）
    add("标题/等级疑似表头行",
        "『作品名称』『奖项』这类词出现在数据里，说明表头没被跳过",
        "SELECT competition_id, title, award_grade FROM awards "
        "WHERE review_status<>'rejected' "
        "  AND (title LIKE '%作品名称%' OR title LIKE '%序号%' "
        "       OR award_grade LIKE '%奖项%' OR award_grade LIKE '%等级%')")

    # 8. 同一赛事同一人同奖项重复（content_hash 漏网）
    #    带上 team 再看：同名项目在不同学校很常见，只比标题会大量误报。
    add("疑似重复记录（同赛事+同标题+同学校+同奖项）",
        "同一条记录被写入两次；不同队伍同名项目不算",
        "SELECT competition_id, title, team, award_grade, COUNT(*) n FROM awards "
        "WHERE review_status<>'rejected' "
        "GROUP BY competition_id, title, IFNULL(team,''), award_grade HAVING n > 1 "
        "ORDER BY n DESC")

    # 9. 官网地址为空（无法复核，也没法再采集）
    add("官网地址为空",
        "详情页「官网」入口会缺失",
        "SELECT id, name FROM competitions WHERE official_url IS NULL OR TRIM(official_url)=''")

    # 10. 白名单内赛事没有任何获奖数据（S1 覆盖率分母）
    #     白名单不落库（competition_whitelist.py 里的封闭集合），这里在主流程外单独算。
    return [(t, w, r[:sample], len(r)) for t, w, r in out]


def whitelist_zero_awards(conn, sample: int) -> tuple:
    """白名单里零获奖的赛事（覆盖率分母，单独算因为白名单在代码里）。"""
    import competition_whitelist as wl

    ids = [row[0] for row in wl.WHITELIST]
    have = {
        r["competition_id"]
        for r in conn.execute(
            "SELECT DISTINCT competition_id FROM awards "
            "WHERE competition_id IS NOT NULL "
            "  AND COALESCE(review_status,'pending')<>'rejected'"
        )
    }
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM competitions")}
    missing = [{"id": i, "name": names.get(i, "（库中无此赛事）")}
               for i in ids if i not in have]
    return ("白名单内零获奖赛事", "S1 覆盖率的分母，直接决定还能补几个赛事",
            missing[:sample], len(missing))


def main() -> int:
    ap = argparse.ArgumentParser(description="全库数据健康度体检（只读）")
    ap.add_argument("--limit", type=int, default=3, help="每项列出的样本条数")
    args = ap.parse_args()

    with db.get_conn() as conn:
        total_c = conn.execute("SELECT COUNT(*) n FROM competitions").fetchone()["n"]
        total_a = conn.execute(
            "SELECT COUNT(*) n FROM awards WHERE COALESCE(review_status,'pending')<>'rejected'"
        ).fetchone()["n"]
        print(f"赛事 {total_c} 个 / 可见奖项 {total_a} 条\n")
        groups = checks(conn, max(args.limit, 0))
        groups.append(whitelist_zero_awards(conn, max(args.limit, 0)))
        for title, why, rows, n in groups:
            flag = "OK " if n == 0 else "⚠  "
            print(f"{flag}{title}：{n} 项")
            if n:
                print(f"     判定：{why}")
                for r in rows:
                    print(f"     · {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
