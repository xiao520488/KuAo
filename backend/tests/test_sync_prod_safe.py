# -*- coding: utf-8 -*-
"""sync_prod_safe 的红线测试：业务表可写，用户资产表一行都不能动。"""

import os
import sqlite3
import sys

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import sync_prod_safe as s


SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE competitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT,
    intro TEXT,
    is_active INTEGER DEFAULT 1
);
CREATE TABLE awards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competition_id INTEGER,
    title TEXT NOT NULL,
    year INTEGER NOT NULL,
    content_hash TEXT
);
CREATE UNIQUE INDEX ux_awards_hash ON awards(content_hash) WHERE content_hash IS NOT NULL;
CREATE TABLE competition_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    raw_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE (source, source_key)
);
CREATE TABLE user_preparations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    competition_id INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY (competition_id) REFERENCES competitions (id) ON DELETE CASCADE
);
CREATE TABLE feedbacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    content TEXT NOT NULL
);
CREATE TABLE ai_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    competition_id INTEGER NOT NULL,
    requested_by INTEGER
);
"""


def make_db(path, *, users=(), preps=(), feedbacks=(), jobs=(), comps=(), awards=(), candidates=()):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO users (id, username, password_hash, is_admin) VALUES (?,?,?,?)", users)
    con.executemany("INSERT INTO competitions (id, name, category, intro) VALUES (?,?,?,?)", comps)
    con.executemany("INSERT INTO awards (id, competition_id, title, year, content_hash) VALUES (?,?,?,?,?)",
                    awards)
    con.executemany(
        "INSERT INTO competition_candidates (id, source, source_key, raw_name) VALUES (?,?,?,?)",
        candidates,
    )
    con.executemany("INSERT INTO user_preparations (user_id, competition_id) VALUES (?,?)", preps)
    con.executemany("INSERT INTO feedbacks (user_id, content) VALUES (?,?)", feedbacks)
    con.executemany("INSERT INTO ai_jobs (kind, competition_id, requested_by) VALUES (?,?,?)", jobs)
    con.commit()
    con.close()


@pytest.fixture()
def pair(tmp_path):
    src = str(tmp_path / "local.db")
    dst = str(tmp_path / "cloud.db")
    make_db(src,
            users=[(1, "local_admin", "h", 1)],
            comps=[(1, "本地-机器人大赛", "机器人", "新简介"), (2, "本地-新赛事", "其他", "简介")],
            awards=[(1, 1, "本地作品", 2025, "hash-1")],
            candidates=[(1, "saikr", "k1", "候选赛")])
    make_db(dst,
            users=[(101, "real_alice", "h", 1), (102, "real_bob", "h", 0)],
            preps=[(101, 1), (102, 2)],
            feedbacks=[(101, "真用户反馈")],
            jobs=[("guide", 2, 102)],
            comps=[(1, "云端-机器人大赛", "机器人", "旧简介"), (2, "云端-新赛事", "其他", "简介")],
            awards=[])
    return src, dst


def digests(path, tables):
    con = sqlite3.connect(path)
    try:
        return {t: s.digest_of(con, t) for t in tables}
    finally:
        con.close()


PROTECTED = ("users", "user_preparations", "feedbacks", "ai_jobs")


def test_dry_run_writes_nothing(pair):
    src, dst = pair
    before = digests(dst, PROTECTED + ("competitions",))
    s.sync(src, dst, apply=False, backup_dir=str(os.path.dirname(dst) + "/bak"),
           make_backup=False, ids=None)
    assert digests(dst, PROTECTED + ("competitions",)) == before


def test_apply_only_touches_business_tables(pair):
    src, dst = pair
    protected_before = digests(dst, PROTECTED)
    s.sync(src, dst, apply=True, backup_dir=str(os.path.dirname(dst) + "/bak"),
           make_backup=False, ids=None)
    assert digests(dst, PROTECTED) == protected_before

    con = sqlite3.connect(dst)
    try:
        # 业务表已同步
        assert con.execute("SELECT intro FROM competitions WHERE id=1").fetchone()[0] == "新简介"
        # 云端独有的比赛不会被删（防 CASCADE 带走用户备赛计划）
        assert con.execute("SELECT COUNT(*) FROM user_preparations").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
        # 唯一键冲突被跳过而不是覆盖
        assert con.execute("SELECT COUNT(*) FROM awards").fetchone()[0] == 1
    finally:
        con.close()


def test_rescue_restores_users_and_preparations(tmp_path):
    backup = str(tmp_path / "cloud-pre.db")
    victim = str(tmp_path / "victim.db")
    make_db(backup,
            users=[(101, "real_alice", "h", 1), (102, "real_bob", "h", 0)],
            preps=[(101, 1), (102, 2)],
            feedbacks=[(101, "真用户反馈")])
    make_db(victim, users=[(1, "local_admin", "h", 1)], comps=[(1, "赛事", "其他", "")])

    s.rescue_users(victim, backup, apply=True,
                   backup_dir=str(tmp_path / "bak"), make_backup=False)
    con = sqlite3.connect(victim)
    try:
        names = {r[0] for r in con.execute("SELECT username FROM users")}
        assert {"real_alice", "real_bob", "local_admin"} <= names
        assert con.execute("SELECT COUNT(*) FROM user_preparations").fetchone()[0] == 2
    finally:
        con.close()


def test_export_sql_is_non_destructive(pair, tmp_path):
    src, _ = pair
    out = str(tmp_path / "sync.sql")
    s.export_sql(src, out)
    text = open(out, encoding="utf-8").read().lower()
    for token in (" delete ", " drop ", " replace ", " truncate ", " alter "):
        assert token not in text
    assert "on conflict" in text and "begin immediate" in text


def test_schema_drift_is_rejected(pair):
    src, dst = pair
    con = sqlite3.connect(dst)
    con.execute("ALTER TABLE competitions ADD COLUMN evil TEXT")
    con.commit()
    con.close()
    with pytest.raises(s.SyncError):
        s.sync(src, dst, apply=False, backup_dir=str(os.path.dirname(dst) + "/bak"),
               make_backup=False, ids=None)


def test_whitelist_is_hardcoded():
    assert set(s.BUSINESS_TABLES) == {"competitions", "awards", "competition_candidates"}
    for table in ("users", "user_preparations", "feedbacks", "ai_jobs", "ai_usage",
                  "ideas", "insights", "search_leads", "crawl_jobs", "ai_analysis_cache"):
        assert table in s.PROTECTED_TABLES
        assert table not in s.BUSINESS_TABLES