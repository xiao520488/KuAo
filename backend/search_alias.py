# -*- coding: utf-8 -*-
"""赛事别称 → 数据库赛事 id（供关键词 / 语义检索加权）。

**为什么需要这一层**
    用户搜「大挑」却搜出「小挑」，是因为关键词只认 name / category / summary：
    大挑的正式名里没有「大挑」二字，而小挑的简介里写着「与『大挑』交替举办」——
    于是唯一的命中落在小挑的简介上（0.35 分），命中即返回，AI 兜底根本不会触发。

**为什么不能直接用 competition_whitelist.ALIASES 的键**
    ALIASES 按「白名单序号」编（1..84），跟 competitions.id 不是一套编号：
    序号 10 是大广赛，库里大广赛是 64。所以必须按「名称」对齐一次。
    另有 4 条白名单名称与库里措辞不同（大赛/竞赛、引号、空格），见 _NAME_FIX。

对外只用两个纯函数（不 import db，避免循环依赖）：
    alias_map(rows)              -> {归一化别称: 赛事 id}
    alias_score(cid, token, map) -> 0.9 / 0.8 / 0.0
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, Mapping

# 白名单名称 → 库里名称（仅这 4 条措辞不一致，逐条核对过）
_NAME_FIX = {
    "“挑战杯”中国大学生创业计划大赛": "“挑战杯”中国大学生创业计划竞赛",
    "中国机器人大赛暨RoboCup机器人世界杯中国赛":
        "中国机器人大赛暨 RoboCup 机器人世界杯中国赛中型组仿真赛",
    "“中国软件杯”大学生软件设计大赛": "中国软件杯大学生软件设计大赛",
    "百度之星程序设计大赛": "百度之星·程序设计大赛",
}

_STRIP = re.compile(r"[\s\u3000“”\"'‘’《》()（）\[\]【】·・\-—_/\\、,，.。:：;；!！?？]+")

# 只给检索用的补丁别称（白名单 ALIASES 里没有、但学生真会这么搜）。
# 不写进 competition_whitelist.ALIASES 是刻意的：那份还兼着入库闸门，
# 检索想宽一点，闸门不能跟着放宽。
EXTRA_ALIASES: dict = {
    5: ("数模",),          # 全国大学生数学建模竞赛
    11: ("智能车",),        # 全国大学生智能汽车竞赛
    12: ("三创",),          # 三创赛
    20: ("市调",),          # 市场调查与分析大赛
    24: ("服创",),          # 服务外包创新创业大赛
    25: ("计设",),          # 中国大学生计算机设计大赛
    33: ("信安",),          # 信息安全竞赛
}


def norm(text: object) -> str:
    """归一化：去标点/空白/引号，统一小写。别称和查询都走这一套。"""
    return _STRIP.sub("", str(text or "")).lower()


def alias_map(rows: Iterable[Mapping[str, object]]) -> Dict[str, str]:
    """构建 {归一化别称: 赛事 id}。

    :param rows: 至少含 ``id`` / ``name`` 的赛事行（db.list_competitions() 的返回即可）
    """
    import competition_whitelist as wl

    by_norm = {}
    for row in rows:
        try:
            by_norm[norm(row.get("name"))] = int(row.get("id"))
        except (TypeError, ValueError):
            continue

    out: Dict[str, str] = {}
    for idx, name, _url in wl.WHITELIST:
        target = _NAME_FIX.get(name, name)
        cid = by_norm.get(norm(target))
        if cid is None:
            continue
        aliases = list(wl.ALIASES.get(idx, ()))
        aliases += list(EXTRA_ALIASES.get(idx, ()))
        aliases += list(wl.SPECIAL_ALIASES.get(target, ()))
        aliases += list(wl.SPECIAL_ALIASES.get(name, ()))
        for alias in aliases:
            key = norm(alias)
            if len(key) >= 2:
                out[key] = cid
    return out


# 别称命中给的分：与「名称包含」同档，低于「名称全等」
ALIAS_EXACT = 0.90
ALIAS_INSIDE = 0.80


def alias_score(cid: int, token: str, amap: Mapping[str, int]) -> float:
    """token 命中该赛事别称时的分数；0 表示没命中。

    * 全等（``大挑`` == 别称）→ 0.90
    * 别称被查询包住（``挑战杯大挑`` 含 ``大挑``）→ 0.80，仅限 3 字以上别称，
      避免「杯」「赛」这种碎片乱命中。
    """
    key = norm(token)
    if not key or not amap:
        return 0.0
    if amap.get(key) == cid:
        return ALIAS_EXACT
    if len(key) >= 3:
        for alias, alias_cid in amap.items():
            if alias_cid == cid and len(alias) >= 3 and alias in key:
                return ALIAS_INSIDE
    return 0.0
