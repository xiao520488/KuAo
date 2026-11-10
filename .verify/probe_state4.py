import sqlite3
c = sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
c.row_factory = sqlite3.Row
print("-- 名称含「统计建模」 --")
for row in c.execute("select id, raw_name, status, source, competition_id, source_url from competition_candidates where raw_name like '%统计建模%'"):
    print(dict(row))
print("\n-- 已通过 edu-roster 候选 -> 主表 --")
q = """select c.id cid, c.raw_name, c.source_url, p.id pid, p.short_name, p.image_url,
p.reg_start_date, p.participants, p.exam_schedule, p.content_scope, p.official_url, p.discover_source
from competition_candidates c left join competitions p on p.id=c.competition_id
where c.source='edu-roster' and c.status='approved'"""
for row in c.execute(q):
    d = dict(row)
    shell = all(not d.get(k) for k in ("image_url","reg_start_date","participants","exam_schedule","content_scope"))
    print(d["cid"], d["raw_name"][:24], "| comp=", d["pid"], "shell=", shell, "| url=", (d["official_url"] or "")[:60])
print("\n-- pending 里带 saikr 名称相近样本(前5) --")
for row in c.execute("select id, raw_name, source_url from competition_candidates where status='pending' limit 5"):
    print(dict(row))
