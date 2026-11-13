# -*- coding: utf-8 -*-
"""contest_dates.py 的护栏测试（纯函数 + 临时库，不联网、不调模型）。

覆盖：
  * 原文佐证：正文没写这一天 -> 丢弃（防模型编造）；
  * 只补空：已有 contest_start_date 绝不覆盖；
  * 年份护栏：往年/远期日期丢弃；只有月日时按离今天最近推断；
  * 起止顺序：end < start 时丢弃 end；
  * 月度轨联动：写日期时顺手补空的 contest_*_month；
  * apply()：只写通过的列，不碰其他字段。

运行::

    python -m pytest test_contest_dates.py -q
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import date

import contest_dates as cd
import db


class EvidenceTest(unittest.TestCase):
    def test_accepts_common_chinese_forms(self) -> None:
        for text in ("决赛于11月14日举行", "于 11 月 14 日 决赛",
                     "比赛：11-14", "2026年11月14日 决赛"):
            with self.subTest(text=text):
                self.assertTrue(cd.evidence(text, "2026-11-14"))

    def test_rejects_other_days(self) -> None:
        self.assertFalse(cd.evidence("决赛于11月15日举行", "2026-11-14"))
        self.assertFalse(cd.evidence("", "2026-11-14"))
        self.assertFalse(cd.evidence("报名截止11月14日", "2026-11-14") is False)


class InferYearTest(unittest.TestCase):
    def test_picks_closest_year(self) -> None:
        today = date(2026, 11, 13)
        self.assertEqual(cd.infer_year(11, 20, today=today), 2026)
        self.assertEqual(cd.infer_year(4, 11, today=today), 2026)
        self.assertEqual(cd.infer_year(2, 1, today=today), 2026)

    def test_clamps_out_of_window_year(self) -> None:
        today = date(2026, 11, 13)
        self.assertTrue(cd._year_plausible("2027-01-01", today=today))
        self.assertFalse(cd._year_plausible("2024-11-14", today=today))
        self.assertFalse(cd._year_plausible("2029-11-14", today=today))


class PlanTest(unittest.TestCase):
    today = date(2026, 11, 13)

    def test_fills_empty_with_evidence(self) -> None:
        patch = cd.plan_date_writes(
            {}, {"contest_start_date": "2026-11-14"},
            "决赛于11月14日举行", today=self.today)
        self.assertEqual(patch["contest_start_date"], "2026-11-14")
        self.assertEqual(patch["contest_start_month"], 11)

    def test_never_overwrites_existing(self) -> None:
        patch = cd.plan_date_writes(
            {"contest_start_date": "2026-05-01"},
            {"contest_start_date": "2026-11-14"},
            "决赛于11月14日举行", today=self.today)
        self.assertNotIn("contest_start_date", patch)

    def test_drops_hallucinated_date(self) -> None:
        patch = cd.plan_date_writes(
            {}, {"contest_start_date": "2026-11-14"},
            "赛程另行通知", today=self.today)
        self.assertEqual(patch, {})

    def test_drops_out_of_range_year(self) -> None:
        patch = cd.plan_date_writes(
            {}, {"contest_start_date": "2024-11-14"},
            "2024年11月14日 决赛", today=self.today)
        self.assertNotIn("contest_start_date", patch)

    def test_end_before_start_is_dropped(self) -> None:
        patch = cd.plan_date_writes(
            {}, {"contest_start_date": "2026-11-16",
                 "contest_end_date": "2026-11-14"},
            "11月16日开赛，11月14日颁奖", today=self.today)
        self.assertEqual(patch["contest_start_date"], "2026-11-16")
        self.assertNotIn("contest_end_date", patch)

    def test_month_only_day_inferred(self) -> None:
        patch = cd.plan_date_writes(
            {}, {"contest_start_date": "11月20日"},
            "11月20日开赛", today=self.today)
        self.assertEqual(patch["contest_start_date"], "2026-11-20")

    def test_no_day_means_null(self) -> None:
        patch = cd.plan_date_writes(
            {}, {"contest_start_date": "7月至8月"},
            "7月至8月举行决赛", today=self.today)
        self.assertEqual(patch, {})

    def test_year_not_in_text_is_reinferred(self) -> None:
        """正文没写年份，模型自己猜的年份不采信，按「优先今年」重新推断。"""
        patch = cd.plan_date_writes(
            {}, {"contest_start_date": "2025-05-02"},
            "正式比赛定于 5 月 2 日至 4 日", today=self.today)
        self.assertEqual(patch["contest_start_date"], "2026-05-02")

    def test_explicit_year_in_text_is_kept(self) -> None:
        patch = cd.plan_date_writes(
            {}, {"contest_start_date": "2026-04-11"},
            "2026年4月11日举行决赛", today=self.today)
        self.assertEqual(patch["contest_start_date"], "2026-04-11")


class ApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="kuao-contest-dates-")
        self._orig = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir, "test.db")
        db.init_db()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO competitions (id, name, category, level, is_active, "
                "exam_schedule) VALUES (1, '测试赛事', '测试', 'national', 1, ?)",
                ("1. 报名：3月1日至4月30日；2. 决赛于11月14日举行。",),
            )

    def tearDown(self) -> None:
        db.DB_PATH = self._orig
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _row(self) -> dict:
        with db.get_conn() as conn:
            conn.row_factory = None
            row = conn.execute(
                "SELECT contest_start_date, contest_start_month FROM competitions "
                "WHERE id = 1").fetchone()
        return {"contest_start_date": row[0], "contest_start_month": row[1]}

    def test_apply_writes_only_validated(self) -> None:
        with open(os.path.join(self._tmpdir, "x"), "w"):
            pass
        written = cd.apply(
            db.DB_PATH, 1,
            {"contest_start_date": "2026-11-14", "contest_end_date": "2026-11-15"},
            self._schedule(), today=date(2026, 11, 13))
        self.assertEqual(written["contest_start_date"], "2026-11-14")
        self.assertNotIn("contest_end_date", written)  # 正文没有 11月15日
        self.assertEqual(self._row(), {"contest_start_date": "2026-11-14",
                                       "contest_start_month": 11})

    def _schedule(self) -> str:
        with db.get_conn() as conn:
            return conn.execute(
                "SELECT exam_schedule FROM competitions WHERE id = 1").fetchone()[0]


if __name__ == "__main__":
    unittest.main()