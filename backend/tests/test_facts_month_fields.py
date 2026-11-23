# -*- coding: utf-8 -*-
"""db.set_competition_facts 的月份字段支持（临时库，不联网）。

覆盖：
  * 只写月份（官网只公布到月）-> 月份落库、日期不动、台账记值；
  * 写比赛日期 -> contest_*_month 自动跟随（新增的 _refresh_contest_months）；
  * 非法月份 -> ValueError，且不落库；
  * 清空（None）-> 台账 value 为 NULL。

运行：
    python -m pytest tests/test_facts_month_fields.py -q
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db

SCHEMA = """
CREATE TABLE competitions (
    id INTEGER PRIMARY KEY,
    name TEXT,
    reg_start_date TEXT, reg_end_date TEXT,
    reg_start_month INTEGER, reg_end_month INTEGER, reg_cross_year INTEGER,
    contest_start_date TEXT, contest_end_date TEXT,
    contest_start_month INTEGER, contest_end_month INTEGER,
    facts_checked_at TEXT
);
CREATE TABLE competition_fact_ledger (
    competition_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    source_url TEXT,
    verified_at TEXT,
    verified_by TEXT,
    PRIMARY KEY (competition_id, field)
);
"""


class SetFactsMonthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "t.db")
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO competitions (id, name) VALUES (1, '测试赛事'), (2, '另一赛')"
        )
        conn.commit()
        conn.close()
        self._old = db.DB_PATH
        db.DB_PATH = self.path

    def tearDown(self):
        db.DB_PATH = self._old
        self.tmp.cleanup()

    def _row(self, cid=1):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM competitions WHERE id = ?", (cid,)).fetchone()
        conn.close()
        return dict(r)

    def _ledger(self, cid=1):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT field, value, source_url, verified_by FROM competition_fact_ledger "
            "WHERE competition_id = ? ORDER BY field", (cid,)
        ).fetchall()
        conn.close()
        return {r["field"]: dict(r) for r in rows}

    def test_month_only_write(self):
        written = db.set_competition_facts(
            1, {"contest_start_month": 7}, source_url="https://x.test/n", verified_by="t"
        )
        self.assertEqual(written, {"contest_start_month": 7})
        row = self._row()
        self.assertEqual(row["contest_start_month"], 7)
        self.assertIsNone(row["contest_start_date"])
        self.assertIsNotNone(row["facts_checked_at"])
        led = self._ledger()
        self.assertEqual(led["contest_start_month"]["value"], "7")
        self.assertEqual(led["contest_start_month"]["source_url"], "https://x.test/n")

    def test_contest_date_backfills_months(self):
        db.set_competition_facts(
            2, {"contest_start_date": "2026-07-09", "contest_end_date": "2026-07-17"}
        )
        row = self._row(2)
        self.assertEqual(row["contest_start_month"], 7)
        self.assertEqual(row["contest_end_month"], 7)

    def test_reg_date_still_backfills_reg_months(self):
        db.set_competition_facts(
            2, {"reg_start_date": "2026-11-01", "reg_end_date": "2027-02-10"}
        )
        row = self._row(2)
        self.assertEqual((row["reg_start_month"], row["reg_end_month"]), (11, 2))
        self.assertEqual(row["reg_cross_year"], 1)

    def test_invalid_month_rejected(self):
        with self.assertRaises(ValueError):
            db.set_competition_facts(1, {"contest_start_month": 13})
        self.assertIsNone(self._row()["contest_start_month"])
        self.assertEqual(self._ledger(), {})

    def test_clear_writes_null_to_ledger(self):
        db.set_competition_facts(1, {"contest_start_month": 7})
        db.set_competition_facts(1, {"contest_start_month": None})
        self.assertIsNone(self._row()["contest_start_month"])
        self.assertIsNone(self._ledger()["contest_start_month"]["value"])

    def test_partial_contest_date_keeps_other_month(self):
        # 已有比赛结束月份（官网只给到月），只补开始日时不能把它抹掉
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE competitions SET contest_end_month = 9 WHERE id = 1")
        conn.commit()
        conn.close()
        db.set_competition_facts(1, {"contest_start_date": "2026-06-15"})
        row = self._row()
        self.assertEqual(row["contest_start_month"], 6)
        self.assertEqual(row["contest_end_month"], 9)

    def test_partial_reg_date_keeps_other_month(self):
        # 只有报名截止（官网没公布开始）时，至少把截止月份填上
        db.set_competition_facts(1, {"reg_end_date": "2026-09-07"})
        row = self._row()
        self.assertEqual(row["reg_end_month"], 9)
        self.assertIsNone(row["reg_cross_year"])

    def test_unknown_field_ignored(self):
        with self.assertRaises(ValueError):
            db.set_competition_facts(1, {"official_url": "https://evil"})


if __name__ == "__main__":
    unittest.main()
