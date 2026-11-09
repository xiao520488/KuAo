# -*- coding: utf-8 -*-
"""short_names.py —— AI 提取「2 字赛事简称」。

用途
----
无封面图（或图片加载失败）时，前端卡片不再只显示名称首字，而是显示
像专属 Logo 一样的 2 字简称（如「中国大学生计算机设计大赛」->「计设」，
「全国大学生数学建模竞赛」->「数模」）。本模块只负责「赛事名 -> 2 字简称」，
不读写数据库；落库由调用方完成：

  * ``generate_short_names.py``：批量补全存量赛事；
  * ``review_candidates.py``：审核通过、写入主表后自动生成一次。

硬性约束
--------
简称必须严格为 **2 个纯汉字**：不含英文字母、数字、标点、空格。
模型输出不合规时会用纠错提示重试一次，仍不合规则抛 ``ShortNameError``，
由调用方决定跳过（前端会回退到名称首字），绝不把脏数据写进库。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

import llm

# Prompt 版本：修改 Prompt 结构时 +1，便于排查历史数据来源
SHORT_NAME_PROMPT_VER = "v1"

# 推理模型的 reasoning 会占用 completion 额度，给足 1024 避免空正文
SHORT_NAME_MAX_TOKENS = 1024

# 常用汉字 + 扩展 A 区；简称不会用到更生僻的扩展区
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

SHORT_NAME_SYSTEM_PROMPT = (
    "你是大学生竞赛平台的品牌命名专家。"
    "你的任务是根据赛事全称，提炼一个像 Logo 一样的 2 字简称。"
    "只输出 JSON，不要输出任何解释、Markdown 或多余文字。"
)


class ShortNameError(ValueError):
    """AI 未能给出符合「2 个纯汉字」要求的简称。"""


def normalize_short_name(raw) -> str:
    """归一化并校验简称：只保留汉字，必须恰好 2 个。

    参数:
        raw: 模型返回的简称（可能带引号、空格、解释文字）。

    返回:
        2 个纯汉字。

    异常:
        ShortNameError: 去掉非汉字后不是 2 个字。
    """
    text = unicodedata.normalize("NFKC", str(raw or ""))
    chars = "".join(_HAN_RE.findall(text))
    if len(chars) != 2:
        raise ShortNameError("简称必须恰好是 2 个纯汉字，实际为 %r" % (raw,))
    return chars


def build_prompt(name: str) -> str:
    """构造单条赛事名的提取 Prompt（用户消息）。"""
    return f"""赛事全称：{name}

请为这个赛事提炼一个用于无图卡片 Logo 兜底的 2 字简称。

硬性要求：
1. 必须且只能是 2 个汉字；不能是 1 个或 3 个，不得包含英文字母、数字、标点、空格。
2. 去掉「杯」「赛」「大赛」「竞赛」「全国」「中国」「国际」「大学生」「创新创业」等通用字眼。
3. 抓住赛事最有辨识度的核心词，优先使用圈内通行的简称。
4. 简称要有区分度，不要取「大学」「比赛」「创新」这类无信息量的词。

参考示例：
- 中国大学生计算机设计大赛 -> 计设
- 全国大学生数学建模竞赛 -> 数模
- 蓝桥杯全国软件和信息技术专业人才大赛 -> 蓝桥
- 全国大学生电子设计竞赛 -> 电设

只输出 JSON：{{"short_name": "两个汉字"}}"""


def extract_short_name(name: str, *, model: Optional[str] = None,
                       max_tokens: int = SHORT_NAME_MAX_TOKENS) -> str:
    """调用大模型，把赛事全称提炼为 2 个纯汉字。

    参数:
        name: 赛事全称（主表 competitions.name）。
        model: 可选模型覆盖，默认使用 config 中的 DEEPSEEK_MODEL。
        max_tokens: 单次调用上限（推理模型需要留足 reasoning 额度）。

    返回:
        2 个纯汉字，如「计设」「数模」。

    异常:
        ShortNameError: 名称为空，或模型两次输出都不合规。
        llm.LLMError: 模型调用失败（网络/配置/重试耗尽）。
    """
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise ShortNameError("赛事名称为空，无法提取简称")

    schema_hint = '{"short_name": "两个汉字"}'
    last_raw = ""
    last_error: Optional[ShortNameError] = None

    for attempt in range(2):
        prompt = build_prompt(cleaned)
        if attempt > 0:
            prompt += (
                "\n\n上一次输出为 %r，不符合「恰好 2 个纯汉字」的要求。"
                "请重新输出，只保留 2 个汉字。" % (last_raw,)
            )
        data = llm.chat_json(
            prompt,
            system=SHORT_NAME_SYSTEM_PROMPT,
            schema_hint=schema_hint,
            temperature=0.2 if attempt == 0 else 0.0,
            max_tokens=max_tokens,
            retries=1,
            model=model,
        )
        if isinstance(data, dict):
            last_raw = data.get("short_name", "")
        else:
            last_raw = data
        try:
            return normalize_short_name(last_raw)
        except ShortNameError as exc:
            last_error = exc

    raise ShortNameError(
        "AI 未能给出 2 字简称（最后输出：%r）" % (last_raw,)
    ) from last_error
