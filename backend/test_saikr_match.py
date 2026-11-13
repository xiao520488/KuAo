# -*- coding: utf-8 -*-
"""saikr_match 的届次偏好与歧义保护。"""

import unittest

import saikr_match as sm


def _index(*names):
    return {"items": [{"contest_id": 1000 + i, "name": name,
                       "norm": sm.normalize_name(name)}
                      for i, name in enumerate(names)]}


class EditionRankTest(unittest.TestCase):
    def test_chinese_numerals(self) -> None:
        self.assertEqual(sm._cn_to_int("十二"), 12)
        self.assertEqual(sm._cn_to_int("二十四"), 24)
        self.assertEqual(sm._cn_to_int("十九"), 19)
        self.assertEqual(sm._cn_to_int("11"), 11)
        self.assertEqual(sm._cn_to_int(""), 0)

    def test_rank_reads_year_and_edition(self) -> None:
        self.assertEqual(sm.edition_rank("2022年第十五届全国大学生结构设计竞赛"), (15, 2022))
        self.assertEqual(sm.edition_rank("第二十四届全国大学生机器人大赛"), (24, 0))
        self.assertEqual(sm.edition_rank("全国大学生物流设计大赛"), (999, 0))


class PreferNewestEditionTest(unittest.TestCase):
    def test_same_score_picks_newest_edition(self) -> None:
        index = _index(
            "第十届“大唐杯”全国大学生新一代信息通信技术大赛",
            "【榜单竞赛】第十二届“大唐杯”全国大学生新一代信息通信技术大赛",
            "第11届“大唐杯”全国大学生新一代信息通信技术大赛",
        )
        hit = sm.resolve("“大唐杯”全国大学生新一代信息通信技术大赛", index=index)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["contest_id"], 1001)
        self.assertEqual(hit["score"], 1.0)

    def test_newest_wins_even_when_index_order_is_old_first(self) -> None:
        index = _index(
            "第二十二届全国大学生机器人大赛",
            "第二十四届全国大学生机器人大赛",
        )
        hit = sm.resolve("全国大学生机器人大赛(CURC)", index=index)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["contest_id"], 1001)

    def test_lower_score_never_beats_exact(self) -> None:
        index = _index(
            "2022一带一路暨金砖国家技能发展与技术创新大赛",
            "2026年第十二届国际发明展览会·“一带一路”暨金砖国家技能发展与技术创新大赛",
        )
        hit = sm.resolve("一带一路暨金砖国家技能发展与技术创新大赛", index=index)
        self.assertEqual(hit["contest_id"], 1000)

    def test_close_fuzzy_scores_still_ambiguous(self) -> None:
        index = _index(
            "全国大学生先进成图技术与产品信息建模创新大赛",
            "2025年第十八届“高教杯”全国大学生先进成图技术与产品信息建模创新大赛",
            "第十九届“高教杯”全国大学生先进成图技术与产品信息建模创新大赛",
        )
        hit = sm.resolve("全国大学生先进成图技术与产品信息建模创新大赛", index=index)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["score"], 1.0)


if __name__ == "__main__":
    unittest.main()