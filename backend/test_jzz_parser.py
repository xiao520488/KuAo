# -*- coding: utf-8 -*-
"""金种子杯适配器 + 白名单特批的单元测试。

跑法：venv\\Scripts\\python.exe -m pytest test_jzz_parser.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import competition_whitelist as whitelist  # noqa: E402
from crawler_official_awards import (  # noqa: E402
    _jzz_clean,
    _jzz_header_map,
    _jzz_latin_split,
    _jzz_track_of,
)


def test_whitelist_special_gate():
    for name in (
        "湖南省“金种子杯”大学生创业大赛",
        "金种子杯",
        "湖南省金种子杯大学生创业投资大赛",
        "2025年“金种子杯”大学生创业大赛",
    ):
        allowed, reason = whitelist.gate(name)
        assert allowed, name
        assert reason.startswith("特批"), reason
    assert whitelist.is_special("第二届“金种子杯”大学生创业大赛")
    # 普通省赛仍然被挡
    assert whitelist.gate("蓝桥杯省赛")[0] is False


def test_clean_keeps_latin_boundary_space():
    # 中文断行不加空格，英文断行要补空格
    assert _jzz_clean("“球秀”—打造下一代AI体育产业\n平台") == "“球秀”—打造下一代AI体育产业平台"
    assert _jzz_clean("SiCHigh-TempIonImplantation\nEquipment") == "SiCHigh-TempIonImplantation Equipment"


def test_latin_split_restores_spaces():
    got = _jzz_latin_split("SiCHigh-TempIonImplantation Equipment R&D&Industrialization")
    assert got == "SiC High-Temp Ion Implantation Equipment R & D & Industrialization"
    got2 = _jzz_latin_split("MetaSample64G:Direct SamplingSoCInnovation")
    assert "MetaSample 64G: Direct Sampling SoC Innovation" == got2
    # 中文标题不动
    assert _jzz_latin_split("基于TripoAI的非遗3D资产内容服务商") == "基于TripoAI的非遗3D资产内容服务商"


def test_track_of():
    assert _jzz_track_of("第二届…人工智能与信息技术赛道获奖名单") == "人工智能与信息技术赛道"
    assert _jzz_track_of("AI 3D大模型应用专项赛") == "AI 3D大模型应用专项赛"
    assert _jzz_track_of("传统产业赛道 序号 参赛项目") == "传统产业赛道"
    assert _jzz_track_of("没有任何赛道字样的页", "未来产业赛道") == "未来产业赛道"


def test_header_map():
    header = ["序号", "参赛项目", "名次", "参赛队员", "指导老师", "所属学校"]
    mapping = _jzz_header_map(header)
    assert mapping["title"] == 1
    assert mapping["grade"] == 2
    assert mapping["members"] == 3
    assert mapping["advisor"] == 4
    assert mapping["school"] == 5
    assert _jzz_header_map(["1", "某项目", "金奖"]) is None