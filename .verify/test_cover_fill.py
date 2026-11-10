import os, sqlite3, sys
BASE = r"D:\agent-lab\KuAo-Platform"
sys.path.insert(0, os.path.join(BASE, "backend"))
os.chdir(os.path.join(BASE, "backend"))
import db, enrich_saikr_details as enrich

DST = os.path.join(BASE, ".verify", "approve_api_test.db")
db.DB_PATH = DST
session = enrich._session()
detail = enrich.fetch_detail(session, 58040)
session.close()

print("fill result:", enrich.fill_dates_and_cover(DST, 53, detail))
con = sqlite3.connect(DST); con.row_factory = sqlite3.Row
row = dict(con.execute("select id, name, image_url, reg_start_date, reg_end_date from competitions where id=53").fetchone())
con.close()
for k, v in row.items():
    print("  %-16s %s" % (k, v))
