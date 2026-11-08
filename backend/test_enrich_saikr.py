# -*- coding: utf-8 -*-
"""赛氪详情补充器 / 审核自动补全的离线测试。

覆盖：
  * AI 输出规范化：「1. 短句」编号、单条 <=50 字、条数上限、去重；
  * 防照抄闸门：连续 24 字与原文重合 -> 丢弃该字段（保留 NULL）；
  * 一次 LLM 调用提纯多个字段；LLM 失败必须抛出，不能回落写原文；
  * --backfill / --daily-update 的选片口径（最近 7 天 created_at）；
  * enrich_competition 只写空字段，不覆盖已有值；
  * review_candidates --approve 后自动抓详情 + AI 提纯并写库。

全部离线：不连网、不写真实库、不调用真实大模型。
运行：
    .\\venv\\Scripts\\python.exe -m pytest test_enrich_saikr.py -q
"""

import json
import sqlite3
from datetime import datetime

import pytest

import db
import enrich_saikr_details as enrich
import review_candidates


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    return path


def _row(comp_id, name, created_at, **fields):
    row = {
        "id": comp_id,
        "name": name,
        "created_at": created_at,
        "participants": None,
        "content_scope": None,
        "exam_schedule": None,
    }
    row.update(fields)
    return row


# ---------------------------------------------------------------------------
# 一、AI 输出规范化
# ---------------------------------------------------------------------------

def test_normalize_purified_numbers_and_caps_items():
    raw = (
        "1. 全日制在校本科生及研究生，专业不限，年龄不超过二十五周岁；"
        "2. 以团队形式参赛，每队 2-5 人，可跨校组队，指导教师最多两人。"
    )
    text = enrich._normalize_purified(raw)
    assert text == (
        "1. 全日制在校本科生及研究生，专业不限，年龄不超过二十五周岁；"
        "2. 以团队形式参赛，每队 2-5 人，可跨校组队，指导教师最多两人"
    )
    for part in text.split("；"):
        body = part.split(". ", 1)[1]
        assert len(body) <= enrich.AI_ITEM_MAX_CHARS


def test_normalize_purified_caps_long_item_and_dedupes():
    text = enrich._normalize_purified(
        "参赛对象：" + "全日制普通高校在校学生" * 10
    )
    parts = text.split("；")
    assert len(parts) == 1
    assert parts[0].startswith("1. ")
    assert parts[0].endswith("…")
    assert len(parts[0].split(". ", 1)[1]) <= enrich.AI_ITEM_MAX_CHARS


def test_looks_copied_detects_verbatim_and_allows_paraphrase():
    raw = "参赛对象为普通高等学校全日制在校本科生、研究生，专业不限，年龄一般不超过二十五周岁。"
    copied = raw[:42]
    assert enrich._looks_copied(copied, raw)
    assert not enrich._looks_copied("在校本科生和研究生都可报名", raw)


# ---------------------------------------------------------------------------
# 二、AI 提纯
# ---------------------------------------------------------------------------

