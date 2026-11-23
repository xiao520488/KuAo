# -*- coding: utf-8 -*-
"""award_sources 表的 DB 层单测（临时库，不碰 competitions.db、不联网）。

覆盖：幂等建表、upsert 只更新传入字段、(competition_id, url) 唯一、
mark_award_source_fetch 的状态校验与错误截断、delete。

运行：
    python -m pytest tests/test_award_sources.py -q
"""

from __future__ import annotations

import os
import tempfile
import unittest

import db


class AwardSourcesDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)
        db.DB_PATH, self._orig = self.path, db.DB_PATH
        # award_sources.competition_id 有外键，先造两个赛事
        self.cid_a = db.add_competition({"name": "测试赛事甲", "category": "测试"})
        self.cid_b = db.add_competition({"name": "测试赛事乙", "category": "测试"})

    def tearDown(self):
        db.DB_PATH = self._orig
        self.tmp.cleanup()

    def test_ensure_is_idempotent(self):
        self.assertFalse(db.ensure_award_sources(self.path))  # init_db 已建
        db.ensure_award_sources(self.path)

    def test_upsert_then_partial_update(self):
        sid = db.upsert_award_source({
            "competition_id": self.cid_a,
            "url": "https://example.com/a.xlsx",
            "adapter": "xlsx_sheet",
            "parse_spec": {"columns": {"作品名称": "title"}},
            "years": "2026",
        })
        same = db.upsert_award_source({
            "competition_id": self.cid_a,
            "url": "https://example.com/a.xlsx",
            "adapter": "xlsx_sheet",
            "title": "第十九届",
        })
        self.assertEqual(sid, same)
        row = db.get_award_source(sid)
        self.assertEqual(row["title"], "第十九届")
        # 未传 parse_spec 时不能被清空
        self.assertEqual(row["parse_spec_obj"]["columns"]["作品名称"], "title")
        self.assertEqual(row["years"], "2026")

    def test_rejects_unknown_adapter(self):
        with self.assertRaises(ValueError):
            db.upsert_award_source({"competition_id": 1, "url": "https://x",
                                    "adapter": "magic"})

    def test_mark_fetch_truncates_error(self):
        sid = db.upsert_award_source({"competition_id": 1, "url": "https://x",
                                      "adapter": "pdf_table"})
        db.mark_award_source_fetch(sid, "failed", rows=0, error="x" * 2000)
        row = db.get_award_source(sid)
        self.assertEqual(row["last_status"], "failed")
        self.assertLessEqual(len(row["last_error"]), 500)
        with self.assertRaises(ValueError):
            db.mark_award_source_fetch(sid, "whatever")

    def test_list_filter_and_delete(self):
        db.upsert_award_source({"competition_id": 1, "url": "https://a",
                                "adapter": "html_table"})
        sid = db.upsert_award_source({"competition_id": 2, "url": "https://b",
                                      "adapter": "pdf_table", "enabled": False})
        self.assertEqual(len(db.list_award_sources()), 2)
        self.assertEqual(len(db.list_award_sources(competition_id=2)), 1)
        self.assertEqual(len(db.list_award_sources(enabled_only=True)), 1)
        self.assertTrue(db.delete_award_source(sid))
        self.assertIsNone(db.get_award_source(sid))


if __name__ == "__main__":
    unittest.main()
