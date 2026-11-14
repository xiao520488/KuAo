# -*- coding: utf-8 -*-
"""后端 API 边界测试（FastAPI TestClient + 临时 SQLite）。

本文件补齐 ``test_embedder.py`` 未覆盖的「接口层边界条件」：

  * 鉴权：缺失 / 伪造 / 过期 / 用户已删除的 Token；
  * AI 配额：并发抢配额、恰好用完、第 N+1 次 429、无限额度；
  * 竞赛 ID：0 / 负数 / 非整数 / 不存在 / 超大 ID；
  * 灵感接口：缺少 competition_id、非法 ID、不存在赛事；
  * AI 分析：非法入参、不存在赛事/作品、LLM 失败 502、缓存命中与失效；
  * 自然语言检索：空查询、top_k 越界、注入载荷；
  * 备赛清单：非法 ID、不存在赛事、重复添加幂等；
  * LLM 重试：超时重试次数、不可重试异常、空正文、max_tokens 扩容；
  * 数据库：AI 缓存指纹失效、空内容拒写、competition_id 守卫；
  * 请求体上限：当前未实现的防护以 ``skip`` 形式记录（见测试计划 TC-SEC-04）。

运行方式（**本次交付未执行任何测试**）::

    # 仅用标准库，无需 pytest
    python -m unittest -v test_api_boundaries.py

    # 若已安装 pytest
    python -m pytest -v test_api_boundaries.py

约束：
  * 每个测试类使用独立临时数据库，绝不读写 ``competitions.db``；
  * 所有大模型调用均被 mock，不产生真实费用、不访问外网；
  * 不依赖 chromadb / embedding API（检索接口会自动降级为关键词检索）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import jwt
from fastapi.testclient import TestClient
from openai import APITimeoutError

import config
import db
import llm
import main
import security
from routers import ai as ai_router


# 复用同一个 bcrypt 哈希，避免每个用例都付出一次哈希计算成本。
# 明文固定为 "pass1234"（仅测试用）。
_PASSWORD_HASH = security.hash_password("pass1234")


class _TempDBTestCase(unittest.TestCase):
    """所有需要数据库的用例的基类：临时库 + 独立 TestClient。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.mkdtemp(prefix="kuao-backend-test-")
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
        self._orig_quota = config.AI_DAILY_QUOTA
        config.AI_DAILY_QUOTA = 100  # 默认给足额度，避免干扰非配额用例
        self._clear_tables()

    def tearDown(self) -> None:
        config.AI_DAILY_QUOTA = self._orig_quota

    @staticmethod
    def _clear_tables() -> None:
        # 顺序：先删子表，再删主表（外键 ON DELETE 规则之外再显式保证一次）
        with db.get_conn() as conn:
            for table in (
                "ai_analysis_cache",
                "user_preparations",
                "ai_usage",
                "insights",
                "ideas",
                "awards",
                "competitions",
                "users",
            ):
                conn.execute(f"DELETE FROM {table}")

    # ------------------------------------------------------------------
    # 测试数据工厂
    # ------------------------------------------------------------------
    def _new_user(self, username: str = "tester") -> tuple[int, str]:
        """直接建库用户并签发 Token（不经过 bcrypt 注册接口，保证速度）。"""
        uid = db.create_user(username, _PASSWORD_HASH)
        token = security.create_access_token(uid, username)
        return uid, token

    @staticmethod
    def _auth(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _new_competition(self, name: str = "全国大学生数学建模竞赛", **overrides) -> int:
        data = {
            "name": name,
            "category": "数学建模",
            "reg_start_month": 3,
            "reg_end_month": 9,
            "reg_cross_year": 0,
            "official_url": "https://example.invalid",
            "level": "national",
        }
        data.update(overrides)
        return db.add_competition(data)

    def _new_award(self, competition_id: int, title: str = "示例获奖作品", **overrides) -> int:
        data = {
            "competition_id": competition_id,
            "title": title,
            "summary": "示例摘要",
            "year": 2024,
            "review_status": "approved",
        }
        data.update(overrides)
        return db.create_award(data)


# ======================================================================
# 一、AI 每日配额与限流（429）
# ======================================================================
class AiQuotaBoundaryTest(_TempDBTestCase):

    def test_quota_exhaustion_returns_429_and_retry_after(self) -> None:
        """恰好用完额度后，第 limit+1 次必须 429，且带 Retry-After。"""
        _, token = self._new_user("quota_user")
        config.AI_DAILY_QUOTA = 2

        with patch("llm.chat", return_value="【反问】\n请补充你的参赛经历。"):
            responses = [
                self.client.post(
                    "/api/ai/plan",
                    json={"user_input": "我想参加比赛"},
                    headers=self._auth(token),
                )
                for _ in range(3)
            ]

        self.assertEqual([r.status_code for r in responses], [200, 200, 429])
        self.assertEqual(responses[2].headers.get("retry-after"), "3600")
        self.assertIn("今日 AI 使用次数已用完", responses[2].json()["detail"])

        quota = self.client.get("/api/ai/quota", headers=self._auth(token)).json()
        self.assertEqual(quota, {"used": 2, "limit": 2, "remaining": 0})

    def test_zero_quota_means_unlimited(self) -> None:
        """AI_DAILY_QUOTA=0 时不应限流，quota 接口 remaining 返回 None。"""
        _, token = self._new_user("unlimited_user")
        config.AI_DAILY_QUOTA = 0

        with patch("llm.chat", return_value="【反问】\n请补充。"):
            for _ in range(5):
                response = self.client.post(
                    "/api/ai/plan",
                    json={"user_input": "继续"},
                    headers=self._auth(token),
                )
                self.assertEqual(response.status_code, 200)

        quota = self.client.get("/api/ai/quota", headers=self._auth(token)).json()
        self.assertEqual(quota, {"used": 0, "limit": 0, "remaining": None})

    def test_concurrent_quota_consumption_never_exceeds_limit(self) -> None:
        """并发抢配额：成功次数必须恰好等于 limit，used 不得越界，且不能死锁。"""
        uid, _ = self._new_user("concurrent_quota_user")
        limit = 5
        workers = 16

        barrier = threading.Barrier(workers)
        results: list[dict] = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                result = db.consume_ai_quota(uid, limit)
                with lock:
                    results.append(result)
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                with lock:
                    errors.append(repr(exc))

        threads = [
            threading.Thread(target=worker, name=f"quota-{i}", daemon=True)
            for i in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(
            any(thread.is_alive() for thread in threads),
            "配额并发测试超时，疑似数据库死锁",
        )
        self.assertEqual(errors, [], f"并发扣减出现异常：{errors}")
        self.assertEqual(len(results), workers)

        allowed = [item for item in results if item["allowed"]]
        self.assertEqual(len(allowed), limit)
        self.assertEqual(db.get_ai_quota(uid, limit)["used"], limit)
        self.assertTrue(all(item["remaining"] >= 0 for item in results))

    def test_concurrent_api_calls_trigger_429(self) -> None:
        """并发调用 AI 接口：恰好 limit 个 200，其余全部 429，不得出现 5xx。"""
        uid, token = self._new_user("quota_api_concurrent_user")
        config.AI_DAILY_QUOTA = 3
        workers = 10

        barrier = threading.Barrier(workers)
        statuses: list[int] = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                # 每个线程独立 TestClient；临时库已由 setUpClass 初始化。
                # 不使用 with，避免每个线程重复触发 startup 里的 init_db。
                client = TestClient(main.app)
                barrier.wait(timeout=10)
                response = client.post(
                    "/api/ai/plan",
                    json={"user_input": "并发请求"},
                    headers=self._auth(token),
                )
                with lock:
                    statuses.append(response.status_code)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

        with patch("llm.chat", return_value="【反问】\n请补充。"):
            threads = [
                threading.Thread(target=worker, name=f"api-{i}", daemon=True)
                for i in range(workers)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertFalse(
            any(thread.is_alive() for thread in threads),
            "并发 API 测试超时，疑似死锁",
        )
        self.assertEqual(errors, [], f"并发请求出现异常：{errors}")
        self.assertEqual(len(statuses), workers)
        self.assertEqual(statuses.count(200), 3)
        self.assertEqual(statuses.count(429), workers - 3)
        self.assertEqual(set(statuses), {200, 429})
        self.assertEqual(db.get_ai_quota(uid, 3)["used"], 3)

    def test_quota_resets_on_next_local_date(self) -> None:
        """配额按本地日期翻滚：换日后应重新可用。"""
        uid, _ = self._new_user("quota_reset_user")
        limit = 2

        with patch.object(db, "_today_str", return_value="2026-11-07"):
            first = db.consume_ai_quota(uid, limit)
            second = db.consume_ai_quota(uid, limit)
            exhausted = db.consume_ai_quota(uid, limit)

        with patch.object(db, "_today_str", return_value="2026-11-08"):
            next_day = db.consume_ai_quota(uid, limit)

        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])
        self.assertFalse(exhausted["allowed"])
        self.assertEqual(exhausted["remaining"], 0)
        self.assertTrue(next_day["allowed"])
        self.assertEqual(next_day["used"], 1)

    def test_quota_is_per_user(self) -> None:
        """配额按用户隔离：A 用尽不影响 B。"""
        _, token_a = self._new_user("quota_a")
        _, token_b = self._new_user("quota_b")
        config.AI_DAILY_QUOTA = 1

        with patch("llm.chat", return_value="【反问】\n请补充。"):
            first = self.client.post(
                "/api/ai/plan", json={"user_input": "A"}, headers=self._auth(token_a)
            )
            exhausted = self.client.post(
                "/api/ai/plan", json={"user_input": "A2"}, headers=self._auth(token_a)
            )
            other_user = self.client.post(
                "/api/ai/plan", json={"user_input": "B"}, headers=self._auth(token_b)
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(exhausted.status_code, 429)
        self.assertEqual(other_user.status_code, 200)


# ======================================================================
# 二、鉴权与 JWT 边界
# ======================================================================
class AuthBoundaryTest(_TempDBTestCase):

    def test_missing_token_returns_401(self) -> None:
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(
            self.client.post("/api/ai/plan", json={"user_input": "hi"}).status_code,
            401,
        )

    def test_malformed_token_returns_401(self) -> None:
        response = self.client.get(
            "/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("www-authenticate"), "Bearer")

    def test_empty_bearer_token_returns_401(self) -> None:
        response = self.client.get(
            "/api/auth/me", headers={"Authorization": "Bearer "}
        )
        self.assertEqual(response.status_code, 401)

    def test_expired_token_returns_401(self) -> None:
        uid, _ = self._new_user("expired_user")
        expired = security.create_access_token(uid, "expired_user", expires_minutes=-1)
        response = self.client.get("/api/auth/me", headers=self._auth(expired))
        self.assertEqual(response.status_code, 401)
        self.assertIn("登录已过期", response.json()["detail"])

    def test_token_signed_with_wrong_secret_returns_401(self) -> None:
        uid, _ = self._new_user("forged_user")
        if config.JWT_ALGORITHM != "HS256":
            self.skipTest("本用例仅覆盖 HS256")

        now = datetime.now(timezone.utc)
        forged = jwt.encode(
            {
                "sub": str(uid),
                "username": "forged_user",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            "definitely-the-wrong-secret",
            algorithm="HS256",
        )
        response = self.client.get("/api/auth/me", headers=self._auth(forged))
        self.assertEqual(response.status_code, 401)

    def test_token_of_deleted_user_returns_401(self) -> None:
        uid, token = self._new_user("deleted_user")
        db.delete_user(uid)
        response = self.client.get("/api/auth/me", headers=self._auth(token))
        self.assertEqual(response.status_code, 401)
        self.assertIn("用户不存在", response.json()["detail"])

    def test_register_field_length_boundaries(self) -> None:
        """用户名 3-32、密码 6-64：下界-1 与上界+1 必须 422。"""
        cases = [
            ({"username": "ab", "password": "123456"}, 422),
            ({"username": "a" * 33, "password": "123456"}, 422),
            ({"username": "abc", "password": "12345"}, 422),
            ({"username": "abc", "password": "a" * 65}, 422),
        ]
        for payload, expected in cases:
            response = self.client.post("/api/auth/register", json=payload)
            self.assertEqual(response.status_code, expected, payload)

        # 边界内且带首尾空格：Pydantic 通过，后端归一化为 abc
        response = self.client.post(
            "/api/auth/register",
            json={"username": "  abc  ", "password": "123456"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["username"], "abc")

    def test_register_duplicate_is_case_insensitive(self) -> None:
        first = self.client.post(
            "/api/auth/register", json={"username": "Bob", "password": "123456"}
        )
        second = self.client.post(
            "/api/auth/register", json={"username": "bob", "password": "123456"}
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

    def test_login_failure_message_does_not_reveal_account_existence(self) -> None:
        self._new_user("login_user")
        wrong_password = self.client.post(
            "/api/auth/login",
            json={"username": "login_user", "password": "wrong-password"},
        )
        unknown_user = self.client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "whatever"},
        )
        self.assertEqual(wrong_password.status_code, 401)
        self.assertEqual(unknown_user.status_code, 401)
        self.assertEqual(
            wrong_password.json()["detail"], unknown_user.json()["detail"]
        )

    def test_login_sql_injection_payload_cannot_bypass(self) -> None:
        self._new_user("victim_user")
        response = self.client.post(
            "/api/auth/login",
            json={"username": "victim_user", "password": "' OR '1'='1"},
        )
        self.assertEqual(response.status_code, 401)

    def test_register_username_with_sql_metacharacters_is_literal(self) -> None:
        username = "a' OR '1'='1"
        registered = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": "123456"},
        )
        self.assertEqual(registered.status_code, 200)

        # 以同样字符串登录应成功，说明是按值匹配而非拼接 SQL
        login = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": "123456"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["username"], username.lower())


# ======================================================================
# 三、竞赛接口边界
# ======================================================================
class CompetitionBoundaryTest(_TempDBTestCase):

    def test_nonexistent_competition_returns_404(self) -> None:
        for path in (
            "/api/competitions/999999",
            "/api/competitions/999999/awards",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)

    def test_zero_and_negative_competition_id_returns_404(self) -> None:
        for cid in (0, -1):
            self.assertEqual(
                self.client.get(f"/api/competitions/{cid}").status_code, 404
            )
            self.assertEqual(
                self.client.get(f"/api/competitions/{cid}/awards").status_code,
                404,
            )

    def test_non_integer_competition_id_returns_422(self) -> None:
        self.assertEqual(
            self.client.get("/api/competitions/abc").status_code, 422
        )

    def test_month_query_boundaries(self) -> None:
        for month in (0, 13, -1):
            response = self.client.get("/api/competitions", params={"month": month})
            self.assertEqual(response.status_code, 422, month)
        for month in (1, 12):
            response = self.client.get("/api/competitions", params={"month": month})
            self.assertEqual(response.status_code, 200, month)

    def test_keyword_sql_injection_payload_is_treated_literally(self) -> None:
        self._new_competition("安全测试赛事")
        payload = "' OR 1=1 --"

        by_name = self.client.get(
            "/api/competitions", params={"keyword": payload}
        )
        by_category = self.client.get(
            "/api/competitions", params={"category": payload}
        )

        self.assertEqual(by_name.status_code, 200)
        self.assertEqual(by_category.status_code, 200)
        self.assertEqual(by_name.json(), [])
        self.assertEqual(by_category.json(), [])

    def test_awards_limit_boundaries(self) -> None:
        cid = self._new_competition()
        for limit in (0, 101):
            response = self.client.get(
                f"/api/competitions/{cid}/awards", params={"limit": limit}
            )
            self.assertEqual(response.status_code, 422, limit)
        for limit in (1, 100):
            response = self.client.get(
                f"/api/competitions/{cid}/awards", params={"limit": limit}
            )
            self.assertEqual(response.status_code, 200, limit)


# ======================================================================
# 四、灵感工作台（competition_id 强隔离）
# ======================================================================
class InspirationBoundaryTest(_TempDBTestCase):

    def test_competition_id_is_required_and_must_be_positive(self) -> None:
        cases = [
            {},
            {"competition_id": 0},
            {"competition_id": -1},
            {"competition_id": "abc"},
            {"competition_id": ""},
        ]
        for params in cases:
            response = self.client.get("/api/ai/inspirations", params=params)
            self.assertEqual(response.status_code, 422, params)

    def test_nonexistent_competition_returns_404(self) -> None:
        response = self.client.get(
            "/api/ai/inspirations", params={"competition_id": 999999}
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("不存在", response.json()["detail"])

    def test_existing_competition_without_data_returns_empty_scoped_payload(self) -> None:
        cid = self._new_competition("蓝桥杯")
        response = self.client.get(
            "/api/ai/inspirations", params={"competition_id": cid}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["competition_id"], cid)
        self.assertEqual(body["insights"], [])
        self.assertEqual(body["ideas"], [])
        self.assertEqual(body["work_count"], 0)

    def test_no_cross_competition_leak_when_empty(self) -> None:
        """两个赛事都为空时，也不得把另一个赛事的 insights/ideas 带出来。"""
        cid_a = self._new_competition("赛事 A")
        cid_b = self._new_competition("赛事 B")

        body_a = self.client.get(
            "/api/ai/inspirations", params={"competition_id": cid_a}
        ).json()
        body_b = self.client.get(
            "/api/ai/inspirations", params={"competition_id": cid_b}
        ).json()

        self.assertEqual(body_a["competition_id"], cid_a)
        self.assertEqual(body_b["competition_id"], cid_b)
        self.assertEqual(body_a["insights"], [])
        self.assertEqual(body_b["ideas"], [])


# ======================================================================
# 五、AI 备赛计划
# ======================================================================
class AiPlanBoundaryTest(_TempDBTestCase):

    def test_nonexistent_competition_returns_404_without_calling_llm(self) -> None:
        _, token = self._new_user("plan_user")
        with patch("llm.chat") as chat:
            response = self.client.post(
                "/api/ai/plan",
                json={"competition_id": 999999, "user_input": "帮我制定计划"},
                headers=self._auth(token),
            )
            chat.assert_not_called()
        self.assertEqual(response.status_code, 404)

    def test_negative_competition_id_is_rejected(self) -> None:
        """当前 PlanRequest 未加 gt=0，负数走 404；修复后应为 422。"""
        _, token = self._new_user("plan_neg_user")
        with patch("llm.chat", return_value="【反问】\n补充信息"):
            response = self.client.post(
                "/api/ai/plan",
                json={"competition_id": -1, "user_input": "hi"},
                headers=self._auth(token),
            )
        self.assertIn(response.status_code, (404, 422))

    def test_ask_marker_returns_ask_status(self) -> None:
        _, token = self._new_user("plan_ask_user")
        with patch("llm.chat", return_value="【反问】\n1. 你每天能投入多少时间？"):
            response = self.client.post(
                "/api/ai/plan",
                json={"user_input": "我想参加"},
                headers=self._auth(token),
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ask")
        self.assertEqual(body["mode"], "create")
        self.assertEqual(body["reply"], "1. 你每天能投入多少时间？")

    def test_plan_marker_returns_plan_status_and_strips_marker(self) -> None:
        _, token = self._new_user("plan_marker_user")
        with patch("llm.chat", return_value="【计划】\n## 备赛计划\n- 第一步"):
            response = self.client.post(
                "/api/ai/plan",
                json={"user_input": "直接生成"},
                headers=self._auth(token),
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "plan")
        self.assertEqual(body["reply"], "## 备赛计划\n- 第一步")
        self.assertNotIn("【计划】", body["reply"])

    def test_current_plan_forces_refine_even_if_model_returns_ask(self) -> None:
        _, token = self._new_user("plan_refine_user")
        with patch("llm.chat", return_value="【反问】\n还需要补充吗？"):
            response = self.client.post(
                "/api/ai/plan",
                json={
                    "current_plan": "# 旧计划\n- 已完成",
                    "user_input": "把周期延长一周",
                },
                headers=self._auth(token),
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "refine")
        self.assertEqual(body["mode"], "refine")

    def test_algorithm_only_competition_uses_playbook_prompt(self) -> None:
        """纯算法类赛事：必须换成刷题路线图人格，且作品赛词汇被兜底改写。"""
        cid = self._new_competition("蓝桥杯全国软件和信息技术专业人才大赛")
        db.update_competition(cid, {"has_project_awards": 0})
        _, token = self._new_user("plan_algo_user")
        with patch(
            "llm.chat",
            return_value="【计划】\n## 刷题路线图\n- 第 1 周：前端开发基础\n- 组队分工待定",
        ) as chat:
            response = self.client.post(
                "/api/ai/plan",
                json={"competition_id": cid, "user_input": "直接生成"},
                headers=self._auth(token),
            )
            system = chat.call_args.kwargs.get("system") or ""
        self.assertEqual(response.status_code, 200)
        self.assertIn("算法刷题路线图", system)
        self.assertIn("绝对禁止", system)
        body = response.json()
        self.assertEqual(body["status"], "plan")
        # 兜底改写：作品赛词汇一个都不许留
        self.assertNotIn("前端开发", body["reply"])
        self.assertNotIn("组队分工", body["reply"])

    def test_llm_error_maps_to_502(self) -> None:
        _, token = self._new_user("plan_error_user")
        with patch("llm.chat", side_effect=llm.LLMError("upstream timeout")):
            response = self.client.post(
                "/api/ai/plan",
                json={"user_input": "生成计划"},
                headers=self._auth(token),
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("大模型调用失败", response.json()["detail"])

    def test_unexpected_llm_exception_maps_to_502(self) -> None:
        _, token = self._new_user("plan_unexpected_user")
        with patch("llm.chat", side_effect=RuntimeError("boom")):
            response = self.client.post(
                "/api/ai/plan",
                json={"user_input": "生成计划"},
                headers=self._auth(token),
            )
        self.assertEqual(response.status_code, 502)

    def test_empty_llm_output_maps_to_502(self) -> None:
        _, token = self._new_user("plan_empty_user")
        with patch("llm.chat", return_value="   "):
            response = self.client.post(
                "/api/ai/plan",
                json={"user_input": "生成计划"},
                headers=self._auth(token),
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("返回内容为空", response.json()["detail"])


# ======================================================================
# 六、赛事获奖作品聚合分析
# ======================================================================
class AiAnalyzeBoundaryTest(_TempDBTestCase):

    def _token(self) -> str:
        return self._new_user("analyze_user")[1]

    def test_competition_id_validation_boundaries(self) -> None:
        token = self._token()
        payloads = [
            {},
            {"competition_id": None},
            {"competition_id": 0},
            {"competition_id": -5},
            {"competition_id": "abc"},
        ]
        for payload in payloads:
            response = self.client.post(
                "/api/ai/analyze", json=payload, headers=self._auth(token)
            )
            self.assertEqual(response.status_code, 422, payload)

    def test_nonexistent_competition_returns_404_without_calling_llm(self) -> None:
        token = self._token()
        with patch("llm.chat") as chat:
            response = self.client.post(
                "/api/ai/analyze",
                json={"competition_id": 999999},
                headers=self._auth(token),
            )
            chat.assert_not_called()
        self.assertEqual(response.status_code, 404)

    def test_competition_without_awards_returns_404_without_calling_llm(self) -> None:
        cid = self._new_competition("空数据赛事")
        token = self._token()
        with patch("llm.chat") as chat:
            response = self.client.post(
                "/api/ai/analyze",
                json={"competition_id": cid},
                headers=self._auth(token),
            )
            chat.assert_not_called()
        self.assertEqual(response.status_code, 404)
        self.assertIn("暂无获奖作品", response.json()["detail"])

    def test_llm_error_maps_to_502(self) -> None:
        cid = self._new_competition()
        self._new_award(cid)
        token = self._token()
        with patch("llm.chat", side_effect=llm.LLMError("timeout")):
            response = self.client.post(
                "/api/ai/analyze",
                json={"competition_id": cid},
                headers=self._auth(token),
            )
        self.assertEqual(response.status_code, 502)

    def test_empty_llm_output_maps_to_502(self) -> None:
        cid = self._new_competition()
        self._new_award(cid)
        token = self._token()
        with patch("llm.chat", return_value=""):
            response = self.client.post(
                "/api/ai/analyze",
                json={"competition_id": cid},
                headers=self._auth(token),
            )
        self.assertEqual(response.status_code, 502)

    def test_analyze_uses_at_most_five_awards(self) -> None:
        """分析 prompt 最多纳入 5 件作品，work_count 不得超过 5。"""
        cid = self._new_competition("多作品赛事")
        for index in range(7):
            self._new_award(cid, title=f"作品 {index}", year=2020 + index)
        token = self._token()

        with patch("llm.chat", return_value="## 分析"):
            response = self.client.post(
                "/api/ai/analyze",
                json={"competition_id": cid},
                headers=self._auth(token),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["work_count"], 5)

    def test_cache_hit_avoids_second_llm_call(self) -> None:
        cid = self._new_competition()
        self._new_award(cid)
        token = self._token()
        config.AI_DAILY_QUOTA = 10

        with patch("llm.chat", return_value="## 共性特征\n- 选题聚焦") as chat:
            first = self.client.post(
                "/api/ai/analyze",
                json={"competition_id": cid},
                headers=self._auth(token),
            )
            second = self.client.post(
                "/api/ai/analyze",
                json={"competition_id": cid},
                headers=self._auth(token),
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(second.json()["analysis"], "## 共性特征\n- 选题聚焦")
        self.assertIsNotNone(db.get_ai_analysis("competition", cid))

    def test_cache_invalidated_when_prompt_content_changes(self) -> None:
        cid = self._new_competition()
        award_id = self._new_award(cid, summary="旧摘要")
        token = self._token()
        config.AI_DAILY_QUOTA = 10

        with patch("llm.chat", side_effect=["分析 A", "分析 B"]) as chat:
            first = self.client.post(
                "/api/ai/analyze",
                json={"competition_id": cid},
                headers=self._auth(token),
            )
            # 修改作品语义字段 -> prompt 指纹变化 -> 缓存必须失效
            db.update_award(award_id, {"summary": "新摘要"})
            second = self.client.post(
                "/api/ai/analyze",
                json={"competition_id": cid},
                headers=self._auth(token),
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(first.json()["analysis"], "分析 A")
        self.assertEqual(second.json()["analysis"], "分析 B")


# ======================================================================
# 七、单作品深度解析
# ======================================================================
class AiWorkAnalyzeBoundaryTest(_TempDBTestCase):

    def test_work_id_validation_boundaries(self) -> None:
        _, token = self._new_user("work_user")
        payloads = [{}, {"work_id": 0}, {"work_id": -1}, {"work_id": "abc"}]
        for payload in payloads:
            response = self.client.post(
                "/api/ai/analyze-work", json=payload, headers=self._auth(token)
            )
            self.assertEqual(response.status_code, 422, payload)

    def test_nonexistent_work_returns_404_without_calling_llm(self) -> None:
        _, token = self._new_user("work_404_user")
        with patch("llm.chat") as chat:
            response = self.client.post(
                "/api/ai/analyze-work",
                json={"work_id": 999999},
                headers=self._auth(token),
            )
            chat.assert_not_called()
        self.assertEqual(response.status_code, 404)

    def test_work_analysis_cache_is_reused(self) -> None:
        cid = self._new_competition()
        work_id = self._new_award(cid)
        _, token = self._new_user("work_cache_user")
        config.AI_DAILY_QUOTA = 10

        with patch("llm.chat", return_value="## 深度解析") as chat:
            first = self.client.post(
                "/api/ai/analyze-work",
                json={"work_id": work_id},
                headers=self._auth(token),
            )
            second = self.client.post(
                "/api/ai/analyze-work",
                json={"work_id": work_id},
                headers=self._auth(token),
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(second.json()["analysis"], "## 深度解析")


# ======================================================================
# 八、自然语言检索
# ======================================================================
class SearchBoundaryTest(_TempDBTestCase):

    def test_query_and_top_k_boundaries(self) -> None:
        payloads = [
            {"query": ""},
            {"query": "   "},
            {"query": "数学", "top_k": 0},
            {"query": "数学", "top_k": -1},
            {"query": "数学", "top_k": 21},
        ]
        for payload in payloads:
            response = self.client.post("/api/ai/search", json=payload)
            self.assertEqual(response.status_code, 422, payload)

    def test_top_k_upper_boundary_is_accepted(self) -> None:
        response = self.client.post(
            "/api/ai/search", json={"query": "数学", "top_k": 20}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])

        lower = self.client.post(
            "/api/ai/search", json={"query": "数学", "top_k": 1}
        )
        self.assertEqual(lower.status_code, 200)

    def test_injection_payload_does_not_crash_or_leak(self) -> None:
        self._new_competition("安全测试赛事")
        response = self.client.post(
            "/api/ai/search", json={"query": "' OR 1=1 --"}
        )
        self.assertEqual(response.status_code, 200)
        # 参数化 + 关键词兜底：不得把不相关赛事全部返回
        self.assertEqual(response.json()["items"], [])

    def test_keyword_fallback_returns_scoped_item(self) -> None:
        cid = self._new_competition("全国大学生数学建模竞赛")
        response = self.client.post(
            "/api/ai/search", json={"query": "数学建模", "top_k": 5}
        )
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertTrue(any(item["id"] == cid for item in items))


# ======================================================================
# 九、备赛清单
# ======================================================================
class PreparationBoundaryTest(_TempDBTestCase):

    def test_requires_authentication(self) -> None:
        self.assertEqual(self.client.get("/api/preparations").status_code, 401)
        self.assertEqual(self.client.post("/api/preparations/1").status_code, 401)
        self.assertEqual(
            self.client.get("/api/preparations/check/1").status_code, 401
        )

    def test_competition_id_boundaries(self) -> None:
        _, token = self._new_user("prep_user")
        for cid in (0, -1):
            response = self.client.post(
                f"/api/preparations/{cid}", headers=self._auth(token)
            )
            self.assertEqual(response.status_code, 422, cid)

        nonexistent = self.client.post(
            "/api/preparations/999999", headers=self._auth(token)
        )
        self.assertEqual(nonexistent.status_code, 404)

    def test_add_is_idempotent_and_remove(self) -> None:
        cid = self._new_competition()
        _, token = self._new_user("prep_idempotent_user")

        first = self.client.post(
            f"/api/preparations/{cid}", headers=self._auth(token)
        )
        second = self.client.post(
            f"/api/preparations/{cid}", headers=self._auth(token)
        )

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["created"])
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])

        listing = self.client.get(
            "/api/preparations", headers=self._auth(token)
        ).json()
        self.assertEqual(listing["total"], 1)

        checked = self.client.get(
            f"/api/preparations/check/{cid}", headers=self._auth(token)
        ).json()
        self.assertTrue(checked["prepared"])

        removed = self.client.delete(
            f"/api/preparations/{cid}", headers=self._auth(token)
        )
        self.assertEqual(removed.status_code, 200)

        removed_again = self.client.delete(
            f"/api/preparations/{cid}", headers=self._auth(token)
        )
        self.assertEqual(removed_again.status_code, 404)

    def test_preparations_are_isolated_per_user(self) -> None:
        cid = self._new_competition()
        _, token_a = self._new_user("prep_iso_a")
        _, token_b = self._new_user("prep_iso_b")

        self.client.post(f"/api/preparations/{cid}", headers=self._auth(token_a))

        listing_b = self.client.get(
            "/api/preparations", headers=self._auth(token_b)
        ).json()
        self.assertEqual(listing_b["total"], 0)


# ======================================================================
# 十、LLM 超时 / 重试机制（不访问网络）
# ======================================================================
class LlmRetryBoundaryTest(unittest.TestCase):

    @staticmethod
    def _fake_client(create):
        return SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create)
            )
        )

    @staticmethod
    def _timeout_error() -> APITimeoutError:
        return APITimeoutError(
            request=httpx.Request(
                "POST", "https://api.example.invalid/v1/chat/completions"
            )
        )

    def test_timeout_is_retried_then_raises_after_exhaustion(self) -> None:
        calls: list[dict] = []

        def create(**kwargs):
            calls.append(kwargs)
            raise self._timeout_error()

        with patch.object(llm, "_client", self._fake_client(create)), \
                patch.object(llm, "MAX_RETRIES", 3), \
                patch.object(llm, "RETRY_BASE_DELAY", 0.0), \
                patch.object(llm.time, "sleep") as sleeper:
            with self.assertRaises(llm.LLMError):
                llm.chat("hello")

        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeper.call_count, 2)

    def test_non_retryable_exception_is_not_retried(self) -> None:
        calls: list[dict] = []

        def create(**kwargs):
            calls.append(kwargs)
            raise ValueError("bad request payload")

        with patch.object(llm, "_client", self._fake_client(create)), \
                patch.object(llm, "MAX_RETRIES", 3), \
                patch.object(llm.time, "sleep") as sleeper:
            with self.assertRaises(llm.LLMError):
                llm.chat("hello")

        self.assertEqual(len(calls), 1)
        sleeper.assert_not_called()

    def test_empty_content_raises_llm_error(self) -> None:
        calls: list[dict] = []

        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="   "),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

        with patch.object(llm, "_client", self._fake_client(create)):
            with self.assertRaises(llm.LLMError):
                llm.chat("hello")

        self.assertEqual(len(calls), 1)

    def test_reasoning_truncation_doubles_max_tokens(self) -> None:
        calls: list[dict] = []

        def create(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=None),
                            finish_reason="length",
                        )
                    ],
                    usage=None,
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok"),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

        with patch.object(llm, "_client", self._fake_client(create)), \
                patch.object(llm, "MAX_TOKENS_CEILING", 8192):
            result = llm.chat("hello", max_tokens=1024)

        self.assertEqual(result, "ok")
        self.assertEqual(calls[0]["max_tokens"], 1024)
        self.assertEqual(calls[1]["max_tokens"], 2048)


