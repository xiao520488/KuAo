# -*- coding: utf-8 -*-
"""
merge_test_competitions.py —— 把测试库里「采到的新赛事」合并进生产库

背景
----
`test_crawl.db` 是从生产库复制出来的采集实验库，爬虫在上面新增了赛事与获奖记录。
本脚本把这些「生产库还没有」的赛事（以及它们关联的获奖记录）合并进
`competitions.db`，让前端立刻能看到真实的官网 / 封面图 / 比赛月份。

安全设计（按交接文档红线 + 用户 2026-11-07 的明确授权）
--------------------------------------------------------
* 默认 **只演练不写库**：不带 --apply 时全是只读，只打印将要插入什么。
* 真正写库前 **必须先生成一份备份**（SQLite backup API，WAL 安全；
  备份名带时间戳，绝不覆盖历史备份）。备份失败 -> 直接终止。
* 去重：赛事按 `name`（以及去重后的归一化名）比对；获奖按 `content_hash`
  唯一索引兜底，已存在的直接跳过，不产生重复。
* ID 策略：能用源库 id 且目标库未被占用就沿用，保证与源库/awards 的
  competition_id 对应关系不被打乱；否则交给 SQLite 自增。
* 占位赛事（category='待确认' 或 sort_order=9999）默认 **不合并**，
  需要时显式加 --include-placeholders。
* 整个写入在一个事务内完成，中途失败整体回滚；写后复检行数、完整性并回读。

用法（在 backend 目录、激活 venv 后）
------------------------------------
    # 1. 演练：只读，打印将要做什么
    .\\venv\\Scripts\\python.exe merge_test_competitions.py

    # 2. 真正合并（自动备份 -> 插入 -> 复检）
    .\\venv\\Scripts\\python.exe merge_test_competitions.py --apply

    # 3. 连占位赛事一起合并
    .\\venv\\Scripts\\python.exe merge_test_competitions.py --apply --include-placeholders

    # 4. 只合并赛事，不动获奖记录
    .\\venv\\Scripts\\python.exe merge_test_competitions.py --apply --no-awards
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

from migrate_production import connect_readonly, human_size, sha256_file, sqlite_uri

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTION_DB_PATH = os.path.join(BASE_DIR, "competitions.db")
TEST_DB_PATH = os.path.join(BASE_DIR, "test_crawl.db")

PLACEHOLDER_CATEGORY = "待确认"
PLACEHOLDER_SORT = 9999


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}")


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老环境 / 被重定向
            pass


# ---------------------------------------------------------------------------
# 基础读取
# ---------------------------------------------------------------------------

def table_columns(conn: sqlite3.Connection, table: str) -> list:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def fetch_dicts(conn: sqlite3.Connection, sql: str, params=()) -> list:
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = None


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def normalize_name(name: str) -> str:
    """名称归一化：只保留中文/字母/数字，用于判断“是不是同一场比赛”。"""
    text = (name or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


def is_placeholder(row: dict) -> bool:
    """占位赛事的判定只看 category。

    注意：爬虫自动建的占位记录 sort_order 都是 9999，但被 crawler_competitions.py
    补全后 category 会从 '待确认' 变成真实类别（如 '机器人'），sort_order 却仍是
    9999——所以不能拿 sort_order 当占位标志，否则会把已补全的真赛事误跳过。
    """
    category = (row.get("category") or "").strip()
    return category == "" or category == PLACEHOLDER_CATEGORY


# ---------------------------------------------------------------------------
# 计划：算出要插入哪些行
# ---------------------------------------------------------------------------

def build_plan(source_path: str, target_path: str, *,
               include_placeholders: bool, with_awards: bool) -> dict:
    src = connect_readonly(source_path)
    tgt = connect_readonly(target_path)
    try:
        if "competitions" not in {r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}:
            raise RuntimeError(f"{source_path} 里没有 competitions 表，指错库了？")

        tgt_cols = table_columns(tgt, "competitions")
        src_cols = table_columns(src, "competitions")
        common_comp_cols = [c for c in tgt_cols if c in src_cols]

        tgt_rows = fetch_dicts(tgt, "SELECT * FROM competitions")
        src_rows = fetch_dicts(src, "SELECT * FROM competitions ORDER BY id")

        tgt_names = {normalize_name(r["name"]) for r in tgt_rows}
        tgt_names_raw = {(r["name"] or "").strip() for r in tgt_rows}
        tgt_ids = {r["id"] for r in tgt_rows}
        existing_target_ids = {r["id"] for r in tgt_rows}

        to_insert, skipped = [], []
        for row in src_rows:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            norm = normalize_name(name)
            if name in tgt_names_raw or norm in tgt_names:
                continue  # 生产库已有同名赛事，跳过（不做字段覆盖）
            if is_placeholder(row) and not include_placeholders:
                skipped.append(("占位赛事（默认跳过）", row))
                continue
            to_insert.append(row)

        plan = {
            "competitions": to_insert,
            "skipped": skipped,
            "common_comp_cols": common_comp_cols,
            "target_ids": tgt_ids,
            "existing_target_ids": existing_target_ids,
            "comp_counts": {"target": len(tgt_rows), "source": len(src_rows)},
            "awards": [],
            "awards_skipped": [],
        }

        if not with_awards or not to_insert:
            plan["award_counts"] = {
                "target": count(tgt, "awards") if "awards" in {
                    r[0] for r in tgt.execute("SELECT name FROM sqlite_master WHERE type='table'")
                } else 0,
                "source": 0,
            }
            return plan

        # ---- 获奖记录 ----
        src_award_cols = table_columns(src, "awards")
        tgt_award_cols = table_columns(tgt, "awards")
        common_award_cols = [c for c in tgt_award_cols if c in src_award_cols]

        tgt_hashes = {r[0] for r in tgt.execute("SELECT content_hash FROM awards")}
        tgt_award_ids = {r[0] for r in tgt.execute("SELECT id FROM awards")}

        comp_ids_after = existing_target_ids | {r["id"] for r in to_insert}
        planned_comp_ids = {r["id"] for r in to_insert}

        awards = []
        for row in fetch_dicts(src, "SELECT * FROM awards ORDER BY id"):
            ch = row.get("content_hash")
            if ch and ch in tgt_hashes:
                continue
            comp_id = row.get("competition_id")
            # 只搬「挂在本次新赛事上」或「挂在生产库已有赛事上」的获奖记录
            if comp_id not in comp_ids_after:
                plan["awards_skipped"].append(("所属赛事未进生产库", row))
                continue
            if comp_id in planned_comp_ids:
                row["_new_competition"] = True
            awards.append(row)

        plan["awards"] = awards
        plan["award_counts"] = {
            "target": count(tgt, "awards"),
            "source": len(fetch_dicts(src, "SELECT id FROM awards")),
        }
        plan["common_award_cols"] = common_award_cols
        plan["target_award_ids"] = tgt_award_ids
        return plan
    finally:
        src.close()
        tgt.close()


def print_plan(plan: dict, source_path: str, target_path: str) -> None:
    log("源库", source_path)
    log("目标库", target_path)
    log("现状", f"生产库 competitions={plan['comp_counts']['target']}，"
                f"源库 competitions={plan['comp_counts']['source']}")

    if not plan["competitions"]:
        log("计划", "没有需要新增的赛事（生产库已包含源库全部赛事）")
    else:
        log("计划", f"将新增 {len(plan['competitions'])} 条赛事：")
        for row in plan["competitions"]:
            image = row.get("image_url") or "-"
            csm, cem = row.get("contest_start_month"), row.get("contest_end_month")
            contest = f"{csm}-{cem}" if csm or cem else "-"
            print(f"        id={row['id']:<3} {row['name']}")
            print(f"              category={row.get('category')!r} level={row.get('level')!r} "
                  f"contest={contest}")
            print(f"              official_url={row.get('official_url')!r}")
            print(f"              image_url={image[:90]!r}")

    for reason, row in plan["skipped"]:
        print(f"        [跳过] id={row['id']} {row['name']} —— {reason}")

    if plan.get("awards"):
        log("计划", f"将新增 {len(plan['awards'])} 条获奖记录：")
        for row in plan["awards"]:
            print(f"        id={row['id']:<3} competition_id={row.get('competition_id')} "
                  f"year={row.get('year')} grade={row.get('award_grade')!r} "
                  f"{(row.get('title') or '')[:32]}")
    for reason, row in plan.get("awards_skipped", []):
        print(f"        [跳过] award id={row['id']} —— {reason}")

    if "award_counts" in plan:
        log("现状", f"生产库 awards={plan['award_counts']['target']}")


# ---------------------------------------------------------------------------
# 备份（复用 migrate_production 的只读连接 + backup API）
# ---------------------------------------------------------------------------

def make_backup(target_path: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{target_path}.bak-{stamp}"
    if os.path.exists(backup_path):
        raise RuntimeError(f"备份文件已存在，拒绝覆盖：{backup_path}")

    src = connect_readonly(target_path)
    try:
        dest = sqlite3.connect(backup_path)
        try:
            src.backup(dest)
            dest.execute("PRAGMA journal_mode = DELETE")
        finally:
            dest.close()
    finally:
        src.close()

    if not os.path.exists(backup_path):
        raise RuntimeError(f"备份未生成：{backup_path}")

    check = sqlite3.connect(sqlite_uri(backup_path), uri=True)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"备份完整性校验失败：{integrity}")
    finally:
        check.close()
    return backup_path


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def pick_id(row_id, used_ids: set):
    """源库 id 可用就沿用，否则返回 None 交给自增。"""
    if isinstance(row_id, int) and row_id not in used_ids:
        used_ids.add(row_id)
        return row_id
    return None


def apply_plan(target_path: str, plan: dict) -> dict:
    conn = sqlite3.connect(target_path, timeout=30)
    result = {"competitions": 0, "awards": 0, "award_skip_fk": 0}
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")

        used_comp_ids = set(plan["existing_target_ids"])
        comp_ids = {}          # 源库 id -> 目标库实际 id
        comp_cols = plan["common_comp_cols"]

        for row in plan["competitions"]:
            payload = {c: row.get(c) for c in comp_cols}
            new_id = pick_id(row.get("id"), used_comp_ids)
            if new_id is not None:
                payload["id"] = new_id
            cols = ", ".join(payload.keys())
            marks = ", ".join("?" for _ in payload)
            cur = conn.execute(
                f"INSERT INTO competitions ({cols}) VALUES ({marks})",
                tuple(payload.values()),
            )
            comp_ids[row["id"]] = new_id if new_id is not None else cur.lastrowid
            result["competitions"] += 1

        if plan.get("awards"):
            award_cols = plan["common_award_cols"]
            used_award_ids = set(plan["target_award_ids"])
            for row in plan["awards"]:
                src_comp_id = row.get("competition_id")
                payload = {c: row.get(c) for c in award_cols}
                payload["competition_id"] = comp_ids.get(src_comp_id, src_comp_id)
                new_id = pick_id(row.get("id"), used_award_ids)
                if new_id is not None:
                    payload["id"] = new_id
                cols = ", ".join(payload.keys())
                marks = ", ".join("?" for _ in payload)
                try:
                    conn.execute(
                        f"INSERT INTO awards ({cols}) VALUES ({marks})",
                        tuple(payload.values()),
                    )
                    result["awards"] += 1
                except sqlite3.IntegrityError as exc:
                    # content_hash 唯一索引兜底：并发/漏检时跳过而不是整体失败
                    if "unique" in str(exc).lower():
                        result["award_skip_fk"] += 1
                        continue
                    raise

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return result


def verify(target_path: str, plan: dict, result: dict) -> bool:
    conn = connect_readonly(target_path)
    ok = True
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            ok = False
            log("复检", f"✗ integrity_check = {integrity}")
        else:
            log("复检", "✓ integrity_check = ok")

        comp_total = count(conn, "competitions")
        expect_min = plan["comp_counts"]["target"] + result["competitions"]
        if comp_total != expect_min:
            ok = False
            log("复检", f"✗ competitions 行数 {comp_total}，预期 {expect_min}")
        else:
            log("复检", f"✓ competitions {plan['comp_counts']['target']} + "
                        f"{result['competitions']} = {comp_total}")

        if "award_counts" in plan:
            award_total = count(conn, "awards")
            expect_award = plan["award_counts"]["target"] + result["awards"]
            if award_total != expect_award:
                ok = False
                log("复检", f"✗ awards 行数 {award_total}，预期 {expect_award}")
            else:
                log("复检", f"✓ awards {plan['award_counts']['target']} + "
                            f"{result['awards']} = {award_total}")

        # 回读新插入的赛事，确认封面/月份真的落库
        log("复检", "回读本次新增的赛事关键字段：")
        for row in plan["competitions"]:
            got = conn.execute(
                "SELECT id, name, official_url, image_url, contest_start_month, "
                "contest_end_month FROM competitions WHERE name = ?",
                (row["name"],),
            ).fetchone()
            if not got:
                ok = False
                log("复检", f"✗ 回读不到：{row['name']}")
                continue
            image = got[3] or "-"
            print(f"        id={got[0]} {got[1]}")
            print(f"              official={got[2]!r}")
            print(f"              image={image[:90]!r} contest={got[4]}-{got[5]}")
    finally:
        conn.close()
    return ok


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="把 test_crawl.db 中生产库没有的赛事（及获奖）合并进 competitions.db。",
    )
    parser.add_argument("--source", default=TEST_DB_PATH, help="源库（默认 test_crawl.db）")
    parser.add_argument("--target", default=PRODUCTION_DB_PATH, help="目标库（默认生产库）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只演练）")
    parser.add_argument("--include-placeholders", action="store_true",
                        help="连 category='待确认' / sort_order=9999 的占位赛事一起合并")
    parser.add_argument("--no-awards", action="store_true", help="只合并赛事，不搬获奖记录")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    _setup_console()
    args = parse_args(argv)

    source_path = os.path.abspath(args.source)
    target_path = os.path.abspath(args.target)

    print("=" * 72)
    print("测试库 -> 生产库 赛事/获奖合并")
    print("=" * 72)

    for path, label in ((source_path, "源库"), (target_path, "目标库")):
        if not os.path.exists(path):
            log("错误", f"{label}不存在：{path}")
            return 2
    if source_path == target_path:
        log("错误", "源库与目标库不能是同一个文件")
        return 2

    target_sha_before = sha256_file(target_path)
    log("目标库", f"SHA-256（合并前）{target_sha_before}（{human_size(os.path.getsize(target_path))}）")

    try:
        plan = build_plan(
            source_path, target_path,
            include_placeholders=args.include_placeholders,
            with_awards=not args.no_awards,
        )
    except RuntimeError as exc:
        log("错误", str(exc))
        return 3

    print("-" * 72)
    print_plan(plan, source_path, target_path)
    print("-" * 72)

    if not plan["competitions"] and not plan.get("awards"):
        log("结论", "无事可做：生产库已包含源库的全部赛事与获奖记录")
        return 0

    if not args.apply:
        log("预检", "当前为演练模式，未改动任何数据。确认后加 --apply 执行。")
        return 0

    log("备份", "写库前先备份目标库…")
    try:
        backup_path = make_backup(target_path)
    except Exception as exc:  # noqa: BLE001 - 备份失败必须停
        log("错误", f"备份失败，已终止，未写入任何数据：{exc}")
        return 4
    log("备份", f"已生成 {backup_path}（{human_size(os.path.getsize(backup_path))}）")

    log("写入", "开始插入（单事务，失败整体回滚）…")
    try:
        result = apply_plan(target_path, plan)
    except Exception as exc:  # noqa: BLE001
        log("错误", f"写入失败，已回滚：{exc}")
        log("回滚", f"如需还原：Copy-Item '{backup_path}' '{target_path}' -Force")
        return 5

    log("写入", f"新增 competitions {result['competitions']} 条，"
                f"awards {result['awards']} 条"
                + (f"（{result['award_skip_fk']} 条因唯一约束跳过）" if result["award_skip_fk"] else ""))

    print("-" * 72)
    ok = verify(target_path, plan, result)
    print("-" * 72)

    after_sha = sha256_file(target_path)
    log("结果", f"SHA-256（合并前）{target_sha_before}")
    log("结果", f"SHA-256（合并后）{after_sha}")
    log("结果", f"备份文件：{backup_path}")

    if ok:
        log("完成", "合并成功。前端刷新即可看到新增赛事的官网/封面/比赛月份。")
        return 0
    log("完成", "写入已提交，但复检有异常，请按上面的 ✗ 逐条核对（备份可回滚）。")
    return 7


if __name__ == "__main__":
    sys.exit(main())
