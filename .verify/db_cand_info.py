# -*- coding: utf-8 -*-
import sqlite3

for p in ["test_crawl.db", "competitions.db"]:
    c = sqlite3.connect(p)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(competition_candidates)")]
        n = c.execute("SELECT COUNT(*) FROM competition_candidates").fetchone()[0]
        pend = c.execute(
            "SELECT COUNT(*) FROM competition_candidates WHERE status='pending'"
        ).fetchone()[0]
        dates = [x for x in cols if x.endswith("_date")]
        print(p, "cols=", len(cols), "total=", n, "pending=", pend,
              "datecols=", dates)
    except Exception as exc:  # noqa: BLE001
        print(p, "ERR", exc)
    c.close()