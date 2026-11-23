# -*- coding: utf-8 -*-
"""competition_whitelist.gate 的入库闸门测试（纯函数）。

重点守护 2026-11-23 修的回归：白名单官方名自带「选拔赛」时不能被省级规则误杀。
"""

from __future__ import annotations

import unittest

import competition_whitelist as cw


class GateTest(unittest.TestCase):
    def test_official_name_with_xuanbasai_is_allowed(self):
        """#82 世界技能大赛中国选拔赛 —— 官方名带「选拔赛」，必须放行。"""
        ok, reason = cw.gate("世界技能大赛中国选拔赛")
        self.assertTrue(ok, reason)
        self.assertIn("#82", reason)

    def test_worldskills_body_still_matches_81(self):
        """#81 世界技能大赛本体不能被 #82 截走。"""
        ok, reason = cw.gate("世界技能大赛")
        self.assertTrue(ok, reason)
        self.assertIn("#81", reason)

    def test_provincial_division_still_rejected(self):
        """省内赛区：命中官方名但不是完全相等 -> 仍然拒绝。"""
        ok, reason = cw.gate("全国大学生数学建模竞赛江苏赛区")
        self.assertFalse(ok)
        self.assertIn("省级", reason)

    def test_provincial_word_still_rejected(self):
        ok, reason = cw.gate("湖南省大学生程序设计竞赛")
        self.assertFalse(ok)

    def test_special_golden_seed_allowed(self):
        ok, reason = cw.gate("湖南省“金种子杯”大学生创业大赛")
        self.assertTrue(ok, reason)
        self.assertIn("特批", reason)

    def test_unrelated_race_rejected(self):
        ok, reason = cw.gate("某培训机构杯全国大学生算法挑战赛")
        self.assertFalse(ok)
        self.assertIn("84", reason)

    def test_core_race_allowed(self):
        for name, idx in (("中国国际大学生创新大赛", "#1"),
                          ("“挑战杯”全国大学生课外学术科技作品竞赛", "#2"),
                          ("蓝桥杯全国软件和信息技术专业人才大赛", "#27"),
                          ("码蹄杯全国职业院校程序设计大赛", "#84")):
            ok, reason = cw.gate(name)
            self.assertTrue(ok, f"{name} -> {reason}")
            self.assertIn(idx, reason, f"{name} -> {reason}")


class RosterShapeTest(unittest.TestCase):
    def test_roster_has_84_entries(self):
        self.assertEqual(len(cw.WHITELIST), 84)

    def test_indexes_are_1_to_84(self):
        idx = sorted(i for i, _n, _u in cw.WHITELIST)
        self.assertEqual(idx, list(range(1, 85)))


if __name__ == "__main__":
    unittest.main()