# -*- coding: utf-8 -*-
"""crawler 核心纯函数单测（不连网、不写库、不调大模型）。

覆盖：
    1. crawler_competitions.extract_image_url：og:image 优先、相对协议绝对化、
       正文首图回退、logo/图标/占位图过滤；
    2. crawler_competitions._normalize_level：级别归一化（省/国/赛区冲突）。

运行：
    .\\venv\\Scripts\\python.exe -m pytest test_crawler_units.py -q
"""

import pytest

import crawler_competitions as cc

PAGE_URL = "https://cise.example.edu/info/1037/6028.htm"


def _wrap(body: str, head: str = "") -> str:
    """拼一个最小可解析的 HTML 页面。"""
    return (
        "<html><head><title>测试赛事通知页</title>"
        f"{head}</head><body>{body}</body></html>"
    )


# 正文容器 #vsb_content：长度需 >= MIN_BODY_CHARS(200)，否则容器选择会走兜底逻辑
LONG_TEXT = "关于开展第十六届中国大学生服务外包创新创业大赛的通知。" * 8


# ===========================================================================
# 一、extract_image_url
# ===========================================================================

def test_og_image_has_priority():
    """<head> 的 og:image 优先于正文配图。"""
    html = _wrap(
        f'<div id="vsb_content"><p>{LONG_TEXT}</p>'
        '<img src="/uploads/body-first.jpg"></div>',
        head='<meta property="og:image" content="https://cdn.example.edu/cover/og.jpg">',
    )
    assert cc.extract_image_url(html, PAGE_URL) == "https://cdn.example.edu/cover/og.jpg"


def test_og_image_protocol_relative_and_relative_absolutized():
    """//cdn 补 https，相对路径按页面 URL 绝对化。"""
    proto = _wrap(
        "",
        head='<meta property="og:image" content="//cdn.example.edu/a/b.png">',
    )
    assert cc.extract_image_url(proto, PAGE_URL) == "https://cdn.example.edu/a/b.png"

    relative = _wrap(
        "",
        head='<meta name="twitter:image" content="/static/cover/c.png">',
    )
    assert (
        cc.extract_image_url(relative, PAGE_URL)
        == "https://cise.example.edu/static/cover/c.png"
    )


def test_empty_og_image_falls_back_to_body():
    """og:image 的 content 为空时，继续回退正文首图。"""
    html = _wrap(
        f'<div id="vsb_content"><p>{LONG_TEXT}</p>'
        '<img src="/uploads/real.jpg"></div>',
        head='<meta property="og:image" content="">',
    )
    assert cc.extract_image_url(html, PAGE_URL) == "https://cise.example.edu/uploads/real.jpg"


def test_body_fallback_skips_logo_and_icon():
    """无 og:image 时回退正文首图，且跳过 logo/icon/小尺寸图标。"""
    html = _wrap(
        f'<div id="vsb_content"><p>{LONG_TEXT}</p>'
        '<img src="/static/logo.png">'
        '<img src="/static/site-icon.png" width="16" height="16">'
        '<img src="/static/spacer.gif">'
        '<img src="/uploads/2025/11/poster.jpg"></div>'
    )
    assert (
        cc.extract_image_url(html, PAGE_URL)
        == "https://cise.example.edu/uploads/2025/11/poster.jpg"
    )


def test_only_noise_images_returns_empty():
    """整页只有 logo/二维码/data URI/svg 时返回空串（不落库脏 URL）。"""
    html = _wrap(
        f'<div id="vsb_content"><p>{LONG_TEXT}</p>'
        '<img src="/static/logo.png">'
        '<img src="data:image/png;base64,iVBORw0KGgo=">'
        '<img src="/uploads/qrcode_wechat.png">'
        '<img src="/uploads/pic.svg">'
        '<img src="/uploads/thumb.png" width="80"></div>'
    )
    assert cc.extract_image_url(html, PAGE_URL) == ""


def test_no_image_at_all_returns_empty():
    """没有任何 img / meta 时返回空串，不抛异常。"""
    assert cc.extract_image_url(_wrap(f"<p>{LONG_TEXT}</p>"), PAGE_URL) == ""


def test_data_uri_in_og_image_is_rejected():
    """og:image 是 data: URI 时视为无效，继续回退正文。"""
    html = _wrap(
        f'<div id="vsb_content"><p>{LONG_TEXT}</p>'
        '<img src="/uploads/fallback.jpg"></div>',
        head='<meta property="og:image" content="data:image/gif;base64,R0lGOD">',
    )
    assert cc.extract_image_url(html, PAGE_URL) == "https://cise.example.edu/uploads/fallback.jpg"


def test_og_image_with_blacklisted_url_falls_back_to_body():
    """og:image 本身是 qrcode/banner 时不能直接用，回退到正文海报。"""
    html = _wrap(
        f'<div id="vsb_content"><p>{LONG_TEXT}</p>'
        '<img src="/uploads/poster.jpg"></div>',
        head='<meta property="og:image" content="/static/qrcode-wechat.png">',
    )
    assert cc.extract_image_url(html, PAGE_URL) == "https://cise.example.edu/uploads/poster.jpg"


def test_fetch_competition_page_drops_images_rejected_by_guard(monkeypatch):
    """单页爬虫也必须过完整闸门：候选全被拒时 image_url 置空。"""
    html = _wrap(
        f'<div id="vsb_content"><p>{LONG_TEXT}</p>'
        '<img src="/uploads/cover.png"></div>',
    )

    def fake_extract_page(raw_html, url):
        return {
            "url": url,
            "title": "测试赛事",
            "site_name": "",
            "publish_date": "",
            "body": LONG_TEXT,
        }

    monkeypatch.setattr(cc.news, "fetch_html", lambda url: html)
    monkeypatch.setattr(cc.news, "extract_page", fake_extract_page)
    monkeypatch.setattr(
        cc.guard, "check_image", lambda url: (False, "检测到二维码", {"verdict": "bad"})
    )

    page = cc.fetch_competition_page(PAGE_URL)
    assert page["image_url"] == ""
    assert len(page["image_candidates"]) == 1
    assert page["image_candidates"][0]["ok"] is False


# ===========================================================================
# 二、_normalize_level
# ===========================================================================

@pytest.mark.parametrize(
    "value,name,expected",
    [
        # 模型显式值优先
        ("national", "", "national"),
        ("国家级", "", "national"),
        ("national", "浙江省大学生程序设计竞赛", "national"),
        ("Provincial", "", "provincial"),
        ("省级", "全国大学生数学建模竞赛", "provincial"),
        # 模型没给值时，从赛事名推断
        (None, "全国大学生数学建模竞赛", "national"),
        (None, "中国大学生计算机设计大赛", "national"),
        (None, "浙江省大学生程序设计竞赛", "provincial"),
        # “赛区”不能被误判成省级
        (None, "第八届中国大学生数学竞赛（广东赛区）", "national"),
        # 无语义线索时默认 national
        (None, "某某杯大赛", "national"),
        ("", "", "national"),
    ],
)
def test_normalize_level(value, name, expected):
    assert cc._normalize_level(value, name) == expected


def test_normalize_level_never_returns_empty():
    """任何输入都必须返回 national / provincial 之一，避免前端 level 为空。"""
    for value in (None, "", "  ", "unknown", 123):
        assert cc._normalize_level(value, "") in ("national", "provincial")
