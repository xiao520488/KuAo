# -*- coding: utf-8 -*-
"""stage_schedule.py 的护栏测试（纯函数 + 临时库，不联网、不调模型）。

覆盖：
  * 阶段名归一化（同义词 -> 报名/初赛/复赛/决赛）；
  * 日期护栏：原文无「月+日」佐证 -> NULL；年份越界 -> 丢弃；
  * 只有月份时保留 note、日期为 NULL（绝不编造某一天）；
  * end < start 时丢弃 end；重复阶段去重；
  * 跨度只算比赛阶段（不含报名）；
  * 只补空：已有 stage_schedule / 日期不覆盖；
  * apply() 真写临时库。

运行::

    python -m pytest test_stage_schedule.py -q
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date

import db
import stage_schedule as ss

TODAY = date(2026, 11, 13)


class NormalizeStageTest(unittest.TestCase):
    def test_aliases(self) -> None:
        cases = {
            "报名时间": "报名",
            "网上报名": "报名",
            "初赛": "初赛",
            "全国初赛": "初赛",
            "省赛": "省赛",
            "省级选拔赛": "省赛",
            "校内赛": "校赛",
            "复赛选拔": "复赛",
            "全国总决赛": "决赛",
            "现场决赛": "决赛",
            "作品申报": "作品申报",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ss.normalize_stage(raw), expected)

    def test_empty(self) -> None:
        self.assertEqual(ss.normalize_stage("  "), "")


class GuardStagesTest(unittest.TestCase):
    def test_keeps_evidenced_dates(self) -> None:
        text = "1.大赛报名时间（2026年9月10日～11月30日）2.作品申报（2026年12月1日～12月20日）"
        out = ss.guard_stages([
            {"stage": "报名", "start_date": "2026-09-10", "end_date": "2026-11-30"},
            {"stage": "初赛", "start_date": "2026-12-01", "end_date": "2026-12-20"},
        ], text, today=TODAY)
        self.assertEqual([row["stage"] for row in out], ["报名", "初赛"])
        self.assertEqual(out[0]["start_date"], "2026-09-10")
        self.assertEqual(out[1]["end_date"], "2026-12-20")

    def test_drops_hallucinated_date(self) -> None:
        text = "决赛于11月14日举行"
        out = ss.guard_stages([
            {"stage": "决赛", "start_date": "2026-11-15", "end_date": None},
        ], text, today=TODAY)
        self.assertEqual(out, [])

    def test_month_only_keeps_note_without_date(self) -> None:
        text = "4.决赛、展示及表彰（预计2027年5月）"
        out = ss.guard_stages([
            {"stage": "决赛", "start_date": None, "end_date": None,
             "note": "预计2027年5月"},
        ], text, today=TODAY)
        self.assertEqual(out[0]["stage"], "决赛")
        self.assertIsNone(out[0]["start_date"])
        self.assertEqual(out[0]["note"], "预计2027年5月")

    def test_year_not_in_text_is_reinferred(self) -> None:
        text = "正式比赛定于 5 月 2 日至 5 月 4 日"
        out = ss.guard_stages([
            {"stage": "决赛", "start_date": "2025-05-02", "end_date": "2025-05-04"},
        ], text, today=TODAY)
        self.assertEqual(out[0]["start_date"], "2026-05-02")
        self.assertEqual(out[0]["end_date"], "2026-05-04")

    def test_out_of_window_year_dropped(self) -> None:
        text = "2020年8月17日至8月22日决赛"
        out = ss.guard_stages([
            {"stage": "决赛", "start_date": "2020-08-17", "end_date": "2020-08-22"},
        ], text, today=TODAY)
        # 日期被丢，但没有 note => 整条丢弃
        self.assertEqual(out, [])

    def test_end_before_start_dropped(self) -> None:
        text = "初赛 2026年12月20日至12月1日"
        out = ss.guard_stages([
            {"stage": "初赛", "start_date": "2026-12-20", "end_date": "2026-12-01"},
        ], text, today=TODAY)
        self.assertEqual(out[0]["end_date"], None)

    def test_dedup_and_order(self) -> None:
        text = "报名 2026年9月10日；决赛 2027年5月8日；初赛 2026年12月1日"
        out = ss.guard_stages([
            {"stage": "决赛", "start_date": "2027-05-08"},
            {"stage": "报名", "start_date": "2026-09-10"},
            {"stage": "初赛", "start_date": "2026-12-01"},
            {"stage": "初赛", "start_date": "2026-12-02"},
        ], text, today=TODAY)
        self.assertEqual([r["stage"] for r in out], ["报名", "初赛", "决赛"])

    def test_noise_items_ignored(self) -> None:
        out = ss.guard_stages([{"stage": ""}, "垃圾", {"stage": "初赛"}], "", today=TODAY)
        self.assertEqual(out, [])

    def test_empty_note_only_stage_dropped(self) -> None:
        """只有「具体时间以官网为准」这种空话的阶段不占行。"""
        out = ss.guard_stages([
            {"stage": "初赛", "note": "具体时间以赛项方案为准"},
        ], "初赛以赛项方案为准", today=TODAY)
        self.assertEqual(out, [])

    def test_stale_year_note_dropped(self) -> None:
        out = ss.guard_stages([
            {"stage": "全国选拔", "note": "第46届，2020年完成全国选拔"},
        ], "第46届，2020年完成全国选拔", today=TODAY)
        self.assertEqual(out, [])

    def test_month_note_kept(self) -> None:
        out = ss.guard_stages([
            {"stage": "决赛", "note": "预计2027年5月"},
        ], "预计2027年5月", today=TODAY)
        self.assertEqual(out[0]["stage"], "决赛")


class SpanTest(unittest.TestCase):
    def test_span_excludes_registration(self) -> None:
        stages = [
            {"stage": "报名", "start_date": "2026-09-10", "end_date": "2026-11-30"},
            {"stage": "初赛", "start_date": "2026-12-01", "end_date": "2026-12-20"},
            {"stage": "决赛", "start_date": None, "end_date": None, "note": "预计2027年5月"},
        ]
        self.assertEqual(ss.span(stages), ("2026-12-01", "2026-12-20"))

    def test_span_only_registration(self) -> None:
        stages = [{"stage": "报名", "start_date": "2026-09-10", "end_date": "2026-11-30"}]
        self.assertEqual(ss.span(stages), (None, None))

    def test_span_excludes_administrative_nodes(self) -> None:
        """三创赛：校赛注册/省赛承办申请/团队审核/成绩公示 不得把顶部跨度拉到去年。"""
        stages = [
            {"stage": "报名", "start_date": "2025-10-20", "end_date": "2025-12-31"},
            {"stage": "校赛", "start_date": "2025-10-20", "end_date": "2025-11-30",
             "note": "校赛注册及备案"},
            {"stage": "省赛", "start_date": "2025-10-20", "end_date": "2025-11-30",
             "note": "省赛承办单位申请"},
            {"stage": "团队审核", "start_date": "2025-10-20", "end_date": "2026-03-10"},
            {"stage": "初赛", "start_date": "2026-03-11", "end_date": "2026-04-10"},
            {"stage": "决赛", "start_date": "2026-07-10", "end_date": "2026-08-10"},
            {"stage": "成绩公示", "start_date": "2026-08-05", "end_date": "2026-08-12"},
        ]
        self.assertEqual(ss.span(stages), ("2026-03-11", "2026-08-10"))

    def test_span_drops_end_when_before_start(self) -> None:
        """「名单公布 6/10」+「决赛 7/12」：结束日早于开始日，只保留开始日。"""
        stages = [
            {"stage": "名单公布", "start_date": None, "end_date": "2026-06-10"},
            {"stage": "决赛", "start_date": "2026-07-12", "end_date": None},
        ]
        self.assertEqual(ss.span(stages), ("2026-07-12", None))


class PlanWritesTest(unittest.TestCase):
    def test_fills_empty_columns(self) -> None:
        stages = ss.guard_stages([
            {"stage": "初赛", "start_date": "2026-12-01", "end_date": "2026-12-20"},
        ], "初赛 2026年12月1日至12月20日", today=TODAY)
        patch = ss.plan_writes({}, stages, "初赛 2026年12月1日至12月20日", today=TODAY)
        self.assertEqual(patch["contest_start_date"], "2026-12-01")
        self.assertEqual(patch["contest_start_month"], 12)
        self.assertEqual(json.loads(patch["stage_schedule"])[0]["stage"], "初赛")

    def test_never_overwrites_existing(self) -> None:
        stages = [{"stage": "初赛", "start_date": "2026-12-01", "end_date": None, "note": ""}]
        current = {"stage_schedule": '[{"stage":"报名","start_date":"2026-09-01"}]',
                   "contest_start_date": "2026-10-01", "contest_end_date": None,
                   "contest_start_month": 10, "contest_end_month": None}
        patch = ss.plan_writes(current, stages, "初赛 2026年12月1日", today=TODAY)
        self.assertEqual(patch, {})

    def test_loads_bad_json(self) -> None:
        self.assertEqual(ss.loads("not json"), [])
        self.assertEqual(ss.loads(None), [])
        self.assertEqual(ss.loads('[{"stage":"初赛"}]'), [{"stage": "初赛"}])


class ApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        old = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()
        self.addCleanup(db.init_db)  # 仅确保连接可关闭
        db.DB_PATH = old
        self.addCleanup(lambda: db.DB_PATH == self.db_path and setattr(db, "DB_PATH", old))
        with db.get_conn(self.db_path) as conn:
            conn.execute(
                "INSERT INTO competitions (id, name, exam_schedule) VALUES (1, ?, ?)",
                ("测试赛事", "报名 2026年9月10日至11月30日；初赛 2026年12月1日至12月20日"),
            )

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_apply_writes_only_guarded_dates(self) -> None:
        raw = "报名 2026年9月10日至11月30日；初赛 2026年12月1日至12月20日"
        stages = ss.guard_stages([
            {"stage": "报名", "start_date": "2026-09-10", "end_date": "2026-11-30"},
            {"stage": "初赛", "start_date": "2026-12-01", "end_date": "2026-12-20"},
            {"stage": "决赛", "start_date": "2026-12-25", "end_date": None},
        ], raw, today=TODAY)
        patch = ss.apply(self.db_path, 1, stages, raw, today=TODAY)
        self.assertIn("stage_schedule", patch)
        self.assertEqual(patch["contest_start_date"], "2026-12-01")
        self.assertEqual(patch["contest_end_date"], "2026-12-20")
        with db.get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT stage_schedule, contest_start_date FROM competitions WHERE id=1"
            ).fetchone()
        stored = json.loads(row[0])
        self.assertEqual([r["stage"] for r in stored], ["报名", "初赛"])
        self.assertEqual(row[1], "2026-12-01")


class DeriveSpanTest(unittest.TestCase):
    """顶部总跨度的聚合：精确到日走 date 轨，只到月走 month 轨（2026-11-13 傍晚补）。"""

    def test_month_only_notes_fill_month_columns(self) -> None:
        stages = [
            {"stage": "报名", "start_date": "2026-09-10", "end_date": "2026-10-31", "note": ""},
            {"stage": "校赛", "start_date": None, "end_date": None, "note": "安排在2026年11月"},
            {"stage": "初赛", "start_date": None, "end_date": None, "note": "省初赛2026年11月"},
            {"stage": "复赛", "start_date": None, "end_date": None, "note": "2026年12月举行"},
            {"stage": "决赛", "start_date": None, "end_date": None, "note": "中国总决赛2027年3月进行"},
        ]
        self.assertEqual(ss.derive_span(stages, today=TODAY),
                         {"contest_start_month": 11, "contest_end_month": 3})
        patch = ss.plan_writes({"stage_schedule": ss.dumps(stages[:1])}, stages,
                               ss.dumps(stages), today=TODAY)
        self.assertEqual(patch, {"contest_start_month": 11, "contest_end_month": 3})

    def test_past_year_month_only_dropped(self) -> None:
        """防呆：只到月的往年数据丢弃（爬到上一届的典型症状）。"""
        stages = [
            {"stage": "校赛", "start_date": None, "end_date": None, "note": "2025年11月"},
            {"stage": "初赛", "start_date": None, "end_date": None, "note": "2026年3月"},
        ]
        span = ss.derive_span(stages, today=TODAY)
        self.assertNotIn(11, span.values())
        self.assertEqual(span, {"contest_start_month": 3})

    def test_date_track_wins_over_month_track(self) -> None:
        stages = [
            {"stage": "初赛", "start_date": "2026-12-01", "end_date": "2026-12-20",
             "note": "2026年12月评审"},
            {"stage": "决赛", "start_date": None, "end_date": None, "note": "预计2027年5月"},
        ]
        span = ss.derive_span(stages, today=TODAY)
        self.assertEqual(span["contest_start_date"], "2026-12-01")
        # 决赛只到月，所以结束侧走月份轨（2027年5月）
        self.assertEqual(span["contest_end_month"], 5)
        self.assertNotIn("contest_end_date", span)

    def test_single_candidate_sets_one_side_only(self) -> None:
        self.assertEqual(ss.derive_span(
            [{"stage": "初赛", "start_date": "2026-12-01", "note": ""}], today=TODAY),
            {"contest_start_date": "2026-12-01"})
        self.assertEqual(ss.derive_span(
            [{"stage": "决赛", "end_date": "2026-12-20", "note": ""}], today=TODAY),
            {"contest_end_date": "2026-12-20"})

    def test_note_day_or_out_of_window_ignored(self) -> None:
        stages = [
            {"stage": "初赛", "start_date": None, "end_date": None, "note": "2020年11月回顾"},
            {"stage": "决赛", "start_date": None, "end_date": None, "note": "12月20日以官网为准"},
        ]
        self.assertEqual(ss.derive_span(stages, today=TODAY), {})

    def test_month_only_does_not_overwrite_existing(self) -> None:
        stages = [{"stage": "初赛", "start_date": None, "end_date": None, "note": "2026年12月"}]
        current = {"stage_schedule": "[{}]", "contest_start_month": 5,
                   "contest_start_date": "2026-12-01"}
        self.assertEqual(ss.plan_writes(current, stages, "", today=TODAY), {})


if __name__ == "__main__":
    unittest.main()