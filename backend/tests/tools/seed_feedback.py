# -*- coding: utf-8 -*-
"""往本地开发库插入 2 条测试反馈（仅本地验收用，勿在生产库执行）。

用法：backend/tests/tools/seed_feedback.py（仅本地库，勿在生产执行）
"""
import os
import sqlite3
from datetime import datetime, timedelta

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "backend", "competitions.db")  # 生产库：仅本地开发用

ROWS = [
    (4, "UI 很好看，暗色模式下也很舒服，就是希望首页再多推荐几场最近的比赛。"),
    (2, "希望增加更多比赛，尤其是人工智能和数据分析方向，现在搜到的还是偏少。"),
]


def main():
    conn = sqlite3.connect(DB)
    try:
        now = datetime.now()
        for idx, (user_id, content) in enumerate(ROWS):
            created = (now - timedelta(minutes=8 * (len(ROWS) - idx))).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO feedbacks (user_id, content, created_at, is_read) VALUES (?, ?, ?, 0)",
                (user_id, content, created),
            )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0]
        unread = conn.execute("SELECT COUNT(*) FROM feedbacks WHERE is_read = 0").fetchone()[0]
        print("feedbacks total=%s unread=%s" % (total, unread))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
