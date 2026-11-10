import requests
url = "https://publicqn.saikr.com/saikr/contest/1776911593416/1776911593416dl"
for name, headers in (
    ("裸请求", {}),
    ("带 UA", {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}),
    ("带 UA+Referer", {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
                        "Referer": "https://www.saikr.com/"}),
):
    try:
        r = requests.get(url, headers=headers, timeout=20, stream=True)
        print(name, r.status_code, r.headers.get("Content-Type"), r.headers.get("Content-Length"))
        r.close()
    except Exception as exc:
        print(name, "ERR", exc)
