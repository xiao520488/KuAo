# -*- coding: utf-8 -*-
"""
generate_guides.py —— 离线批量生成「参赛全过程攻略」脚本

用途：
    读取数据库 competitions 表中的所有比赛，逐条调用大模型生成
    「参赛全过程攻略」（Markdown），并回写到该比赛的
    full_process_guide 字段。

特点：
    - 独立离线脚本，不依赖 FastAPI 服务，直接复用 db.py / llm.py
    - 支持断点续跑（--only-missing 只处理攻略为空的比赛）
    - 支持限量（--limit N）、指定比赛（--id 1,2,3）、强制覆盖（--force）
    - 单条失败不中断整体流程，最后打印汇总
    - 默认 dry-run 关闭；--dry-run 只打印不写库

用法示例（Windows CMD）：
    python generate_guides.py
    python generate_guides.py --only-missing
    python generate_guides.py --id 1,5,9 --force
    python generate_guides.py --limit 10 --dry-run

注意：
    本脚本会真实调用大模型并消耗额度，请确认后再执行。
"""

import argparse
import logging
import sys
import time
from typing import List, Optional

import db
import llm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("generate_guides")


# ---------------------------------------------------------------------------
# Prompt 定义
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "你是一位资深的学科竞赛指导老师，熟悉各类大学生竞赛的完整参赛流程。"
    "你的任务是输出一份准确、可执行的参赛全过程攻略。\n"
    "硬性要求：\n"
    "1. 只输出 Markdown 正文，不要用代码块包裹整篇内容；\n"
    "2. 严禁编造赛制细节、时间节点、奖项设置等具体事实；\n"
    "3. 若你对某个比赛确实不了解，必须明确说明「该比赛信息不足，请前往官网查询」，"
    "并给出官网查询建议，而不是凭空杜撰；\n"
    "4. 结构清晰，使用二级标题分节。"
)


def build_prompt(comp: dict) -> str:
    """根据比赛信息构造用户 Prompt。"""
    name = (comp.get("name") or "").strip() or "未知比赛"
    category = (comp.get("category") or "").strip()
    level = (comp.get("level_label") or comp.get("level") or "").strip()
    official_url = (comp.get("official_url") or "").strip()
    intro = (comp.get("intro") or comp.get("summary") or "").strip()

    ctx_lines = [f"比赛名称：{name}"]
    if category:
        ctx_lines.append(f"比赛类别：{category}")
    if level:
        ctx_lines.append(f"比赛级别：{level}")
    if official_url:
        ctx_lines.append(f"官方网站：{official_url}")
    if intro:
        ctx_lines.append(f"已知简介：{intro}")

    context = "\n".join(ctx_lines)

    return (
        f"{context}\n\n"
        f"请详细总结【{name}】的参赛全过程。必须包含："
        "准备物品（如PPT、代码、作品）、参赛流程（报名、初赛、决赛）、"
        "展示与答辩方式、是否需要线下到达指定地点、以及拿奖的避坑建议。"
        "请用 Markdown 格式输出。"
        "如果你对该比赛不了解，请说明去官网查询，不要瞎编。"
    )


