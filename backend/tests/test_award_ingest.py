# -*- coding: utf-8 -*-
"""award_ingest 的解析/归一化单测（纯函数，不联网、不写库）。

覆盖 S1 踩过的四个坑：
  * 多列映射到同一语义字段（队长 / 队员1 / 队员2 → members）不能互相覆盖；
  * 合并单元格造成的「一队多行」必须按 group_by 合并，否则 1,479 队写成 6,446 条；
  * 「团队编号+队名」这类合并列要按首个空格拆开；
  * PDF 每页重复的表头不能被当成数据行入库。

运行：
    python -m pytest tests/test_award_ingest.py -q
"""

from __future__ import annotations

import io
import unittest

import award_ingest as A


class NormalizeRowsTest(unittest.TestCase):
    def test_multi_column_members_accumulate(self):
        raw = [{"赛题编号": "A1", "团队编号+队名": "08014276 Caspar",
                "学校": "河海大学", "队长": "刘经哲", "队员1": "曹智翔",
                "队员2": "王亭今", "指导教师": "秦少玲"}]
        spec = {
            "columns": {"赛题编号": "topic", "学校": "school", "队长": "members",
                        "队员1": "members", "队员2": "members",
                        "指导教师": "advisor"},
            "split_first_space": {"团队编号+队名": ["team_code", "title"]},
        }
        out = A.prepare_records(raw, spec)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Caspar")
        self.assertEqual(out[0]["members"], "刘经哲、曹智翔、王亭今")
        self.assertIn("队伍编号：08014276", out[0]["summary"])
        self.assertIn("赛题：A1", out[0]["summary"])

    def test_fill_down_then_group_by_collapses_team_rows(self):
        # 服创的真实形态：首行填团队字段，后续行只剩成员
        raw = [
            {"团队编号": "T1", "团队名称": "甲队", "学校": "A大学",
             "奖项等级": "一等奖", "成员姓名": "张三"},
            {"团队编号": "", "团队名称": "", "学校": "",
             "奖项等级": "", "成员姓名": "李四"},
            {"团队编号": "", "团队名称": "", "学校": "",
             "奖项等级": "", "成员姓名": "王五"},
        ]
        spec = {
            "columns": {"团队编号": "team_code", "团队名称": "title",
                        "学校": "school", "奖项等级": "award_grade",
                        "成员姓名": "members"},
            "fill_down": ["team_code", "title", "school", "award_grade"],
            "group_by": {"key": ["team_code"], "concat": ["members"]},
        }
        records = A.prepare_records(raw, spec)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "甲队")
        self.assertEqual(records[0]["members"], "张三、李四、王五")

    def test_header_row_is_dropped(self):
        raw = [{"标题": "作品名称", "单位": "学校名称"},
               {"标题": "真作品", "单位": "真学校"}]
        spec = {"columns": {"标题": "title", "单位": "school"}}
        out = A.prepare_records(raw, spec)
        self.assertEqual([r["title"] for r in out], ["真作品"])

    def test_title_template_when_no_title_column(self):
        raw = [{"学校": "中国人民大学", "成员": "王立天、徐儒康"}]
        spec = {"columns": {"学校": "school", "成员": "members"},
                "title_template": "{school}·{members}"}
        out = A.prepare_records(raw, spec)
        self.assertEqual(out[0]["title"], "中国人民大学·王立天、徐儒康")

    def test_members_separators_normalized(self):
        raw = [{"作品名称": "x", "作者": "刘春,"}]
        spec = {"columns": {"作品名称": "title", "作者": "members"}}
        out = A.prepare_records(raw, spec)
        self.assertEqual(out[0]["members"], "刘春")
        self.assertNotIn("成员：刘春,", out[0]["summary"] or "")

    def test_fixed_fields_and_year(self):
        raw = [{"作品名称": "x"}]
        spec = {"columns": {"作品名称": "title"},
                "fixed": {"year": 2026, "session": "第19届"}}
        out = A.prepare_records(raw, spec)
        self.assertEqual(out[0]["year"], 2026)
        self.assertEqual(out[0]["session"], "第19届")


