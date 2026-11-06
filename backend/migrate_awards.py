# -*- coding: utf-8 -*-
"""awards 表补列迁移脚本（幂等 / 只加列 / 不动数据）

背景
----
线上库 competitions.db 是「老库」，awards 表的列与 schema_v2.sql 的定义
存在双向差异：

    * schema_v2.sql 有、线上库没有：advisor / award_level / detail_md
    * 线上库有、schema_v2.sql 没声明：competition_raw / level / session /
      award_grade / track / difficulty / metrics

本脚本按并集把缺的列补上，让「线上老库」与「全新部署」的 awards 表结构一致。

安全性
------
* 只执行 ALTER TABLE awards ADD COLUMN，绝不 DROP TABLE / DELETE / UPDATE 业务行
* 已存在的列自动跳过，可反复执行（幂等）
* 执行前建议备份数据库文件（脚本会提示，但不会替你决定）

用法
----
    python migrate_awards.py            # 体检 + 执行迁移
    python migrate_awards.py --dry-run  # 只体检，不写库
"""

import argparse
import os
import sys

import db


def _snapshot() -> tuple:
    """返回 (列名列表, 行数)；表不存在时列名列表为空。"""
    with db.get_conn() as conn:
        columns = [
            r["name"] for r in conn.execute("PRAGMA table_info(awards)").fetchall()
        ]
        if not columns:
            return [], 0
        row = conn.execute("SELECT COUNT(*) AS c FROM awards").fetchone()
        return columns, int(row["c"])


def _quick_check() -> str:
    with db.get_conn() as conn:
        row = conn.execute("PRAGMA quick_check").fetchone()
    return str(row[0]) if row else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description="awards 表补列迁移（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只体检，不修改数据库")
    args = parser.parse_args()

    print("=" * 66)
    print("awards 表结构迁移" + ("（dry-run，不写库）" if args.dry_run else ""))
    print("=" * 66)
    print("数据库路径:", db.DB_PATH)

    if not os.path.exists(db.DB_PATH):
        print("!! 数据库文件不存在，请先运行 db.init_db() 建库")
        return 1

    before_cols, before_rows = _snapshot()
    if not before_cols:
        print("!! awards 表不存在，请先运行 db.init_db() 建库")
        return 1

    missing = db.missing_award_columns()
    print(f"\n迁移前 awards 列数: {len(before_cols)}")
    print("已有列:", ", ".join(before_cols))
    print(f"\n待补列({len(missing)}):", ", ".join(missing) if missing else "无（已对齐）")
    print(f"迁移前 awards 行数: {before_rows}")

    if not missing:
        print("\n无需迁移，结构已对齐。")
        return 0

    if args.dry_run:
        print("\ndry-run 结束，未修改数据库。")
        return 0

    added = db.migrate_award_columns()

    after_cols, after_rows = _snapshot()
    print(f"\n本次实际新增列({len(added)}):", ", ".join(added) if added else "无")
    print(f"迁移后 awards 列数: {len(after_cols)}")
    print(f"迁移后 awards 行数: {after_rows}")

    # 数据完整性校验
    ok = True
    if after_rows != before_rows:
        print(f"!! 行数发生变化（{before_rows} -> {after_rows}），请立即检查！")
        ok = False
    check = _quick_check()
    print("PRAGMA quick_check:", check)
    if check.lower().strip() != "ok":
        print("!! 数据库完整性检查未通过")
        ok = False

    print("\n" + ("迁移完成，数据完整。" if ok else "迁移完成，但校验未通过，请人工确认。"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
