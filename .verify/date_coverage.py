import sqlite3
c=sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
print("sign_up_time 非空:", c.execute("select count(*) from competitions where coalesce(sign_up_time,'')<>''").fetchone()[0])
print("有日期行:", c.execute("select count(*) from competitions where coalesce(reg_start_date,'')<>'' or coalesce(reg_end_date,'')<>'' or coalesce(contest_start_date,'')<>'' or coalesce(contest_end_date,'')<>''").fetchone()[0])
print("四日期全空:", c.execute("select count(*) from competitions where coalesce(reg_start_date,'')='' and coalesce(reg_end_date,'')='' and coalesce(contest_start_date,'')='' and coalesce(contest_end_date,'')=''").fetchone()[0])