# ---------------------------------------------------------------------------
# 单条生成
# ---------------------------------------------------------------------------
def generate_one(comp: dict) -> str:
    """调用大模型为单场比赛生成攻略文本，失败抛异常。"""
    prompt = build_prompt(comp)
    text = llm.chat(
        prompt,
        system=SYSTEM_PROMPT,
        temperature=0.4,
        max_tokens=4096,
    )
    if not text or not text.strip():
        raise RuntimeError("大模型返回内容为空")
    return text.strip()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def parse_ids(raw: Optional[str]) -> Optional[List[int]]:
    """解析 --id 参数，如 "1,5,9" -> [1, 5, 9]。"""
    if not raw:
        return None
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise SystemExit(f"非法的比赛 ID：{part!r}")
    return ids or None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="批量生成比赛参赛全过程攻略并回写数据库"
    )
    parser.add_argument(
        "--only-missing", action="store_true",
        help="只处理 full_process_guide 为空的比赛（断点续跑）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="即使已有攻略也重新生成并覆盖",
    )
    parser.add_argument(
        "--id", dest="ids", default=None,
        help="只处理指定比赛 ID，逗号分隔，如 1,5,9",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="最多处理多少条比赛",
    )
    parser.add_argument(
        "--sleep", type=float, default=0.5,
        help="每条之间的间隔秒数，避免触发限流（默认 0.5）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只生成并打印，不写回数据库",
    )
    args = parser.parse_args(argv)

    # 确保表结构最新（含 full_process_guide 列）
    try:
        db.init_db()
    except Exception as exc:
        logger.error("数据库初始化失败：%r", exc)
        return 1

    # 取比赛列表
    only_missing = args.only_missing and not args.force
    try:
        comps = db.list_competitions_for_guide(only_missing=only_missing)
    except Exception as exc:
        logger.error("读取比赛列表失败：%r", exc)
        return 1

    # 按 --id 过滤
    wanted_ids = parse_ids(args.ids)
    if wanted_ids:
        wanted = set(wanted_ids)
        comps = [c for c in comps if c.get("id") in wanted]
        missing = wanted - {c.get("id") for c in comps}
        if missing:
            logger.warning("以下 ID 未找到或已被过滤：%s", sorted(missing))

    # 非 force 且非 only_missing 时，跳过已有攻略的（避免无谓消耗）
    if not args.force and not args.only_missing:
        comps = [c for c in comps if not (c.get("full_process_guide") or "").strip()]

    if args.limit is not None and args.limit >= 0:
        comps = comps[: args.limit]

    total = len(comps)
    if total == 0:
        logger.info("没有需要处理的比赛，退出。")
        return 0

    logger.info("待处理比赛 %d 条%s", total, "（dry-run，不写库）" if args.dry_run else "")

    ok, failed = 0, 0
    for idx, comp in enumerate(comps, 1):
        cid = comp.get("id")
        name = comp.get("name") or f"ID={cid}"
        logger.info("[%d/%d] 正在生成：%s (id=%s)", idx, total, name, cid)
        try:
            guide = generate_one_with_retry(comp)
        except llm.LLMError as exc:
            failed += 1
            logger.error("  [id=%s] 大模型调用失败（已重试 %d 次）：%s: %s",
                         cid, GUIDE_MAX_RETRIES, type(exc).__name__, exc)
            continue
        except Exception as exc:
            failed += 1
            logger.error("  [id=%s] 生成异常（已重试 %d 次）：%s: %s",
                         cid, GUIDE_MAX_RETRIES, type(exc).__name__, exc)
            continue

        if args.dry_run:
            logger.info("  [id=%s] [dry-run] 生成 %d 字符，未写入", cid, len(guide))
        else:
            try:
                db.update_full_process_guide(cid, guide)
                ok += 1
                logger.info("  [id=%s] 已写入数据库（%d 字符）", cid, len(guide))
            except Exception as exc:
                failed += 1
                logger.error("  [id=%s] 写入数据库失败：%s: %s",
                             cid, type(exc).__name__, exc)
                continue

        if args.sleep and idx < total:
            time.sleep(args.sleep)

    logger.info("完成：成功 %d 条，失败 %d 条，共 %d 条", ok, failed, total)
    return 0 if failed == 0 else 2




# ---------------------------------------------------------------------------
# 重试包装（外层兜底）
# ---------------------------------------------------------------------------
# 说明：llm._call 内部已有 MAX_RETRIES 次重试 + 指数退避，但重试耗尽后
# 仍会抛 LLMError。这里再加一层外层重试，用于应对「内部重试全部失败」
# 或「网络瞬时抖动」的情况。两层重试的间隔不叠加，外层固定间隔。
GUIDE_MAX_RETRIES = 3
GUIDE_RETRY_DELAY = 3.0


def generate_one_with_retry(comp: dict) -> str:
    """
    带重试的攻略生成。

    最多尝试 GUIDE_MAX_RETRIES 次，每次失败后等待 GUIDE_RETRY_DELAY 秒。
    全部失败时抛出最后一次的异常（保留原始异常类型与信息）。

    参数:
        comp: 比赛记录 dict（需含 id / name 等字段）
    返回:
        生成的 Markdown 文本（非空）
    异常:
        最后一次尝试的异常，原样抛出，由调用方记录 cid 与原因
    """
    cid = comp.get("id")
    last_exc: Optional[Exception] = None

    for attempt in range(1, GUIDE_MAX_RETRIES + 1):
        try:
            return generate_one(comp)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "  [id=%s] 第 %d/%d 次生成失败：%s: %s",
                cid, attempt, GUIDE_MAX_RETRIES, type(exc).__name__, exc,
            )
            if attempt < GUIDE_MAX_RETRIES:
                logger.info("  [id=%s] %g 秒后重试……", cid, GUIDE_RETRY_DELAY)
                time.sleep(GUIDE_RETRY_DELAY)

    assert last_exc is not None
    raise last_exc

if __name__ == "__main__":
    sys.exit(main())
