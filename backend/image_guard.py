# -*- coding: utf-8 -*-
"""image_guard.py —— 赛事封面图统一闸门（爬虫 / 补全 / 清理共用）。

背景
----
历史采集直接把页面第一张图（甚至 og:image）写进 ``competitions.image_url``，
结果混进了 QQ 群二维码、站点 banner、logo、超长竖图等垃圾封面，前端卡片
被裁得没法看。本模块把所有判断收在一处，所有写图入口都必须先过闸门：

    1. URL 黑名单：qr / qrcode / erweima / logo / icon / banner ...
    2. 可达性 + Content-Type + 体积上限；
    3. 尺寸与比例：最小 300x150，宽高比必须在 0.45 ~ 2.6 之间；
    4. 二维码识别：用 zxing-cpp 真解码，能扫出条码的图直接拒收；
    5. 认不出尺寸 / 图片格式异常：一律拒收（宁可无图走简称兜底）。

接口
----
``is_bad_image_url(url) -> bool``
    不联网的 URL 黑名单判断，可安全用在 db 写入层。

``inspect_image(url) -> (ok, reason, info)``
    完整校验。``info["verdict"]`` 为 ``ok`` / ``bad`` / ``transient``，
    清理脚本据此区分「确定是脏图」和「本次网络没打通、先保留」。

``check_image(url) -> (ok, reason, info)``
    ``inspect_image`` 的布尔兼容包装，沿用 batch_update / pipeline 的旧签名。
"""

import io
import re
from urllib.parse import unquote, urlparse

import requests

# ---------------------------------------------------------------------------
# 阈值（与 16:9 卡片封面匹配）
# ---------------------------------------------------------------------------
IMAGE_MIN_WIDTH = 300
IMAGE_MIN_HEIGHT = 150
IMAGE_MAX_ASPECT = 2.6      # 宽/高 超过即当 banner 处理
IMAGE_MIN_ASPECT = 0.45     # 高/宽 超过 2.2（超长竖图）同样丢弃
IMAGE_MAX_BYTES = 3 * 1024 * 1024

# 文件名 / 路径里出现这些片段直接拒收（产品要求：命中即 NULL）。
# logo/icon/banner 按子串匹配，logo2026.png / my-icon.svg 都不会漏；
# qr / ewm 太短，按 token 匹配，避免误伤 query 这类正常单词。
_BANNED_SUBSTRINGS = (
    "qrcode", "qr_code", "qr-code", "erweima", "logo", "icon", "banner",
    "avatar", "wechat", "weixin", "favicon", "loading",
    "spacer", "placeholder", "1x1", "pixel.gif", "blank.gif",
)
_BANNED_TOKENS = {"qr", "ewm"}
# 页面 img 的 alt/title/class 里出现这些文案，说明是二维码/公众号图
_QR_TEXT_HINTS = (
    "二维码", "扫码", "扫一扫", "公众号", "qq群", "qq 群", "微信群", "加群",
    "qrcode", "qr code", "qr_code",
)

_TOKEN_SPLIT = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# URL 层：不联网
# ---------------------------------------------------------------------------
def is_bad_image_url(url) -> bool:
    """URL 是否命中图片黑名单 / 根本不是 http(s) 图片地址。"""
    if not isinstance(url, str):
        return True
    text = url.strip()
    if not text:
        return True
    low = text.lower()
    if low.startswith(("data:", "javascript:", "blob:")):
        return True

    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return True
    if parsed.path.lower().endswith(".svg"):
        return True

    # 路径 + query 都参与判断：很多站点的二维码图挂在 ?img=qrcode 这种参数里
    haystack = unquote(low)
    if any(token in haystack for token in _BANNED_SUBSTRINGS):
        return True

    tokens = {token for token in _TOKEN_SPLIT.split(haystack) if token}
    if tokens & _BANNED_TOKENS:
        return True
    # qr123 / qr_123 这类以 qr 开头的文件名
    if re.search(r"(?:^|[/_.\-])qr[-_]?\d", haystack):
        return True
    return False


def is_qr_text(*values) -> bool:
    """img 的 alt/title/class 等文本是否在描述二维码。"""
    for value in values:
        text = str(value or "").strip().lower()
        if text and any(hint in text for hint in _QR_TEXT_HINTS):
            return True
    return False


