import sqlite3
c = sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
print("pending 候选:", c.execute("select count(*) from competition_candidates where status='pending'").fetchone()[0])
print("competitions:", c.execute("select count(*) from competitions").fetchone()[0])
print("管理员:", [r[0] for r in c.execute("select username from users where is_admin=1")])
print("最近入库:", [tuple(r) for r in c.execute("select id,name,short_name from competitions order by id desc limit 3")])