# ======================================================================
# 十一、数据库层边界（缓存 / 守卫）
# ======================================================================
class DbBoundaryTest(_TempDBTestCase):

    def test_ai_analysis_cache_rejects_stale_hash(self) -> None:
        self.assertTrue(db.save_ai_analysis("work", 1, "hash-a", "内容 A"))
        self.assertEqual(
            db.get_ai_analysis("work", 1, input_hash="hash-a")["content_md"],
            "内容 A",
        )
        self.assertIsNone(db.get_ai_analysis("work", 1, input_hash="hash-b"))

    def test_ai_analysis_cache_upserts_single_row(self) -> None:
        db.save_ai_analysis("work", 1, "hash-a", "内容 A")
        db.save_ai_analysis("work", 1, "hash-b", "内容 B")

        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM ai_analysis_cache "
                "WHERE target_type = 'work' AND target_id = 1"
            ).fetchone()
        self.assertEqual(int(row["c"]), 1)
        self.assertEqual(db.get_ai_analysis("work", 1)["content_md"], "内容 B")

    def test_ai_analysis_rejects_empty_content_and_bad_target_type(self) -> None:
        self.assertFalse(db.save_ai_analysis("work", 1, "hash", "   "))
        with self.assertRaises(ValueError):
            db.get_ai_analysis("unknown-type", 1)

    def test_competition_id_guard_rejects_zero_and_none(self) -> None:
        for cid in (None, 0, -1):
            with self.assertRaises(ValueError):
                db.list_insights_by_competition(cid)
            with self.assertRaises(ValueError):
                db.list_ideas_by_competition(cid)

    def test_add_preparation_rejects_nonexistent_competition(self) -> None:
        uid, _ = self._new_user("prep_db_user")
        with self.assertRaises(ValueError):
            db.add_preparation(uid, 999999)

    def test_concurrent_add_preparation_creates_single_row(self) -> None:
        """并发加入同一竞赛：不得死锁，且只能创建一行（created 仅一次为 True）。"""
        uid, _ = self._new_user("prep_race_user")
        cid = self._new_competition("并发备赛赛事")
        workers = 10

        barrier = threading.Barrier(workers)
        results: list[dict] = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                result = db.add_preparation(uid, cid)
                with lock:
                    results.append(result)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

        threads = [
            threading.Thread(target=worker, name=f"prep-{i}", daemon=True)
            for i in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertFalse(
            any(thread.is_alive() for thread in threads),
            "并发备赛测试超时，疑似数据库锁等待",
        )
        self.assertEqual(errors, [], f"并发备赛出现异常：{errors}")
        self.assertEqual(len(results), workers)
        self.assertEqual(len([item for item in results if item["created"]]), 1)

        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM user_preparations "
                "WHERE user_id = ? AND competition_id = ?",
                (uid, cid),
            ).fetchone()
        self.assertEqual(int(row["c"]), 1)

    @unittest.skip(
        "schema_v2.sql 的 insights 表缺 evidence / model 列，"
        "save_insight 在新库上会失败；修复 schema 后启用。"
        "见 CODE_REVIEW_BACKEND.md R16"
    )
    def test_save_insight_on_fresh_schema(self) -> None:
        cid = self._new_competition("洞察落库赛事")
        row_id = db.save_insight(
            "trend",
            f"competition:{cid}",
            "## 趋势",
            competition_id=cid,
            evidence=[1],
            model="deepseek-flash",
        )
        self.assertGreater(row_id, 0)
        rows = db.list_insights_by_competition(cid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "deepseek-flash")

    def test_month_range_boundaries(self) -> None:
        self.assertTrue(db._month_in_range(3, 3, 9, 0))
        self.assertFalse(db._month_in_range(10, 3, 9, 0))
        self.assertTrue(db._month_in_range(1, 11, 3, 1))
        self.assertTrue(db._month_in_range(12, 11, 3, 1))
        self.assertFalse(db._month_in_range(6, 11, 3, 1))
        self.assertFalse(db._month_in_range(None, 3, 9, 0))
        self.assertFalse(db._month_in_range(3, None, 9, 0))