# ---------------------------------------------------------------------------
# 字节层：尺寸 / 二维码
# ---------------------------------------------------------------------------
def image_dimensions(data: bytes):
    """从文件头解析宽高（PNG/JPEG/GIF/WebP），认不出返回 (None, None)。"""
    if len(data) < 24:
        return None, None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":  # JPEG：扫 SOFn 段
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(data[i + 5:i + 7], "big")
                width = int.from_bytes(data[i + 7:i + 9], "big")
                return width, height
            i += 2 + seg_len
        return None, None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":
            return (int.from_bytes(data[24:27], "little") + 1,
                    int.from_bytes(data[27:30], "little") + 1)
        if kind == b"VP8 ":
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
        if kind == b"VP8L":
            b = data[21:25]
            return (1 + (((b[1] & 0x3F) << 8) | b[0]),
                    1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6)))
    return None, None


def has_barcode(data: bytes):
    """图片里是否真的能扫出二维码/条码。

    返回 True（有）/ False（没有）/ None（环境缺 Pillow 或 zxing-cpp，
    无法判断）。二维码识别是「宁可错过、不可放过」：只有明确扫出条码
    才拒收，不会因为装不了解码库就把所有图都误杀。
    """
    try:
        from PIL import Image
        import zxingcpp
    except ImportError:
        return None

    try:
        with Image.open(io.BytesIO(data)) as image:
            # 超大图先缩到合理尺寸，避免解码炸弹拖垮爬虫
            image.thumbnail((2048, 2048))
            results = zxingcpp.read_barcodes(image)
        return bool(results)
    except Exception:  # noqa: BLE001 - 解码失败当作"判断不了"
        return None


# ---------------------------------------------------------------------------
# 网络层：完整校验
# ---------------------------------------------------------------------------
def inspect_image(url: str, timeout=(8, 15)) -> tuple:
    """下载并校验封面图。

    返回 ``(ok, reason, info)``；reason 为空表示通过。
    info 里带 ``verdict``：
      * ``ok``         —— 通过；
      * ``bad``        —— 确定是脏图/死链，可以置 NULL；
      * ``transient``  —— 本次网络/服务端异常，先保留，下次再查。
    """
    info = {"url": url, "verdict": "bad"}
    if is_bad_image_url(url):
        return False, "URL 命中图片黑名单", info

    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": _UA},
            stream=True,
        )
    except Exception as exc:  # noqa: BLE001
        info["verdict"] = "transient"
        return False, f"图片请求失败（{type(exc).__name__}）", info

    with resp:
        status = resp.status_code
        if status in (403, 404, 410):
            return False, f"图片 HTTP {status}（死链/禁止访问）", info
        if status != 200:
            info["verdict"] = "transient"
            return False, f"图片 HTTP {status}", info

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("image/"):
            return False, f"不是图片（Content-Type={ctype or '未知'}）", info

        chunks, total = [], 0
        for chunk in resp.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > IMAGE_MAX_BYTES:
                return False, f"图片超过 {IMAGE_MAX_BYTES // 1024 // 1024} MB", info
        data = b"".join(chunks)

    width, height = image_dimensions(data)
    info.update({
        "bytes": len(data),
        "width": width,
        "height": height,
        "content_type": ctype,
    })
    if not width or not height:
        return False, f"无法识别图片尺寸（{len(data) // 1024} KB）", info
    if width < IMAGE_MIN_WIDTH or height < IMAGE_MIN_HEIGHT:
        return False, f"尺寸过小 {width}x{height}", info

    ratio = width / height
    if ratio > IMAGE_MAX_ASPECT:
        return False, f"宽高比 {ratio:.2f} 过大（{width}x{height}，疑似 banner）", info
    if ratio < IMAGE_MIN_ASPECT:
        return False, f"高宽比 {height / width:.2f} 过大（{width}x{height}，竖图）", info

    barcode = has_barcode(data)
    info["barcode"] = barcode
    if barcode is True:
        return False, f"检测到二维码/条码（{width}x{height}）", info
    if barcode is None:
        info["barcode_note"] = "未安装 Pillow/zxing-cpp，跳过二维码内容识别"
        # 识别能力缺失时按「宁可不用」处理：竖版卡片最常见于 QQ 群/公众号二维码，
        # 而 16:9 卡片本来也不适合竖图，保守拒收。
        if ratio < 0.85:
            return False, f"竖版图且无法识别二维码（{width}x{height}），保守拒收", info

    info["verdict"] = "ok"
    return True, "", info


def check_image(url: str, timeout=(8, 15)) -> tuple:
    """``inspect_image`` 的布尔兼容包装（保留旧调用签名）。"""
    return inspect_image(url, timeout)
