# -*- coding: utf-8 -*-
"""大创（cy.ncss.cn）获奖名单解析单测：不连网、不写库。

覆盖：
  1. _ncss_iter_rows：分节标题 / 等级行 / 表头行 / 数据行的状态机；
  2. docx 合并单元格造成的相邻重复列（列位不位移）；
  3. ncss_track_name / ncss_article_year 的年份与赛道归一化；
  4. ncss_attachments：/picture?path=...&name=... 形式的附件链接与扩展名；
  5. build_ncss_awards：字段映射、advisor、content_hash 去重。

运行：.\\venv\\Scripts\\python.exe -m pytest test_ncss_parser.py -q
"""

from crawler_official_awards import (
    _ncss_iter_rows,
    build_ncss_awards,
    ncss_article_year,
    ncss_attachments,
    ncss_track_name,
)

HEADER_XLSX = ["序号", "参赛项目", "省份", "学校", "负责人", "参赛队员", "指导教师"]

ROWS_XLSX = [
    ["附件1：高教主赛道拟获奖项目名单", "", "", "", "", "", ""],
    ["一、中国大陆项目", "", "", "", "", "", ""],
    ["金奖", "", "", "", "", "", ""],
    HEADER_XLSX,
    [1, "基于类脑架构的下一代通用模型及智能体生态", "北京市", "清华大学", "王冠", "CHEN WILLIAM、SIY ANDY L", ""],
    [2, "光子芯力-全波光电融合计算芯片", "北京市", "清华大学", "杨其晟", "吴晓萱、林国昌", "郝秀清、陆蕾"],
    ["银奖", "", "", "", "", "", ""],
    HEADER_XLSX,
    [1, "凌波净轨——空间动态避障的引领者", "北京市", "北京航空航天大学", "白筱瑀", "陈宇轩、黄海山", "陈培、尹继豪"],
    ["二、国际参赛项目", "", "", "", "", "", ""],
]

# docx 里合并单元格会让「参赛队员」相邻重复两列
ROWS_DOCX = [
    ["冠军"] * 8,
    ["序号", "参赛项目", "省（区、市）", "学校", "负责人", "参赛队员", "参赛队员", "指导教师"],
    ["D1", "哪吒——全球首个“海空一体”跨域航行器平台", "上海市", "上海交通大学", "吕晨昕", "金羽飞、毕鸢博", "金羽飞、毕鸢博", "曾铮、连琏"],
    ["亚军"] * 8,
    ["序号", "参赛项目", "省（区、市）", "学校", "负责人", "参赛队员", "参赛队员", "指导教师"],
]


def test_xlsx_state_machine_assigns_grades() -> None:
    rows = list(_ncss_iter_rows(iter(ROWS_XLSX), "高教主赛道"))
    assert [r["grade"] for r in rows] == ["金奖", "金奖", "银奖"]
    assert rows[0]["title"] == "基于类脑架构的下一代通用模型及智能体生态"
    assert rows[0]["school"] == "清华大学"
    assert rows[0]["leader"] == "王冠"
    # 英文姓名里的空格不能被吞掉
    assert rows[0]["members"] == "CHEN WILLIAM、SIY ANDY L"
    assert rows[2]["title"].startswith("凌波净轨")


def test_docx_merged_cells_keep_columns_aligned() -> None:
    rows = list(_ncss_iter_rows(iter(ROWS_DOCX), "高教主赛道"))
    assert len(rows) == 1
    row = rows[0]
    assert row["grade"] == "冠军"
    assert row["school"] == "上海交通大学"
    assert row["advisors"] == "曾铮、连琏"
    assert row["province"] == "上海市"


def test_header_only_row_is_not_emitted_as_award() -> None:
    rows = list(_ncss_iter_rows(iter(ROWS_XLSX), "高教主赛道"))
    assert all(r["title"] != "参赛项目" for r in rows)


def test_track_name_and_year() -> None:
    assert ncss_track_name("附件2-“青年红色筑梦之旅”赛道拟获奖项目名单") == "青年红色筑梦之旅赛道"
    assert ncss_track_name("附件4-产业命题赛道拟获奖项目名单") == "产业赛道"
    assert ncss_track_name("附件5- 萌芽赛道拟获奖项目名单") == "萌芽赛道"
    assert ncss_track_name("未知附件") == ""

    assert ncss_article_year("教育部关于公布中国国际大学生创新大赛（2025）获奖名单的通知", "2026-02-28") == 2025
    assert ncss_article_year("关于公示第八届中国国际“互联网+”大学生创新创业大赛总决赛有关赛事获奖名单的通知", "2023-02-20") == 2022
    assert ncss_article_year("无年份标题", "2021-09-25") == 2021


def test_attachments_from_picture_proxy() -> None:
    html = (
        '<div><a href="/picture?path=yz78p962rc4zsefz&amp;name=%E9%99%84%E4%BB%B61-5%E5%A4%A7%E8%B5%9B%E6%8B%9F%E8%8E%B7%E5%A5%96%E9%A1%B9%E7%9B%AE%E5%90%8D%E5%8D%95%EF%BC%882025%EF%BC%89.xlsx">'
        "附件1-5大赛拟获奖项目名单（2025）.xlsx</a></div>"
    )
    found = ncss_attachments(html, "https://cy.ncss.cn/information/abc")
    assert len(found) == 1
    assert found[0]["ext"] == ".xlsx"
    assert "2025" in found[0]["name"]
    assert found[0]["url"].startswith("https://cy.ncss.cn/picture?path=")


def test_build_awards_maps_fields_and_dedupes_hash() -> None:
    rows = list(_ncss_iter_rows(iter(ROWS_XLSX), "高教主赛道"))
    competition = {"id": 1, "name": "中国国际大学生创新大赛", "category": "创业"}
    awards = build_ncss_awards(
        rows,
        competition=competition,
        year=2025,
        session_label="第11届",
        source_url="https://cy.ncss.cn/information/x",
    )
    assert len({a["content_hash"] for a in awards}) == len(awards)
    first = awards[0]
    assert first["title"] == "基于类脑架构的下一代通用模型及智能体生态"
    assert first["team"] == "清华大学"
    assert first["award_grade"] == "金奖"
    assert first["track"] == "高教主赛道"
    assert first["session"] == "第11届"
    assert first["source_type"] == "crawl"
    assert first["advisor"] is None  # 该行没有指导教师
    assert "负责人：王冠" in first["summary"]
    assert "共 2 人" in first["summary"]
    assert awards[1]["advisor"] == "郝秀清、陆蕾"