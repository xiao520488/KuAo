#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_prod_safe.py — 本地 → 云端【只同步业务表】的安全数据同步工具。

背景（2026-11-11 事故）：用 `cp`/`install` 把整库覆盖到云端，`users` 表被本地
版本替换，真实注册用户全部丢失。本工具的存在就是为了让这类操作在物理上不可
能发生：

  1. 白名单硬编码：只允许 `competitions` / `awards` / `competition_candidates`；
     其它任何表既不读也不写（含 users / user_preparations / feedbacks /
     ai_jobs / ai_usage / ai_analysis_cache / ideas / insights / search_leads /
     crawl_jobs）。
  2. 只 UPSERT（`ON CONFLICT ... DO UPDATE`），**绝不 DELETE / DROP / REPLACE**。
     `REPLACE INTO` 会先删后插，进而触发 `ON DELETE CASCADE` 把用户的备赛计划
     一起带走，所以连 REPLACE 都在禁止列表里。
  3. 全表指纹护栏：写入事务开始前记录目标库**除白名单外所有表**的行级指纹，
     写完后重算；不一致立即 ROLLBACK 并报错。用户表哪怕只动一行也会被拦下。
  4. 写入前自动备份目标库（`sqlite3.Connection.backup()`，含已提交 WAL 的一致
     快照），并用 `PRAGMA integrity_check` / `foreign_key_check` 复核结果。
  5. 默认 dry-run（只看计划不写库），`--apply` 才落盘；跨库读取用
     `ATTACH DATABASE 'file:...?mode=ro'`，源库以只读方式挂载。
  6. 另附 `--rescue-from`：从云端事故前备份里**只回灌用户资产**
     （users / user_preparations / feedbacks / ai_usage），按主键补齐缺失行，
     不删除、不覆盖已有行；用于本次事故的数据恢复。

用法
----
    # 1) 先看计划（默认，不写任何东西）
    python sync_prod_safe.py --source competitions.db --target prod-snapshot.db

    # 2) 确认无误后落盘（自动备份目标库）
    python sync_prod_safe.py --source competitions.db --target prod-snapshot.db --apply

    # 3) 只想生成差异 SQL 包，拿回服务器上执行
    python sync_prod_safe.py --source competitions.db --export-sql workspace/sync.sql

    # 4) 事故恢复：从事故前备份回灌用户数据
    python sync_prod_safe.py --target competitions.db \
        --rescue-from backups/competitions.db.bak-20261111-224000-pre-db-sync --apply

云端执行的最安全姿势（不在本地持有云端库文件也行）：
    ssh root@<host> "systemctl stop kuao-backend"
    scp root@<host>:/root/KuAo/backend/competitions.db ./workspace/prod-snapshot.db
    python sync_prod_safe.py --target ./workspace/prod-snapshot.db --apply
    scp ./workspace/prod-snapshot.db root@<host>:/tmp/prod-sync.db
    # 云端：install 回位 + 删 -wal/-shm + 起服务（这一步仍由人工执行）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOURCE = os.path.join(BASE_DIR, "competitions.db")
DEFAULT_BACKUP_DIR = os.path.join(BASE_DIR, "backups")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")

# ---------------------------------------------------------------------------
# 红线：白名单（可写）与受保护表（只读、只做指纹校验）
# ---------------------------------------------------------------------------
BUSINESS_TABLES = ("competitions", "awards", "competition_candidates")

PROTECTED_TABLES = (
    "users",
    "user_preparations",
    "feedbacks",
    "ai_jobs",
    "ai_usage",
    "ai_analysis_cache",
    "ideas",
    "insights",
    "search_leads",
    "crawl_jobs",
)

# 事故恢复（--rescue-from）只允许回灌用户资产，且逻辑是"补齐缺失行"
RESCUE_TABLES = ("users", "user_preparations", "feedbacks", "ai_usage")

FORBIDDEN_SQL_TOKENS = ("delete", "drop", "truncate", "replace", "alter")


