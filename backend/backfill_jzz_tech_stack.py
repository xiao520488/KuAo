# -*- coding: utf-8 -*-
"""backfill_jzz_tech_stack.py —— 用 LLM 给获奖项目补 tech_stack（技术栈标签）。

背景：金种子杯 364 条获奖记录的「赛道」已由官方名单 PDF 自带（不缺），
      缺的是 `awards.tech_stack`（技术栈），前端作品卡片/详情用它做技术方向参考。

用法：
    venv\\Scripts\\python.exe backfill_jzz_tech_stack.py --limit 4            # 试跑 4 条
    venv\\Scripts\\python.exe backfill_jzz_tech_stack.py --apply              # 全量写入
    venv\\Scripts\\python.exe backfill_jzz_tech_stack.py --apply --force      # 覆盖已有

设计：
  * 分批（默认 12 条/次）请求，要求模型返回严格 JSON；
  * 校验：每项 2–6 个标签、去重、去空白、单标签 ≤ 24 字；
  * 只更新 tech_stack 仍为空的行（--force 才覆盖），可反复重跑；
  * 进度落盘到 workspace/jzz_techstack_cache.json，中断后继续。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import llm  # noqa: E402

CACHE = BASE_DIR / "workspace" / "jzz_techstack_cache.json"
SYSTEM = (
    "你是中国高校创新创业赛事的资深评审与技术顾问。给定一批获奖项目名称、赛道、学校，"
    "为每个项目推断其最可能用到的核心技术栈标签（技术/框架/器件/算法/领域技术词），"
    "用于给参赛者做技术方向参考。要求：\n"
    "1. 只输出 JSON，不要解释；\n"
    "2. 每个项目 2–6 个标签，按重要性排序；\n"
    "3. 标签用规范写法：编程语言/框架/硬件用英文原名（Python、PyTorch、ROS2、STM32、FPGA、"
    "OpenCV、YOLOv8、Vue3、MySQL），行业/算法方向可用中文（机器视觉、钙钛矿电池、强化学习、"
    "多模态大模型、结构力学仿真）；\n"
    "4. 不确定时给「领域技术词」而不是编造具体框架；禁止输出泛词（如「人工智能」「软件」）。"
)
USER_TMPL = """请为下列 {n} 个项目推断技术栈，输出 JSON：
{{"items":[{{"id":<项目id>,"tech_stack":["标签1","标签2"]}}]}}

{rows}"""


def _clean_tags(tags) -> list[str]:
    out: list[str] = []
    if not isinstance(tags, list):
        return out
    for tag in tags:
        if not isinstance(tag, str):
            continue
        t = re.sub(r"\s+", " ", tag).strip(" ·,，、;；")
        if not t or len(t) > 24 or t.lower() in {"人工智能", "软件", "技术", "其他", "无"}:
            continue
        if t not in out:
            out.append(t)
    return out[:6]


def build_rows(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        lines.append(
            "- id={id}｜赛道：{track}｜奖项：{grade}｜学校：{team}｜项目：{title}".format(
                id=r["id"], track=r["track"] or "未标注", grade=r["award_grade"],
                team=(r["team"] or "未标注")[:40], title=r["title"][:80],
            )
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="用 LLM 补金种子杯 tech_stack")
    parser.add_argument("--db", default=str(BASE_DIR / "competitions.db"))
    parser.add_argument("--competition-id", type=int, default=120)
    parser.add_argument("--batch", type=int, default=12)
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 条（0=全部）")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true", help="覆盖已有 tech_stack")
    parser.add_argument("--sleep", type=float, default=0.5, help="批次间隔秒")
    parser.add_argument("--max-batches", type=int, default=0, help="最多跑 N 批（调试用）")
    args = parser.parse_args(argv)

    if not llm.llm_available():
        print("LLM 不可用：", llm.llm_config_hint())
        return 2

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    where = "" if args.force else " and (tech_stack is null or tech_stack in ('[]',''))"
    sql = ("select id, title, track, award_grade, team from awards "
           f"where competition_id = ?{where} order by year desc, award_grade, id")
    todo = [dict(r) for r in con.execute(sql, (args.competition_id,))]
    if args.limit:
        todo = todo[: args.limit]
    print(f"待补 tech_stack：{len(todo)} 条（batch={args.batch}, apply={args.apply}）")
    if not todo:
        con.close()
        return 0

    cache: dict = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}

    ok = skip = 0
    batches = (len(todo) + args.batch - 1) // args.batch
    for b in range(batches):
        if args.max_batches and b >= args.max_batches:
            break
        chunk = todo[b * args.batch:(b + 1) * args.batch]
        prompt = USER_TMPL.format(n=len(chunk), rows=build_rows(chunk))
        try:
            data = llm.chat_json(prompt, system=SYSTEM, schema_hint='{"items":[{"id":0,"tech_stack":[""]}]}')
        except llm.LLMError as e:
            print(f"[批 {b + 1}/{batches}] LLM 失败：{e}")
            continue
        items = (data or {}).get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            print(f"[批 {b + 1}/{batches}] 返回结构异常，跳过")
            continue
        index = {r["id"]: r for r in chunk}
        written = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                rid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if rid not in index:
                continue
            tags = _clean_tags(item.get("tech_stack"))
            if len(tags) < 2:
                skip += 1
                continue
            cache[str(rid)] = tags
            if args.apply:
                con.execute(
                    "update awards set tech_stack = ?, updated_at = datetime('now','localtime') "
                    "where id = ? and competition_id = ?",
                    (json.dumps(tags, ensure_ascii=False), rid, args.competition_id),
                )
            written += 1
        if args.apply:
            con.commit()
        ok += written
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
        usage = llm.get_usage()
        print(f"[批 {b + 1}/{batches}] 写入 {written}/{len(chunk)}，累计 {ok}，tokens={usage['total_tokens']}")
        if args.sleep:
            time.sleep(args.sleep)

    con.close()
    print(f"完成：成功 {ok} 条，跳过 {skip} 条，缓存 {len(cache)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())