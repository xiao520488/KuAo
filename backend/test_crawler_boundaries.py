# -*- coding: utf-8 -*-
"""爬虫上云边界测试（无人值守 cron 场景）。

目标：证明凌晨自动运行时，下面三类异常不会把整批任务打挂。

    一、网络层：requests 的 Timeout / ConnectionError
        - fetch_html() 必须把它包装成 CrawlError（带重试次数与原因）；
        - _run_batch() 逐条隔离，单条网络异常不阻断后续条目。

    二、大模型输出层：残缺 / 非标准 JSON
        - safe_json() 永不抛异常：能修就修，修不了回落 fallback；
        - 代码围栏、截断对象、截断数组都能被修复；
        - strict=True 时才允许抛 LLMError。

    三、页面层：完全没有任何正文的空网页
        - extract_page() 抛可预期的 CrawlError（批量循环能捕获），
          而不是 AttributeError / TypeError；
        - _clean_text() 对 None / 空白 / 零宽字符返回空串；
        - 空网页出现在批量中时，循环继续处理下一条。

约束：全部离线，不连网、不写真实数据库、不调用大模型。
运行：
    .\\venv\\Scripts\\python.exe -m pytest test_crawler_boundaries.py -q
"""

import json
import types

import pytest
import requests

import crawler_news as news
import llm


# ---------------------------------------------------------------------------
# 公共夹具：最小可解析的「有正文」页面 + 批量参数
# ---------------------------------------------------------------------------
LIST_URL = "https://example.edu/news/list.htm"
PAGE_URL_A = "https://example.edu/news/a.htm"
PAGE_URL_B = "https://example.edu/news/b.htm"
PAGE_URL_C = "https://example.edu/news/c.htm"

# 正文需 >= MIN_BODY_CHARS(200)，否则 extract_page 会按空页处理
VALID_HTML = (
    "<html><head><title>我校学子在计算机设计大赛中获佳绩</title></head>"
    "<body><div id='vsb_content'><p>"
    + ("我校学子荣获全国一等奖，指导教师获优秀指导教师奖。" * 12)
    + "</p></div></body></html>"
)
EMPTY_HTML = "<html><head><title>空白页</title></head><body></body></html>"

# 满足 _run_batch 字段完整性检查的抽取结果
EXTRACTED = {
    "is_award_news": True,
    "title": "某作品",
    "competition": "中国大学生计算机设计大赛",
    "year": 2026,
    "award_level": "国家级一等奖",
    "team": "某校某队",
    "summary": "获奖简介",
    "source_url": PAGE_URL_C,
    "confidence": 0.9,
}


def _make_args(tmp_path):
    """构造 _run_batch 需要的最小 args。"""
    return types.SimpleNamespace(
        list_url=LIST_URL,
        db_path=str(tmp_path / "test_crawl.db"),
        allow_production=False,
        dry_run=True,
        max_pages=1,
        max_items=10,
        no_title_filter=True,
        pages=None,
        page_url=None,
        max_chars=6000,
        report=str(tmp_path / "batch_report.json"),
    )


def _patch_batch_deps(monkeypatch, tmp_path, entries):
    """把批量链路的网络 / 模型 / 写库依赖全部替换为离线桩。"""
    monkeypatch.setattr(news, "extract_detail_entries", lambda html, url: entries)
    monkeypatch.setattr(news, "find_next_page_url", lambda html, url: None)
    monkeypatch.setattr(news, "_source_url_exists", lambda url: None)
    monkeypatch.setattr(news, "_polite_sleep", lambda *a, **kw: None)
    monkeypatch.setattr(news, "_looks_like_award_page", lambda page: True)
    monkeypatch.setattr(news, "extract_award", lambda page, max_chars: ({}, dict(EXTRACTED)))
    monkeypatch.setattr(news, "build_award_row", lambda extracted, page, competition_id: {})
    monkeypatch.setattr(news, "ensure_competition", lambda *a, **kw: (1, "existing"))
    # 让生产库保护判断使用临时路径（本测试根本不写库）
    monkeypatch.setattr(news.db, "DB_PATH", str(tmp_path / "test_crawl.db"))


def _read_report(tmp_path):
    return json.loads((tmp_path / "batch_report.json").read_text(encoding="utf-8"))


# ===========================================================================
# 一、网络异常：Timeout / ConnectionError
# ===========================================================================

@pytest.mark.parametrize(
    "exc",
    [
        requests.Timeout("read timed out"),
        requests.ConnectionError("connection reset by peer"),
    ],
    ids=["timeout", "connection-error"],
)
def test_fetch_html_wraps_network_error_as_crawl_error(monkeypatch, exc):
    """fetch_html 必须把底层网络异常包装成 CrawlError，并保留尝试次数。"""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        raise exc

    monkeypatch.setattr(news.requests, "get", fake_get)
    monkeypatch.setattr(news.time, "sleep", lambda *_: None)  # 不真等重试间隔

    with pytest.raises(news.CrawlError) as excinfo:
        news.fetch_html("https://example.edu/news/1.htm", retries=2)

    assert len(calls) == 2, "应按 retries 重试，而不是一次就放弃"
    message = str(excinfo.value)
    assert "抓取失败" in message
    assert str(exc) in message, "错误信息应保留底层异常原因，便于 cron 日志排障"


