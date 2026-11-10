import json, requests
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Referer": "https://www.saikr.com/"}
BASE = "https://apiv4buffer.saikr.com/api/pc/contest/list"
tries = [
    ("keyword", {"page": 1, "limit": 10, "keyword": "统计建模"}),
    ("search",  {"page": 1, "limit": 10, "search": "统计建模"}),
    ("title",   {"page": 1, "limit": 10, "title": "统计建模"}),
]
for name, params in tries:
    try:
        r = requests.get(BASE, params=params, headers=UA, timeout=20)
        data = r.json()
        body = data.get("data") or {}
        lst = body.get("list") or []
        names = [str(i.get("title") or i.get("name"))[:28] for i in lst[:3]]
        print(name, r.status_code, "count=", body.get("total") or body.get("count"), "|", names)
    except Exception as exc:
        print(name, "ERR", type(exc).__name__, str(exc)[:80])
print("robots:", end=" ")
try:
    rr = requests.get("https://apiv4buffer.saikr.com/robots.txt", headers=UA, timeout=15)
    print(rr.status_code, repr(rr.text[:120]))
except Exception as exc:
    print("ERR", exc)
