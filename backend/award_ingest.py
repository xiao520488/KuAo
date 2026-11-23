# -*- coding: utf-8 -*-
"""获奖名单抓取与入库（award_ingest.py）— S1「获奖作品补全」的执行器

背景
----
85 个赛事里 68 个没有任何获奖作品，AI 选题灵感因此被置灰（`awardsCount === 0`）。
名单散落在三类地方（官网公告 PDF/Excel/网页表格、官网作品展示库、公众号推文），
没有统一接口，所以这里不做「一个通用爬虫」，做的是**声明式适配器 + 来源登记表**：

  * `award_sources` 表（schema_v2.sql 第 15 节）一行 = 一个「赛事 × 公示 URL」，
    抓哪个页面、用哪个适配器、哪一列映射成什么字段，全在 `parse_spec`（JSON）；
  * 本脚本只负责：抓取 → 解析 → 归一化 → 去重 → 写 `awards`（默认 pending）；
  * 解析口径要改，只改数据库里那一行，不改代码、不重新部署。

适配器（adapter）
----------------
  * `html_table` —— 公示页正文就是 HTML 表格（如中国软件杯）。
  * `xlsx_sheet` —— 附件是 xlsx（如全国大学生信息安全竞赛）。
  * `docx_table` —— 附件是 docx（大创 2023/2024 用的就是这种）。
  * `pdf_table`  —— 附件是 PDF，两种模式：
      - `mode: "table"`（默认）pdfplumber 抽表，适合有表格线的电子版 PDF
        （服创 13 列、计设 8 列、数模 8 列）；
      - `mode: "lines"` 逐行正则 + 段标题（适合合并单元格把列冲乱的 PDF，
        如嵌入式芯片竞赛）。扫描件（无文字层）一律失败并写 last_error，
        **不做 OCR**——OCR 归人工/离线，宁可留空也不写错。

`parse_spec` 字段（都是可选的，不写就用默认）
-------------------------------------------
    {
      "columns":       {"源表头": "title|school|award_grade|year|session|track|advisor|..."},
      "extra_columns": {"源表头": "展示名"},        # 拼进 summary，如 编号 / 省份 / 队员
      "fill_down":     ["school", "award_grade"],   # 合并单元格：空值向下填充
      "fixed":         {"year": 2026, "level": "national", "session": "第17届"},
      "section_map":   {"一等奖": "一等奖"},        # 段标题 → award_grade（PDF 分段）
      "strip":         "，,;；\\u00a0",              # 单元格首尾要剥掉的字符（默认一套）
      "drop_if_empty": ["title"],                   # 这些字段为空就丢行（默认 title）
      "dedupe_key":    ["title", "school", "award_grade"],
      "sheet":         "Sheet1",                    # xlsx：sheet 名，缺省用第一个
      "header_row":    0,                           # 表头所在行（0 基），缺省自动探测
      "table_index":   0,                           # html/docx：第几张表，缺省按表头匹配
      "encoding":      "gb18030",                   # 抓 html 时强制编码
      "referer":       "https://...",               # 附件防盗链
      "bootstrap_url": "https://...",               # 先 GET 它拿 cookie（vsb9 download.jsp 必用）
      "mode":          "table|lines",               # pdf 专用
      "row_regex":     "^(?P<work_no>\\\\d{4,6})\\\\s+(?P<title>.+)$",  # pdf lines 模式
      "section_regex": "^(?:本科组|高职高专组)?(一等奖|二等奖|三等奖)$"
    }

CLI
---
    # 看已登记的来源
    python award_ingest.py --list

    # 从 seed 文件注册/更新来源（幂等）
    python award_ingest.py --seed

    # 演练：抓 + 解析 + 打印，不写库（默认就是演练）
    python award_ingest.py --source-id 3 --print-rows 5

    # 入库（先自动备份），--approve 直接把 review_status 置 approved
    python award_ingest.py --source-id 3 --apply

    # 全部已登记来源跑一遍
    python award_ingest.py --all --apply

    # 临时登记一条（调试新站点时用），配合 --dry-run 只看解析
    python award_ingest.py --add --competition-id 17 --url https://... --adapter xlsx_sheet \\
        --spec-file spec.json --print-rows 3

退出码：0 全部成功；1 有来源失败（cron 可据此告警）。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

import db

log = logging.getLogger("award_ingest")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 40
MAX_BYTES = 40 * 1024 * 1024
SEED_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "award_sources.seed.json"
)

# 与 awards 表语义字段对齐（school 落到 team，见 _to_award_payload）
FIELD_ALIASES = {
    "school": "school",
    "team": "school",
    "title": "title",
    "work_title": "title",
    "award_grade": "award_grade",
    "grade": "award_grade",
    "award": "award_grade",
    "year": "year",
    "session": "session",
    "track": "track",
    "category": "category",
    "advisor": "advisor",
    "teacher": "advisor",
    "members": "members",
    "member": "members",
    "province": "province",
    "work_no": "work_no",
    "team_code": "team_code",
    "team_name": "team_name",
    "topic": "topic",
}

_DEFAULT_STRIP = "，,;；;、\u00a0 \t\r\n"


# =============================================================================
# 一、抓取
# =============================================================================
def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return s


def http_get(url: str, *, session: Optional[requests.Session] = None,
             referer: Optional[str] = None,
             timeout: int = DEFAULT_TIMEOUT) -> Tuple[bytes, str, str]:
    """下载 url，返回 (content, final_url, content_type)。

    referer 用于防盗链；给 None 时不发 Referer 头。
    大小超 MAX_BYTES 直接报错（防止把几百 MB 的名单包读进内存）。
    """
    s = session or build_session()
    headers = {"Referer": referer} if referer else {}
    resp = s.get(url, headers=headers, timeout=timeout, stream=True)
    resp.raise_for_status()
    chunks, total = [], 0
    for chunk in resp.iter_content(65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_BYTES:
            raise ValueError(f"响应超过 {MAX_BYTES // 1024 // 1024} MB，已中止：{url}")
        chunks.append(chunk)
    return b"".join(chunks), resp.url, resp.headers.get("Content-Type", "")


def fetch_source(source: dict, *, timeout: int = DEFAULT_TIMEOUT) -> Tuple[bytes, str, str]:
    """抓一个来源。vsb9 系站点的附件（download.jsp）必须先建会话，见 parse_spec。

    bootstrap_url: 先 GET 该页拿 cookie（不解析），再抓目标 URL；
    referer: 抓附件时带上，否则 vsb9 返回「请输入验证码下载附件」的 HTML。
    """
    spec = source.get("parse_spec_obj") or {}
    session = build_session()
    boot = spec.get("bootstrap_url") or source.get("bootstrap_url")
    if boot:
        log.info("bootstrap 会话：%s", boot)
        http_get(boot, session=session, timeout=timeout)
    referer = spec.get("referer") or boot
    return http_get(source["url"], session=session, referer=referer, timeout=timeout)


def fetch_paged(source: dict, *, timeout: int = DEFAULT_TIMEOUT) -> List[Tuple[str, bytes]]:
    """抓「一个来源 = 多个页面」的名单（大广赛这类一页 15 行、120 页的分页表）。

    parse_spec.urls 给出除主 URL 之外的其余页面；同一会话内顺序抓，任何一页失败
    就整体失败（宁可这条来源 failed，也不要拿半截名单入库）。
    """
    spec = source.get("parse_spec_obj") or {}
    explicit = [u for u in (spec.get("urls") or []) if u]
    template = spec.get("url_template")
    page_count = int(spec.get("page_count") or 0)
    if template and page_count > 0:
        # 分页站点（大广赛 500 页）用模板展开，seed 里只存一条模板 + 页数，
        # 不必把 500 个 URL 塞进 JSON
        urls = [template.replace("{page}", str(i)) for i in range(1, page_count + 1)]
        urls += explicit
    else:
        urls = [source["url"]] + explicit
    session = build_session()
    boot = spec.get("bootstrap_url") or source.get("bootstrap_url")
    if boot:
        log.info("bootstrap 会话：%s", boot)
        http_get(boot, session=session, timeout=timeout)
    referer = spec.get("referer") or boot
    out = []
    for idx, url in enumerate(urls, 1):
        content, final_url, _ctype = http_get(url, session=session, referer=referer,
                                              timeout=timeout)
        out.append((final_url or url, content))
        if idx % 20 == 0 or idx == len(urls):
            log.info("分页抓取 %d/%d", idx, len(urls))
    return out


# =============================================================================
# 二、格式判定与解码
# =============================================================================
def detect_format(url: str, content_type: str, content: bytes) -> str:
    """按「URL 后缀 → magic bytes → content-type」的顺序判定格式。

    不信 content-type：官网常用 application/octet-stream 代理下载，
    甚至把 xlsx 报成 image/jpeg（cy.ncss.cn 的 /picture 代理就是这样）。
    """
    path = url.split("?")[0].lower()
    for ext, fmt in ((".xlsx", "xlsx"), (".xls", "xlsx"), (".docx", "docx"),
                     (".pdf", "pdf")):
        if path.endswith(ext):
            return fmt
    head = content[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        # zip 容器：xlsx 与 docx 都是 zip，再看内部目录
        if b"xl/workbook.xml" in content[:4096] or b"xl/" in content[:65536]:
            return "xlsx"
        if b"word/document.xml" in content[:65536]:
            return "docx"
        return "zip"
    if b"<html" in content[:2048].lower() or b"<!doctype" in content[:2048].lower():
        return "html"
    ctype = (content_type or "").lower()
    if "pdf" in ctype:
        return "pdf"
    if "html" in ctype:
        return "html"
    return "html" if content else "unknown"


def decode_html(raw: bytes, spec: dict) -> str:
    """解 HTML：优先 spec.encoding，否则在 utf-8 / gb18030 里挑替换符更少的。"""
    forced = (spec or {}).get("encoding")
    if forced:
        return raw.decode(forced, errors="replace")
    best, best_bad = "", None
    for enc in ("utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            text = raw.decode(enc, errors="replace")
        bad = text.count("\ufffd")
        if best_bad is None or bad < best_bad:
            best, best_bad = text, bad
        if bad == 0:
            break
    return best


# =============================================================================
# 三、四个适配器（都返回 List[dict]：键是「源列名」或字段名）
# =============================================================================
def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _clean(value, spec: dict) -> str:
    text = _cell(value)
    strip_chars = spec.get("strip")
    chars = strip_chars if isinstance(strip_chars, str) and strip_chars else _DEFAULT_STRIP
    text = text.strip(chars)
    # PDF 单元格常把长校名/多人折成两行：换行一律当空格，再压掉多余空白；
    # 「哈尔滨工业大学\n（深圳）」这种括号前不留空格，避免把校名拆成两家。
    text = text.replace("\r", "").replace("\n", " ")
    text = re.sub(r"[ \t\u3000]{2,}", " ", text)
    text = re.sub(r"\s+([（(])", r"\1", text)
    return text.strip(chars)


def parse_html_table(content: bytes, spec: dict) -> List[dict]:
    """解析 HTML 表格。table_index 指定则取第 N 张，否则按表头命中数列挑一张。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(decode_html(content, spec), "lxml")
    tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if any(c for c in cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    if not tables:
        raise ValueError("页面里没有可用表格（可能名单只在附件里）")

    want = spec.get("columns") or {}
    index = spec.get("table_index")
    if index is not None:
        if index >= len(tables):
            raise ValueError(f"table_index={index} 越界，页面只有 {len(tables)} 张表")
        chosen = tables[index]
    else:
        def score(rows):
            head = "".join(rows[0]) if rows else ""
            return sum(1 for col in want if col and col in head)

        best_i = max(range(len(tables)), key=lambda i: score(tables[i]))
        chosen = tables[best_i]
        log.info("共 %d 张表，选中第 %d 张（表头命中 %d 列）",
                 len(tables), best_i, score(chosen))

    header_row = spec.get("header_row")
    if header_row is None:
        header_row = 0
    header = [h for h in chosen[header_row]]
    out = []
    for row in chosen[header_row + 1:]:
        if not any(row):
            continue
        rec = {header[i]: row[i] for i in range(min(len(header), len(row))) if header[i]}
        out.append(rec)
    return out


def parse_xlsx(content: bytes, spec: dict) -> List[dict]:
    """解析 xlsx：取指定 sheet（缺省第一个），按 header_row（缺省自动探测）建字典。"""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        name = spec.get("sheet")
        ws = wb[name] if name and name in wb.sheetnames else wb[wb.sheetnames[0]]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()

    out = _rows_to_dicts(rows, spec)
    if not out:
        raise ValueError(f"sheet {ws.title!r} 里没有数据行")
    return out


def parse_docx(content: bytes, spec: dict) -> List[dict]:
    """解析 docx 表格（python-docx）。

    docx 的合并单元格会让同一文本在相邻列重复，`_dedupe_adjacent=True` 时
    把相邻重复项置空，保证列位与表头对齐（大创 2024 名单必须这么做）。
    """
    from docx import Document

    document = Document(io.BytesIO(content))
    if not document.tables:
        raise ValueError("docx 里没有表格")
    index = spec.get("table_index")
    if index is None:
        want = spec.get("columns") or {}
        def score(t):
            if not t.rows:
                return 0
            head = "".join(c.text for c in t.rows[0].cells)
            return sum(1 for col in want if col and col in head)

        index = max(range(len(document.tables)),
                    key=lambda i: score(document.tables[i]))
        log.info("共 %d 张表，选中第 %d 张", len(document.tables), index)
    if index >= len(document.tables):
        raise ValueError(f"table_index={index} 越界，docx 只有 {len(document.tables)} 张表")

    rows = []
    for tr in document.tables[index].rows:
        cells = [c.text for c in tr.cells]
        if spec.get("dedupe_adjacent", True):
            prev = None
            for i, cell_text in enumerate(cells):
                if i and cell_text == prev:
                    cells[i] = ""
                prev = cells[i]
        rows.append(cells)
    return _rows_to_dicts(rows, spec)


def _rows_to_dicts(rows: List[list], spec: dict) -> List[dict]:
    """二维表 → List[dict]：找表头行（缺省自动探测「命中 columns 键最多」的行）。"""
    want = spec.get("columns") or {}
    header_row = spec.get("header_row")
    if header_row is None:
        probe = min(len(rows), 30)
        best_i, best_hits = None, -1
        for i in range(probe):
            hits = sum(1 for col in want if col and any(str(c or "") == col for c in rows[i]))
            if hits > best_hits:
                best_i, best_hits = i, hits
        header_row = best_i if best_hits > 0 else 0
        log.info("表头探测：第 %d 行（命中 %d 列）", header_row, best_hits)

    header = [_clean(h, spec) for h in rows[header_row]]
    out = []
    for row in rows[header_row + 1:]:
        if not any(_clean(c, spec) for c in row):
            continue
        rec = {}
        for i, key in enumerate(header):
            if not key or i >= len(row):
                continue
            rec[key] = _cell(row[i])
        out.append(rec)
    return out


def parse_pdf(content: bytes, spec: dict) -> List[dict]:
    """解析 PDF：mode=table（默认，抽表）或 mode=lines（逐行正则 + 段标题）。

    * 只处理**有文字层**的 PDF；扫描件在这里会抛 ValueError（带页数），
      由调用方把错误写进 award_sources.last_error，交给人工/OCR，不硬猜。
    * mode=table 会顺便用 `section_regex` 找段标题，按 y 坐标把「表格上方的
      最后一个标题」当作该表的 award_grade（数模名单就是「本科组一等奖」分段的）。
    """
    import pdfplumber

    spec = dict(spec or {})
    mode = (spec.get("mode") or "table").lower()
    section_re = re.compile(spec.get("section_regex")) if spec.get("section_regex") else None
    row_re = re.compile(spec.get("row_regex")) if spec.get("row_regex") else None
    if mode == "lines" and row_re is None:
        raise ValueError("pdf mode=lines 必须提供 row_regex")

    out: List[dict] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        if len(pdf.pages) == 0:
            raise ValueError("PDF 没有页面")
        text_chars = 0
        current_section = ""
        for page_no, page in enumerate(pdf.pages, 1):
            page_text = page.extract_text() or ""
            text_chars += len(page_text.strip())
            if mode == "lines":
                for line in page_text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if section_re and section_re.search(line) and not row_re.search(line):
                        current_section = section_re.search(line).group(1) \
                            if section_re.groups else line
                        continue
                    m = row_re.search(line)
                    if not m:
                        continue
                    groups = m.groupdict() or {}
                    if not groups:
                        # 无名分组：按 spec.regex_fields 顺序取值
                        fields = spec.get("regex_fields") or []
                        groups = {f: (m.group(i + 1) if i + 1 <= m.re.groups else "")
                                  for i, f in enumerate(fields)}
                    groups = {k: _clean(v, spec) for k, v in groups.items()}
                    groups.setdefault("_section", current_section)
                    groups["_page"] = page_no
                    out.append(groups)
                continue

            # mode == table
            headings = []
            if section_re:
                try:
                    text_lines = page.extract_text_lines()
                except Exception:  # noqa: BLE001
                    text_lines = []
                for line in text_lines:
                    hit = section_re.search(line.get("text") or "")
                    if hit:
                        grade = hit.group(1) if hit.groups else (line.get("text") or "")
                        headings.append((line.get("top") or 0, grade))
            try:
                tables = page.find_tables()
            except Exception:  # noqa: BLE001 —— 个别页版式会让 find_tables 抛错
                continue
            # 段标题只印在段首那一页，后面的页没有标题，所以奖项必须"粘住"跨页传递；
            # 同一页里标题可能夹在表格中间，故按**行**的 y 坐标切，而不是按表切。
            headings.sort(key=lambda h: h[0])
            pointer = 0
            for table in sorted(tables, key=lambda t: t.bbox[1]):
                data = table.extract() or []
                if not data:
                    continue
                row_objs = list(getattr(table, "rows", []) or [])
                for index, row in enumerate(data):
                    if headings and index < len(row_objs):
                        top = row_objs[index].bbox[1]
                        while pointer < len(headings) and headings[pointer][0] < top:
                            current_section = headings[pointer][1]
                            pointer += 1
                    cells = [_clean(c, spec) for c in (row or [])]
                    if not any(cells):
                        continue
                    rec = {"_cells": cells, "_section": current_section,
                           "_page": page_no}
                    out.append(rec)

        if text_chars < 20:
            raise ValueError(
                f"PDF 共 {len(pdf.pages)} 页但没有文字层（扫描件），需要 OCR；"
                "本脚本不猜内容，先人工/离线转文字再入库"
            )

    if mode == "table":
        out = _table_rows_to_dicts(out, spec)
    if not out:
        raise ValueError("PDF 里没有解析出数据行（版式可能变了，检查 parse_spec）")
    return out


def _table_rows_to_dicts(rows: List[dict], spec: dict) -> List[dict]:
    """把 pdf 抽出的「裸单元格行」按表头映射成字典。

    三种识别方式，按 spec 里给了哪个决定：
      * `columns` + 表头行 —— 电子版 PDF 有表头（服创 / 计设）；
      * `positional` —— 没有表头、列位固定（数模：序号/组别/省份/学校/队员×3/指导教师）；
      * 两者都没有时只保留 `_section`（供 lines 模式使用）。
    同一语义字段出现多次时（队员1/队员2/队员3）用「、」累加，不覆盖。
    """
    columns = spec.get("columns") or {}
    positional = spec.get("positional") or []
    section_default = spec.get("section_default") or ""
    header_re = re.compile(spec.get("header_regex")) if spec.get("header_regex") else None
    header_words = {_norm_key(c) for c in columns}
    keys: Optional[List[str]] = None
    out: List[dict] = []
    for rec in rows:
        cells = rec.get("_cells") or []
        joined = "".join(cells)
        if header_re and header_re.search(joined):
            continue
        if columns and (keys is None or spec.get("repeating_header")):
            hits = sum(1 for cell in cells if cell in columns)
            if hits >= max(2, len(columns) - 2) and hits > 0:
                keys = [cell if cell in columns else "" for cell in cells]
                log.info("表头行：%s", [k for k in keys if k])
                continue

        if keys is not None:
            item = {}
            for i, key in enumerate(keys):
                if key and i < len(cells):
                    _append_cell(item, key, cells[i])
        elif positional:
            item = {}
            for i, key in enumerate(positional):
                if i < len(cells):
                    _append_cell(item, key, cells[i])
            if _norm_key(item.get("title")) and header_words and \
                    _norm_key(item.get("title")) in header_words:
                continue
        else:
            continue
        item["_section"] = rec.get("_section") or section_default
        item["_page"] = rec.get("_page")
        out.append(item)
    return out


def _append_cell(item: dict, key: str, value: str) -> None:
    """同一列名/字段出现多次时累加（队员1/队员2/队员3 → members）。"""
    value = value or ""
    if not value:
        return
    if item.get(key) and value != item[key]:
        item[key] = f"{item[key]}、{value}"
    else:
        item[key] = value


# =============================================================================
# 四、归一化
# =============================================================================
def _map_field(name: str) -> Optional[str]:
    return FIELD_ALIASES.get((name or "").strip().lower()) or FIELD_ALIASES.get(name)


def normalize_rows(raw_rows: Sequence[dict], spec: dict, source: dict) -> List[dict]:
    """把适配器吐出的「源列字典」变成统一的 awards 候选行。

    1) columns 映射 → 语义字段；2) fill_down 填合并单元格；3) 丢空 title；
    4) section_map 覆盖 award_grade；5) fixed 兜底；6) extra_columns 拼 summary。
    """
    spec = spec or {}
    columns = spec.get("columns") or {}
    extra = spec.get("extra_columns") or {}
    fixed = spec.get("fixed") or {}
    fill_down = spec.get("fill_down") or []
    section_map = spec.get("section_map") or {}
    drop_if_empty = spec.get("drop_if_empty") or ["title"]
    # title_template 存在时标题由后面的字段拼出来，这里不能因为 title 为空就丢行
    drop_if_empty = [f for f in drop_if_empty
                     if not (f == "title" and spec.get("title_template"))]
    split_first_space = spec.get("split_first_space") or {}

    records: List[dict] = []
    carried: Dict[str, str] = {}
    for raw in raw_rows:
        rec: Dict[str, str] = {}
        extras: List[Tuple[str, str]] = []
        for src_col, value in raw.items():
            if src_col in ("_section", "_page") or value is None:
                continue
            text = _clean(value, spec)
            if src_col in split_first_space:
                # 「团队编号+队名」这种合并列：按首个空白切成两段
                first, second = split_first_space[src_col]
                code, _, name = text.partition(" ")
                rec[first] = code.strip()
                rec[second] = name.strip() or code.strip()
                continue
            if src_col in columns:
                field = _map_field(columns[src_col]) or columns[src_col]
                # 多列映射到同一语义字段（队长 / 队员1 / 队员2 → members）时累加，
                # 否则后一列会把前一列覆盖掉，名单里只剩最后一个人。
                if rec.get(field) and text and text != rec[field]:
                    rec[field] = f"{rec[field]}、{text}"
                elif text:
                    rec[field] = text
            elif src_col in extra:
                extras.append((extra[src_col], text))
            else:
                field = _map_field(src_col)
                if field:
                    rec[field] = text
                elif text:
                    extras.append((_EXTRA_LABELS.get(src_col, src_col), text))

        for field in fill_down:
            if rec.get(field):
                carried[field] = rec[field]
            elif carried.get(field):
                rec[field] = carried[field]

        section = raw.get("_section") or ""
        if section:
            grade = section_map.get(section, section)
            rec.setdefault("award_grade", grade)
            if not rec.get("award_grade"):
                rec["award_grade"] = grade

        grade_allow = spec.get("grade_allow") or []
        if grade_allow and (rec.get("award_grade") or "") not in grade_allow:
            # 例外的「优秀组织奖」「机构奖」这类不是队伍奖，按白名单丢掉
            continue

        if any(not rec.get(f) for f in drop_if_empty):
            continue

        for key, value in fixed.items():
            if value not in (None, ""):
                target = _map_field(key) or key
                if target in ("year", "session", "track", "category"):
                    rec[target] = str(value) if target != "year" else value
                else:
                    rec.setdefault(target, value)

        summary_parts = [f"{label}：{value}" for label, value in extras if value]
        if rec.get("session"):
            summary_parts.insert(0, f"届次：{rec['session']}")
        rec["_extras"] = summary_parts
        records.append(rec)
    return records


def _apply_grouping(records: List[dict], spec: dict) -> List[dict]:
    """按 group_by 把「一队多行」合并成「一队一行」。

    服创的获奖名单是 103 页、一队占多行（首个数据行填团队级字段，后续行只剩
    成员/角色/指导老师），不合并就会把 1,479 支队写成 6,446 条获奖记录。

    spec 形如：{"group_by": {"key": ["team_code"], "concat": ["members", "advisor"]}}
    合并后保留首行的团队级字段，concat 里的字段用「、」拼接并去重。
    """
    group = (spec or {}).get("group_by") or {}
    keys = group.get("key") or []
    concat = group.get("concat") or []
    if not keys:
        return records

    merged: Dict[tuple, dict] = {}
    order: List[tuple] = []
    for rec in records:
        key = tuple(_norm_key(rec.get(k)) for k in keys)
        if not any(key):
            continue
        if key not in merged:
            merged[key] = dict(rec)
            merged[key]["_concat_buf"] = {f: [] for f in concat}
            order.append(key)
        target = merged[key]
        buf = target["_concat_buf"]
        for field in concat:
            value = (rec.get(field) or "").strip()
            if field in buf and value and value not in buf[field]:
                buf[field].append(value)
            elif field not in buf and value and not target.get(field):
                target[field] = value
    out = []
    for key in order:
        rec = merged[key]
        buf = rec.pop("_concat_buf", {})
        for field, values in buf.items():
            if values:
                rec[field] = "、".join(values)
        out.append(rec)
    return out


_SEMANTIC_FIELDS = {
    "title", "school", "award_grade", "year", "session",
    "track", "category", "advisor", "members", "summary",
}

_EXTRA_LABELS = {
    "work_no": "作品编号",
    "team_code": "队伍编号",
    "team_name": "队伍",
    "province": "省份",
    "topic": "赛题",
    "level": "级别",
    "seq": "序号",
    "group": "组别",
}

# 名单类 PDF/HTML 每页重复的表头文字，被当成数据行时按标题命中丢弃
_HEADER_WORDS = {
    "作品名称", "团队名称", "参赛项目", "项目名称", "队伍名称",
    "姓名", "学校", "学校名称", "参赛学校", "参赛报名院校",
}


def _render_template(template: str, rec: dict) -> str:
    """用 title_template 拼标题，如 "{school}·{members}"；缺失字段留空不报错。"""
    out = template
    for key in re.findall(r"\{(\w+)\}", template):
        out = out.replace("{" + key + "}", str(rec.get(key) or "").strip())
    return out.strip(" ·（）()")


def _normalize_members(text: str, spec: Optional[dict] = None) -> str:
    """成员分隔符归一：半角/全角逗号、斜杠、顿号统一成「、」，去掉空项与尾随符。

    `spec.members_split == "space"` 时额外按空格切（大广赛「易小露 吴玮玲 林嘉瑜」
    这种整队成员空格串；默认不按空格切，因为中文姓名里不该有空格）。
    """
    pattern = r"[,，、/;；\s]+" if (spec or {}).get("members_split") == "space" \
        else r"[,，、/;；]+"
    parts = [p.strip() for p in re.split(pattern, text or "") if p.strip()]
    return "、".join(parts)


# 官方 PDF 常把奖项写成「特等 / 一等 / 优胜」，而全库既有口径是「特等奖」，
# 不补这个后缀，同一场比赛在页面上会同时出现「特等」和「特等奖」两种写法。
_BARE_GRADES = {"特等", "一等", "二等", "三等", "优胜", "优秀"}


def _normalize_grade(value: Optional[str], spec: Optional[dict] = None) -> Optional[str]:
    """奖项归一：先按 `spec.grade_map` 精确覆盖，再对裸档位补「奖」字。

    「本科组一等奖」「一等奖（MVP）」这类已经完整的写法不动。
    """
    text = (value or "").strip()
    if not text:
        return None
    mapping = (spec or {}).get("grade_map") or {}
    if text in mapping:
        return mapping[text]
    if text in _BARE_GRADES:
        return text + "奖"
    return text


def _finalize(records: List[dict], spec: Optional[dict] = None) -> List[dict]:
    """收尾：拼 summary、补 title、规范化 year、清掉内部字段。"""
    spec = spec or {}
    template = spec.get("title_template")
    header_words = {_norm_key(c) for c in (spec.get("columns") or {})} | _HEADER_WORDS
    out = []
    for rec in records:
        # 表头行被当成数据混进来时（PDF 每页重复表头很常见）在这里兜底丢掉
        if rec.get("award_grade"):
            rec["award_grade"] = _normalize_grade(rec["award_grade"], spec)
        if not rec.get("title") and template:
            rec["title"] = _render_template(template, rec)
        strip_chars = spec.get("strip_chars")
        if strip_chars and rec.get("title"):
            # 大广赛第 18 届标题带书名号《…》，第 17 届不带，入库统一去掉
            rec["title"] = str(rec["title"]).strip(strip_chars).strip()
        if _norm_key(rec.get("title")) in header_words:
            continue
        parts: List[str] = list(rec.pop("_extras", []) or [])
        ignore = set(spec.get("ignore_columns") or [])
        if ignore:
            for key in [k for k in rec if k in ignore]:
                rec.pop(key, None)
        if rec.get("members"):
            rec["members"] = _normalize_members(rec["members"], spec)
            parts.append(f"成员：{rec['members']}")
        for key in [k for k in rec if k not in _SEMANTIC_FIELDS]:
            value = rec.pop(key)
            if value not in (None, ""):
                parts.append(f"{_EXTRA_LABELS.get(key, key)}：{value}")
        summary = "；".join(p for p in parts if p)[:400]
        rec["summary"] = summary or None
        year = rec.get("year")
        if year not in (None, ""):
            try:
                rec["year"] = int(str(year).rstrip("年"))
            except (TypeError, ValueError):
                rec["year"] = None
        else:
            rec["year"] = None
        rec.pop("_section", None)
        rec.pop("_page", None)
        out.append(rec)
    return out


def parse_pdf_dual_border(content: bytes, spec: dict) -> List[dict]:
    """双线边框表格专用解析（机械创新设计大赛 #62 这类）。

    通用 pdf_table 在这种版式上会崩：单元格边框被重复绘制两次，extract_tables()
    把 5 列识别成 11 列；「序号」垂直居中、跨行，靠「本行有序号就新起一行」会错行。

    这里改用显式几何：
      1. 竖线按 tol 聚类得到真实列边界，用 bisect 把每个 word 归列；
      2. 行边界 = 「序号」列单元格自身的 top/bottom（合并重复绘制的矩形）；
      3. 奖项等级只在段标题里（「一等奖（173项）」…「（一等奖结束）」），按 y 坐标
         在页内生效并跨页延续；
      4. 页首没有序号的文字是上一条的续行，append 回上一条。
    """
    import bisect

    import pdfplumber

    names = spec.get("col_names") or ["序号", "作品名称", "所在学校", "参赛者", "指导教师"]
    ncol = len(names)
    sec_re = re.compile(spec.get("section_regex")
                        or r"^(一等奖|二等奖|三等奖)\s*[（(]\s*\d+\s*项")
    end_re = re.compile(spec.get("section_end_regex")
                        or r"^[（(]\s*(一等奖|二等奖|三等奖)\s*结束")
    tol = float(spec.get("line_tol", 8))

    def cluster(vals: List[float]) -> List[float]:
        vals = sorted(vals)
        if not vals:
            return []
        groups = [[vals[0]]]
        for v in vals[1:]:
            if v - groups[-1][-1] <= tol:
                groups[-1].append(v)
            else:
                groups.append([v])
        return [sum(g) / len(g) for g in groups]

    def strip_marks(text: str) -> str:
        return re.sub(r"(一等奖|二等奖|三等奖)\s*[（(]\s*\d+\s*项\s*[）)]"
                      r"|[（(]\s*(一等奖|二等奖|三等奖)\s*结束\s*[）)]", "", text)

    rows: List[dict] = []
    cur: Optional[dict] = None
    state: Optional[str] = None
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            events = []
            try:
                for line in page.extract_text_lines():
                    text = (line.get("text") or "").strip()
                    m = sec_re.match(text)
                    if m:
                        events.append((line["top"], m.group(1)))
                        continue
                    if end_re.match(text):
                        events.append((line["top"], None))
            except Exception:  # noqa: BLE001 - 结构异常就当这页没有段标题
                pass
            events.sort()

            vlines = [e for e in page.edges if e["orientation"] == "v"]
            xs = cluster([e["x0"] for e in vlines]) if vlines else []
            if len(xs) < ncol:
                continue

            def col_of(x: float) -> int:
                return min(max(bisect.bisect_right(xs, x) - 1, 0), ncol - 1)

            marks = [(e["top"], e["bottom"]) for e in vlines
                     if xs[0] - 2 <= e["x0"] <= xs[1] + 2 and (e["bottom"] - e["top"]) > 8]
            merged: List[Tuple[float, float]] = []
            for top, bottom in sorted({(round(t, 1), round(b, 1)) for t, b in marks}):
                if merged and abs(merged[-1][0] - top) < 3 and abs(merged[-1][1] - bottom) < 3:
                    continue
                merged.append((top, bottom))

            words = [w for w in page.extract_words()
                     if xs[0] - 3 <= (w["x0"] + w["x1"]) / 2 <= xs[-1] + 3]

            def cells_of(band: List[dict]) -> List[str]:
                buckets: dict = {}
                for w in band:
                    buckets.setdefault(col_of((w["x0"] + w["x1"]) / 2), []).append(w["text"])
                return [strip_marks(re.sub(r"\s+", "", "".join(buckets.get(i, []))))
                        for i in range(ncol)]

            top0 = merged[0][0] if merged else 10 ** 6
            head = [w for w in words if 40 < (w["top"] + w["bottom"]) / 2 < top0 - 0.5]
            if head and cur is not None:                    # 页首续行
                vals = cells_of(head)
                for i in range(1, ncol):
                    if vals[i]:
                        cur[names[i]] = cur.get(names[i], "") + vals[i]

            grade_now = state
            for top, bottom in merged:
                for ev_top, ev_grade in events:
                    if ev_top <= top + 1:
                        grade_now = ev_grade
                vals = cells_of([w for w in words
                                 if top - 0.5 <= (w["top"] + w["bottom"]) / 2 < bottom + 0.5])
                if not vals or vals[0] == names[0] or str(vals[1]).startswith(names[1]):
                    continue
                if not re.fullmatch(r"\d{1,4}", str(vals[0])):
                    continue
                if cur and any(cur.get(n) for n in names[1:]):
                    rows.append(cur)
                cur = dict(zip(names, vals))
                cur["段内奖项等级"] = grade_now
            state = grade_now
    if cur and any(cur.get(n) for n in names[1:]):
        rows.append(cur)
    return rows


def parse_by_adapter(content: bytes, adapter: str, spec: dict) -> List[dict]:
    """按 adapter 名分发到具体解析器（CLI 与维护脚本共用，别各写一份 if）。"""
    name = (adapter or "").lower()
    if name in ("xlsx_sheet", "xlsx"):
        return parse_xlsx(content, spec)
    if name in ("docx_table", "docx"):
        return parse_docx(content, spec)
    if name in ("pdf_table", "pdf"):
        return parse_pdf(content, spec)
    if name in ("pdf_dual_border", "pdf_dual"):
        return parse_pdf_dual_border(content, spec)
    if name in ("html_table", "html"):
        return parse_html_table(content, spec)
    raise ValueError(f"未知 adapter：{adapter!r}")


def prepare_records(raw_rows: Sequence[dict], spec: dict,
                    source: Optional[dict] = None) -> List[dict]:
    """适配器裸行 → 可直接入库的 awards 候选（归一 → 合并 → 收尾）。

    对外只暴露这一个函数：单测与 CLI 都走它，避免「忘了调 _finalize」导致
    summary 缺失或表头行漏删。
    """
    records = normalize_rows(raw_rows, spec, source or {})
    if (spec or {}).get("group_by"):
        before = len(records)
        records = _apply_grouping(records, spec)
        if len(records) != before:
            log.info("一队多行合并：%d → %d 条", before, len(records))
    return _finalize(records, spec)


def _norm_key(text: Optional[str]) -> str:
    return re.sub(r"[\s\u3000（）()【】\[\]·,，]", "", text or "").lower()


def content_hash(competition_id: int, rec: dict) -> str:
    """稳定去重键：赛事 + 届次 + 年份 + 学校 + 作品名 + 奖项 + 赛道。

    与大创历史数据同一口径（同校同名同奖项视为同一条，跨赛道因 track 不同而各自保留）。
    """
    parts = [
        str(int(competition_id)),
        _norm_key(rec.get("session")),
        str(rec.get("year") or ""),
        _norm_key(rec.get("school")),
        _norm_key(rec.get("title")),
        _norm_key(rec.get("award_grade")),
        _norm_key(rec.get("track")),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _to_award_payload(competition_id: int, rec: dict, source: dict, *,
                      status: str, confidence: float) -> dict:
    """归一化记录 → db.create_award 的 payload。"""
    year = rec.get("year")
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    return {
        "competition_id": int(competition_id),
        "title": rec.get("title") or "",
        "team": rec.get("school") or None,
        "award_grade": rec.get("award_grade") or None,
        "year": year,
        "session": rec.get("session") or None,
        "track": rec.get("track") or None,
        "category": rec.get("category") or None,
        "advisor": rec.get("advisor") or None,
        "summary": rec.get("summary") or None,
        "source_url": source.get("url"),
        "source_type": "crawl",
        "confidence": confidence,
        "review_status": status,
        "content_hash": content_hash(competition_id, rec),
    }


# =============================================================================
# 五、单来源执行
# =============================================================================
def _spec_of(source: dict) -> dict:
    spec = source.get("parse_spec_obj")
    if isinstance(spec, dict):
        return spec
    try:
        return json.loads(source.get("parse_spec") or "{}")
    except (TypeError, ValueError):
        return {}


def ingest_source(source: dict, *, apply: bool = False, approve: bool = False,
                  limit: Optional[int] = None, print_rows: int = 0,
                  timeout: int = DEFAULT_TIMEOUT,
                  db_path: Optional[str] = None) -> dict:
    """跑一个来源：抓 → 解 → 归一 → 去重 → 写库。返回统计字典。"""
    spec = _spec_of(source)
    cid = int(source["competition_id"])
    stats = {
        "source_id": source.get("id"), "competition_id": cid,
        "url": source["url"], "adapter": source.get("adapter"),
        "format": None, "parsed": 0, "dedup": 0, "inserted": 0,
        "skipped": 0, "status": "failed", "error": None,
    }
    try:
        # parse_spec.skip 非空 = 这个来源已知「不公示作品名」，只公示获奖者名单。
        # 产品口径（2026-11-23）：名单型记录不作为作品入库（对 AI 解析毫无价值），
        # 保留来源行是为了留下「已核查过、确实没有作品名」的证据，别反复重抓。
        skip_reason = str(spec.get("skip") or "").strip()
        if skip_reason:
            log.info("跳过（名单型，无作品名）：%s", skip_reason)
            stats["status"] = "skipped"
            stats["error"] = skip_reason
            if apply:
                db.mark_award_source_fetch(source["id"], "skipped", rows=0,
                                           error=skip_reason, db_path=db_path)
            return stats

        # 分页来源（parse_spec.urls）：逐页抓、逐页解析，最后合并成一批候选行
        paged = bool(spec.get("urls")
                     or (spec.get("url_template") and int(spec.get("page_count") or 0)))
        if paged:
            pages = fetch_paged(source, timeout=timeout)
            favg = detect_format(pages[0][0], "", pages[0][1]) if pages else "html"
            stats["format"] = favg
            log.info("分页来源：%d 页，判定格式 %s", len(pages), favg)
            raw = []
            track_map = spec.get("track_by_url") or {}
            for page_url, page_bytes in pages:
                page_fmt = detect_format(page_url, "", page_bytes)
                pg_spec = dict(spec)
                pg_spec.setdefault("_page_url", page_url)
                page_rows = parse_by_adapter(page_bytes, source.get("adapter") or "", pg_spec)
                # 分页名单常按类目分页（大广赛 19 个类目）：URL 里能看出类目的，
                # 就按 track_by_url 打上 track，不用抽同一列
                for frag, track in track_map.items():
                    if frag in page_url:
                        for row in page_rows:
                            row.setdefault("track", track)
                        break
                raw.extend(page_rows)
            stats["parsed"] = len(raw)
        else:
            content, final_url, ctype = fetch_source(source, timeout=timeout)
            fmt = detect_format(final_url or source["url"], ctype, content)
            stats["format"] = fmt
            log.info("抓到 %d 字节，判定格式 %s（%s）", len(content), fmt, final_url)

            adapter = (source.get("adapter") or "").lower()
            raw = parse_by_adapter(content, adapter, spec)
            stats["parsed"] = len(raw)
        records = prepare_records(raw, spec, source)
        if records:
            from collections import Counter
            grades = Counter(r.get("award_grade") or "(无)" for r in records)
            log.info("奖项分布：%s", "，".join(f"{k} {v}" for k, v in grades.most_common(6)))

        dedupe_key = spec.get("dedupe_key") or ["title", "school", "award_grade"]
        seen, unique = set(), []
        for rec in records:
            key = tuple(_norm_key(rec.get(k)) for k in dedupe_key)
            if key in seen:
                stats["dedup"] += 1
                continue
            seen.add(key)
            unique.append(rec)
        if limit:
            unique = unique[:limit]

        for rec in unique[:max(print_rows, 0)]:
            print("  · " + json.dumps(
                {k: rec.get(k) for k in ("title", "school", "award_grade", "year",
                                          "session", "track", "summary")},
                ensure_ascii=False)[:300])
        if print_rows:
            print(f"  （共 {len(unique)} 条候选，仅打印前 {min(print_rows, len(unique))} 条）")

        status = "approved" if approve else "pending"
        if not apply:
            stats.update(status="dry_ok", skipped=len(unique))
            if source.get("id"):
                db.mark_award_source_fetch(source["id"], "dry_ok", rows=len(unique),
                                            db_path=db_path)
            return stats

        import sqlite3
        for rec in unique:
            payload = _to_award_payload(cid, rec, source, status=status, confidence=1.0)
            if not payload["title"]:
                stats["skipped"] += 1
                continue
            try:
                db.create_award(payload)
                stats["inserted"] += 1
            except sqlite3.IntegrityError:
                stats["skipped"] += 1
            except Exception as exc:  # noqa: BLE001 —— 单行失败不拖垮整批
                stats["skipped"] += 1
                log.warning("单行写入失败（已跳过）：%s｜%s", exc, payload.get("title"))

        stats["status"] = "ok"
        if source.get("id"):
            db.mark_award_source_fetch(source["id"], "ok", rows=stats["inserted"],
                                        db_path=db_path)
        return stats
    except Exception as exc:  # noqa: BLE001 —— 来源级失败：记录原文，继续下一个
        stats["error"] = f"{type(exc).__name__}: {exc}"
        stats["status"] = "failed"
        log.warning("来源失败 [%s] %s → %s", cid, source["url"], stats["error"])
        if source.get("id"):
            try:
                db.mark_award_source_fetch(source["id"], "failed", rows=0,
                                            error=stats["error"], db_path=db_path)
            except Exception:  # noqa: BLE001
                pass
        return stats


# =============================================================================
# 六、CLI
# =============================================================================
def _load_seed(path: str) -> List[dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("sources") or []
    return list(data)


def _print_source_table(sources: Sequence[dict]) -> None:
    if not sources:
        print("（没有登记任何来源）")
        return
    print(f"{'id':>4} {'cid':>4} {'adapter':<12} {'status':<8} {'rows':>6}  url")
    for s in sources:
        print(f"{s['id']:>4} {s['competition_id']:>4} {s['adapter']:<12} "
              f"{(s.get('last_status') or '-'):<8} {s.get('last_rows') or 0:>6}  "
              f"{s['url'][:100]}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="获奖名单抓取与入库（award_sources 声明式适配器）")
    p.add_argument("--db", default=None, help="指定数据库路径（默认用 db.DB_PATH）")
    p.add_argument("--list", action="store_true", help="列出已登记的来源")
    p.add_argument("--seed", nargs="?", const=SEED_PATH, default=None,
                   help=f"从 seed JSON 注册来源（默认 {os.path.basename(SEED_PATH)}）")
    p.add_argument("--source-id", type=int, action="append", default=[],
                   help="要跑的来源 id，可重复")
    p.add_argument("--competition-id", type=int, default=None,
                   help="配合 --all：只跑这个赛事的来源")
    p.add_argument("--all", action="store_true", help="跑所有 enabled 来源")
    p.add_argument("--apply", action="store_true", help="真正写库（默认只演练）")
    p.add_argument("--approve", action="store_true",
                   help="入库时直接 review_status='approved'（默认 pending）")
    p.add_argument("--limit", type=int, default=None, help="每个来源最多入多少条")
    p.add_argument("--print-rows", type=int, default=0, help="打印前 N 条解析结果")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单次抓取超时秒数")
    p.add_argument("--no-backup", action="store_true",
                   help="--apply 时不自动备份数据库（默认备份）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出统计")
    p.add_argument("--add", action="store_true",
                   help="临时登记一条来源（需 --competition-id --url --adapter）")
    p.add_argument("--url", default=None)
    p.add_argument("--adapter", default=None, help="html_table / xlsx_sheet / docx_table / pdf_table")
    p.add_argument("--title", default=None)
    p.add_argument("--years", default=None)
    p.add_argument("--spec-file", default=None, help="parse_spec 的 JSON 文件")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    if args.db:
        db.DB_PATH = args.db

    db.ensure_award_sources()

    if args.seed:
        entries = _load_seed(args.seed)
        for entry in entries:
            sid = db.upsert_award_source(entry)
            log.info("登记 cid=%s → id=%s（%s）",
                     entry.get("competition_id"), sid, entry.get("adapter"))

    if args.add:
        if not (args.competition_id and args.url and args.adapter):
            print("--add 需要 --competition-id --url --adapter", file=sys.stderr)
            return 2
        spec = {}
        if args.spec_file:
            with open(args.spec_file, encoding="utf-8") as fh:
                spec = json.load(fh)
        db.upsert_award_source({
            "competition_id": args.competition_id,
            "url": args.url,
            "title": args.title,
            "years": args.years,
            "adapter": args.adapter,
            "parse_spec": spec,
        })
        args.source_id.append(max(s["id"] for s in db.list_award_sources(
            args.competition_id)))

    if args.list:
        _print_source_table(db.list_award_sources(args.competition_id))
        return 0

    sources: List[dict] = []
    if args.source_id:
        for sid in args.source_id:
            src = db.get_award_source(sid)
            if src is None:
                print(f"来源 id={sid} 不存在", file=sys.stderr)
                return 2
            sources.append(src)
    elif args.all:
        sources = db.list_award_sources(args.competition_id, enabled_only=True)
    if not sources:
        if not args.seed:
            print("没有要跑的来源：用 --list 看已登记，或 --all / --source-id",
                  file=sys.stderr)
        return 0

    if args.apply and not args.no_backup:
        try:
            import backup_db
            info = backup_db.make_backup(db.DB_PATH, os.path.join(
                os.path.dirname(os.path.abspath(db.DB_PATH)), "backups"),
                label="award_ingest")
            log.info("写库前已备份：%s", info.get("path") or info)
        except Exception as exc:  # noqa: BLE001
            log.warning("自动备份失败（继续执行）：%s", exc)

    results = []
    for src in sources:
        log.info("=== cid=%s id=%s %s", src["competition_id"], src.get("id"), src["url"])
        results.append(ingest_source(
            src, apply=args.apply, approve=args.approve, limit=args.limit,
            print_rows=args.print_rows, timeout=args.timeout,
        ))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'cid':>4} {'id':>4} {'format':<7} {'解析':>6} {'去重':>5} "
              f"{'写入':>6} {'跳过':>5}  状态")
        for r in results:
            print(f"{r['competition_id']:>4} {r.get('source_id') or 0:>4} "
                  f"{str(r.get('format') or '-'):<7} {r['parsed']:>6} {r['dedup']:>5} "
                  f"{r['inserted']:>6} {r['skipped']:>5}  {r['status']}"
                  + (f"  ← {r['error']}" if r.get("error") else ""))

    failed = [r for r in results if r["status"] == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
