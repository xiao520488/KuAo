# -*- coding: utf-8 -*-
"""AI 攻略 / ai_jobs / 按需接口的单元与接口测试。

覆盖（对应 AI_AUTOMATION_PLAN.md 的 A/B/C 验收点）：

  * 结构：init_db 建出 ai_jobs + 4 个 guide_* 列；
  * 抢占：同赛事同类任务只允许 1 个活跃任务；失败冷却；15 分钟僵尸回收；
  * 写入：save_generated_guide 写攻略 + 溯源字段；
  * Prompt：月份/「未公布」/获奖作品进入 Prompt；输入指纹随事实变化；
  * 接口：未登录 401、首次 202、重复触发幂等同 job、完成后再触发 200、
    失败冷却 409、每日预算 429、不存在 404；
  * 列表瘦身：GET /api/competitions 不再返回 full_process_guide，详情返回；
  * 孤儿清理：count/delete 只动 competition_id 为空或悬空的行。

运行::

    python -m pytest test_ai_guide.py -q

所有大模型调用都被 mock，不联网、不产生费用；每个测试类使用独立临时库。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import ai_content
import config
import db
import main
import security

_PASSWORD_HASH = security.hash_password("pass1234")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.mkdtemp(prefix="kuao-ai-guide-test-")
        cls._orig_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(cls._tmpdir, "test.db")
        db.init_db()
        cls._client_cm = TestClient(main.app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._client_cm.__exit__(None, None, None)
        finally:
            db.DB_PATH = cls._orig_db_path
            shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self) -> None:
        self._orig_limits = (
            config.AI_GUIDE_DAILY_LIMIT,
            config.AI_GUIDE_QUEUE_MAX,
            config.AI_GUIDE_FAIL_COOLDOWN_MIN,
        )
        config.AI_GUIDE_DAILY_LIMIT = 8
        config.AI_GUIDE_QUEUE_MAX = 5
        config.AI_GUIDE_FAIL_COOLDOWN_MIN = 30
        self._clear_tables()

    def tearDown(self) -> None:
        (
            config.AI_GUIDE_DAILY_LIMIT,
            config.AI_GUIDE_QUEUE_MAX,
            config.AI_GUIDE_FAIL_COOLDOWN_MIN,
        ) = self._orig_limits

    @staticmethod
    def _clear_tables() -> None:
        with db.get_conn() as conn:
            for table in (
                "ai_jobs", "ai_analysis_cache", "user_preparations", "ai_usage",
                "insights", "ideas", "awards", "competitions", "users",
            ):
                conn.execute("DELETE FROM %s" % table)

    # ---------------------------------------------------------------- 工厂
    def _new_user(self, username: str = "tester") -> tuple:
        uid = db.create_user(username, _PASSWORD_HASH)
        return uid, security.create_access_token(uid, username)

    @staticmethod
    def _auth(token: str) -> dict:
        return {"Authorization": "Bearer %s" % token}

    def _new_competition(self, name: str = "全国大学生数学建模竞赛", **kw) -> int:
        data = {
            "name": name,
            "category": "数学建模",
            "reg_start_month": 3,
            "reg_end_month": 7,
            "official_url": "https://example.edu.cn",
            "level": "national",
            "level_label": "国家级",
            "sort_order": 10,
            "intro": "测试用赛事",
        }
        data.update(kw)
        return db.add_competition(data)

    @staticmethod
    def _wait_job(job_id: int, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with db.get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM ai_jobs WHERE id = ?", (job_id,)
                ).fetchone()
            if row is not None and row["status"] not in ("queued", "running"):
                return dict(row)
            time.sleep(0.05)
        raise AssertionError("任务 %s 在 %.1fs 内未结束" % (job_id, timeout))


class SchemaAndDbTests(_Base):
    def test_ai_jobs_table_and_guide_columns_exist(self) -> None:
        with db.get_conn() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            assert "ai_jobs" in tables
            cols = {r[1] for r in conn.execute("PRAGMA table_info(competitions)")}
        for col in (
            "guide_generated_at", "guide_model", "guide_prompt_ver", "guide_input_hash"
        ):
            assert col in cols, "缺少列 %s" % col

    def test_claim_is_exclusive_and_cooldown(self) -> None:
        cid = self._new_competition()
        first = db.claim_ai_job("guide", cid, trigger="on_demand")
        assert first["claimed"] is True
        second = db.claim_ai_job("guide", cid, trigger="daily")
        assert second["claimed"] is False
        assert second["reason"] == "active"
        assert second["job_id"] == first["job_id"]

        # 失败后立刻再抢：冷却生效
        assert db.start_ai_job(first["job_id"]) is True
        db.fail_ai_job(first["job_id"], "boom")
        cooled = db.claim_ai_job("guide", cid, cooldown_minutes=30)
        assert cooled["claimed"] is False
        assert cooled["reason"] == "cooldown"
        assert cooled["retry_after_sec"] > 0

        # 冷却为 0 时可以立刻重试（新任务）
        retry = db.claim_ai_job("guide", cid, cooldown_minutes=0)
        assert retry["claimed"] is True
        assert retry["job_id"] != first["job_id"]

    def test_stale_running_job_is_reaped(self) -> None:
        cid = self._new_competition()
        job = db.claim_ai_job("guide", cid)
        db.start_ai_job(job["job_id"])
        # 伪造成 20 分钟前开始，下一次抢占应把它回收
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE ai_jobs SET started_at = datetime('now','localtime','-20 minutes') "
                "WHERE id = ?", (job["job_id"],),
            )
        another = self._new_competition("挑战杯")
        db.claim_ai_job("guide", another)
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT status, error FROM ai_jobs WHERE id = ?", (job["job_id"],)
            ).fetchone()
        assert row["status"] == "failed"
        assert "stale" in (row["error"] or "")

    def test_save_generated_guide_writes_metadata(self) -> None:
        cid = self._new_competition()
        db.save_generated_guide(
            cid, "## 一、赛前准备\n内容", model="deepseek-flash",
            prompt_ver="v2", input_hash="abc123",
        )
        comp = db.get_competition(cid)
        assert comp["full_process_guide"].startswith("## 一、赛前准备")
        assert comp["guide_model"] == "deepseek-flash"
        assert comp["guide_prompt_ver"] == "v2"
        assert comp["guide_input_hash"] == "abc123"
        assert comp["guide_generated_at"]
        state = db.get_guide_state(cid)
        assert state["status"] == "ready"
        assert state["guide_length"] > 0

    def test_orphan_ai_content_count_and_delete(self) -> None:
        cid = self._new_competition()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO insights (kind, scope, competition_id, content_md) "
                "VALUES ('gap', 's', NULL, 'orphan')"
            )
            conn.execute(
                "INSERT INTO ideas (competition_id, title) VALUES (?, 'ok')", (cid,)
            )
            conn.execute(
                "INSERT INTO ideas (competition_id, title) VALUES (NULL, 'orphan')"
            )
        before = db.count_orphan_ai_content()
        assert before == {"insights": 1, "ideas": 1}
        removed = db.delete_orphan_ai_content()
        assert removed == {"insights": 1, "ideas": 1}
        assert db.count_orphan_ai_content() == {"insights": 0, "ideas": 0}
        # 有归属的 idea 保留
        assert len(db.list_ideas_by_competition(cid)) == 1


class PromptTests(unittest.TestCase):
    COMP = {
        "id": 1, "name": "蓝桥杯", "category": "程序设计", "level": "national",
        "level_label": "国家级", "official_url": "https://dasai.lanqiao.cn",
        "reg_start_month": 10, "reg_end_month": 3, "reg_cross_year": 1,
        "contest_start_month": 4, "contest_end_month": 4,
        "intro": "软件类竞赛",
    }
    AWARDS = [{"id": 1, "year": 2025, "title": "智能排课系统", "award_grade": "国家级一等奖",
               "tech_stack": ["Python", "Vue"]}]

    def test_prompt_contains_months_and_awards(self) -> None:
        prompt = ai_content.build_guide_prompt(self.COMP, self.AWARDS)
        assert "10 月 - 次年 3 月" in prompt
        assert "4 月" in prompt
        assert "智能排课系统" in prompt
        assert "国家级一等奖" in prompt
        assert "Python" in prompt

    def test_prompt_says_unknown_when_months_missing(self) -> None:
        comp = dict(self.COMP)
        comp.update(reg_start_month=None, reg_end_month=None,
                    contest_start_month=None, contest_end_month=None)
        prompt = ai_content.build_guide_prompt(comp, [])
        assert prompt.count("未公布") >= 2
        assert "不要编造往届获奖情况" in prompt

    def test_input_hash_changes_with_facts(self) -> None:
        base = ai_content.guide_input_hash(self.COMP, self.AWARDS)
        changed = dict(self.COMP)
        changed["official_url"] = "https://other.example.com"
        assert ai_content.guide_input_hash(changed, self.AWARDS) != base
        assert ai_content.guide_input_hash(self.COMP, self.AWARDS) == base

    def test_strip_fence(self) -> None:
        text = "```markdown\n## 一、赛前准备\n" + "内容" * 200 + "\n```"
        out = ai_content._strip_fence(text)
        assert out.startswith("## 一、赛前准备")
        assert "```" not in out


class GuideApiTests(_Base):
    def _fake_guide(self, *args, **kwargs) -> dict:
        return {
            "markdown": "## 一、赛前准备\n" + "按官方通知准备材料。" * 30,
            "tokens": 1234,
            "model": "fake-model",
            "prompt_ver": ai_content.GUIDE_PROMPT_VER,
            "input_hash": "fakehash",
        }

    def _slow_fake_guide(self, *args, **kwargs) -> dict:
        """让第一次请求稳定处于 running，才能验证幂等（否则可能已生成完）。"""
        time.sleep(0.4)
        return self._fake_guide()

    def test_requires_login(self) -> None:
        cid = self._new_competition()
        resp = self.client.post(f"/api/competitions/{cid}/generate_guide")
        assert resp.status_code == 401
        resp2 = self.client.get(f"/api/competitions/{cid}/generate_guide")
        assert resp2.status_code == 401

    def test_404_for_missing_competition(self) -> None:
        _, token = self._new_user()
        resp = self.client.post(
            "/api/competitions/99999/generate_guide", headers=self._auth(token)
        )
        assert resp.status_code == 404

    def test_generate_flow_idempotent_and_ready(self) -> None:
        cid = self._new_competition()
        _, token = self._new_user()

        with patch.object(ai_content, "generate_guide", side_effect=self._slow_fake_guide):
            first = self.client.post(
                f"/api/competitions/{cid}/generate_guide", headers=self._auth(token)
            )
            assert first.status_code == 202, first.text
            body = first.json()
            assert body["status"] in ("queued", "running")
            job_id = body["job_id"]
            assert job_id

            # 立刻再点一次：不应产生第二个任务
            second = self.client.post(
                f"/api/competitions/{cid}/generate_guide", headers=self._auth(token)
            )
            assert second.status_code == 202
            assert second.json()["job_id"] == job_id

            job = self._wait_job(job_id)
            assert job["status"] == "done", job

        detail = self.client.get(f"/api/competitions/{cid}").json()
        assert detail["guide_status"] == "ready"
        assert detail["full_process_guide"].startswith("## 一、赛前准备")
        assert detail["guide_generated_at"]

        # 已有攻略：再触发直接 200，不新建任务
        again = self.client.post(
            f"/api/competitions/{cid}/generate_guide", headers=self._auth(token)
        )
        assert again.status_code == 200
        assert again.json()["status"] == "ready"
        with db.get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0]
        assert total == 1

        status = self.client.get(
            f"/api/competitions/{cid}/generate_guide", headers=self._auth(token)
        ).json()
        assert status["status"] == "ready"
        assert status["guide_length"] > 0

    def test_list_payload_excludes_guide(self) -> None:
        cid = self._new_competition()
        db.save_generated_guide(cid, "## 攻略\n" + "内容" * 300)
        items = self.client.get("/api/competitions").json()
        assert items and "full_process_guide" not in items[0]
        assert "summary" in items[0]  # 其它字段仍在

    def test_cooldown_returns_409(self) -> None:
        cid = self._new_competition()
        _, token = self._new_user()
        claim = db.claim_ai_job("guide", cid)
        db.start_ai_job(claim["job_id"])
        db.fail_ai_job(claim["job_id"], "llm timeout")
        resp = self.client.post(
            f"/api/competitions/{cid}/generate_guide", headers=self._auth(token)
        )
        assert resp.status_code == 409
        assert resp.json()["reason"] == "cooldown"
        assert resp.headers.get("Retry-After")

    def test_daily_budget_returns_429(self) -> None:
        config.AI_GUIDE_DAILY_LIMIT = 1
        first = self._new_competition("赛事 A")
        second = self._new_competition("赛事 B")
        _, token = self._new_user()

        with patch.object(ai_content, "generate_guide", side_effect=self._fake_guide):
            r1 = self.client.post(
                f"/api/competitions/{first}/generate_guide", headers=self._auth(token)
            )
            assert r1.status_code == 202
            self._wait_job(r1.json()["job_id"])
            r2 = self.client.post(
                f"/api/competitions/{second}/generate_guide", headers=self._auth(token)
            )
        assert r2.status_code == 429
        assert r2.json()["reason"] == "daily_budget"
        assert r2.headers.get("Retry-After")

    def test_failure_marks_job_failed_and_status(self) -> None:
        cid = self._new_competition()
        _, token = self._new_user()

        def boom(*args, **kwargs):
            raise RuntimeError("mock LLM down")

        with patch.object(ai_content, "generate_guide", side_effect=boom):
            resp = self.client.post(
                f"/api/competitions/{cid}/generate_guide", headers=self._auth(token)
            )
            assert resp.status_code == 202
            job = self._wait_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "RuntimeError" in (job["error"] or "")
        status = self.client.get(
            f"/api/competitions/{cid}/generate_guide", headers=self._auth(token)
        ).json()
        assert status["status"] == "failed"
        # 详情接口同样反映 failed（前端据此显示重试按钮）
        assert self.client.get(f"/api/competitions/{cid}").json()["guide_status"] == "failed"


if __name__ == "__main__":
    unittest.main(verbosity=2)