def test_purify_fields_calls_llm_once_for_all_fields(monkeypatch):
    calls = []

    def fake_chat_json(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return {
            "participants": ["全日制在校生", "团队 2-5 人"],
            "content_scope": "1. 算法设计；2. 软件开发",
            "exam_schedule": ["1. 报名截止 5 月 1 日", "2. 决赛 7 月举行"],
        }

    monkeypatch.setattr(enrich.llm, "chat_json", fake_chat_json)
    raw = {
        "participants": {"text": "参赛对象为普通高等学校全日制在校本科生、研究生。",
                         "source": "章节：参赛对象"},
        "content_scope": {"text": "赛项分为算法设计、软件开发、人工智能三个赛道。",
                          "source": "章节：竞赛内容"},
        "exam_schedule": {"text": "报名截止 5 月 1 日，决赛于 7 月举行。",
                          "source": "章节：赛程安排"},
    }
    result = enrich.purify_fields("测试赛", raw)

    assert len(calls) == 1  # 三字段共用一次调用
    assert result["participants"]["text"] == "1. 全日制在校生；2. 团队 2-5 人"
    assert result["content_scope"]["text"] == "1. 算法设计；2. 软件开发"
    assert result["exam_schedule"]["text"] == "1. 报名截止 5 月 1 日；2. 决赛 7 月举行"
    assert all("AI提纯" in value["source"] for value in result.values())


def test_purify_fields_drops_copied_field(monkeypatch):
    raw_text = (
        "参赛对象为普通高等学校全日制在校本科生、研究生，专业不限，"
        "年龄一般不超过二十五周岁。"
    )
    monkeypatch.setattr(
        enrich.llm, "chat_json",
        lambda *a, **k: {"participants": raw_text[:45]},
    )
    result = enrich.purify_fields(
        "测试赛", {"participants": {"text": raw_text, "source": "章节"}}
    )
    assert result == {}  # 照抄 -> 丢弃，绝不回落写原文


def test_purify_fields_raises_on_llm_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise enrich.llm.LLMError("upstream timeout")

    monkeypatch.setattr(enrich.llm, "chat_json", boom)
    with pytest.raises(enrich.llm.LLMError):
        enrich.purify_fields(
            "测试赛", {"participants": {"text": "全日制在校本科生、研究生均可报名参加本次竞赛。",
                                         "source": "章节"}}
        )


# ---------------------------------------------------------------------------
# 三、模式选片
# ---------------------------------------------------------------------------

def _mapping():
    return {
        1: {"competition_id": 1, "saikr_contest_id": 111, "match_type": "exact"},
        2: {"competition_id": 2, "saikr_contest_id": 222, "match_type": "exact"},
    }


def test_select_targets_backfill_vs_daily_window():
    now = datetime(2026, 11, 8, 12, 0, tzinfo=enrich.SHANGHAI)
    rows = {
        1: _row(1, "最近入库的空赛事", "2026-11-05 10:00:00"),
        2: _row(2, "很久以前入库的空赛事", "2026-09-01 10:00:00"),
        3: _row(3, "已补全赛事", "2026-11-07 10:00:00",
                participants="已有", content_scope="已有", exam_schedule="已有"),
    }

    daily = enrich.select_targets(rows, _mapping(), mode="daily", now=now)
    assert [t["competition_id"] for t in daily["targets"]] == [1]
    assert daily["outside_window"] == 1
    assert daily["complete"] == 1

    backfill = enrich.select_targets(rows, _mapping(), mode="backfill", now=now)
    assert [t["competition_id"] for t in backfill["targets"]] == [1, 2]
    assert backfill["outside_window"] == 0


def test_select_targets_reports_no_mapping():
    now = datetime(2026, 11, 8, 12, 0, tzinfo=enrich.SHANGHAI)
    rows = {9: _row(9, "完全陌生的赛事名称XYZ", "2026-11-08 09:00:00")}
    selection = enrich.select_targets(rows, _mapping(), mode="daily", now=now)
    assert selection["targets"] == []
    assert selection["no_mapping"][0][0] == 9


# ---------------------------------------------------------------------------
# 四、单条补全只写空字段
# ---------------------------------------------------------------------------

def test_enrich_competition_only_fills_empty(monkeypatch, tmpdb):
    comp_id = db.add_competition({
        "name": "只补空字段测试赛", "category": "程序设计",
        "official_url": "https://example.edu.cn",
    })
    with sqlite3.connect(tmpdb) as conn:
        conn.execute(
            "UPDATE competitions SET participants = '库里已有' WHERE id = ?",
            (comp_id,),
        )

    monkeypatch.setattr(enrich, "fetch_detail", lambda session, cid: {"contest_name": "x"})
    monkeypatch.setattr(enrich, "extract_fields", lambda detail: {
        "participants": {"text": "全日制在校本科生均可报名。", "source": "章节"},
        "content_scope": {"text": "赛项分为算法与软件两个赛道。", "source": "章节"},
        "exam_schedule": {"text": "报名截止 5 月，决赛 7 月。", "source": "章节"},
    })
    monkeypatch.setattr(enrich, "purify_fields", lambda name, needed, **kw: {
        "content_scope": {"text": "1. 算法设计；2. 软件开发", "source": "AI提纯"},
        "exam_schedule": {"text": "1. 5 月报名截止；2. 7 月决赛", "source": "AI提纯"},
    })

    result = enrich.enrich_competition(tmpdb, comp_id, contest_id=123)
    assert result["status"] == "updated"
    assert result["written"] == ["content_scope", "exam_schedule"]

    with sqlite3.connect(tmpdb) as conn:
        row = conn.execute(
            "SELECT participants, content_scope, exam_schedule "
            "FROM competitions WHERE id = ?", (comp_id,)
        ).fetchone()
    assert row[0] == "库里已有"
    assert row[1] == "1. 算法设计；2. 软件开发"
    assert row[2] == "1. 5 月报名截止；2. 7 月决赛"


# ---------------------------------------------------------------------------
# 五、CLI 每日模式 + 审核 approve 自动补全
# ---------------------------------------------------------------------------

def _fake_detail_pipeline(monkeypatch):
    monkeypatch.setattr(enrich, "fetch_detail", lambda session, cid: {"contest_name": "x"})
    monkeypatch.setattr(enrich, "extract_fields", lambda detail: {
        "participants": {"text": "全日制在校本科生均可报名。", "source": "章节"},
        "content_scope": {"text": "赛项分为算法与软件两个赛道。", "source": "章节"},
        "exam_schedule": {"text": "报名截止 5 月，决赛 7 月。", "source": "章节"},
    })
    monkeypatch.setattr(enrich, "purify_fields", lambda name, needed, **kw: {
        "participants": {"text": "1. 全日制在校生；2. 团队 2-5 人", "source": "AI提纯"},
        "content_scope": {"text": "1. 算法设计；2. 软件开发", "source": "AI提纯"},
        "exam_schedule": {"text": "1. 5 月报名截止；2. 7 月决赛", "source": "AI提纯"},
    })


def test_cli_daily_update_only_touches_recent(monkeypatch, tmpdb, tmp_path):
    old_id = db.add_competition({"name": "老赛事", "category": "程序设计"})
    new_id = db.add_competition({"name": "新赛事", "category": "程序设计"})
    with sqlite3.connect(tmpdb) as conn:
        conn.execute("UPDATE competitions SET created_at = '2026-09-01 10:00:00' "
                     "WHERE id = ?", (old_id,))

    monkeypatch.setattr(enrich, "load_map", lambda: {
        old_id: {"competition_id": old_id, "saikr_contest_id": 111, "match_type": "exact"},
        new_id: {"competition_id": new_id, "saikr_contest_id": 222, "match_type": "exact"},
    })
    _fake_detail_pipeline(monkeypatch)

    rc = enrich.main([
        "--daily-update", "--apply", "--db-path", tmpdb,
        "--preview", str(tmp_path / "preview.json"),
    ])
    assert rc == 0

    with sqlite3.connect(tmpdb) as conn:
        old_row = conn.execute(
            "SELECT participants FROM competitions WHERE id = ?", (old_id,)
        ).fetchone()
        new_row = conn.execute(
            "SELECT participants, content_scope, exam_schedule "
            "FROM competitions WHERE id = ?", (new_id,)
        ).fetchone()
    assert old_row[0] is None
    assert new_row == ("1. 全日制在校生；2. 团队 2-5 人",
                       "1. 算法设计；2. 软件开发",
                       "1. 5 月报名截止；2. 7 月决赛")

    payload = json.loads((tmp_path / "preview.json").read_text(encoding="utf-8"))
    assert payload["mode"] == "daily"
    assert payload["stats"]["updated_competitions"] == 1


def test_review_approve_auto_enriches(monkeypatch, tmpdb):
    inserted = db.add_candidate({
        "source": "saikr",
        "source_key": "saikr:777",
        "raw_name": "全国大学生测试赛事",
        "norm_name": db.normalize_competition_name("全国大学生测试赛事"),
        "level_label": "国家级",
        "category_hint": "程序设计",
        "raw_json": json.dumps({"contest_id": 777}, ensure_ascii=False),
    })
    assert inserted["status"] == "inserted"

    _fake_detail_pipeline(monkeypatch)
    rc = review_candidates.main([
        "--approve", str(inserted["id"]), "--db-path", tmpdb,
    ])
    assert rc == 0

    with sqlite3.connect(tmpdb) as conn:
        comp = conn.execute(
            "SELECT id, participants, content_scope, exam_schedule "
            "FROM competitions WHERE name = '全国大学生测试赛事'"
        ).fetchone()
        candidate = conn.execute(
            "SELECT status, competition_id FROM competition_candidates WHERE id = ?",
            (inserted["id"],),
        ).fetchone()
    assert comp is not None
    assert comp[1] == "1. 全日制在校生；2. 团队 2-5 人"
    assert comp[2] == "1. 算法设计；2. 软件开发"
    assert comp[3] == "1. 5 月报名截止；2. 7 月决赛"
    assert candidate == ("approved", comp[0])
