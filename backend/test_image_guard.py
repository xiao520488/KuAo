# -*- coding: utf-8 -*-
"""image_guard 纯函数单测（不连网）。

覆盖：
    1. URL 黑名单：qr/qrcode/erweima/logo/icon/banner 等必须拒收；
    2. 正常图片 URL（query、silicon 等）不能被误杀；
    3. 二维码内容识别：真二维码必须被识别为条码图。
"""

import io

import pytest

import image_guard as guard


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/qr.png",
        "https://cdn.example.com/qrcode/abc.png",
        "https://cdn.example.com/qr_code.png",
        "https://cdn.example.com/erweima.jpg",
        "https://cdn.example.com/site-logo.png",
        "https://cdn.example.com/icon/share.png",
        "https://cdn.example.com/banner/2026.jpg",
        "https://cdn.example.com/abc.png?qrcode=1",
        "https://cdn.example.com/abc.png?img=erweima",
        "https://cdn.example.com/cover.svg",
        "data:image/png;base64,iVBORw0KGgo=",
        "/uploads/local-relative.jpg",
        "javascript:alert(1)",
        "",
        None,
    ],
)
def test_bad_image_urls_are_rejected(url):
    assert guard.is_bad_image_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/2026/poster.jpg",
        "https://cdn.example.com/cover/abc.png?query=1",
        "https://cdn.example.com/technology.jpg",
        "https://cdn.example.com/news/12345.png",
    ],
)
def test_normal_image_urls_pass(url):
    assert guard.is_bad_image_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/logo2026.png",
        "https://cdn.example.com/my-icon.png",
        "https://cdn.example.com/static/banner_2026.jpg",
    ],
)
def test_logo_icon_banner_substrings_rejected(url):
    assert guard.is_bad_image_url(url) is True


def test_qr_text_hints():
    assert guard.is_qr_text("扫码加入QQ群") is True
    assert guard.is_qr_text("", None, "qrcode") is True
    assert guard.is_qr_text("赛事海报", "2026 大赛") is False


def test_has_barcode_detects_real_qr():
    zxingcpp = pytest.importorskip("zxingcpp")
    Image = pytest.importorskip("PIL.Image")
    numpy = pytest.importorskip("numpy")
    barcode = zxingcpp.create_barcode(
        "https://example.com/register", zxingcpp.BarcodeFormat.QRCode
    )
    image = Image.fromarray(numpy.asarray(barcode.to_image()))
    buf = io.BytesIO()
    image.save(buf, format="PNG")

    assert guard.has_barcode(buf.getvalue()) is True


def test_has_barcode_returns_false_for_plain_image():
    Image = pytest.importorskip("PIL.Image")
    image = Image.new("RGB", (400, 225), (30, 90, 160))
    buf = io.BytesIO()
    image.save(buf, format="PNG")

    assert guard.has_barcode(buf.getvalue()) is False


def test_image_dimensions_png():
    Image = pytest.importorskip("PIL.Image")
    image = Image.new("RGB", (320, 180), (255, 255, 255))
    buf = io.BytesIO()
    image.save(buf, format="PNG")

    assert guard.image_dimensions(buf.getvalue()) == (320, 180)
