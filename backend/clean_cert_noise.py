# -*- coding: utf-8 -*-
"""清掉大广赛 summary 里的「证书：证书」噪声（ignore_columns 之前没生效留下的）。

只动 summary 文本，不改任何结构化字段；可重复执行。
"""
import db

with db.get_conn() as c:
    before = c.execute(
        "SELECT COUNT(*) n FROM awards WHERE competition_id=64 "
        "AND summary LIKE '%证书：%'").fetchone()["n"]
    c.execute(
        "UPDATE awards SET summary = TRIM(REPLACE(REPLACE(summary, '；证书：证书', ''),"
        " '证书：证书；', '')) "
        "WHERE competition_id=64 AND summary LIKE '%证书：%'")
    after = c.execute(
        "SELECT COUNT(*) n FROM awards WHERE competition_id=64 "
        "AND summary LIKE '%证书：%'").fetchone()["n"]
print(f"含证书噪声 {before} 条 → 清理后 {after} 条")
