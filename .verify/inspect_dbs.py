# -*- coding: utf-8 -*-
"""列出各 sqlite 库的表与候选数量，确认审批测试该用哪个库。"""
import os
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(BASE, "backend")

for name in ("competitions.db", "test_crawl.db"):
    path = os.path.join(BACKEND, name)
    print("=" * 70)
    print(name, os.path.getsize(path) if os.path.exists(path) else "MISSING")
    if not os.path.exists(path):
        continue
    con = sqlite3.connect(path)
    tables = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' order by name")]
    print("tables:", tables)
    for table in ("competitions", "candidates", "users"):
        if table in tables:
            print("  %-14s %d 行" % (table, con.execute(
                "select count(*) from %s" % table).fetchone()[0]))
    if "candidates" in tables:
        print("  pending:", con.execute(
            "select count(*) from candidates where status='pending'").fetchone()[0])
    if "users" in tables:
        print("  admins:", [tuple(r) for r in con.execute(
            "select id, username, is_admin from users where is_admin=1")])
    con.close()