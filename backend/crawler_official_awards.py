"""官网获奖名单定向爬虫（P1）。

当前实现的源：
  * lanqiao  蓝桥杯（dasai.lanqiao.cn / guoxinlanqiao.com 与 x-static.lanqiao.cn CDN）

流水线：
  列表 API → 详情 HTML → 附件（rar/zip/pdf）下载 → 解包 → pdfplumber 抽表
  → 规范化（学校/姓名/赛项/奖项）→ awards 表（content_hash 去重）

用法：
  # 只看列表，不下载
  python crawler_official_awards.py --list-only

  # dry-run（下载 + 解析 + 打印，不写库）
  python crawler_official_awards.py --only-news 1951 --print-rows 5

  # 入库
  python crawler_official_awards.py --only-news 1951 --apply

依赖：pdfplumber；RAR 解包优先用 Windows 自带 bsdtar（tar.exe），其次 7z/WinRAR，最后 rarfile。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "competitions.db"
CACHE_DIR = ROOT / "workspace" / "cache_official_awards"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 60
POLITE_SLEEP = 1.0  # 限速：每次请求之间至少 1s

LANQIAO_LIST_API = "https://www.guoxinlanqiao.com/api/news/find"
LANQIAO_DETAIL_API = "https://www.guoxinlanqiao.com/api/web/news/selectone"
LANQIAO_PROGID = 20
LANQIAO_HUMAN_URL = "https://dasai.lanqiao.cn/notices/detail/?nnid={nnid}"

# 蓝桥杯在库里的赛事：全国总决赛 / 省赛
LANQIAO_COMPETITION_HINTS = {
    "national": "蓝桥杯全国软件和信息技术专业人才大赛",
    "provincial": "“蓝桥杯”全国软件大赛·省赛",
}

# ---------------------------------------------------------------------------
# 【已废弃 2026-11-14】蓝桥杯个人获奖名单抓取
#
# 蓝桥杯是纯算法竞技赛，官网只公示「姓名（组别）」式个人名单，没有项目作品。
# 这些名单对备赛（刷题路线、真题解析）没有业务价值，平台也不再展示
# （competitions.has_project_awards = 0 时隐藏「往年获奖作品」「灵感工作台」，
#  /api/competitions/{id}/awards 直接返回空）。
#
# 因此本文件中 lanqiao_* 相关解析逻辑整体作废：保留代码只是为了留档与万一
# 需要回溯抓取规则，正常运行一律直接退出，不再写库。详见 CRAWLER_HANDOVER.md §0.6。
# ---------------------------------------------------------------------------
LANQIAO_DEPRECATED = True
LANQIAO_DEPRECATED_REASON = (
    "蓝桥杯等纯算法赛只公示个人获奖名单、无项目作品，名单对备赛无价值；"
    "该抓取逻辑已于 2026-11-14 废弃，库内历史名单亦已物理删除。"
)

AWARD_GRADES = ("特等奖", "一等奖", "二等奖", "三等奖", "优秀奖", "优胜奖", "金奖", "银奖", "铜奖")
SCHOOL_HINTS = ("大学", "学院", "学校", "职院", "中学", "研究院", "职业技术", "分校")
CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 与「选手个人赛」无关的获奖名单（教师奖/组织奖/青少组/作品类赛事），默认跳过
EXCLUDE_TOKENS = (
    "教师",
    "组织单位",
    "组织奖",
    "示范单位",
    "优秀学校",
    "征集",
    "论文",
    "青少",
    "少儿",
    "STEMA",
    "国际赛",
    "中数杯",
    "元境杯",
    "金融科技",
    "网络安全春秋",
    "智能装备",
    "5G全网",
    "AI办公",
    "智能体",
    "视觉艺术",
    "设计赛",
    "最具创意",
)


# --------------------------------------------------------------------------- 基础工具
# --------------------------------------------------------------------------- 大创（全国大学生创业服务网）
# 官网：https://cy.ncss.cn —— 获奖名单在「大赛动态」列表（SSR HTML 片段接口）里，
# 正文挂 /picture?path=xxx&name=yyy 形式的附件：2025 为单个 xlsx（5 个赛道分 sheet），
# 2024/2023 为按赛道拆分的 docx 表格，教育部最终公布版常为 PDF（暂不解析）。
NCSS_BASE = "https://cy.ncss.cn"
NCSS_LIST_API = NCSS_BASE + "/information/dsdtlist"
NCSS_TYPE_CODE = "CYDS_2TH_DSDT"
NCSS_COMPETITION_NAME = "中国国际大学生创新大赛"
NCSS_TITLE_HINTS = ("中国国际大学生创新大赛", "互联网+")
NCSS_AWARD_TITLE_RE = re.compile(r"(拟获奖项目名单|获奖项目名单|获奖名单)")
NCSS_GRADES = ("冠军", "亚军", "季军", "创新潜力奖", "金奖", "银奖", "铜奖")
NCSS_TRACK_HINTS = (
    ("高教", "高教主赛道"),
    ("红色筑梦", "青年红色筑梦之旅赛道"),
    ("职教", "职教赛道"),
    ("产业", "产业赛道"),
    ("萌芽", "萌芽赛道"),
)
NCSS_SKIP_ATTACHMENT_TOKENS = (
    "优秀组织奖",
    "集体奖",
    "信息核对",
    "操作手册",
    "评审规则",
)
NCSS_HEADER_FIELDS = (
    ("title", ("参赛项目", "参赛命题", "项目名称")),
    ("province", ("省（区、市）", "省份", "省区市", "省")),
    ("school", ("学校",)),
    ("leader", ("负责人",)),
    ("members", ("参赛队员", "团队成员", "队员")),
    ("advisors", ("指导教师", "指导老师")),
    ("enterprise", ("命题企业",)),
)
NCSS_LIST_RE = re.compile(
    r'<a href="(/information/[0-9a-f]{20,})"[^>]*>(.*?)</a>.*?发布日期：<span>([\d-]+)</span>',
    re.S,
)
NCSS_SECTION_RE = re.compile(r"^[一二三四五六七八九十]+、")


def log(msg: str) -> None:
    print(msg, flush=True)


def cn_number(text: str) -> int | None:
    """把「十七」「二十」「十一」这类中文数字转成 int。"""
    if not text:
        return None
    text = text.strip()
    if text.isdigit():
        return int(text)
    if "十" not in text:
        return CN_DIGITS.get(text[0]) if len(text) == 1 else None
    head, _, tail = text.partition("十")
    tens = CN_DIGITS.get(head, 1) if head else 1
    ones = CN_DIGITS.get(tail, 0) if tail else 0
    return tens * 10 + ones


def digest(*parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    return session


def http_get(
    session: requests.Session,
    url: str,
    *,
    params: dict | None = None,
    retries: int = 3,
) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            time.sleep(POLITE_SLEEP)
            return resp
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                wait = attempt * 2
                log(f"  [retry {attempt}/{retries - 1}] {url} -> {exc}，{wait}s 后重试")
                time.sleep(wait)
    raise RuntimeError(f"请求失败：{url}（{last}）")


# --------------------------------------------------------------------------- 蓝桥杯：列表 / 详情
def lanqiao_list_news(
    session: requests.Session,
    *,
    keyword: str = "获奖名单",
    page_size: int = 100,
    max_pages: int = 5,
) -> list[dict]:
    """按标题过滤拉取蓝桥杯新闻列表。"""
    items: list[dict] = []
    total = None
    for page in range(1, max_pages + 1):
        resp = http_get(
            session,
            LANQIAO_LIST_API,
            params={
                "progid": LANQIAO_PROGID,
                "pageno": page,
                "pagesize": page_size,
                "status": 1,
                "title": keyword,
            },
        )
        payload = resp.json()
        data = payload.get("datalist") or []
        total = payload.get("total", total)
        items.extend(data)
        log(f"  列表 p{page}: 本页 {len(data)} 条，累计 {len(items)}/{total}")
        if not data or (total is not None and len(items) >= int(total)):
            break
    return items


def lanqiao_detail(session: requests.Session, nnid: int) -> dict:
    resp = http_get(session, LANQIAO_DETAIL_API, params={"nnid": nnid})
    payload = resp.json()
    news = payload.get("news") or {}
    if not news:
        raise RuntimeError(f"详情为空：nnid={nnid}")
    return news


def extract_attachments(content_html: str) -> list[dict]:
    """从详情正文 HTML 里找附件链接。"""
    soup = BeautifulSoup(content_html or "", "html.parser")
    found: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not re.search(r"\.(rar|zip|pdf|7z|xlsx?|docx?)(\?|$)", href, re.IGNORECASE):
            continue
        name = (a.get("title") or a.get_text(" ", strip=True) or Path(href).name).strip()
        found.append({"name": name, "url": href})
    return found


# --------------------------------------------------------------------------- 下载 / 解包
def safe_dirname(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", name)[:80] or "item"


def download_file(session: requests.Session, url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        log(f"  [cache] {dest.name} ({dest.stat().st_size / 1024 / 1024:.2f} MB)")
        return dest
    resp = http_get(session, url)
    dest.write_bytes(resp.content)
    log(f"  [下载] {dest.name} ({len(resp.content) / 1024 / 1024:.2f} MB)")
    return dest


def _run(cmd: Sequence[str], cwd: Path | None = None) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0, (proc.stderr or b"").decode("utf-8", "ignore")[:400]
    except FileNotFoundError:
        return False, "not found"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def extract_archive(archive: Path, outdir: Path) -> list[Path]:
    """解压 rar/zip/7z，返回其中的全部文件。优先 bsdtar（Windows 自带，能读 rar/zip）。"""
    outdir.mkdir(parents=True, exist_ok=True)
    if any(outdir.iterdir()):
        return sorted(p for p in outdir.rglob("*") if p.is_file())

    ok, err = _run(["tar", "-xf", str(archive), "-C", str(outdir)])
    if ok:
        log(f"  [解包] bsdtar 完成：{archive.name}")
    else:
        log(f"  [解包] bsdtar 失败（{err}），尝试 7z / WinRAR / rarfile")
        for exe in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe", "7z"):
            if shutil.which(exe) or os.path.exists(exe):
                ok, err = _run([exe, "x", "-y", f"-o{outdir}", str(archive)])
                if ok:
                    break
        if not ok:
            for exe in (r"C:\Program Files\WinRAR\UnRAR.exe", r"C:\Program Files\WinRAR\WinRAR.exe", "unrar"):
                if shutil.which(exe) or os.path.exists(exe):
                    ok, err = _run([exe, "x", "-y", str(archive), str(outdir) + os.sep])
                    if ok:
                        break
        if not ok:
            try:  # 最后兜底：rarfile + 外部 unrar 后端
                import rarfile  # type: ignore

                with rarfile.RarFile(archive) as rf:
                    rf.extractall(outdir)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"无法解压 {archive.name}：{exc}\n"
                    "请安装 7-Zip（https://www.7-zip.org/）或 WinRAR，"
                    "或 pip install rarfile 并配置可用的 unrar。"
                ) from exc

    files = sorted(p for p in outdir.rglob("*") if p.is_file())
    nested: list[Path] = []
    for path in files:
        if path.suffix.lower() in {".zip", ".7z"}:
            nested.extend(extract_archive(path, outdir / (path.stem + "_x")))
    return files + nested


# --------------------------------------------------------------------------- PDF 解析
ROW_RE = re.compile(
    r"^(?P<zk>\d{5,})\s+(?P<school>\S.*?)\s+(?P<name>[\u4e00-\u9fff·]{2,10})\s+"
    r"(?P<subject>.+?)\s+(?P<grade>" + "|".join(AWARD_GRADES) + r")$"
)
HEADER_CELLS = {
    "序号",
    "奖项",
    "获奖等级",
    "奖项等级",
    "准考证号",
    "学校名称",
    "学校",
    "选手姓名",
    "姓名",
    "科目名称",
    "赛项",
    "赛道",
}

# 表头关键词 → 标准字段（国家级表无「省份/是否进入决赛」，省赛表有，按表头动态映射）
HEADER_FIELDS = (
    ("province", ("省份", "赛区", "地区")),
    ("zk", ("准考证号", "报名号", "考号", "编号")),
    ("school", ("学校名称", "学校", "院校")),
    ("name", ("选手姓名", "姓名", "参赛选手", "参赛者")),
    ("subject", ("科目名称", "赛项名称", "科目", "赛项", "组别", "赛道")),
    ("grade", ("奖项", "获奖等级", "奖项等级", "等级")),
)


def _header_map(cells: Sequence[str]) -> dict | None:
    """识别表头行并建立 字段 → 列号 映射。"""
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(cells or []):
        text = re.sub(r"\s+", "", cell or "")
        if not text:
            continue
        for field, keys in HEADER_FIELDS:
            if field in mapping:
                continue
            if any(key in text for key in keys):
                mapping[field] = idx
                break
    return mapping if {"school", "name", "grade"} <= set(mapping) else None


def _row_from_map(cells: Sequence[str], mapping: dict) -> dict | None:
    def get(field: str) -> str:
        idx = mapping.get(field)
        if idx is None or idx >= len(cells or []):
            return ""
        return re.sub(r"\s+", "", (cells[idx] or "").strip())

    grade = _grade_of(get("grade"))
    name = get("name")
    school = get("school")
    if not grade or not name or not school:
        return None
    return {
        "zk": get("zk"),
        "school": school,
        "name": name,
        "subject": get("subject"),
        "grade": grade,
        "province": get("province"),
    }


def _grade_of(cell: str) -> str | None:
    cell = (cell or "").strip()
    for grade in AWARD_GRADES:
        if grade in cell:
            return grade
    return None


def _looks_school(cell: str) -> bool:
    return any(hint in (cell or "") for hint in SCHOOL_HINTS)


def _classify_cells(cells: Sequence[str]) -> dict | None:
    """把不定列序的一行单元格归到 学校/姓名/科目/奖项；表头行、残行返回 None。"""
    values = [(c or "").strip() for c in cells]
    grade = _grade_of(next((v for v in values if _grade_of(v)), ""))
    if not grade:
        return None

    rest: list[str] = []
    for value in values:
        if not value or value in HEADER_CELLS:
            continue
        if grade in value:  # 奖项单元格本身
            continue
        rest.append(value)

    zk = next((v for v in rest if re.fullmatch(r"\d{5,}", v)), "")
    rest = [v for v in rest if v != zk]
    school = next((v for v in rest if _looks_school(v)), "")
    name = next(
        (v for v in rest if v != school and re.fullmatch(r"[\u4e00-\u9fff·]{2,10}", v)),
        "",
    )
    subject = next((v for v in rest if v not in {school, name}), "")
    if not name or not school:
        return None
    return {"zk": zk, "school": school, "name": name, "subject": subject, "grade": grade}


def pdf_rows(pdf_path: Path, stats: dict | None = None) -> Iterator[dict]:
    """逐页抽表；有表头时按表头映射列，无表格线的页退化为按行正则。"""
    import pdfplumber

    stats = stats if stats is not None else {}
    with pdfplumber.open(str(pdf_path)) as doc:
        mapping: dict | None = None
        for page_no, page in enumerate(doc.pages, start=1):
            got = 0
            try:
                tables = page.extract_tables() or []
            except Exception:  # noqa: BLE001
                tables = []
            for table in tables:
                for cells in table:
                    header = _header_map(cells)
                    if header:
                        mapping = header
                        continue
                    row = _row_from_map(cells, mapping) if mapping else _classify_cells(cells or [])
                    if not row:
                        if mapping:
                            stats["dropped"] = stats.get("dropped", 0) + 1
                        continue
                    row["page"] = page_no
                    got += 1
                    yield row
            if got:
                continue
            for line in (page.extract_text() or "").splitlines():
                match = ROW_RE.match(line.strip())
                if not match:
                    continue
                yield {
                    "zk": match.group("zk"),
                    "school": match.group("school").strip(),
                    "name": match.group("name").strip(),
                    "subject": match.group("subject").strip(),
                    "grade": match.group("grade"),
                    "province": "",
                    "page": page_no,
                }


def pdf_track(pdf_path: Path) -> str:
    """从文件名里取赛项名，如「软件赛-C/C++程序设计大学A组」。"""
    stem = pdf_path.stem
    stem = re.sub(r"获奖名单|名单|全国总决赛|总决赛", "", stem)
    stem = stem.replace("_", "-").replace("CC++", "C/C++").strip(" -")
    return stem or pdf_path.stem


# --------------------------------------------------------------------------- 规范化 → awards
def resolve_competition(con, level: str) -> dict | None:
    hint = LANQIAO_COMPETITION_HINTS.get(level) or LANQIAO_COMPETITION_HINTS["national"]
    row = con.execute(
        "select id, name, category from competitions where name = ?", (hint,)
    ).fetchone()
    if row:
        return dict(row)
    rows = con.execute(
        "select id, name, category from competitions where name like ? order by id", ("%蓝桥杯%",)
    ).fetchall()
    for candidate in rows:
        cand = dict(candidate)
        if level == "provincial" and "省赛" in cand["name"]:
            return cand
        if level == "national" and "省赛" not in cand["name"]:
            return cand
    return dict(rows[0]) if rows else None


def build_awards(
    news: dict,
    pdf_path: Path,
    *,
    competition: dict,
    level: str,
    session_label: str,
    year: int,
    source_url: str,
    stats: dict | None = None,
) -> list[dict]:
    track = pdf_track(pdf_path)
    rows: list[dict] = []
    for row in pdf_rows(pdf_path, stats):
        name, school, grade = row["name"], row["school"], row["grade"]
        subject = row["subject"] or track
        summary = f"准考证号 {row['zk'] or '—'}；科目：{subject}"
        if row.get("province"):
            summary += f"；赛区：{row['province']}"
        summary += f"；赛事：{news.get('title', '')}"
        rows.append(
            {
                "competition_id": competition["id"],
                "competition_raw": competition["name"],
                "category": competition.get("category"),
                "level": "national" if level == "national" else "provincial",
                "year": year,
                "session": session_label,
                "title": f"{name}（{subject}）",
                "team": school,
                "award_grade": grade,
                "track": subject,
                "summary": summary,
                "source_url": source_url,
                "source_type": "crawl",
                "confidence": 1.0,
                "review_status": "approved",
                "content_hash": digest(
                    competition["id"], session_label, year, school, name, subject, grade
                ),
            }
        )
    return rows


INSERT_COLUMNS = (
    "competition_id",
    "competition_raw",
    "category",
    "level",
    "year",
    "session",
    "title",
    "team",
    "award_grade",
    "track",
    "summary",
    "source_url",
    "source_type",
    "confidence",
    "review_status",
    "content_hash",
    "advisor",
)


def insert_awards(con, rows: Sequence[dict]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    before = con.total_changes
    sql = (
        f"INSERT OR IGNORE INTO awards ({', '.join(INSERT_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in INSERT_COLUMNS)})"
    )
    con.executemany(sql, [tuple(r.get(c) for c in INSERT_COLUMNS) for r in rows])
    con.commit()
    inserted = con.total_changes - before
    return inserted, len(rows) - inserted


# --------------------------------------------------------------------------- 主流程
def keep_news(title: str) -> bool:
    """只保留选手个人赛（软件赛/电子赛/个人赛）的获奖名单。"""
    if not title:
        return False
    if any(token in title for token in EXCLUDE_TOKENS):
        return False
    return any(token in title for token in ("软件赛", "电子赛", "个人赛", "总决赛", "省赛"))


def crawl_lanqiao(args) -> list[dict]:
    import sqlite3

    if LANQIAO_DEPRECATED:
        log(f"[废弃] 蓝桥杯个人名单抓取已停用：{LANQIAO_DEPRECATED_REASON}")
        log("[废弃] 如需重启，请先修改 crawler_official_awards.py 的 LANQIAO_DEPRECATED，并同步更新 CRAWLER_HANDOVER.md")
        return []

    session = make_session()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    log(f"[蓝桥杯] 关键词「{args.keyword}」")
    news_items = lanqiao_list_news(
        session, keyword=args.keyword, page_size=args.page_size, max_pages=args.max_pages
    )
    if args.topic == "individual":
        before = len(news_items)
        news_items = [n for n in news_items if keep_news(n.get("title", ""))]
        log(f"[蓝桥杯] 个人赛过滤：保留 {len(news_items)}/{before} 条（教师奖/组织奖/青少组/作品类已跳过）")
    if args.only_news:
        only = set(args.only_news)
        news_items = [n for n in news_items if int(n.get("nnid", 0)) in only]
    if args.limit_news:
        news_items = news_items[: args.limit_news]
    log(f"[蓝桥杯] 待处理 {len(news_items)} 条获奖名单")

    if args.list_only:
        for item in news_items:
            log(f"  nnid={item.get('nnid')}   {str(item.get('publishTime', ''))[:10]}   {item.get('title')}")
        return []

    all_rows: list[dict] = []
    for item in news_items:
        nnid = int(item["nnid"])
        title = item.get("title", "")
        published = str(item.get("publishTime") or "")[:10]
        year = int(published[:4]) if published[:4].isdigit() else 0
        edition_match = re.search(r"第([一二三四五六七八九十]+)届", title)
        edition = cn_number(edition_match.group(1)) if edition_match else None
        session_label = f"第{edition}届" if edition else ""

        detail = lanqiao_detail(session, nnid)
        attachments = extract_attachments(detail.get("content", ""))
        log(f"\n[第 {nnid} 条] {detail.get('title', title)}（{published}，附件 {len(attachments)} 个）")
        if not attachments:
            log("  [跳过] 正文里没有附件链接")
            continue

        news_level = "provincial" if "省赛" in title else "national"
        competition = resolve_competition(con, news_level)
        if not competition:
            log(f"  [跳过] 库里找不到蓝桥杯赛事（{news_level}）")
            continue

        work_dir = CACHE_DIR / f"lanqiao_{nnid}"
        for att in attachments:
            url_path = urlsplit(att["url"]).path  # OSS 直链带签名 query，取扩展名要先剥掉
            ext = Path(url_path).suffix.lower()
            target = work_dir / f"{safe_dirname(att['name'])}{ext}"
            try:
                archive = download_file(session, att["url"], target)
            except Exception as exc:  # noqa: BLE001
                log(f"  [下载失败] {att['name']}: {exc}")
                continue
            if ext in {".rar", ".zip", ".7z"}:
                try:
                    files = extract_archive(archive, work_dir / (safe_dirname(archive.stem) + "_x"))
                except Exception as exc:  # noqa: BLE001
                    log(f"  [解包失败] {archive.name}: {exc}")
                    continue
            else:
                files = [archive]

            pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
            if args.max_pdfs:
                pdfs = pdfs[: args.max_pdfs]
            log(f"  [解包结果] {len(files)} 个文件，其中 PDF {len(pdfs)} 个")
            for pdf in pdfs:
                stats: dict = {}
                try:
                    rows = build_awards(
                        detail,
                        pdf,
                        competition=competition,
                        level=news_level,
                        session_label=session_label,
                        year=year,
                        source_url=LANQIAO_HUMAN_URL.format(nnid=nnid),
                        stats=stats,
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"  [解析失败] {pdf.name}: {exc}")
                    continue
                dropped = stats.get("dropped", 0)
                suffix = f"（跳过 {dropped} 行换行断行）" if dropped else ""
                log(f"    · {pdf.name} → {len(rows)} 行{suffix}")
                all_rows.extend(rows)

    con.close()
    return all_rows


def ncss_track_name(text: str) -> str:
    for hint, label in NCSS_TRACK_HINTS:
        if hint in (text or ""):
            return label
    return ""


def _ncss_text(cell: Any) -> str:
    # 只压空白、不删空格：英文姓名（CHEN WILLIAM）与「A B 组」这类内容要保持词间距
    return re.sub(r"\s+", " ", str(cell if cell is not None else "")).strip()


def _ncss_dedupe_adjacent(values: Sequence[str]) -> list[str]:
    """docx 合并单元格会让同一段文本在相邻列重复出现；保留首个、其余置空，列位不位移。"""
    out: list[str] = []
    prev = None
    for value in values:
        out.append("" if (value and value == prev) else value)
        prev = value
    return out


def _ncss_grade(text: str) -> str | None:
    for grade in NCSS_GRADES:
        if text.startswith(grade):
            return grade
    return None


def _ncss_header_map(cells: Sequence[str]) -> dict | None:
    mapping: dict[str, int] = {}
    for idx, text in enumerate(cells):
        if not text:
            continue
        for field, keys in NCSS_HEADER_FIELDS:
            if field in mapping:
                continue
            if any(key in text for key in keys):
                mapping[field] = idx
                break
    return mapping if {"title", "school"} <= set(mapping) else None


def _ncss_row(cells: Sequence[str], mapping: dict, grade: str, track: str) -> dict | None:
    def get(field: str) -> str:
        idx = mapping.get(field)
        return cells[idx] if idx is not None and idx < len(cells) else ""

    serial = cells[0] if cells else ""
    title = get("title")
    school = get("school")
    if not title or not school or len(title) < 3:
        return None
    if title in {"参赛项目", "参赛命题", "项目名称"}:
        return None
    if not re.fullmatch(r"[A-Za-z]?\d{1,5}", serial or ""):
        return None
    return {
        "title": title,
        "school": school,
        "province": get("province"),
        "leader": get("leader"),
        "members": get("members"),
        "advisors": get("advisors"),
        "enterprise": get("enterprise"),
        "grade": grade,
        "track": track,
    }


def _ncss_iter_rows(
    rows: Iterator[Sequence[Any]] | Any, track: str, stats: dict | None = None
) -> Iterator[dict]:
    """通用状态机：分节标题 → 奖项等级行 → 表头行 → 数据行（xlsx / docx 表共用）。"""
    grade: str | None = None
    mapping: dict | None = None
    for raw in rows:
        cells = _ncss_dedupe_adjacent([_ncss_text(c) for c in raw])
        nonempty = [c for c in cells if c]
        if not nonempty:
            continue
        if len(set(nonempty)) == 1:
            label = nonempty[0]
            found = _ncss_grade(label)
            if found:
                grade = found
                mapping = None
                continue
            if NCSS_SECTION_RE.match(label) or "参赛项目" in label:
                continue  # 「一、中国大陆项目」「二、国际参赛项目」等分节标题
        header = _ncss_header_map(cells)
        if header:
            mapping = header
            continue
        if not grade or not mapping:
            continue
        row = _ncss_row(cells, mapping, grade, track)
        if row:
            yield row
        elif stats is not None:
            stats["dropped"] = stats.get("dropped", 0) + 1


def ncss_xlsx_rows(path: Path, stats: dict | None = None) -> Iterator[dict]:
    import openpyxl

    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            track = ncss_track_name(sheet.title) or ncss_track_name(path.name)
            yield from _ncss_iter_rows(sheet.iter_rows(values_only=True), track, stats)
    finally:
        workbook.close()


def ncss_docx_rows(path: Path, stats: dict | None = None) -> Iterator[dict]:
    import docx

    document = docx.Document(str(path))
    track = ncss_track_name(path.name)
    for table in document.tables:
        rows = ([cell.text for cell in row.cells] for row in table.rows)
        yield from _ncss_iter_rows(rows, track, stats)


def ncss_list_items(
    session: requests.Session, *, max_pages: int = 8, page_size: int = 50
) -> list[dict]:
    items: list[dict] = []
    for page_index in range(max_pages):
        html = http_get(
            session,
            NCSS_LIST_API,
            params={
                "typeCode": NCSS_TYPE_CODE,
                "title": "",
                "pageIndex": page_index,
                "pageSize": page_size,
            },
        ).text
        found = NCSS_LIST_RE.findall(html or "")
        if not found:
            break
        for href, raw_title, date in found:
            title = BeautifulSoup(raw_title, "html.parser").get_text(" ", strip=True)
            items.append(
                {"href": href, "title": title, "date": date, "url": NCSS_BASE + href}
            )
        log(f"  列表 p{page_index + 1}: {len(found)} 条，累计 {len(items)}")
        if len(found) < page_size:
            break
    return items


def ncss_article_year(title: str, date: str) -> int:
    """「第X届」优先（2015=第一届 → year = 2014 + X），否则取标题里的四位年份。"""
    match = re.search(r"第([一二三四五六七八九十]+)届", title)
    if match:
        edition = cn_number(match.group(1))
        if edition:
            return 2014 + edition
    match = re.search(r"(20\d{2})", title)
    if match:
        return int(match.group(1))
    return int(date[:4]) if date[:4].isdigit() else 0


def ncss_articles(session: requests.Session, *, max_pages: int = 8) -> dict[int, list[dict]]:
    articles: dict[int, list[dict]] = {}
    for item in ncss_list_items(session, max_pages=max_pages):
        title = item["title"]
        if not any(hint in title for hint in NCSS_TITLE_HINTS):
            continue
        if not NCSS_AWARD_TITLE_RE.search(title):
            continue
        year = ncss_article_year(title, item["date"])
        if not year:
            continue
        articles.setdefault(year, []).append(item)
    for year in articles:
        articles[year].sort(key=lambda x: x["date"], reverse=True)
    return articles


def ncss_attachments(html: str, page_url: str) -> list[dict]:
    from urllib.parse import unquote, urljoin

    soup = BeautifulSoup(html or "", "html.parser")
    found: list[dict] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not re.search(
            r"/picture\?path=|\.(pdf|docx?|xlsx?|zip|rar|7z)(\?|$)", href, re.IGNORECASE
        ):
            continue
        query_name = ""
        if "name=" in href:
            query_name = unquote(href.split("name=", 1)[1])
        text = anchor.get_text(" ", strip=True)
        name = query_name or text or Path(urlsplit(href).path).name
        url = urljoin(page_url, href)
        ext = Path(name).suffix.lower() or Path(urlsplit(href).path).suffix.lower()
        found.append({"name": name.strip(), "url": url, "ext": ext})
    return found


def resolve_ncss_competition(con) -> dict | None:
    row = con.execute(
        "select id, name, category from competitions where name = ?",
        (NCSS_COMPETITION_NAME,),
    ).fetchone()
    if row is None:
        row = con.execute(
            "select id, name, category from competitions where name like ? order by id limit 1",
            ("%中国国际大学生创新大赛%",),
        ).fetchone()
    return dict(row) if row else None


def build_ncss_awards(
    rows: Sequence[dict],
    *,
    competition: dict,
    year: int,
    session_label: str,
    source_url: str,
) -> list[dict]:
    awards: list[dict] = []
    for row in rows:
        bits: list[str] = []
        if row.get("track"):
            bits.append(f"赛道：{row['track']}")
        if row.get("province"):
            bits.append(f"省份：{row['province']}")
        if row.get("leader"):
            bits.append(f"负责人：{row['leader']}")
        members = row.get("members") or ""
        if members:
            # 中文名单用「、」，国际项目名单用「,」，两种都要能拆
            count = len([m for m in re.split(r"[、,，;；]", members) if m.strip()])
            shown = members if len(members) <= 60 else members[:60] + "…"
            bits.append(f"成员：{shown}（共 {count} 人）")
        if row.get("advisors"):
            bits.append(f"指导教师：{row['advisors']}")
        if row.get("enterprise"):
            bits.append(f"命题企业：{row['enterprise']}")
        awards.append(
            {
                "competition_id": competition["id"],
                "competition_raw": competition["name"],
                "category": competition.get("category"),
                "level": "national",
                "year": year,
                "session": session_label,
                "title": row["title"],
                "team": row["school"],
                "award_grade": row["grade"],
                "track": row.get("track") or "",
                "advisor": row.get("advisors") or None,
                "summary": "；".join(bits),
                "source_url": source_url,
                "source_type": "crawl",
                "confidence": 1.0,
                "review_status": "approved",
                "content_hash": digest(
                    competition["id"],
                    session_label,
                    year,
                    row["school"],
                    row["title"],
                    row["grade"],
                    row.get("track"),
                ),
            }
        )
    return awards


def _crawl_ncss_article(
    session: requests.Session,
    article: dict,
    *,
    competition: dict,
    year: int,
    session_label: str,
    max_files: int = 0,
) -> list[dict]:
    html = http_get(session, article["url"]).text
    attachments = ncss_attachments(html, article["url"])
    log(f"\n[大创 {year}] {article['title']}")
    log(f"  {article['url']}")
    if not attachments:
        log("  [跳过] 正文里没有附件")
        return []

    parsable = [
        a
        for a in attachments
        if a["ext"] in {".xlsx", ".docx"}
        and not any(token in a["name"] for token in NCSS_SKIP_ATTACHMENT_TOKENS)
    ]
    skipped = [a for a in attachments if a not in parsable]
    log(f"  附件 {len(attachments)} 个：可解析 {len(parsable)}，跳过 {len(skipped)}")
    for item in skipped:
        log(f"  [跳过] {item['name'][:70]}")
    if not parsable:
        return []

    order = {".xlsx": 0, ".docx": 1}
    parsable.sort(key=lambda a: order.get(a["ext"], 9))
    if max_files:
        parsable = parsable[:max_files]

    work_dir = CACHE_DIR / f"ncss_{year}"
    awards: list[dict] = []
    for attachment in parsable:
        stem = safe_dirname(Path(attachment["name"]).stem)
        target = work_dir / f"{stem}{attachment['ext']}"
        try:
            path = download_file(session, attachment["url"], target)
        except Exception as exc:  # noqa: BLE001
            log(f"  [下载失败] {attachment['name']}: {exc}")
            continue
        stats: dict = {}
        try:
            rows = list(
                ncss_xlsx_rows(path, stats)
                if attachment["ext"] == ".xlsx"
                else ncss_docx_rows(path, stats)
            )
        except Exception as exc:  # noqa: BLE001
            log(f"  [解析失败] {path.name}: {exc}")
            continue
        built = build_ncss_awards(
            rows,
            competition=competition,
            year=year,
            session_label=session_label,
            source_url=article["url"],
        )
        tracks: dict[str, int] = {}
        for award in built:
            tracks[award["track"]] = tracks.get(award["track"], 0) + 1
        detail = "，".join(f"{k or '未标注'} {v}" for k, v in tracks.items())
        log(
            f"  · {path.name} → {len(built)} 条"
            f"（跳过 {stats.get('dropped', 0)} 行；{detail}）"
        )
        awards.extend(built)
    return awards


def crawl_ncss(args) -> list[dict]:
    import sqlite3

    session = make_session()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    competition = resolve_ncss_competition(con)
    if not competition:
        log(f"[大创] 库里找不到「{NCSS_COMPETITION_NAME}」，先补赛事再抓")
        con.close()
        return []
    log(f"[大创] 目标赛事 id={competition['id']} {competition['name']}")

    articles = ncss_articles(session, max_pages=args.max_pages)
    years = sorted(articles, reverse=True)
    if args.year:
        years = [y for y in years if y == args.year]
    if args.limit_years:
        years = years[: args.limit_years]
    log(f"[大创] 命中 {len(articles)} 个年份：{sorted(articles, reverse=True)}；本次处理 {years}")

    if args.list_only:
        for year in years:
            for article in articles[year]:
                log(f"  {article['date']} | {year} | {article['title']}")
                log(f"      {article['url']}")
        con.close()
        return []

    all_rows: list[dict] = []
    for year in years:
        session_label = f"第{year - 2014}届" if year > 2014 else str(year)
        for article in articles[year]:
            rows = _crawl_ncss_article(
                session,
                article,
                competition=competition,
                year=year,
                session_label=session_label,
                max_files=args.max_files,
            )
            if rows:
                all_rows.extend(rows)
                break
            log("  [换源] 该文没有解析出数据，尝试同年的下一条")
    con.close()
    return all_rows


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(description="官网获奖名单定向爬虫（蓝桥杯 / 大创）")
    parser.add_argument(
        "--source",
        choices=["lanqiao", "ncss"],
        default="lanqiao",
        help="lanqiao=蓝桥杯（已废弃）；ncss=中国国际大学生创新大赛（cy.ncss.cn）",
    )
    parser.add_argument("--keyword", default="获奖名单", help="列表接口的标题过滤词")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=8, help="列表翻页上限")
    parser.add_argument("--limit-news", type=int, default=0, help="只处理前 N 条获奖名单（0=全部）")
    parser.add_argument("--only-news", type=int, nargs="*", default=None, help="只处理指定 nnid")
    parser.add_argument(
        "--topic",
        choices=["individual", "all"],
        default="individual",
        help="individual=只抓选手个人赛（默认）；all=不过滤（含教师/组织/青少组）",
    )
    parser.add_argument("--max-pdfs", type=int, default=0, help="每个附件最多解析 N 个 PDF（0=全部，调试用）")
    parser.add_argument("--year", type=int, default=0, help="大创：只抓指定年份（0=全部）")
    parser.add_argument("--limit-years", type=int, default=0, help="大创：最多处理 N 个年份（0=全部）")
    parser.add_argument("--max-files", type=int, default=0, help="大创：每个文章最多解析 N 个附件（0=全部）")
    parser.add_argument("--list-only", action="store_true", help="只打印列表，不下载")
    parser.add_argument("--print-rows", type=int, default=5, help="dry-run 时打印前 N 行")
    parser.add_argument("--apply", action="store_true", help="真写入 awards 表")
    args = parser.parse_args(argv)

    if not DB_PATH.exists():
        log(f"找不到数据库：{DB_PATH}")
        return 2

    rows = crawl_ncss(args) if args.source == "ncss" else crawl_lanqiao(args)
    if args.list_only:
        return 0

    unique: dict[str, dict] = {}
    for row in rows:
        unique.setdefault(row["content_hash"], row)
    rows = list(unique.values())
    log(f"\n[汇总] 解析到 {len(rows)} 条获奖记录（按 content_hash 去重后）")
    if not rows:
        return 0

    for row in rows[: args.print_rows]:
        log(
            "  {year} {session} | {school} | {title} | {track} | {grade} | {level} | comp={comp} | hash={h}".format(
                year=row["year"],
                session=row["session"],
                school=row["team"],
                title=row["title"],
                track=row["track"],
                grade=row["award_grade"],
                level=row["level"],
                comp=row["competition_id"],
                h=row["content_hash"][:10],
            )
        )
        if row.get("summary"):
            log(f"      {row['summary'][:160]}")

    import sqlite3

    con = sqlite3.connect(DB_PATH)
    if args.apply:
        inserted, skipped = insert_awards(con, rows)
        log(f"[入库] 新增 {inserted} 条，已存在跳过 {skipped} 条")
    else:
        existing = {
            r[0] for r in con.execute("select content_hash from awards where content_hash is not null")
        }
        would = sum(1 for r in rows if r["content_hash"] not in existing)
        log(f"[dry-run] 未写库；预计新增 {would} 条，重复 {len(rows) - would} 条")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())