class ContentHashTest(unittest.TestCase):
    def test_same_team_same_hash_ignores_spacing(self):
        a = {"title": "甲 队", "school": "河海大学", "award_grade": "一等奖",
             "year": 2026, "session": "第15届", "track": None}
        b = {"title": "甲队", "school": "河海 大学", "award_grade": "一等奖",
             "year": 2026, "session": "第15届", "track": None}
        self.assertEqual(A.content_hash(13, a), A.content_hash(13, b))

    def test_different_competition_differs(self):
        rec = {"title": "甲队", "school": "河海大学", "award_grade": "一等奖"}
        self.assertNotEqual(A.content_hash(13, rec), A.content_hash(17, rec))

    def test_track_participates(self):
        rec = {"title": "甲队", "school": "河海大学", "award_grade": "一等奖"}
        one = dict(rec, track="高教")
        two = dict(rec, track="红旅")
        self.assertNotEqual(A.content_hash(1, one), A.content_hash(1, two))


class DetectFormatTest(unittest.TestCase):
    def test_pdf_magic_wins_over_content_type(self):
        self.assertEqual(A.detect_format("https://x/a.bin", "image/jpeg", b"%PDF-1.7"),
                         "pdf")

    def test_xlsx_by_extension(self):
        self.assertEqual(A.detect_format("https://x/a.xlsx?x=1", "", b"PK\x03\x04"),
                         "xlsx")

    def test_html_by_body(self):
        self.assertEqual(A.detect_format("https://x/a", "text/html",
                                         b"<!DOCTYPE html><html>"), "html")


class ParseHtmlTableTest(unittest.TestCase):
    HTML = ("<html><body><table>"
            "<tr><th>作品名称</th><th>参赛学校</th><th>奖项</th></tr>"
            "<tr><td>甲</td><td>A 大学</td><td>一等奖</td></tr>"
            "</table></body></html>").encode("utf-8")

    def test_columns_and_rows(self):
        rows = A.parse_html_table(self.HTML, {"columns": {"作品名称": "title"}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["作品名称"], "甲")

    def test_picks_table_by_header_hits(self):
        html = ("<html><body><table><tr><td>无关</td></tr></table>"
                "<table><tr><th>作品名称</th><th>奖项</th></tr>"
                "<tr><td>乙</td><td>二等奖</td></tr></table></body></html>").encode()
        rows = A.parse_html_table(html, {"columns": {"作品名称": "title",
                                                     "奖项": "award_grade"}})
        self.assertEqual(rows[0]["作品名称"], "乙")


class ParseXlsxTest(unittest.TestCase):
    def test_reads_first_sheet(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        ws.append(["作品名称", "参赛学校", "奖项"])
        ws.append(["丙", "B 大学", "三等奖"])
        buf = io.BytesIO()
        wb.save(buf)
        rows = A.parse_xlsx(buf.getvalue(), {"columns": {"作品名称": "title"}})
        self.assertEqual(rows[0]["作品名称"], "丙")
        self.assertEqual(rows[0]["奖项"], "三等奖")


class ParsePdfLinesTest(unittest.TestCase):
    """pdf lines 模式：行正则 + 段标题（嵌入式那个 9 列被合并单元格冲乱的 PDF）。"""

    def test_regex_groups(self):
        spec = {"mode": "lines",
                "row_regex": r"^(?P<work_no>\d{4,6})\s+(?P<school>\S+)\s+(?P<title>.+)$",
                "section_regex": "^(?:全国总决赛)?(一等奖|二等奖|三等奖)$"}
        # 直接验证正则行为，不构造真 PDF（真实 PDF 已在 --dry-run 里验过）
        import re
        row_re = re.compile(spec["row_regex"])
        sec_re = re.compile(spec["section_regex"])
        m = row_re.search("10042 淮安大学 0854 雄起队")
        self.assertEqual(m.group("work_no"), "10042")
        self.assertEqual(m.group("school"), "淮安大学")
        self.assertEqual(m.group("title"), "0854 雄起队")
        self.assertEqual(sec_re.search("全国总决赛三等奖").group(1), "三等奖")
        self.assertIsNone(sec_re.search("10430 广东技术师范大学 智教先锋"))


if __name__ == "__main__":
    unittest.main()