# ======================================================================
# 十二、路由内部纯函数边界
# ======================================================================
class RouterHelperBoundaryTest(unittest.TestCase):

    def test_parse_stage(self) -> None:
        self.assertEqual(ai_router._parse_stage("【反问】\n问题"), "ask")
        self.assertEqual(ai_router._parse_stage("  【计划】\n## 计划"), "plan")
        self.assertEqual(ai_router._parse_stage("## 没有标记但有标题"), "plan")
        self.assertEqual(ai_router._parse_stage("没有标记也没有标题"), "ask")
        self.assertEqual(ai_router._parse_stage(""), "ask")

    def test_strip_marker(self) -> None:
        self.assertEqual(
            ai_router._strip_marker("【计划】\n## 正文"), "## 正文"
        )
        self.assertEqual(
            ai_router._strip_marker("【反问】问题"), "问题"
        )
        self.assertEqual(
            ai_router._strip_marker("没有标记"), "没有标记"
        )

    def test_to_int_list(self) -> None:
        self.assertEqual(ai_router._to_int_list(None), [])
        self.assertEqual(ai_router._to_int_list(""), [])
        self.assertEqual(ai_router._to_int_list("[1, 2]"), [1, 2])
        self.assertEqual(ai_router._to_int_list("1,2,2,3"), [1, 2, 3])
        self.assertEqual(ai_router._to_int_list([1, "2", "x"]), [1, 2])
        self.assertEqual(ai_router._to_int_list('{"a": 1}'), [])

    def test_clean_field_strips_repeated_prefix(self) -> None:
        self.assertEqual(ai_router._clean_field("亮点：亮点：内容"), "内容")
        self.assertEqual(ai_router._clean_field(None), "")
        self.assertEqual(ai_router._clean_field("  正常内容  "), "正常内容")


