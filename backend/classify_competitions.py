"""给赛事打「是否有项目作品」标记（has_project_awards）。

背景（2026-11-14）
------------------
蓝桥杯、ACM-ICPC 这类**纯算法/知识类**赛事，历年公示的只有获奖名单，
没有任何项目作品。基于名单做「选题灵感 / 技术趋势」分析既没有备赛价值
（用户需要的是刷题），又会诱导用户去看名单，纯粹浪费。

这类赛事只保留「历年获奖名单」来证明含金量；灵感入口在前端隐藏，
后端 API 直接 400，一次大模型调用都不发起。

判定口径（宁可少标、不要误标）
------------------------------
1. 名称命中算法类关键词：蓝桥杯 / 码蹄杯 / ACM-ICPC / 百度之星 / 算法精英
2. 或 category == '程序设计'（该类别下全部为 OJ 式编程赛）

其余赛事（设计、建模、创业、机器人…）都默认 true，因为它们的获奖信息
本身就是「作品」，可以提炼选题与评审偏好。

用法
----
    python classify_competitions.py            # 应用
    python classify_competitions.py --dry-run  # 只看会改哪些，不写库
"""

from __future__ import annotations

import argparse
import sys

import db

# 纯算法 / 知识类赛事：名称关键词（小写匹配）
ALGORITHM_KEYWORDS = (
    "蓝桥杯",
    "码蹄杯",
    "acm-icpc",
    "icpc",
    "百度之星",
    "算法精英",
)

# 纯算法 / 知识类赛事：类别
ALGORITHM_CATEGORIES = ("程序设计",)


def is_algorithm_only(name: str, category: str) -> bool:
    """判断某赛事是否只有获奖名单、没有项目作品。"""
    name_l = (name or "").strip().lower()
    if any(k in name_l for k in ALGORITHM_KEYWORDS):
        return True
    return (category or "").strip() in ALGORITHM_CATEGORIES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="标记无项目作品的赛事")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    args = parser.parse_args(argv)

    comps = db.list_competitions(include_inactive=True)
    changed, already = [], 0
    for comp in comps:
        cid = int(comp["id"])
        name = comp.get("name") or ""
        category = comp.get("category") or ""
        target = 0 if is_algorithm_only(name, category) else 1
        current = 1 if comp.get("has_project_awards", 1) else 0
        if current == target:
            already += 1
            continue
        changed.append((cid, name, category, current, target))

    if not changed:
        print(f"[i] 无需修改（{already} 条已符合口径）")
        return 0

    for cid, name, category, current, target in changed:
        flag = "算法/知识类（无项目作品）" if target == 0 else "有项目作品"
        print(f"  ID={cid:<4} {current}->{target}  {flag:<20} {name[:34]} [{category}]")

    if args.dry_run:
        print(f"[dry-run] 将修改 {len(changed)} 条，未写库")
        return 0

    for cid, _name, _category, _current, target in changed:
        db.update_competition(cid, {"has_project_awards": target})
    print(f"[ok] 已更新 {len(changed)} 条；其余 {already} 条保持不变")

    # 复查：算法类赛事里是否还有漏标的
    leftover = [
        c for c in db.list_competitions(include_inactive=True)
        if is_algorithm_only(c.get("name") or "", c.get("category") or "")
        and c.get("has_project_awards", 1)
    ]
    if leftover:
        print(f"[!] 仍有 {len(leftover)} 条算法类赛事未标记：", file=sys.stderr)
        for c in leftover:
            print(f"    ID={c['id']} {c.get('name')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())