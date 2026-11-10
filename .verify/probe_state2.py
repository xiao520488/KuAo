import json, sqlite3, requests
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Referer": "https://www.saikr.com/"}
r = requests.get("https://apiv4buffer.saikr.com/api/pc/contest/lists",
                 params={"page": 1, "limit": 100, "sort": 2}, headers=UA, timeout=25)
d = r.json(); body = d.get("data") or {}; lst = body.get("list") or []
print("limit=100 -> n=%d total=%s" % (len(lst), body.get("total")))
print("keys:", sorted(lst[0].keys())[:40] if lst else None)
if lst:
    s = lst[0]
    print({k: s.get(k) for k in list(s.keys())[:14]})

c = sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
c.row_factory = sqlite3.Row
print("\n-- 候选来源分布 --")
for row in c.execute("select source, status, count(*) n from competition_candidates group by source, status"):
    print(dict(row))
print("\n-- 名称含「统计建模」的候选/赛事 --")
for tbl in ("competition_candidates", "competitions"):
    for row in c.execute("select * from %s where name like '%%统计建模%%'" % tbl):
        d2 = dict(row)
        print(tbl, {k: d2.get(k) for k in ("id","name","status","source","source_key","competition_id","short_name","image_url","reg_start_date") if k in d2})
print("\n-- competitions 来源/空壳 --")
for row in c.execute("select discover_source, count(*) n, sum(case when image_url is null and reg_start_date is null and participants is null then 1 else 0 end) shell from competitions group by discover_source"):
    print(dict(row))
