# -*- coding: utf-8 -*-
"""关键词检索「带分」接口：弱命中要能被前端识别出来。

背景：搜索页原来只在「0 命中」时才补 AI 语义检索。可是「大挑」当时并不是 0 命中——
它弱命中了小挑简介里的「与『大挑』交替举办」（0.35 分），于是错误结果被当成唯一答案。
现在接口把匹配分一起回传，前端据 best_score < 0.6 判定「弱命中」再补一次语义检索。
"""

from __future__ import annotations

import os
import tempfile
import unittest

import db

NAME_DA = "“挑战杯”全国大学生课外学术科技作品竞赛"
NAME_XIAO = "“挑战杯”中国大学生创业计划竞赛"


class ScoredSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)
        db.DB_PATH, self._orig = self.path, db.DB_PATH
        self.cid_xiao = db.add_competition({"name": NAME_XIAO, "category": "创新创业"})
        self.cid_da = db.add_competition({"name": NAME_DA, "category": "科技创新"})
        db.update_competition(self.cid_xiao, {
            "summary": "俗称“小挑”，与“大挑”交替举办，侧重商业模式与成果转化。",
        })
        db.update_competition(self.cid_da, {
            "summary": "被誉为大学生科技创新的“奥林匹克”盛会。",
        })

    def tearDown(self):
        db.DB_PATH = self._orig
        self.tmp.cleanup()

    def test_scored_returns_score_per_row(self):
        scored = db.search_competitions_scored("大挑")
        self.assertTrue(scored)
        score, row = scored[0]
        self.assertEqual(row["id"], self.cid_da)
        self.assertGreaterEqual(score, 0.9)  # 别称全等

    def test_summary_only_hit_is_weak(self):
        """只命中简介 → 分数低于 0.6，前端据此补语义检索。"""
        scored = db.search_competitions_scored("奥林匹克")
        self.assertTrue(scored)
        self.assertEqual(scored[0][1]["id"], self.cid_da)
        self.assertLess(scored[0][0], 0.6)

    def test_search_competitions_still_returns_rows(self):
        rows = db.search_competitions("大挑")
        self.assertEqual(rows[0]["id"], self.cid_da)

    def test_empty_query_is_empty_list(self):
        self.assertEqual(db.search_competitions_scored("   "), [])


class ScoredEndpointTest(unittest.TestCase):
    """路由层：分数要真的出现在响应里，best_score 取全批最高。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        db.init_db(self.path)
        db.DB_PATH, self._orig = self.path, db.DB_PATH
        db.add_competition({"name": NAME_XIAO, "category": "创新创业"})
        cid_da = db.add_competition({"name": NAME_DA, "category": "科技创新"})
        # 造一个「只出现在简介里」的词，用来验证 weak 判定
        db.update_competition(cid_da, {
            "summary": "被誉为大学生科技创新的“奥林匹克”盛会。",
        })

        try:
            from routers.competitions import search_competitions_scored
        except Exception as exc:  # noqa: BLE001 - 缺可选依赖时跳过
            raise unittest.SkipTest(f"routers.competitions 不可导入：{exc}")
        self._handler = search_competitions_scored

    def tearDown(self):
        db.DB_PATH = self._orig
        self.tmp.cleanup()

    def test_handler_returns_scores(self):
        resp = self._handler(q="大挑", limit=10)
        self.assertEqual(resp.q, "大挑")
        self.assertEqual(resp.total, len(resp.items))
        self.assertTrue(resp.items)
        self.assertGreaterEqual(resp.best_score, 0.9)
        self.assertFalse(resp.weak)  # 强命中不该触发 AI 语义兜底
        self.assertEqual(resp.weak_threshold, db.SEARCH_WEAK_SCORE)
        self.assertTrue(all(0.0 <= it.match_score <= 1.0 for it in resp.items))

    def test_handler_flags_weak_hit(self):
        """只命中简介 → weak=True，前端据此补 AI；阈值由后端一处给出。"""
        resp = self._handler(q="奥林匹克", limit=10)
        self.assertTrue(resp.items)
        self.assertLess(resp.best_score, resp.weak_threshold)
        self.assertTrue(resp.weak)

    def test_handler_best_score_on_empty(self):
        resp = self._handler(q="量子织布机", limit=10)
        self.assertEqual(resp.total, 0)
        self.assertEqual(resp.best_score, 0.0)
        self.assertFalse(resp.weak)  # 0 命中不算弱命中，走另一条分支


if __name__ == "__main__":
    unittest.main()
