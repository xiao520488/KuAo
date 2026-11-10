import sqlite3
c = sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
c.row_factory = sqlite3.Row
print("competitions:", c.execute("select count(*) from competitions").fetchone()[0])
print("候选分布:", [(r[0], r[1], r[2]) for r in c.execute("select source,status,count(*) from competition_candidates group by source,status")])
print("\n剩余可能空壳（已通过且详情全空）:")
q = """select c.id, c.source, c.raw_name, p.id pid from competition_candidates c join competitions p on p.id=c.competition_id
where c.status='approved' and (p.participants is null or p.participants='') and (p.reg_start_date is null or p.reg_start_date='')
and (p.image_url is null or p.image_url='') and (p.exam_schedule is null or p.exam_schedule='')"""
rows = [dict(r) for r in c.execute(q)]
print(rows or "无")
print("\n最近入库5条:")
for r in c.execute("select id,name,short_name,image_url,reg_start_date from competitions order by id desc limit 5"):
    d = dict(r); print(d["id"], d["name"][:26], "| short:", d["short_name"], "| img:", bool(d["image_url"]), "| reg:", d["reg_start_date"])
