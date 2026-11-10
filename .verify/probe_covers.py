import os, sys
BASE = r"D:\agent-lab\KuAo-Platform"
sys.path.insert(0, os.path.join(BASE, "backend"))
os.chdir(os.path.join(BASE, "backend"))
import enrich_saikr_details as enrich

session = enrich._session()
for cid in (58040, 58054, 58069, 58028, 58049):
    try:
        detail = enrich.fetch_detail(session, cid, retries=0)
        pic = str(detail.get("web_pic_big") or "")
        print(cid, repr(pic[:70]), "| contest_end:", detail.get("contest_end_time"))
    except Exception as exc:
        print(cid, "ERR", exc)
session.close()
