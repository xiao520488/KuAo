# -*- coding: utf-8 -*-
"""别称检索回归测试（「搜大挑出来的是小挑」）。

口径：
  * 白名单别称（ALIASES）按白名单序号编，必须按名称对齐到 competitions.id；
  * 别称命中优先于「别的赛事简介里提到这个词」——小挑简介写着「与『大挑』交替举办」，
    不能因此把「大挑」判给小挑；
  * 别称不能被意图词剥掉（「大广赛」里的「赛」是名字的一部分，不是虚词）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

import db
import search_alias

# 用白名单里的正式名，保证 alias_map 能按名称对齐
NAME_DA = "“挑战杯”全国大学生课外学术科技作品竞赛"
NAME_XIAO = "“挑战杯”中国大学生创业计划竞赛"
NAME_GUANG = "全国大学生广告艺术大赛"


class SearchAliasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)
        db.DB_PATH, self._orig = self.path, db.DB_PATH
        # 故意让 id 与白名单序号错开：小挑先建 → id=1，大挑第二 → id=2
        self.cid_xiao = db.add_competition({
            "name": NAME_XIAO,
            "category": "创新创业",
            "summary": "俗称“小挑”，与“大挑”交替举办，侧重商业模式与成果转化。",
        })
        self.cid_da = db.add_competition({
            "name": NAME_DA,
            "category": "科技创新",
            "summary": "大学生科技创新的奥林匹克盛会。",
        })
        self.cid_guang = db.add_competition({
            "name": NAME_GUANG,
            "category": "工业&创意设计",
            "summary": "广告创意与设计类赛事。",
        })
        # add_competition 不落 summary（那是 AI 生成的字段），这里按线上真实数据补上
        db.update_competition(self.cid_xiao, {
            "summary": "俗称“小挑”，与“大挑”交替举办，侧重商业模式与成果转化。",
        })

    def tearDown(self):
        db.DB_PATH = self._orig
        self.tmp.cleanup()

    # ---------------- 别称表 ----------------

    def test_alias_map_aligns_by_name_not_by_index(self):
        rows = db.list_competitions()
        amap = search_alias.alias_map(rows)
        # 大挑的 id 是 2，而白名单序号也是 2 纯属巧合；这里确认取的是库里的 id
        self.assertEqual(amap["大挑"], self.cid_da)
        self.assertEqual(amap["小挑"], self.cid_xiao)
        self.assertEqual(amap["大广赛"], self.cid_guang)

    def test_alias_score_exact_and_inside(self):
        rows = db.list_competitions()
        amap = search_alias.alias_map(rows)
        self.assertEqual(search_alias.alias_score(self.cid_da, "大挑", amap),
                         search_alias.ALIAS_EXACT)
        # 「挑战杯大挑」整句里含别称 → 次一档
        self.assertEqual(search_alias.alias_score(self.cid_da, "挑战杯大挑", amap),
                         search_alias.ALIAS_INSIDE)
        self.assertEqual(search_alias.alias_score(self.cid_da, "小挑", amap), 0.0)
        self.assertEqual(search_alias.alias_score(999, "大挑", amap), 0.0)

    def test_norm_strips_quotes_and_spaces(self):
        self.assertEqual(search_alias.norm("“挑战杯” 大挑"), "挑战杯大挑")

    # ---------------- 端到端排序 ----------------

    def test_da_tiao_ranks_da_before_xiao(self):
        rows = db.search_competitions("大挑")
        self.assertTrue(rows, "「大挑」不该搜不到")
        self.assertEqual(rows[0]["id"], self.cid_da)
        ids = [r["id"] for r in rows]
        self.assertIn(self.cid_xiao, ids)  # 简介提到也算弱命中，可以排在后面

    def test_xiao_tiao_ranks_xiao_first(self):
        rows = db.search_competitions("小挑")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["id"], self.cid_xiao)

    def test_nickname_survives_intent_word_stripping(self):
        """「大广赛」不能被单字虚词「赛」拆成「大广」而搜不到。"""
        rows = db.search_competitions("大广赛")
        self.assertTrue(rows, "「大广赛」被意图词剥成了「大广」")
        self.assertEqual(rows[0]["id"], self.cid_guang)

    def test_plain_sentence_still_falls_back_to_tokens(self):
        """带口语前缀的查询仍要能命中（别称保护不能把整句挡死）。"""
        rows = db.search_competitions("我想找大广赛")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["id"], self.cid_guang)


class KeywordFallbackTest(unittest.TestCase):
    """routers/ai 的关键词兜底也要认别称（与 /competitions/search 同一套口径）。"""

    def setUp(self):
        try:
            from routers.ai import _keyword_search  # noqa: WPS433
        except Exception as exc:  # noqa: BLE001 - 缺 chromadb 等可选依赖时跳过
            raise unittest.SkipTest(f"routers.ai 不可导入：{exc}")
        self._kw = _keyword_search

    def test_alias_beats_summary_mention(self):
        rows = [
            {"id": 11, "name": NAME_XIAO, "category": "创新创业",
             "summary": "俗称“小挑”，与“大挑”交替举办。"},
            {"id": 12, "name": NAME_DA, "category": "科技创新", "summary": ""},
        ]
        scores = self._kw("大挑", lambda **_: rows)
        self.assertIn(12, scores)
        self.assertGreater(scores[12], scores.get(11, 0.0))


if __name__ == "__main__":
    unittest.main()
