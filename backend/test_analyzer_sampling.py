# -*- coding: utf-8 -*-
"""analyzer 跨赛道抽样 / Prompt 隔离 / idea 追加的单测（不调用大模型）。

覆盖 2026-11-14 重构的四个产品问题：
  1. 跨赛道抽样——不再只抽「先进制造」一个赛道就总结全局；
  2. 「共性分析」与「灵感工作台」Prompt 隔离——一个讲技术架构，一个讲商业选题；
  3. 不把全量 SQL 统计（条数/占比）塞进大模型上下文；
  4. 「换一批新灵感」把已看过的选题写进「必须避开」清单。
"""

import re

import analyzer


def _works(prefix: str, n: int, grade: str = "金奖") -> list:
    return [
        {
            "id": i,
            "title": f"{prefix}作品{i}",
            "track": prefix,
            "year": 2025,
            "award_grade": grade,
            "summary": f"{prefix}摘要{i}",
            "tech_stack": ["Python", "边缘计算"],
        }
        for i in range(n)
    ]


def test_cross_track_sample_covers_main_tracks() -> None:
    """4 个赛道：样本必须覆盖全部赛道，每个赛道 3-5 条，总量 20-30。"""
    works = []
    works += _works("先进制造", 40)
    works += _works("新能源", 30)
    works += _works("智慧农业", 20)
    works += _works("生物医疗", 10)

    sampled, meta = analyzer.sample_cross_track(works)

    assert 20 <= len(sampled) <= 30
    assert len(meta) == 4
    assert {m["track"] for m in meta} == {
        "先进制造", "新能源", "智慧农业", "生物医疗"
    }
    assert all(analyzer.PER_TRACK_MIN <= m["sampled"] <= analyzer.PER_TRACK_MAX
               for m in meta)
    # 样本里必须真的包含每个赛道的作品，而不是只有元数据
    sampled_tracks = {w["track"] for w in sampled}
    assert sampled_tracks == {"先进制造", "新能源", "智慧农业", "生物医疗"}


def test_sampling_is_order_independent() -> None:
    """抽样必须与输入顺序无关：否则 prompt 指纹每次都变，「终身缓存」就失效了。"""
    works = []
    works += _works("先进制造", 30)
    works += _works("新能源", 20)
    works += _works("智慧农业", 10)

    first, _ = analyzer.sample_cross_track(works)
    shuffled, _ = analyzer.sample_cross_track(list(reversed(works)))

    assert [w["id"] for w in first] == [w["id"] for w in shuffled]


def test_single_track_competition_falls_back_to_grade_sample() -> None:
    """单赛道（或个人赛无赛道）：退化为赛道内奖项分层，样本量仍受上限约束。"""
    sampled, meta = analyzer.sample_cross_track(_works("个人赛", 50, "一等奖"))
    assert len(meta) == 1
    assert analyzer.SAMPLE_TARGET_MIN <= len(sampled) <= analyzer.MAX_AWARDS


def test_context_has_no_full_sql_statistics() -> None:
    """任务 3：上下文只能有抽样，不能有全量条数/占比统计。"""
    comp = {"id": 1, "name": "测试赛", "category": "创业"}
    works = []
    for track in ("先进制造", "新能源", "智慧农业"):
        works += _works(track, 12)

    ctx, ids = analyzer._context_block(comp, works, 2024, 2026)

    assert "全量统计" not in ctx
    assert not re.search(r"\d+(?:\.\d+)?\s*%", ctx)  # 没有任何百分比
    assert len(ids) <= analyzer.MAX_AWARDS
    for track in ("先进制造", "新能源", "智慧农业"):
        assert track in ctx


def test_insight_prompt_isolated_from_common_analysis() -> None:
    """任务 2：灵感到洞察只谈商业/选题，明确禁止写技术栈与架构。"""
    prompt = analyzer._insight_prompt("CTX", "gap")
    assert "商业趋势" in prompt
    assert "蓝海" in prompt or "红海" in prompt
    assert "系统架构" in prompt            # 以「禁止写…」的形式出现
    assert "必须严格" in prompt or "严格区分" in prompt
    # 三类口径互不相同
    assert len({analyzer.KIND_TASK[k] for k in ("experience", "trend", "gap")}) == 3
    for kind in ("experience", "trend", "gap"):
        assert analyzer.KIND_LABEL[kind]


def test_ideas_prompt_lists_seen_titles_as_forbidden() -> None:
    """任务 3：生成更多时「已看过的主题」必须完整进 prompt，并明确要求避开。"""
    prompt = analyzer._ideas_prompt("CTX", 3, [1, 2], ["旧选题A", "旧选题B"])
    assert "必须全部避开" in prompt
    assert "旧选题A" in prompt and "旧选题B" in prompt
    # 已看清单 + 条数口径
    assert "用户已看过的 2 个主题" in prompt
    assert "再生成 3 条差异化灵感" in prompt
    assert "【本次生成 3 条" in prompt