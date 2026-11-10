import requests
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Referer": "https://www.saikr.com/"}
BASE = "https://apiv4buffer.saikr.com/api/pc/contest/lists"
def probe(label, params):
    try:
        r = requests.get(BASE, params=params, headers=UA, timeout=25)
        d = r.json()
        body = d.get("data") or {}
        lst = body.get("list") or []
        titles = [str(i.get("title") or i.get("name"))[:26] for i in lst[:3]]
        print("%-10s %s code=%s total=%s n=%d %s" % (label, r.status_code, d.get("code"), body.get("total"), len(lst), titles))
    except Exception as exc:
        print("%-10s ERR %r" % (label, exc))

probe("baseline", {"page": 1, "limit": 5, "sort": 2})
probe("keyword",  {"page": 1, "limit": 5, "sort": 2, "keyword": "统计建模"})
probe("search",   {"page": 1, "limit": 5, "sort": 2, "search": "统计建模"})
probe("title",    {"page": 1, "limit": 5, "sort": 2, "title": "统计建模"})
probe("name",     {"page": 1, "limit": 5, "sort": 2, "name": "统计建模"})
