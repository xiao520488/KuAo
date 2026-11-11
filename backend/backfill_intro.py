# -*- coding: utf-8 -*-
"""backfill_intro.py —— 为「缺简介」的核心赛事补 summary（一句话）+ intro（介绍）

84 项补齐后仍有大量赛事 `summary` / `intro` 为空：这两列不是赛氪详情里能抓到的
结构化字段，只能靠模型根据赛事名称/类别/官网写一段客观介绍。本脚本只补这两个
字段，不碰日期、封面、参赛对象，也不生成整篇攻略（那是 generate_guides.py 的活）。

约定：
  * 默认 dry-run，只打印将写什么；
  * 写生产库必须 `--apply --allow-production`，写前自动备份；
  * 已有一句话简介的赛事默认跳过（`--force` 才覆盖），方便断点续跑。

用法：
    .\\venv\\Scripts\\python.exe -X utf8 backfill_intro.py                  # 预演
    .\\venv\\Scripts\\python.exe -X utf8 backfill_intro.py --apply --allow-production
    .\\venv\\Scripts\\python.exe -X utf8 backfill_intro.py --ids 60,65 --limit 5
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

import db
import discover_common
import llm

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "competitions.db")

SYSTEM = (
    "你是中国高校学科竞赛领域的资料编辑。你只写客观、克制、可核对的赛事介绍，"
    "不写营销话术，不编造具体年份、报名日期、获奖名单和数字。"
)

PROMPT = """请为下面这场中国高校学科竞赛写两段文字，用 JSON 返回。

赛事名称：{name}
学科类别：{category}
官方网站：{url}

要求：
1. `summary`：一句话简介，40–60 字，说清「谁办 / 面向谁 / 比什么」。
2. `intro`：赛事介绍，150–260 字，交代主办或承办背景、赛事定位、赛制大致流程、
   含金量与影响力；不确定的信息宁可不写，禁止编造具体年份、日期和数量。
3. 中文简体，不用 Markdown 标题和列表，纯段落。
4. 只返回 JSON：{{"summary": "...", "intro": "..."}}
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def pick_rows(conn, ids=None, force=False, limit=0):
    sql = ("SELECT id, name, category, official_url, COALESCE(summary,''), COALESCE(intro,'') "
           "FROM competitions WHERE COALESCE(is_active,1)=1")
    rows = conn.execute(sql).fetchall()
    if ids:
        wanted = set(ids)
        rows = [r for r in rows if r["id"] in wanted]
    if not force:
        rows = [r for r in rows if not (r[4] or "").strip()]
    rows = [r for r in rows if (r[1] or "").strip()]
    return rows[:limit] if limit > 0 else rows


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(description="为缺简介的核心赛事补 summary/intro")
    ap.add_argument("--db-path", default=DEFAULT_DB_PATH)
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--allow-production", action="store_true")
    ap.add_argument("--ids", default=None, help="只处理这些 id，逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="最多处理前 N 条")
    ap.add_argument("--sleep", type=float, default=0.5, help="每条之间的间隔秒数")
    ap.add_argument("--force", action="store_true", help="已有一句话简介也重写")
    args = ap.parse_args(argv)

    db_path = os.path.abspath(os.path.expanduser(args.db_path))
    if not os.path.exists(db_path):
        log("[错误] 目标库不存在：%s" % db_path)
        return 4
    log("[目标库] %s" % db_path)
    if discover_common.is_production_db(db_path):
        log("[目标库] ⚠ 这是生产库")
        if args.apply and not args.allow_production:
            log("[拒绝] 写生产库必须同时传 --allow-production")
            return 4

    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = pick_rows(conn, ids, args.force, args.limit)
    if not rows:
        log("[跳过] 没有需要补简介的赛事")
        return 0
    log("[计划] 待补 %d 条%s" % (len(rows), "" if args.apply else "（dry-run）"))

    if not llm.llm_available():
        log("[错误] 模型不可用：%s" % llm.llm_config_hint())
        return 4

    if args.apply:
        log("[备份] %s" % discover_common.backup_db(db_path))
        db.DB_PATH = db_path

    ok = fail = 0
    for pos, row in enumerate(rows, 1):
        name = row["name"]
        prompt = PROMPT.format(name=name, category=row["category"] or "未分类",
                               url=row["official_url"] or "（暂无）")
        try:
            data = llm.chat_json(prompt, system=SYSTEM, temperature=0.4,
                                 max_tokens=900, schema_hint='{"summary": "", "intro": ""}')
            if not isinstance(data, dict):
                raise ValueError("返回不是对象：%r" % (data,))
            summary = str(data.get("summary") or "").strip()
            intro = str(data.get("intro") or "").strip()
            if not summary:
                raise ValueError("summary 为空")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            log("[%d/%d] %s 失败：%r" % (pos, len(rows), name[:26], exc))
            time.sleep(args.sleep)
            continue

        if args.apply:
            db.update_competition(row["id"], {"summary": summary, "intro": intro},
                                  db_path=db_path)
            log("[%d/%d] %s\n      summary：%s" % (pos, len(rows), name[:26], summary))
        else:
            log("[%d/%d] %s\n      summary：%s\n      intro：%s…"
                % (pos, len(rows), name[:26], summary, intro[:60]))
        ok += 1
        time.sleep(args.sleep)

    log("\n[完成] 成功 %d 条，失败 %d 条，共 %d 条；用量 %s"
        % (ok, fail, len(rows), json.dumps(llm.get_usage(), ensure_ascii=False)))
    conn.close()
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())