# ======================================================================
# 十三、超大请求体（防护尚未实现，先以 skip 记录预期）
# ======================================================================
class RequestSizeBoundaryTest(_TempDBTestCase):

    @unittest.skip(
        "待实现：应用层请求体上限（413）。"
        "见 CODE_REVIEW_BACKEND.md R3 / TEST_CASES_BACKEND.md TC-SEC-04"
    )
    def test_oversized_request_body_should_be_rejected_with_413(self) -> None:
        _, token = self._new_user("oversize_user")
        payload = {"user_input": "x" * (2 * 1024 * 1024)}  # 2 MiB
        response = self.client.post(
            "/api/ai/plan", json=payload, headers=self._auth(token)
        )
        self.assertEqual(response.status_code, 413)

    def test_oversized_user_input_should_fail_validation_with_422(self) -> None:
        """user_input 已设 max_length=8000，20 万字符必须 422。"""
        _, token = self._new_user("oversize_field_user")
        response = self.client.post(
            "/api/ai/plan",
            json={"user_input": "x" * 200_000},
            headers=self._auth(token),
        )
        self.assertEqual(response.status_code, 422)

    def test_oversized_search_query_should_fail_validation_with_422(self) -> None:
        """SearchRequest.query 已设 max_length=200，超长 query 必须 422。"""
        response = self.client.post(
            "/api/ai/search",
            json={"query": "x" * 201},
        )
        self.assertEqual(response.status_code, 422)

    def test_oversized_current_plan_should_fail_validation_with_422(self) -> None:
        """current_plan 已设 max_length=32000，超长计划必须 422。"""
        _, token = self._new_user("oversize_plan_user")
        response = self.client.post(
            "/api/ai/plan",
            json={"current_plan": "x" * 32_001, "user_input": "继续"},
            headers=self._auth(token),
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
