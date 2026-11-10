import sqlite3
p = r"D:\agent-lab\KuAo-Platform\backend\competitions.db"
c = sqlite3.connect(p)
print("count:", c.execute("select count(*) from competitions").fetchone()[0])
print("max id:", c.execute("select max(id) from competitions").fetchone()[0])
print("ids 50-58:", [r[0] for r in c.execute("select id from competitions where id between 50 and 58 order by id")])
print("journal_mode:", c.execute("pragma journal_mode").fetchone()[0])
import os
print("files:", [(f, os.path.getsize(os.path.join(os.path.dirname(p), f))) for f in os.listdir(os.path.dirname(p)) if f.startswith("competitions.db")])
