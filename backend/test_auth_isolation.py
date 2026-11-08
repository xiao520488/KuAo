# -*- coding: utf-8 -*-
"""P0 账号串号回归测试。

覆盖：
  * 不带 Token / 空 Bearer / 畸形 Token / 伪造签名 → 必须是 401，绝不能兜底成某个用户；
  * 合法 Token 只能解析出它自己的用户（A 的 Token 是 A，B 的 Token 是 B）；
  * A 的备赛数据不会被 B 的 Token 读到；
  * /api/auth/me 等鉴权响应必须带 no-store，禁止被任何中间缓存复用；
  * 缺少 exp / sub 的畸形 Token 必须被拒绝；
  * 注册接口为每个新用户签发不同的 Token。

运行：
    python -m unittest -v test_auth_isolation.py
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient

import config
import db
import main
import security

# 复用同一个 bcrypt 哈希，避免每个用例都付一次哈希成本；明文 "pass1234" 仅测试用。
_PASSWORD_HASH = security.hash_password("pass1234")


class AuthIsolationTest(unittest.TestCase):
    """使用独立临时库，绝不读写 competitions.db。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.mkdtemp(prefix="kuao-auth-isolation-")
        cls._orig_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(cls._tmpdir, "auth.db")
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
        # 外键子表先删，再删主表
        with db.get_conn() as conn:
            for table in ("user_preparations", "ai_usage", "users"):
                conn.execute(f"DELETE FROM {table}")

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _new_user(self, username: str) -> tuple[int, str]:
        uid = db.create_user(username, _PASSWORD_HASH)
        return uid, security.create_access_token(uid, username)

    @staticmethod
    def _auth(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _new_competition(self, name: str = "全国大学生数学建模竞赛") -> int:
        return db.add_competition(
            {
                "name": name,
                "category": "数学建模",
                "reg_start_month": 3,
                "reg_end_month": 9,
                "reg_cross_year": 0,
                "official_url": "https://example.invalid",
                "level": "national",
            }
        )

    # ------------------------------------------------------------------
    # 1. 没有 / 错误 Token 一律 401
    # ------------------------------------------------------------------
    def test_missing_token_returns_401(self) -> None:
        responses = [
            self.client.get("/api/auth/me"),
            self.client.get("/api/preparations"),
            self.client.get("/api/ai/quota"),
            self.client.post("/api/ai/plan", json={"user_input": "hi"}),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 401, response.request.url)
            self.assertEqual(response.headers.get("www-authenticate"), "Bearer")

    def test_empty_bearer_token_returns_401(self) -> None:
        response = self.client.get(
            "/api/auth/me", headers={"Authorization": "Bearer "}
        )
        self.assertEqual(response.status_code, 401)

    def test_malformed_token_returns_401(self) -> None:
        for token in ("not-a-jwt", "a.b.c", "null", "12345"):
            response = self.client.get(
                "/api/auth/me", headers=self._auth(token)
            )
            self.assertEqual(response.status_code, 401, token)

    def test_non_bearer_scheme_returns_401(self) -> None:
        response = self.client.get(
            "/api/auth/me", headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
        self.assertEqual(response.status_code, 401)

    def test_token_signed_with_wrong_secret_returns_401(self) -> None:
        uid, _ = self._new_user("forged_user")
        now = datetime.now(timezone.utc)
        forged = jwt.encode(
            {
                "sub": str(uid),
                "username": "forged_user",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            "definitely-the-wrong-secret",
            algorithm=config.JWT_ALGORITHM,
        )
        response = self.client.get("/api/auth/me", headers=self._auth(forged))
        self.assertEqual(response.status_code, 401)

    def test_token_missing_required_claims_returns_401(self) -> None:
        uid, _ = self._new_user("claims_user")
        now = datetime.now(timezone.utc)

        # 缺少 exp
        no_exp = jwt.encode(
            {"sub": str(uid), "username": "claims_user", "iat": now},
            config.JWT_SECRET_KEY,
            algorithm=config.JWT_ALGORITHM,
        )
        # 缺少 sub
        no_sub = jwt.encode(
            {
                "username": "claims_user",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            config.JWT_SECRET_KEY,
            algorithm=config.JWT_ALGORITHM,
        )
        for token in (no_exp, no_sub):
            response = self.client.get("/api/auth/me", headers=self._auth(token))
            self.assertEqual(response.status_code, 401, token)

    # ------------------------------------------------------------------
    # 2. A / B 身份严格隔离
    # ------------------------------------------------------------------
    def test_token_maps_to_its_own_user(self) -> None:
        uid_a, token_a = self._new_user("alice")
        uid_b, token_b = self._new_user("bob")

        me_a = self.client.get("/api/auth/me", headers=self._auth(token_a))
        me_b = self.client.get("/api/auth/me", headers=self._auth(token_b))

        self.assertEqual(me_a.status_code, 200)
        self.assertEqual(me_b.status_code, 200)
        self.assertEqual(me_a.json()["id"], uid_a)
        self.assertEqual(me_a.json()["username"], "alice")
        self.assertEqual(me_b.json()["id"], uid_b)
        self.assertEqual(me_b.json()["username"], "bob")

    def test_a_preparations_are_not_visible_to_b(self) -> None:
        _, token_a = self._new_user("alice")
        _, token_b = self._new_user("bob")
        cid = self._new_competition()

        added = self.client.post(
            f"/api/preparations/{cid}", headers=self._auth(token_a)
        )
        self.assertEqual(added.status_code, 200)

        list_a = self.client.get("/api/preparations", headers=self._auth(token_a))
        list_b = self.client.get("/api/preparations", headers=self._auth(token_b))

        self.assertEqual(list_a.status_code, 200)
        self.assertEqual(list_b.status_code, 200)
        self.assertEqual(list_a.json()["total"], 1)
        self.assertEqual(list_a.json()["items"][0]["competition_id"], cid)
        self.assertEqual(list_b.json()["total"], 0)
        self.assertEqual(list_b.json()["items"], [])

        # B 也不能通过「check」接口探测到 A 的清单
        check_b = self.client.get(
            f"/api/preparations/check/{cid}", headers=self._auth(token_b)
        )
        self.assertEqual(check_b.status_code, 200)
        self.assertFalse(check_b.json()["prepared"])

    def test_register_issues_distinct_token_per_user(self) -> None:
        first = self.client.post(
            "/api/auth/register", json={"username": "register_a", "password": "pass1234"}
        )
        second = self.client.post(
            "/api/auth/register", json={"username": "register_b", "password": "pass1234"}
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        token_a = first.json()["access_token"]
        token_b = second.json()["access_token"]
        self.assertNotEqual(token_a, token_b)

        payload_a = jwt.decode(
            token_a, config.JWT_SECRET_KEY, algorithms=[config.JWT_ALGORITHM]
        )
        payload_b = jwt.decode(
            token_b, config.JWT_SECRET_KEY, algorithms=[config.JWT_ALGORITHM]
        )
        self.assertEqual(payload_a["sub"], str(first.json()["user"]["id"]))
        self.assertEqual(payload_b["sub"], str(second.json()["user"]["id"]))
        self.assertNotEqual(payload_a["sub"], payload_b["sub"])

    # ------------------------------------------------------------------
    # 3. 鉴权响应禁止被缓存
    # ------------------------------------------------------------------
    def test_auth_responses_are_not_cacheable(self) -> None:
        _, token = self._new_user("cache_user")

        ok = self.client.get("/api/auth/me", headers=self._auth(token))
        unauthorized = self.client.get("/api/auth/me")

        for response in (ok, unauthorized):
            cache_control = response.headers.get("cache-control", "")
            self.assertIn("no-store", cache_control.lower())
            self.assertIn("private", cache_control.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
