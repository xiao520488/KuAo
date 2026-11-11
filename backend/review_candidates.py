# -*- coding: utf-8 -*-
"""review_candidates.py —— 候选赛事审核 CLI（list / approve / reject）

流程：
  * `--list`            列出 pending 候选（ID / 名称 / 级别 / 来源）；
  * `--approve <id>`    候选 -> competitions（图片重新过 check_image 闸门），
                        同一事务内把候选标记 approved 并回填 competition_id；
                        **随后 AI 提取 2 字简称 + 自动抓赛氪详情并提纯三字段**
                        （失败只告警，不回滚已入库赛事）；
  * `--reject <id>`     标记 rejected，不动主表。

安全红线（CRAWLER_HANDOVER.md 第四/九节）：
  * 默认目标库 test_crawl.db；写生产库必须显式 --allow-production；
  * 写生产库前自动 SQLite backup + integrity_check；
  * 只允许提拔 status='pending' 的候选；主表已有同名/高相似赛事则拒绝；
  * 封面不合格时宁可不写 image_url（仍可入库），绝不放宽宽高比；
  * 新入库赛事 sort_order=9999，首页不曝光。

用法（backend 目录）：
  .\\venv\\Scripts\\python.exe review_candidates.py --list
  .\\venv\\Scripts\\python.exe review_candidates.py --approve 1
  .\\venv\\Scripts\\python.exe review_candidates.py --reject 2

生产库：
  .\\venv\\Scripts\\python.exe review_candidates.py --list --db-path .\\competitions.db
  .\\venv\\Scripts\\python.exe review_candidates.py --approve 1 `
    --db-path .\\competitions.db --allow-production
"""
import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime

import db
import discover_common as common
import enrich_saikr_details as enrich
import short_names
import competition_whitelist as whitelist
from batch_update_competitions import check_image

DEFAULT_DB_PATH = common.DEFAULT_DB_PATH
DEFAULT_SORT_ORDER = 9999


def log(message: str) -> None:
    print(message, flush=True)


