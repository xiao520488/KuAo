# -*- coding: utf-8 -*-
"""清掉解析残渣：team 为空且标题只有 1 个字（例：cid=60 的 title='平'）。

这类行来自 PDF 单元格被拆行：队员姓名单独成行、学校列空，归一化后既没有作品
也没有归属，属于纯噪声。
"""
import db

with db.get_conn() as c:
    rows = [dict(r) for r in c.execute(
        "SELECT id, competition_id, title, team, award_grade, summary FROM awards "
        "WHERE COALESCE(review_status,'pending')<>'rejected' "
        "  AND (team IS NULL OR TRIM(team)='') "
        "  AND LENGTH(TRIM(IFNULL(title,''))) < 2").fetchall()]
    print(f"命中 {len(rows)} 条" + ("：" if rows else ""))
    for r in rows:
        print("  ", r)
    if rows:
        c.executemany("DELETE FROM awards WHERE id=?", [(r["id"],) for r in rows])
        after = c.execute("SELECT COUNT(*) n FROM awards WHERE "
                          "COALESCE(review_status,'pending')<>'rejected'").fetchone()["n"]
        print(f"已删除，可见奖项剩 {after} 条")
