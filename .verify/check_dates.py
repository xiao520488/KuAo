import sqlite3, json
c=sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
c.row_factory=sqlite3.Row
rows=c.execute("""select id,name,reg_start_date,reg_end_date,contest_start_date,contest_end_date,
 reg_start_month,reg_end_month,contest_start_month,contest_end_month
 from competitions where coalesce(reg_start_date,'')<>'' or coalesce(contest_start_date,'')<>'' order by id limit 6""").fetchall()
for r in rows: print(dict(r))
