# -*- coding: utf-8 -*-
"""单场上线配额（300 条）+ 奖项等级优先级的单测（临时库，不联网）。

产品口径（2026-11-23）：
  * 每个赛事最多上线 300 条获奖作品，库里数据不删；
  * 截断时按「金 > 银 > 铜」优先级保留，高等级必须排在前面；
  * 分页 offset 越不过 300，超出返回空列表（total 也封顶，避免前端无限翻页）。

运行：
    python -m pytest tests/test_award_public_cap.py -q
"""

from __future__ import annotations

import os
import tempfile
import unittest

import db


def _add(comp_id: int, title: str, grade: str, year: int = 2026) -> int:
    return db.create_award({
        "competition_id": comp_id,
        "title": title,
        "award_grade": grade,
        "year": year,
        "review_status": "pending",
    })


class PublicAwardsCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)
        db.DB_PATH, self._orig = self.path, db.DB_PATH
        self.cid = db.add_competition({"name": "测试赛事", "category": "测试"})

    def tearDown(self):
        db.DB_PATH = self._orig
        self.tmp.cleanup()

    def test_cap_is_300(self):
        self.assertEqual(db.PUBLIC_AWARDS_CAP, 300)
        for i in range(305):
            _add(self.cid, f"作品{i}", "三等奖")
        rows = db.list_awards_by_competition(self.cid)
        self.assertEqual(len(rows), 300)

    def test_offset_beyond_cap_returns_empty(self):
        for i in range(302):
            _add(self.cid, f"作品{i}", "三等奖")
        self.assertEqual(db.list_awards_by_competition(self.cid, limit=20, offset=300), [])
        self.assertEqual(len(db.list_awards_by_competition(self.cid, limit=20, offset=299)), 1)

    def test_limit_is_clamped_at_cap(self):
        for i in range(301):
            _add(self.cid, f"作品{i}", "三等奖")
        # 传 1000 也只能拿到 300（默认 limit 同样是 300）
        self.assertEqual(len(db.list_awards_by_competition(self.cid, limit=1000)), 300)

    def test_gold_before_silver_before_bronze(self):
        # 故意先插铜奖、再银奖、最后金奖，验证排序而不是插入顺序
        for i in range(5):
            _add(self.cid, f"铜{i}", "铜奖")
        for i in range(5):
            _add(self.cid, f"银{i}", "银奖")
        for i in range(5):
            _add(self.cid, f"金{i}", "金奖")
        grades = [r["award_grade"] for r in db.list_awards_by_competition(self.cid)]
        self.assertEqual(grades, ["金奖"] * 5 + ["银奖"] * 5 + ["铜奖"] * 5)

    def test_grade_aliases_share_tier(self):
        """一等/二等/三等与金/银/铜同层，特等/冠军排最前。"""
        _add(self.cid, "a", "三等奖")
        _add(self.cid, "b", "二等奖")
        _add(self.cid, "c", "一等奖")
        _add(self.cid, "d", "特等奖")
        _add(self.cid, "e", "总冠军（一等奖）")
        _add(self.cid, "f", "优胜奖")
        titles = [r["title"] for r in db.list_awards_by_competition(self.cid)]
        # 特等/总冠军最先；然后一等/金；二等/银；三等/铜；最后优胜
        self.assertEqual(titles[:2], ["d", "e"])
        self.assertEqual(titles[2], "c")
        self.assertEqual(titles[3], "b")
        self.assertEqual(titles[4], "a")
        self.assertEqual(titles[5], "f")

    def test_truncation_keeps_best_awards(self):
        """308 条里混 8 个金奖：截断后 8 个金奖必须都在，且排在 300 条最前。"""
        for i in range(300):
            _add(self.cid, f"三等{i}", "三等奖")
        for i in range(8):
            _add(self.cid, f"金{i}", "金奖")
        rows = db.list_awards_by_competition(self.cid)
        self.assertEqual(len(rows), 300)
        self.assertEqual([r["award_grade"] for r in rows[:8]], ["金奖"] * 8)

    def test_rejected_still_filtered(self):
        keep = _add(self.cid, "留下的", "金奖")
        drop = _add(self.cid, "被拒的", "特等奖")
        db.update_award(drop, {"review_status": "rejected"})
        rows = db.list_awards_by_competition(self.cid)
        self.assertEqual([r["id"] for r in rows], [keep])


if __name__ == "__main__":
    unittest.main()