def clean_name_for_main(raw_name: str) -> str:
    """候选名 -> 主表展示名：去营销标签/年份/届次，保留官方全称。"""
    text = unicodedata.normalize("NFKC", str(raw_name or "")).strip()
    text = re.sub(r"^(?:【[^】]*】|\[[^\]]*\])\s*", "", text)
    text = re.sub(r"\s*(?:【[^】]*】|\[[^\]]*\])\s*$", "", text)
    text = re.sub(r"^(?:19|20)\d{2}\s*年?\s*", "", text)
    text = re.sub(r"^第[一二三四五六七八九十百零\d]+届\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def infer_level(candidate: dict) -> tuple:
    """返回 (level, level_label)；现有枚举只有 national / provincial。"""
    label = str(candidate.get("level_label") or "").strip()
    text = label + " " + str(candidate.get("raw_name") or "")
    provincial_words = (
        "省级", "省赛", "区域级", "区域赛", "赛区", "分赛区",
        "华南", "华东", "华北", "华中", "西南", "西北", "东北",
    )
    if any(word in text for word in provincial_words):
        return "provincial", label or "省级"
    return "national", label or "国家级"


def build_competition_data(candidate: dict, image_url) -> dict:
    level, level_label = infer_level(candidate)
    source = str(candidate.get("source") or "").strip()
    if source == "saikr":
        discover_source = "saikr:list"
    elif source == "edu-roster":
        discover_source = "edu:roster"
    else:
        discover_source = source or None
    return {
        "name": clean_name_for_main(candidate.get("raw_name")),
        "category": candidate.get("category_hint") or "待确认",
        "reg_start_month": candidate.get("reg_start_month"),
        "reg_end_month": candidate.get("reg_end_month"),
        "reg_cross_year": candidate.get("reg_cross_year") or 0,
        "official_url": candidate.get("official_url"),
        "image_url": image_url,
        "contest_start_month": candidate.get("contest_start_month"),
        "contest_end_month": candidate.get("contest_end_month"),
        # 双轨制：精确日期可选，为空就留空（不推断）
        "reg_start_date": candidate.get("reg_start_date"),
        "reg_end_date": candidate.get("reg_end_date"),
        "contest_start_date": candidate.get("contest_start_date"),
        "contest_end_date": candidate.get("contest_end_date"),
        "discover_source": discover_source,
        "last_checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_active": 1,
        "level": level,
        "level_label": level_label,
        "sort_order": DEFAULT_SORT_ORDER,
        "tech_stack": "[]",
    }


def verify_cover(candidate: dict):
    """封面重新过闸门；返回 (可用 URL 或 None, 说明)。"""
    url = (candidate.get("image_url") or "").strip()
    if not url:
        return None, "无封面，按方案 C 走前端兜底"
    ok, reason, info = check_image(url)
    if ok:
        size = "%sx%s" % (info.get("width"), info.get("height"))
        return url, "封面通过（%s）" % size
    return None, "封面拒收：%s" % (reason or "未通过")


def candidate_contest_id(candidate: dict):
    """从 saikr 候选里取 contest_id；其它来源返回 None（由名称相似度兜底）。"""
    if str(candidate.get("source") or "").strip() != "saikr":
        return None
    match = re.search(r"(\d+)\s*$", str(candidate.get("source_key") or ""))
    if match:
        return int(match.group(1))
    raw = candidate.get("raw_json")
    if raw:
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            try:
                contest_id = int(data.get("contest_id") or 0)
            except (TypeError, ValueError):
                contest_id = 0
            if contest_id > 0:
                return contest_id
    return None


def enrich_after_approve(args, candidate: dict, competition_id: int) -> dict:
    """approve 后自动抓赛氪详情 + AI 提纯并写库。

    失败只告警，不回滚已入库的赛事（详情属于增强数据，不是准入条件）。
    """
    if args.skip_enrich:
        log("[补全] 已按 --skip-enrich 跳过详情抓取")
        return {"status": "skipped"}
    log("[补全] 抓取赛氪详情 + AI 提纯（可能需要 20~60 秒）…")
    try:
        result = enrich.enrich_competition(
            args.db_path,
            competition_id,
            contest_id=candidate_contest_id(candidate),
            allow_stale=False,
        )
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - 增强失败不影响主流程
        log("[补全] ⚠ 失败（赛事已入库，详情保持 NULL）：%s" % exc)
        return {"status": "failed", "error": str(exc)}

    status = result.get("status")
    if status == "updated":
        labels = [enrich.FIELD_LABELS[field] for field in result.get("written") or []]
        log("[补全] 已写入：%s" % "、".join(labels))
    else:
        reason = result.get("reason") or ""
        log("[补全] 跳过（%s%s），详情保持 NULL" % (
            status, "：" + reason if reason else ""))
    return result


def generate_short_name_after_approve(args, competition_id: int,
                                      name: str) -> dict:
    """审核入库后自动提取 2 字简称并写入主表。

    失败只告警，不回滚已入库的赛事：short_name 是视觉兜底字段，
    为空时前端会回退到名称首字，不影响数据可用性。
    """
    if args.skip_enrich:
        log("[简称] 已按 --skip-enrich 跳过 AI 提取")
        return {"status": "skipped"}
    log("[简称] AI 提取 2 字简称…")
    try:
        short_name = short_names.extract_short_name(name)
        db.save_short_name(competition_id, short_name, db_path=args.db_path)
    except Exception as exc:  # noqa: BLE001 - 简称失败不影响主流程
        log("[简称] ⚠ 提取失败（short_name 保持 NULL，前端回退首字）：%s" % exc)
        return {"status": "failed", "error": str(exc)}
    log("[简称] 已写入：%s" % short_name)
    return {"status": "updated", "short_name": short_name}


def require_production_permission(db_path: str, allow_production: bool) -> None:
    if common.is_production_db(db_path) and not allow_production:
        raise SystemExit("拒绝写生产库：必须同时传 --allow-production")


def write_backup_if_production(db_path: str) -> None:
    if common.is_production_db(db_path):
        log("[备份] %s" % common.backup_db(db_path))


def cmd_list(args) -> int:
    log("[目标库] %s" % args.db_path)
    total = db.count_candidates(status="pending", db_path=args.db_path)
    rows = db.query_candidates(
        status="pending", limit=args.limit, db_path=args.db_path
    )
    log("[待审] %d 条（显示 %d 条）" % (total, len(rows)))
    if not rows:
        return 0
    print("ID    来源        级别        名称")
    print("----  ----------  ----------  ----------------------------------------")
    for row in rows:
        name = row["raw_name"] or ""
        if len(name) > 60:
            name = name[:57] + "..."
        print("%-4s  %-10s  %-10s  %s" % (
            row["id"], row["source"], row["level_label"] or "-", name,
        ))
    if total > len(rows):
        log("（其余 %d 条请加 --limit 或按 ID 审核）" % (total - len(rows)))
    return 0


def cmd_approve(args) -> int:
    log("[目标库] %s" % args.db_path)
    require_production_permission(args.db_path, args.allow_production)

    candidate = db.get_candidate(args.approve, db_path=args.db_path)
    if not candidate:
        raise SystemExit("候选不存在：%s" % args.approve)
    if candidate["status"] != "pending":
        raise SystemExit("候选 %s 当前状态为 %s，只有 pending 可以提拔" % (
            args.approve, candidate["status"]))

    # 白名单闸门：非 84 项核心赛（含省级/区域赛）默认拒绝入库
    allowed, gate_reason = whitelist.gate(candidate.get("raw_name") or "")
    if not allowed and not getattr(args, "allow_non_whitelist", False):
        raise SystemExit(
            "拒绝入库：%s 不在「教育部 84 项核心国赛」内（%s）。\n"
            "  如确认要人工破例，加 --allow-non-whitelist 重跑。" % (
                candidate.get("raw_name"), gate_reason))
    log("[白名单] %s -> %s" % (candidate.get("raw_name"), gate_reason))

    duplicate = db.find_duplicate_candidate(
        candidate["norm_name"],
        official_url=candidate.get("official_url"),
        db_path=args.db_path,
        include_candidates=False,
    )
    if duplicate:
        raise SystemExit("主表已有同名/高相似赛事：id=%s《%s》（%s），拒绝入库" % (
            duplicate["id"], duplicate["name"], duplicate["reason"]))

    image_url, image_note = verify_cover(candidate)
    log("[封面] %s" % image_note)

    write_backup_if_production(args.db_path)
    competition_data = build_competition_data(candidate, image_url)
    competition_id = db.promote_candidate(
        args.approve, competition_data, db_path=args.db_path,
    )
    log("[通过] 候选 %s -> competitions id=%s（sort_order=%s，首页不曝光）" % (
        args.approve, competition_id, DEFAULT_SORT_ORDER))
    # 新赛事入库即生成 2 字简称，保证无图卡片有 Logo 式兜底
    generate_short_name_after_approve(
        args, competition_id, competition_data.get("name") or ""
    )
    # 2.2：入库后立即补详情（抓赛氪 + AI 提纯），失败不回滚
    enrich_after_approve(args, candidate, competition_id)
    return 0


def cmd_reject(args) -> int:
    log("[目标库] %s" % args.db_path)
    require_production_permission(args.db_path, args.allow_production)

    candidate = db.get_candidate(args.reject, db_path=args.db_path)
    if not candidate:
        raise SystemExit("候选不存在：%s" % args.reject)
    if candidate["status"] != "pending":
        raise SystemExit("候选 %s 当前状态为 %s，只有 pending 可以拒绝" % (
            args.reject, candidate["status"]))

    write_backup_if_production(args.db_path)
    db.update_candidate_status(
        args.reject, "rejected", db_path=args.db_path
    )
    log("[拒绝] 候选 %s 已标记 rejected（主表未动）" % args.reject)
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="候选赛事审核 CLI（默认目标库 test_crawl.db）"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true", help="列出 pending 候选")
    action.add_argument("--approve", type=int, metavar="ID",
                        help="通过候选并插入 competitions")
    action.add_argument("--reject", type=int, metavar="ID",
                        help="拒绝候选（仅改状态）")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH,
                        help="目标库（默认 test_crawl.db）")
    parser.add_argument("--allow-production", action="store_true",
                        help="允许写生产库（写前自动备份）")
    parser.add_argument("--skip-enrich", action="store_true",
                        help="approve 后不抓取赛氪详情、不调模型（离线/应急）")
    parser.add_argument("--allow-non-whitelist", action="store_true",
                        help="破例允许提拔「非 84 项白名单」候选（默认拒绝）")
    parser.add_argument("--limit", type=int, default=100,
                        help="--list 最多显示条数（默认 100）")
    args = parser.parse_args(argv)
    if not (args.list or args.approve or args.reject):
        parser.print_help()
        raise SystemExit(2)
    if args.limit < 1:
        raise SystemExit("--limit 必须大于 0")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.list:
        return cmd_list(args)
    if args.approve:
        return cmd_approve(args)
    return cmd_reject(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
