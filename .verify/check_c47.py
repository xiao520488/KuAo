import sqlite3
c=sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
print("候选47:", c.execute("select id,raw_name,status from competition_candidates where id=47").fetchone())
print("pending 总数:", c.execute("select count(*) from competition_candidates where status='pending'").fetchone()[0])