def test_batch_loop_survives_timeout_and_connection_error(monkeypatch, tmp_path):
    """两条网络异常 + 一条正常页：批量不能中断，正常条目要继续处理。"""
    entries = [
        {"url": PAGE_URL_A, "title": "获奖新闻 A"},
        {"url": PAGE_URL_B, "title": "获奖新闻 B"},
        {"url": PAGE_URL_C, "title": "获奖新闻 C"},
    ]

    def fake_fetch(url, *args, **kwargs):
        if url == PAGE_URL_A:
            raise requests.Timeout("read timed out")
        if url == PAGE_URL_B:
            raise requests.ConnectionError("connection reset by peer")
        return VALID_HTML

    monkeypatch.setattr(news, "fetch_html", fake_fetch)
    _patch_batch_deps(monkeypatch, tmp_path, entries)

    rc = news._run_batch(_make_args(tmp_path))
    stats = _read_report(tmp_path)

    assert rc == 0, "批量有单条失败但整体仍应返回 0（cron 不应被单条网络故障判死）"
    assert stats["pages_fetched"] == 1
    assert stats["attempted"] == 3, "三条候选都必须被尝试"
    assert stats["failed"] == 2
    assert len(stats["failures"]) == 2
    assert {item["url"] for item in stats["failures"]} == {PAGE_URL_A, PAGE_URL_B}
    # 未预期的 requests 异常也会被逐条兜住（stage=unexpected 只是归类，不影响继续）
    assert all(item["stage"] in ("crawl/llm", "unexpected") for item in stats["failures"])


# ===========================================================================
# 二、大模型残缺 / 非标准 JSON
# ===========================================================================

def test_safe_json_truncated_json_falls_back_without_raising():
    """截断到「键名悬空」的 JSON 无法修复时，返回 fallback 而不是抛异常。"""
    sentinel = object()
    broken = '{"name": "中国大学生计算机设计大赛", "year": 2026, "awards": '

    result = llm.safe_json(broken, fallback=sentinel, log=False)

    assert result is sentinel


def test_safe_json_repairs_truncated_and_nonstandard_json():
    """能修的残缺 JSON（截断对象 / 截断数组 / 代码围栏）应被修复为容器。"""
    truncated_obj = '{"name": "中国大学生计算机设计大赛", "year": 2026'
    truncated_array = '[{"a": 1}, {"b": 2}'
    fenced = '```json\n{"name": "蓝桥杯"}\n```'

    obj = llm.safe_json(truncated_obj, fallback=None, log=False)
    arr = llm.safe_json(truncated_array, fallback=None, log=False)
    fenced_obj = llm.safe_json(fenced, fallback=None, log=False)

    assert isinstance(obj, dict) and obj["name"] and obj["year"] == 2026
    assert isinstance(arr, list) and len(arr) == 2
    assert fenced_obj == {"name": "蓝桥杯"}


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "   ",
        "抱歉，我无法完成这个请求。",
        "这是解释文字，没有 JSON。",
        None,
        123,
        {"already": "parsed"},
        [{"already": "parsed"}],
    ],
    ids=["empty", "blank", "refusal", "prose", "none", "int", "dict", "list"],
)
def test_safe_json_never_raises_for_garbage(payload):
    """无论模型吐什么，safe_json 默认模式都不能抛异常（cron 无人值守）。"""
    result = llm.safe_json(payload, fallback={}, log=False)
    assert result is not None, "默认模式必须返回 fallback 或修复结果，不能返回 None 以外的异常态"


def test_safe_json_strict_mode_raises_llm_error():
    """strict=True 是给需要强一致调用方预留的出口，必须抛 LLMError。"""
    with pytest.raises(llm.LLMError):
        llm.safe_json('{"a": 1, "b": ', fallback=None, strict=True, log=False)


# ===========================================================================
# 三、完全无正文的空网页
# ===========================================================================

@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html></html>",
        "<html><head><title>空白</title></head><body> \n\t </body></html>",
        "<html><body><div id='vsb_content'></div></body></html>",
    ],
    ids=["empty-string", "bare-html", "whitespace-body", "empty-container"],
)
def test_extract_page_empty_html_raises_crawl_error_not_crash(html):
    """空网页必须是可预期的 CrawlError（批量循环能捕获），不能是 AttributeError。"""
    with pytest.raises(news.CrawlError) as excinfo:
        news.extract_page(html, PAGE_URL_A)

    assert "正文抽取失败" in str(excinfo.value)
    assert not isinstance(excinfo.value, (AttributeError, TypeError))


def test_clean_text_handles_none_and_blank():
    """清洗函数的输入边界：None / 空白 / 零宽字符都返回空串。"""
    assert news._clean_text(None) == ""
    assert news._clean_text("") == ""
    assert news._clean_text(" \n\t\r ") == ""
    assert news._clean_text("\u200b\u200c\u200d\ufeff") == ""


def test_batch_loop_survives_empty_page(monkeypatch, tmp_path):
    """空网页出现在候选里时，批量循环记录失败后继续处理下一条。"""
    entries = [
        {"url": PAGE_URL_A, "title": "空白页"},
        {"url": PAGE_URL_B, "title": "正常获奖新闻"},
    ]

    def fake_fetch(url, *args, **kwargs):
        return EMPTY_HTML if url == PAGE_URL_A else VALID_HTML

    monkeypatch.setattr(news, "fetch_html", fake_fetch)
    _patch_batch_deps(monkeypatch, tmp_path, entries)

    rc = news._run_batch(_make_args(tmp_path))
    stats = _read_report(tmp_path)

    assert rc == 0, "空网页只是单条失败，整体批量应正常收尾"
    assert stats["attempted"] == 2
    assert stats["failed"] == 1
    assert stats["failures"][0]["url"] == PAGE_URL_A
    assert stats["failures"][0]["stage"] == "crawl/llm"
    assert "正文抽取失败" in stats["failures"][0]["error"]
