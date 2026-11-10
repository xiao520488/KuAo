# -*- coding: utf-8 -*-
"""通过后台 HTTP 接口审批一条候选，打印响应与入库结果。

用法: python smoke_approve.py <candidate_id> [base_url]
"""
import json
import os
import sqlite3
import sys

import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(BASE, "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

DST = os.path.join(BASE, ".verify", "approve_api_test.db")

import db  # noqa: E402

db.DB_PATH = DST
import security  # noqa: E402

candidate_id = int(sys.argv[1])
base_url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8010"

con = sqlite3.connect(DST)
con.row_factory = sqlite3.Row
admin = dict(con.execute(
    "select id, username from users where is_admin = 1 order by id limit 1").fetchone())
before = dict(con.execute(
    "select id, source, source_key, raw_name, status from competition_candidates "
    "where id = ?", (candidate_id,)).fetchone())
con.close()

token = security.create_access_token(admin["id"], admin["username"])

print("== 审批前 ==")
print(json.dumps(before, ensure_ascii=False))

resp = requests.post(
    "%s/api/admin/candidates/%s/approve" % (base_url, candidate_id),
    headers={"Authorization": "Bearer %s" % token},
    timeout=300,
)
print("\n== HTTP %s ==" % resp.status_code)
try:
    payload = resp.json()
except ValueError:
    payload = {"raw": resp.text[:500]}
print(json.dumps(payload, ensure_ascii=False, indent=2))

competition_id = payload.get("competition_id")
if not competition_id:
    sys.exit(0)

con = sqlite3.connect(DST)
con.row_factory = sqlite3.Row
row = dict(con.execute("select * from competitions where id = ?", (competition_id,)).fetchone())
cand = dict(con.execute(
    "select id, status, competition_id, reviewed_at from competition_candidates where id = ?",
    (candidate_id,)).fetchone())
con.close()

fields = ["name", "short_name", "image_url", "participants", "content_scope",
          "exam_schedule", "reg_start_date", "reg_end_date",
          "contest_start_date", "contest_end_date", "reg_start_month",
          "contest_start_month", "level", "category", "official_url"]
print("\n== competitions id=%s ==" % competition_id)
for field in fields:
    value = row.get(field)
    text = "" if value is None else str(value)
    print("  %-20s %s" % (field, (text[:70] + "…") if len(text) > 70 else text or "（空）"))

filled = [f for f in ("short_name", "image_url", "participants", "content_scope",
                      "exam_schedule", "reg_start_date", "contest_start_date")
          if str(row.get(f) or "").strip()]
print("\n  非空关键字段: %s（%d/%d）" % ("、".join(filled) or "无", len(filled), 7))
print("  候选状态: %s -> competition_id=%s" % (cand["status"], cand["competition_id"]))