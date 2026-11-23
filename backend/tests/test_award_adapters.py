# -*- coding: utf-8 -*-
"""adapter 分发的单测：五个 adapter 都能被 parse_by_adapter 路由到，未知名字要报错。

（双线边框 adapter 的行为已在真实 PDF 上验过 801 行，这里只锁「分发注册」，
避免以后加 adapter 忘了同步 db.AWARD_SOURCE_ADAPTERS 或 dispatch 分支。）
"""
from __future__ import annotations

import unittest
from unittest import mock

import award_ingest as ai
import db


class AdapterDispatchTest(unittest.TestCase):
    def test_db_constant_covers_all_adapters(self):
        for name in ("html_table", "xlsx_sheet", "docx_table", "pdf_table",
                     "pdf_dual_border"):
            self.assertIn(name, db.AWARD_SOURCE_ADAPTERS)

    def test_dispatch_routes_each_adapter(self):
        cases = {
            "html_table": "parse_html_table",
            "xlsx_sheet": "parse_xlsx",
            "docx_table": "parse_docx",
            "pdf_table": "parse_pdf",
            "pdf_dual_border": "parse_pdf_dual_border",
        }
        for adapter, fn_name in cases.items():
            with mock.patch.object(ai, fn_name, return_value=[{"title": adapter}]) as patched:
                rows = ai.parse_by_adapter(b"x", adapter, {})
            self.assertEqual(rows, [{"title": adapter}], adapter)
            self.assertTrue(patched.called, f"{adapter} 没有路由到 {fn_name}")

    def test_unknown_adapter_raises(self):
        with self.assertRaises(ValueError):
            ai.parse_by_adapter(b"x", "nope", {})


if __name__ == "__main__":
    unittest.main()
