# -*- coding: utf-8 -*-
"""赛氪每日更新的两道护栏：

1) 往届报名窗口（报名截止已过去 30 天以上）不许覆盖库里的日期；
2) 人工核对过（facts_checked_at 非空）的赛事，事实列一律不覆盖。

背景（2026-11-17）：每日 04:10 的 saikr_whitelist_update.py 把蓝桥杯人工核对好的
2026-10-08 ~ 2026-12-18 用赛氪上一届的 2025-10-20 ~ 2026-04-27 盖了回去。
"""

import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import saikr_whitelist_update as s


NOW = datetime(2026, 11, 17, 12, 0)


def _parse(value):
    if not value:
        return None
    return datetime.strptime(str(value)[:10], "%Y-%m-%d")


def _detail(reg_start, reg_end, contest_start=None, contest_end=None):
    return {
        "regist_start_time": reg_start,
        "regist_end_time": reg_end,
        "contest_start_time": contest_start,
        "contest_end_time": contest_end,
    }


def test_stale_registration_window_is_not_written():
    """上一届窗口（2025-10-20 ~ 2026-04-27）绝不能盖回库里。"""
    detail = _detail("2025-10-20T10:00:00", "2026-04-27T14:00:00",
                     "2026-04-11T09:00:00", "2026-04-25T18:00:00")
    patch, notes = s.build_patch(detail, {"id": 12}, NOW, _parse)

    for column in s.FACT_COLUMNS:
        assert column not in patch, column
    assert any("往届" in note for note in notes)


def test_current_window_still_updates():
    """现行窗口（未结束）照常刷新日期/月份/跨年。"""
    detail = _detail("2026-10-08T10:00:00", "2026-12-18T14:00:00")
    patch, notes = s.build_patch(detail, {"id": 12}, NOW, _parse)

    assert patch["reg_start_date"] == "2026-10-08"
    assert patch["reg_end_date"] == "2026-12-18"
    assert patch["reg_start_month"] == 10
    assert patch["reg_end_month"] == 12
    assert patch["reg_cross_year"] == 0
    assert not any("往届" in note for note in notes)


def test_verified_row_keeps_manual_facts():
    """人工核对过的行：日期字段被摘掉，只留 last_checked_at 之类的非事实列。"""
    patch = {
        "reg_start_date": "2025-10-20",
        "reg_end_date": "2026-04-27",
        "reg_start_month": 10,
        "reg_end_month": 4,
        "reg_cross_year": 1,
        "contest_start_date": "2026-04-11",
        "contest_end_date": "2026-04-25",
        "last_checked_at": "2026-11-17 04:10:00",
    }
    notes = []
    dropped = s.drop_locked_facts(patch, notes, "2026-11-16 17:42:52")

    assert set(dropped) == set(s.FACT_COLUMNS)
    assert patch == {"last_checked_at": "2026-11-17 04:10:00"}
    assert notes and "人工核对" in notes[0]


def test_unverified_row_is_writable():
    patch = {"reg_start_date": "2026-10-08", "last_checked_at": "x"}
    notes = []
    dropped = s.drop_locked_facts(patch, notes, None)

    assert dropped == []
    assert patch["reg_start_date"] == "2026-10-08"
    assert notes == []


def test_overwrite_flag_restores_old_behaviour():
    patch = {"reg_start_date": "2025-10-20"}
    notes = []
    dropped = s.drop_locked_facts(patch, notes, "2026-11-16 17:42:52", overwrite=True)

    assert dropped == []
    assert patch["reg_start_date"] == "2025-10-20"


def test_fact_columns_are_all_writable_columns():
    """护栏覆盖的列必须都在 WRITABLE 里，否则写了也落不了盘，护栏会形同虚设。"""
    assert set(s.FACT_COLUMNS) <= set(s.WRITABLE)