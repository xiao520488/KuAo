import sqlite3
c = sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
c.row_factory = sqlite3.Row
cols = [r[1] for r in c.execute("PRAGMA table_info(competition_candidates)")]
print("candidate cols:", cols)
print("\n-- 名称含「统计建模」 --")
for tbl, col in (("competition_candidates", "title"), ("competitions", "name")):
    if col not in [r[1] for r in c.execute("PRAGMA table_info(%s)" % tbl)]:
        print(tbl, "no col", col); continue
    for row in c.execute("select * from %s where %s like '%%统计建模%%'" % (tbl, col)):
        d = dict(row)
        print(tbl, {k: d.get(k) for k in ("id","title","name","status","source","competition_id","short_name","image_url","reg_start_date") if k in d})
print("\n-- 已通过的名录候选 -> 主表空壳 --")
q = """select c.id cid, c.title, c.competition_id, p.id pid, p.short_name, p.image_url, p.reg_start_date,
       p.participants, p.exam_schedule, p.official_url
       from competition_candidates c join competitions p on p.id=c.competition_id
       where c.source='edu-roster' and c.status='approved'"""
for row in c.execute(q):
    d = dict(row); print(d)