class SyncError(RuntimeError):
    """红线被触碰或校验失败：一律中止，不做任何写入。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def q(ident: str) -> str:
    """安全引用标识符（表名/列名只来自 schema 或白名单）。"""
    return '"' + ident.replace('"', '""') + '"'


def file_uri(path: str, mode: str | None = None) -> str:
    """把 Windows/POSIX 路径转成 SQLite 能认的 URI（ATTACH 也走这套）。"""
    from urllib.parse import quote

    posix = os.path.abspath(path).replace("\\", "/")
    if not posix.startswith("/"):  # C:/xxx → /C:/xxx
        posix = "/" + posix
    uri = "file:" + quote(posix, safe="/:")
    return f"{uri}?mode={mode}" if mode else uri


def connect(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise SyncError(f"数据库不存在：{path}")
    if readonly:
        con = sqlite3.connect(file_uri(path, "ro"), uri=True)
    else:
        # uri=True 让本连接后续的 ATTACH 也能识别 file:...?mode=ro（只读挂载源库）
        con = sqlite3.connect(file_uri(path), uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def app_tables(con: sqlite3.Connection, schema: str = "main") -> list[str]:
    rows = con.execute(
        f"SELECT name FROM {schema}.sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def columns_of(con: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    rows = con.execute(f"PRAGMA {schema}.table_info({q(table)})").fetchall()
    return [r["name"] for r in rows]


def count_of(con: sqlite3.Connection, table: str, schema: str = "main") -> int:
    return con.execute(f"SELECT COUNT(*) FROM {schema}.{q(table)}").fetchone()[0]


def digest_of(con: sqlite3.Connection, table: str, schema: str = "main") -> dict:
    """行级指纹：行数 + 全部列取值的 SHA-256（按 rowid 排序，稳定可复算）。"""
    h = hashlib.sha256()
    n = 0
    cursor = con.execute(f"SELECT * FROM {schema}.{q(table)} ORDER BY rowid")
    names = [d[0] for d in cursor.description] or []
    h.update(("\x1f".join(names)).encode("utf-8"))
    for row in cursor:
        h.update(b"\x1e")
        for value in row:
            h.update(type(value).__name__.encode("utf-8"))
            h.update(b"\x1d")
            h.update(repr(value).encode("utf-8"))
        n += 1
    return {"rows": n, "sha256": h.hexdigest()}


def digest_all(con: sqlite3.Connection, *, schema: str = "main", exclude: tuple[str, ...] = ()) -> dict:
    out = {}
    for table in app_tables(con, schema):
        if table in exclude:
            continue
        out[table] = digest_of(con, table, schema)
    return out


def unique_keys(con: sqlite3.Connection, table: str, schema: str = "main") -> list[dict]:
    """收集非 partial 的唯一索引，用于检测"同一业务行在目标库是另一个主键"。"""
    keys = []
    for idx in con.execute(f"PRAGMA {schema}.index_list({q(table)})").fetchall():
        # fields: seq, name, unique, origin, partial
        if not idx["unique"] or idx["partial"] or idx["origin"] == "pk":
            continue
        cols = [
            r["name"]
            for r in con.execute(f"PRAGMA {schema}.index_info({q(idx['name'])})").fetchall()
        ]
        if cols:
            keys.append({"name": idx["name"], "columns": cols})
    return keys


def backup_target(path: str, backup_dir: str, label: str = "pre-sync") -> str:
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_dir, f"{os.path.basename(path)}.bak-{stamp}-{label}")
    src = sqlite3.connect(path, timeout=30)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)  # 含已提交 WAL 的一致性快照
            dst.execute("PRAGMA journal_mode = DELETE")
            dst.execute("PRAGMA integrity_check").fetchone()
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def guard_sql(sql: str) -> None:
    low = sql.lower()
    for token in FORBIDDEN_SQL_TOKENS:
        if f" {token} " in low or f"\n{token} " in low or low.startswith(f"{token} "):
            raise SyncError(f"生成语句含禁止操作 {token.upper()}：{sql[:120]}…")


# ---------------------------------------------------------------------------
# 差异计算：只算白名单表
# ---------------------------------------------------------------------------
def plan_table(con: sqlite3.Connection, table: str) -> dict:
    """对比 main（目标）与 src（源），返回 inserts / updates / conflicts / stale。"""
    src_cols = columns_of(con, table, "src")
    dst_cols = columns_of(con, table, "main")
    if not src_cols or not dst_cols:
        raise SyncError(f"表 {table} 在源库或目标库缺失")
    if set(src_cols) != set(dst_cols):
        only_src = sorted(set(src_cols) - set(dst_cols))
        only_dst = sorted(set(dst_cols) - set(src_cols))
        raise SyncError(
            f"表 {table} 结构不一致（源多列 {only_src} / 目标多列 {only_dst}）；"
            "先对齐两侧代码版本再同步"
        )

    src_rows = con.execute(f"SELECT * FROM src.{q(table)}").fetchall()
    dst_rows = {
        r["id"]: dict(r)
        for r in con.execute(f"SELECT * FROM main.{q(table)}")
    } if "id" in dst_cols else {}

    # 目标库唯一键索引：content_hash / (source,source_key) / username 等
    key_indexes = unique_keys(con, table, "main") if "id" in dst_cols else []
    key_maps = []
    for spec in key_indexes:
        mapping = {}
        for pk, row in dst_rows.items():
            values = tuple(row[c] for c in spec["columns"])
            if any(v is None for v in values):
                continue
            mapping[values] = pk
        key_maps.append((spec, mapping))

    inserts, updates, conflicts, unchanged = [], [], [], 0
    for row in src_rows:
        item = {c: row[c] for c in src_cols}
        pk = item.get("id")
        if pk in dst_rows:
            old = dst_rows[pk]
            changed = {c: item[c] for c in src_cols if old.get(c) != item[c]}
            if changed:
                updates.append({"id": pk, "columns": sorted(changed)})
            else:
                unchanged += 1
            continue

        conflict = None
        for spec, mapping in key_maps:
            values = tuple(item.get(c) for c in spec["columns"])
            if any(v is None for v in values):
                continue
            hit = mapping.get(values)
            if hit is not None:
                conflict = {
                    "id": pk,
                    "key": spec["name"],
                    "columns": spec["columns"],
                    "existing_id": hit,
                }
                break
        if conflict:
            conflicts.append(conflict)
        else:
            inserts.append(pk)

    src_ids = {r["id"] for r in src_rows}
    stale = sorted(set(dst_rows) - src_ids)
    return {
        "table": table,
        "source_rows": len(src_rows),
        "target_rows": len(dst_rows),
        "inserts": inserts,
        "updates": updates,
        "conflicts": conflicts,
        "unchanged": unchanged,
        "stale_on_target": stale,
    }


def print_plan(plan: dict) -> None:
    name = plan["table"]
    print(
        f"  {name:<22} 源 {plan['source_rows']:>5} 行 / 目标 {plan['target_rows']:>5} 行 | "
        f"新增 {len(plan['inserts']):>5} 更新 {len(plan['updates']):>5} "
        f"相同 {plan['unchanged']:>5} 冲突 {len(plan['conflicts']):>3} "
        f"目标独有 {len(plan['stale_on_target']):>4}"
    )
    if plan["conflicts"]:
        detail = ", ".join(
            f"id={c['id']}({c['key']} 已存在于 id={c['existing_id']})"
            for c in plan["conflicts"][:5]
        )
        print(f"      冲突（自动跳过，不覆盖）：{detail}")


# ---------------------------------------------------------------------------
# 同步实现
# ---------------------------------------------------------------------------
def sync(source: str, target: str, *, apply: bool, backup_dir: str, make_backup: bool,
         ids: set[int] | None, dry_run_report: str | None = None) -> dict:
    source = os.path.abspath(source)
    target = os.path.abspath(target)
    if source == target:
        raise SyncError("源库与目标库是同一个文件；本工具只做跨库同步")
    if not os.path.exists(source):
        raise SyncError(f"源库不存在：{source}")

    con = connect(target)
    try:
        con.execute("ATTACH DATABASE ? AS src", (file_uri(source, "ro"),))
        # SQLite 的 ATTACH 不识别 URI 时退化为普通路径
        try:
            con.execute("SELECT COUNT(*) FROM src.competitions").fetchone()
            src_ok = True
        except sqlite3.Error:
            src_ok = False
        if not src_ok:
            con.execute("DETACH DATABASE src")
            con.execute("ATTACH DATABASE ? AS src", (file_uri(source),))

        missing = [t for t in BUSINESS_TABLES if t not in app_tables(con, "src")]
        if missing:
            raise SyncError(f"源库缺少白名单表：{missing}")

        print(f"[源] {source}")
        print(f"[目标] {target}")
        print(f"[白名单] {', '.join(BUSINESS_TABLES)}")
        print(f"[受保护表] {', '.join(sorted(set(app_tables(con)) - set(BUSINESS_TABLES)))}")

        plans = []
        for table in BUSINESS_TABLES:
            plan = plan_table(con, table)
            if ids and table == "competitions":
                plan["inserts"] = [i for i in plan["inserts"] if i in ids]
                plan["updates"] = [u for u in plan["updates"] if u["id"] in ids]
            plans.append(plan)
            print_plan(plan)

        protected_before = digest_all(con, exclude=BUSINESS_TABLES)
        fk_before = _fk_violations(con)

        report = {
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "target": target,
            "apply": apply,
            "plans": plans,
            "protected_tables": {t: d["rows"] for t, d in protected_before.items()},
            "warnings": [],
        }

        stale_total = sum(len(p["stale_on_target"]) for p in plans)
        conflicts_total = sum(len(p["conflicts"]) for p in plans)
        if stale_total:
            report["warnings"].append(
                f"目标库有 {stale_total} 行本地不存在（不会删除；比赛可用 --deactivate-missing 下架）"
            )
        if conflicts_total:
            report["warnings"].append(f"{conflicts_total} 行因唯一键冲突被跳过")

        if not apply:
            print("\n[DRY-RUN] 未写入任何数据。加 --apply 才会落盘。")
            report["result"] = "dry-run"
            _write_report(report, dry_run_report)
            return report

        backup_path = None
        if make_backup:
            backup_path = backup_target(target, backup_dir, "pre-sync")
            print(f"\n[备份] {backup_path}")
        report["backup"] = backup_path

        written = {"inserted": 0, "updated": 0}
        with con:  # 单事务：要么全成，要么全回滚
            for plan in plans:
                table = plan["table"]
                cols = columns_of(con, table, "main")
                update_cols = [c for c in cols if c != "id"]
                upsert = (
                    f"INSERT INTO main.{q(table)} ({', '.join(q(c) for c in cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)}) "
                    f"ON CONFLICT({q('id')}) DO UPDATE SET "
                    + ", ".join(f"{q(c)} = excluded.{q(c)}" for c in update_cols)
                )
                guard_sql(upsert)
                pk_list = plan["inserts"] + [u["id"] for u in plan["updates"]]
                if not pk_list:
                    continue
                placeholders = ", ".join("?" for _ in pk_list)
                rows = con.execute(
                    f"SELECT {', '.join(q(c) for c in cols)} FROM src.{q(table)} "
                    f"WHERE id IN ({placeholders})",
                    pk_list,
                ).fetchall()
                for row in rows:
                    con.execute(upsert, tuple(row[c] for c in cols))
                written["inserted"] += len(plan["inserts"])
                written["updated"] += len(plan["updates"])

            protected_after = digest_all(con, exclude=BUSINESS_TABLES)
            fk_after = _fk_violations(con)

            if protected_after != protected_before:
                changed = sorted(
                    t for t in set(protected_before) | set(protected_after)
                    if protected_before.get(t) != protected_after.get(t)
                )
                raise SyncError(f"受保护表被改动：{changed}；事务已回滚")
            new_fk = fk_after - fk_before
            if new_fk:
                raise SyncError(f"出现新的外键违规：{sorted(new_fk)[:5]}；事务已回滚")

        integrity = con.execute("PRAGMA main.integrity_check").fetchone()[0]
        counts = {t: count_of(con, t) for t in app_tables(con)}
        report.update(
            {
                "result": "applied",
                "written": written,
                "integrity_check": integrity,
                "protected_after": {t: d["rows"] for t, d in protected_after.items()},
                "counts": counts,
            }
        )
        print(f"\n[完成] 新增 {written['inserted']} 行 / 更新 {written['updated']} 行；"
              f"integrity={integrity}")
        print("[受保护表] 行数与指纹未变："
              + ", ".join(f"{t}={d['rows']}" for t, d in protected_before.items()))
        _write_report(report, None)
        return report
    finally:
        con.close()


def _fk_violations(con: sqlite3.Connection) -> set:
    out = set()
    for row in con.execute("PRAGMA main.foreign_key_check").fetchall():
        out.add((row[0], row[1], row[2] if len(row) > 2 else None))
    return out


def _write_report(report: dict, path: str | None) -> None:
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    path = path or os.path.join(
        WORKSPACE_DIR, f"sync_prod_safe_{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"[报告] {path}")


# ---------------------------------------------------------------------------
# 差异 SQL 包（拿回服务器执行，不需要本地持有云端库）
# ---------------------------------------------------------------------------
def export_sql(source: str, out_path: str) -> str:
    con = connect(source, readonly=True)
    try:
        lines = [
            "-- 由 sync_prod_safe.py 生成：只含白名单表，只做 UPSERT，不含 DELETE/DROP/REPLACE",
            f"-- 源库: {os.path.abspath(source)}",
            "-- 执行前务必先备份目标库（backup_db.py），并在事务里跑：",
            "--   sqlite3 competitions.db < sync.sql",
            "BEGIN IMMEDIATE;",
        ]
        for table in BUSINESS_TABLES:
            cols = columns_of(con, table)
            rows = con.execute(f"SELECT * FROM {q(table)}").fetchall()
            if not rows:
                continue
            update_cols = [c for c in cols if c != "id"]
            header = (
                f"INSERT INTO {q(table)} ({', '.join(q(c) for c in cols)}) VALUES "
                f"({', '.join('?' for _ in cols)}) ON CONFLICT({q('id')}) DO UPDATE SET "
                + ", ".join(f"{q(c)} = excluded.{q(c)}" for c in update_cols)
            )
            for row in rows:
                stmt = header.replace("?", "{}")
                # 逐列填充字面量（列数固定，按顺序替换）
                values = [sql_literal(row[c]) for c in cols]
                parts = stmt.split("{}")
                sql = parts[0]
                for value, rest in zip(values, parts[1:]):
                    sql += value + rest
                guard_sql(sql)
                lines.append(sql + ";")
        lines.append("COMMIT;")
        sql_text = "\n".join(lines) + "\n"
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(sql_text)
        return out_path
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 事故恢复：只回灌用户资产
# ---------------------------------------------------------------------------
def rescue_users(target: str, backup: str, *, apply: bool, backup_dir: str,
                 make_backup: bool) -> dict:
    target = os.path.abspath(target)
    backup = os.path.abspath(backup)
    if target == backup:
        raise SyncError("目标库与备份是同一个文件")

    con = connect(target)
    try:
        con.execute("ATTACH DATABASE ? AS bak", (file_uri(backup, "ro"),))
        try:
            con.execute("SELECT COUNT(*) FROM bak.users").fetchone()
        except sqlite3.Error:
            con.execute("DETACH DATABASE bak")
            con.execute("ATTACH DATABASE ? AS bak", (file_uri(backup),))

        print(f"[备份源] {backup}")
        print(f"[目标]   {target}")
        plans = {}
        bak_tables = set(app_tables(con, "bak"))
        main_tables = set(app_tables(con))
        for table in RESCUE_TABLES:
            if table not in bak_tables or table not in main_tables:
                print(f"[{table}] 备份或目标库缺该表，跳过")
                continue
            cols = columns_of(con, table, "bak")
            bak_rows = con.execute(f"SELECT * FROM bak.{q(table)}").fetchall()
            if table == "users":
                existing_ids = {r[0] for r in con.execute("SELECT id FROM main.users")}
                existing_names = {r[0] for r in con.execute("SELECT username FROM main.users")}
                todo = [
                    row for row in bak_rows
                    if row["id"] not in existing_ids and row["username"] not in existing_names
                ]
                skipped_names = [
                    row["username"] for row in bak_rows
                    if row["username"] in existing_names and row["id"] not in existing_ids
                ]
                print(f"[users] 备份 {len(bak_rows)} 行，可补回 {len(todo)} 行，"
                      f"用户名冲突 {len(skipped_names)} 行")
                if skipped_names:
                    print(f"        冲突用户名（不同 id，需人工裁决）：{skipped_names[:5]}")
                plans[table] = {"rows": len(todo), "skipped": skipped_names}
            else:
                if table == "user_preparations":
                    # 目标库现存的用户 + 本事务里即将补回的备份用户（users 在循环中最先处理）
                    valid = {r[0] for r in con.execute("SELECT id FROM main.users")}
                    users_plan = plans.get("users") or {}
                    pending = users_plan.get("payload") or []
                    if pending:
                        id_pos = users_plan["columns"].index("id")
                        valid |= {row[id_pos] for row in pending}
                    todo = [row for row in bak_rows if row["user_id"] in valid]
                    existing = {
                        (r["user_id"], r["competition_id"])
                        for r in con.execute("SELECT user_id, competition_id FROM main.user_preparations")
                    }
                    todo = [row for row in todo if (row["user_id"], row["competition_id"]) not in existing]
                elif table == "ai_usage":
                    existing = {
                        (r["user_id"], r["usage_date"])
                        for r in con.execute("SELECT user_id, usage_date FROM main.ai_usage")
                    }
                    todo = [row for row in bak_rows if (row["user_id"], row["usage_date"]) not in existing]
                else:  # feedbacks
                    existing_ids = {r[0] for r in con.execute("SELECT id FROM main.feedbacks")}
                    todo = [row for row in bak_rows if row["id"] not in existing_ids]
                print(f"[{table}] 备份 {len(bak_rows)} 行，可补回 {len(todo)} 行")
                plans[table] = {"rows": len(todo)}
            plans[table]["columns"] = cols
            plans[table]["payload"] = [tuple(row[c] for c in cols) for row in todo]

        if not apply:
            print("\n[DRY-RUN] 未写入。加 --apply 回落用户资产。")
            return {"result": "dry-run", "plans": {k: v["rows"] for k, v in plans.items()}}

        if make_backup:
            print(f"[备份] {backup_target(target, backup_dir, 'pre-rescue')}")

        protected = [t for t in app_tables(con) if t not in RESCUE_TABLES]
        before = {t: digest_of(con, t) for t in protected}
        written = {}
        with con:
            for table, plan in plans.items():
                cols = plan["columns"]
                sql = (
                    f"INSERT INTO main.{q(table)} ({', '.join(q(c) for c in cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)}) ON CONFLICT DO NOTHING"
                )
                guard_sql(sql)
                for payload in plan["payload"]:
                    con.execute(sql, payload)
                written[table] = len(plan["payload"])
            after = {t: digest_of(con, t) for t in protected}
            if after != before:
                raise SyncError("非用户资产表被改动；事务已回滚")
        print(f"[完成] {written}")
        return {"result": "rescued", "written": written}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地→云端安全同步：只同步 competitions / awards / competition_candidates"
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="本地库（默认 competitions.db）")
    parser.add_argument("--target", help="目标库文件（云端库的本地快照，或已挂载路径）")
    parser.add_argument("--apply", action="store_true", help="真正写入；不加则只做 dry-run")
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR, help="写入前备份目录")
    parser.add_argument("--no-backup", action="store_true", help="跳过写入前备份（不推荐）")
    parser.add_argument("--ids", default="", help="只同步指定 competitions id，逗号分隔")
    parser.add_argument("--export-sql", default="", help="改为生成差异 SQL 包，不连目标库")
    parser.add_argument("--rescue-from", default="", help="事故恢复：从该备份回灌用户资产")
    parser.add_argument("--report", default="", help="dry-run 报告输出路径")
    args = parser.parse_args(argv)

    ids = {int(x) for x in args.ids.replace(" ", "").split(",") if x.strip().isdigit()}

    try:
        if args.export_sql:
            path = export_sql(args.source, args.export_sql)
            print(f"[SQL 包] {path}")
            return 0
        if args.rescue_from:
            if not args.target:
                raise SyncError("--rescue-from 需要一并给出 --target")
            rescue_users(
                args.target, args.rescue_from, apply=args.apply,
                backup_dir=args.backup_dir, make_backup=not args.no_backup,
            )
            return 0
        if not args.target:
            raise SyncError("请用 --target 指定目标库（或用 --export-sql / --rescue-from）")
        sync(
            args.source, args.target, apply=args.apply, backup_dir=args.backup_dir,
            make_backup=not args.no_backup, ids=ids or None,
            dry_run_report=args.report or None,
        )
        return 0
    except SyncError as exc:
        print(f"[中止] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())