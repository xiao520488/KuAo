import sqlite3
c = sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
tables = [r[0] for r in c.execute("select name from sqlite_master where type='table' order by name")]
print("tables:", tables)
for t in tables:
    cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
    if "competition_id" in cols:
        n = c.execute("select count(*) from %s" % t).fetchone()[0]
        print("  child:", t, "rows=", n)
