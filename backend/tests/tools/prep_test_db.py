# -*- coding: utf-8 -*-
"""为一轮审批冒烟测试准备隔离库：复制生产库 -> <tmp>/kuo-verify/approve_api_test.db。

不碰 competitions.db 本身；复制完成后挑一条 pending 候选（优先 saikr，
且主表无同名赛事），并保证库里存在一个管理员账号。
"""
import json
import tempfile
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # 仓库根
BACKEND = os.path.join(BASE, "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

SRC = os.path.join(BACKEND, "competitions.db")
DST = os.path.join(tempfile.gettempdir(), "kuo-verify", "approve_api_test.db")
os.makedirs(os.path.dirname(DST), exist_ok=True)

for suffix in ("", "-wal", "-shm"):
    path = DST + suffix
    if os.path.exists(path):
        os.remove(path)

src = sqlite3.connect(SRC)
dst = sqlite3.connect(DST)
src.backup(dst)
dst.close()
src.close()

import db  # noqa: E402 - 必须在改完路径后导入

db.DB_PATH = DST

con = sqlite3.connect(DST)
con.row_factory = sqlite3.Row

users = [dict(r) for r in con.execute("select id, username, is_admin from users order by id")]
if not users:
    print("该库没有任何用户，无法测试管理员接口")
    sys.exit(1)
admin = next((u for u in users if u["is_admin"]), users[0])
con.execute("update users set is_admin = 1 where id = ?", (admin["id"],))
con.commit()
admin = dict(con.execute("select id, username, is_admin from users where id = ?",
                         (admin["id"],)).fetchone())

rows = [dict(r) for r in con.execute(
    "select id, source, source_key, raw_name, image_url from competition_candidates "
    "where status = 'pending' order by id"
)]
picked = None
for row in rows:
    name = (row.get("raw_name") or "").strip()
    if not name:
        continue
    duplicate = db.find_duplicate_candidate(
        db.normalize_competition_name(name),
        official_url=None,
        db_path=DST,
        include_candidates=False,
    )
    if duplicate:
        continue
    if row["source"] == "saikr" and picked is None:
        picked = row  # 首选 saikr：能验证 contest_id 直连抓取
    elif picked is None:
        picked = row

con.close()

print(json.dumps({
    "dst_db": DST,
    "admin": admin,
    "pending_total": len(rows),
    "picked": picked,
}, ensure_ascii=False, indent=2))