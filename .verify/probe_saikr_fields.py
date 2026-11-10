# -*- coding: utf-8 -*-
"""打印赛氪详情里「时间 / 封面」相关字段，确认可用的键名。"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(BASE, "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import enrich_saikr_details as enrich  # noqa: E402

contest_id = int(sys.argv[1]) if len(sys.argv) > 1 else 58028
session = enrich._session()
detail = enrich.fetch_detail(session, contest_id)
session.close()

keys = sorted(detail)
print("字段总数:", len(keys))
print("\n时间相关:")
for key in keys:
    if any(word in key.lower() for word in ("time", "date", "begin", "end", "deadline")):
        print("  %-28s %r" % (key, str(detail.get(key))[:60]))

print("\n图片相关:")
for key in keys:
    if any(word in key.lower() for word in ("image", "cover", "poster", "banner", "pic", "img")):
        print("  %-28s %r" % (key, str(detail.get(key))[:90]))

print("\ncontest_stage 结构:")
print(json.dumps(detail.get("contest_stage"), ensure_ascii=False)[:400])