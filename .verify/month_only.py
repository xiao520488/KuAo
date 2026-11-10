import sqlite3
c=sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
rows=c.execute("""select id,name,reg_start_month,reg_end_month,contest_start_month,contest_end_month
from competitions
where coalesce(reg_start_date,'')='' and coalesce(reg_end_date,'')=''
  and coalesce(contest_start_date,'')='' and coalesce(contest_end_date,'')
  and (coalesce(reg_start_month,0)<>0 or coalesce(contest_start_month,0)<>0)
order by id limit 3""").fetchall()
for r in rows: print(r)
