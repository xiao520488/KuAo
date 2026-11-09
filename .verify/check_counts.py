# -*- coding: utf-8 -*-
import sqlite3

c = sqlite3.connect(r"D:\agent-lab\KuAo-Platform\backend\competitions.db")
print("feedbacks=", c.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0])
print("unread=", c.execute("SELECT COUNT(*) FROM feedbacks WHERE is_read=0").fetchone()[0])
print("pending=", c.execute("SELECT COUNT(*) FROM competition_candidates WHERE status='pending'").fetchone()[0])
c.close()
