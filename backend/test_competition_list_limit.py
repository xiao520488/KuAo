# -*- coding: utf-8 -*-
"""GET /api/competitions 的 limit 契约。

背景：客户端一直可以传 `?limit=N`，但接口没有声明这个参数，FastAPI 静默忽略，
于是"限制条数"从来没生效过（`?limit=1` 仍返回全部 84 条）。
补上 limit 时有个坑：month 现在是 SQL 之后用 Python 过滤的，若把 LIMIT 直接写进
SQL，就会变成"先截断再过滤"，返回行数会少于 limit —— 正是本次要防住的回归。

运行::

    python -m pytest test_competition_list_limit.py -q
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient

import db
import main


class CompetitionListLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.mkdtemp(prefix="kuao-list-limit-test-")
        cls._orig_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(cls._tmpdir, "test.db")
        db.init_db()
        cls._client_cm = TestClient(main.app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._client_cm.__exit__(None, None, None)
        finally:
            db.DB_PATH = cls._orig_db_path
            shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self) -> None:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM competitions")

    def _seed(self, rows: list[tuple[str, int, int, int]]) -> None:
        """(name, reg_start_month, reg_end_month, cross_year)"""
        with db.get_conn() as conn:
            for name, sm, em, cross in rows:
                conn.execute(
                    "INSERT INTO competitions (name, category, level, is_active, "
                    "reg_start_month, reg_end_month, reg_cross_year) "
                    "VALUES (?, '测试', 'national', 1, ?, ?, ?)",
                    (name, sm, em, cross),
                )

    def test_limit_truncates_result(self) -> None:
        self._seed([(f"赛事{i}", 11, 11, 0) for i in range(5)])
        body = self.client.get("/api/competitions", params={"limit": 2}).json()
        self.assertEqual(len(body), 2, f"limit=2 应返回 2 条，实际 {len(body)} 条")

    def test_no_limit_returns_everything(self) -> None:
        self._seed([(f"赛事{i}", 11, 11, 0) for i in range(5)])
        self.assertEqual(len(self.client.get("/api/competitions").json()), 5)

    def test_limit_applies_after_month_filter(self) -> None:
        """月份过滤在 Python 侧，limit 必须在其之后生效，不能先截断再过滤。"""
        # 报名月不含 11 月的行排在前面（phase 3 沉底会排后面，这里靠排序自然分不开，
        # 关键点是：过滤后仍有 >=3 条，limit=3 就必须拿到 3 条）
        self._seed([
            ("一月赛", 1, 2, 0),
            ("二月赛", 2, 2, 0),
            ("十一月赛A", 11, 11, 0),
            ("十一月赛B", 11, 11, 0),
            ("十一月赛C", 11, 11, 0),
        ])
        body = self.client.get(
            "/api/competitions", params={"month": 11, "limit": 3}
        ).json()
        self.assertEqual(len(body), 3, f"month+limit 应返回 3 条，实际 {len(body)} 条")
        self.assertTrue(all("十一月" in row["name"] for row in body))

    def test_limit_out_of_range_is_rejected(self) -> None:
        for bad in (0, -1, 501):
            with self.subTest(limit=bad):
                resp = self.client.get("/api/competitions", params={"limit": bad})
                self.assertEqual(resp.status_code, 422, f"limit={bad} 应被拒绝")


if __name__ == "__main__":
    unittest.main()