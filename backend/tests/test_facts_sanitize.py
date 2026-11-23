# -*- coding: utf-8 -*-
"""competition_facts.sanitize_facts 的护栏测试（纯函数，不联网、不调模型、不写库）。

覆盖：
  * 合法的一对报名 / 比赛日期原样保留；
  * 报名对内部倒挂 -> 丢弃截止；
  * 比赛对内部倒挂 -> 丢弃结束；
  * 跨对矛盾：报名晚于比赛开始（往届赛期被拼进来）-> 丢比赛对、留报名对；
  * 跨对矛盾：比赛结束早于报名开始 -> 丢弃比赛对；
  * 年份越界 / 只有月份 -> 丢弃。

运行：
    python -m pytest tests/test_facts_sanitize.py -q
"""

from __future__ import annotations

import unittest
from datetime import date

import competition_facts

TODAY = date(2026, 11, 23)


def _clean(**facts) -> dict:
    return competition_facts.sanitize_facts(facts, today=TODAY)


class SanitizeFactsTest(unittest.TestCase):
    def test_keeps_valid_pair(self):
        out = _clean(
            reg_start_date="2026-03-01",
            reg_end_date="2026-04-30",
            contest_start_date="2026-07-15",
            contest_end_date="2026-07-17",
        )
        self.assertEqual(out["reg_start_date"], "2026-03-01")
        self.assertEqual(out["reg_end_date"], "2026-04-30")
        self.assertEqual(out["contest_start_date"], "2026-07-15")
        self.assertEqual(out["contest_end_date"], "2026-07-17")

    def test_drops_inverted_reg_end(self):
        out = _clean(reg_start_date="2026-05-01", reg_end_date="2026-04-01")
        self.assertEqual(out["reg_start_date"], "2026-05-01")
        self.assertNotIn("reg_end_date", out)

    def test_drops_inverted_contest_end(self):
        out = _clean(contest_start_date="2026-05-10", contest_end_date="2026-05-01")
        self.assertEqual(out["contest_start_date"], "2026-05-10")
        self.assertNotIn("contest_end_date", out)

    def test_cross_year_registration_is_not_a_conflict(self):
        """预报名跨年（2025-11-27 ~ 2026-02-15），比赛在 2026-07，属正常。"""
        out = _clean(
            reg_start_date="2025-11-27",
            reg_end_date="2026-02-15",
            contest_start_date="2026-07-17",
            contest_end_date="2026-07-22",
        )
        self.assertEqual(len(out), 4)

    def test_drops_stale_contest_when_registration_later(self):
        """线上实证 #115：报名截止 2026-01-25 配 2025 年的比赛，丢比赛对。"""
        out = _clean(
            reg_end_date="2026-01-25",
            contest_start_date="2025-01-31",
            contest_end_date="2025-02-15",
        )
        self.assertEqual(out, {"reg_end_date": "2026-01-25"})

    def test_drops_contest_ending_before_registration_start(self):
        out = _clean(
            reg_start_date="2026-06-01",
            contest_start_date="2026-03-01",
            contest_end_date="2026-03-10",
        )
        self.assertEqual(out, {"reg_start_date": "2026-06-01"})

    def test_keeps_registration_when_no_contest(self):
        out = _clean(reg_end_date="2026-10-23")
        self.assertEqual(out, {"reg_end_date": "2026-10-23"})

    def test_drops_out_of_range_year(self):
        out = _clean(reg_end_date="2019-05-01", contest_start_date="2030-05-01")
        self.assertEqual(out, {})

    def test_month_only_is_dropped(self):
        out = _clean(reg_end_date="2026-10", contest_start_date="2026年10月")
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()