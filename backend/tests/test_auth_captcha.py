# -*- coding: utf-8 -*-
"""图形验证码 + 登录防爆破的回归测试。

覆盖：
  * /api/auth/captcha 返回可用的 data URL，且 key 与明文对得上；
  * 登录 / 注册缺少验证码字段 → 422；验证码错误 → 400「验证码错误或已过期」；
  * 验证码一次性：同一 key 第二次使用即失效（成功、失败都作废）；
  * 过期验证码失效（直接构造 TTL=0 的 store 验证）；
  * 登录成功 / 注册成功会把验证码消费掉；
  * 连续 5 次密码错误 → 429「请稍后再试」，且此时即使密码正确也被挡；
  * 换用户名但同一 IP 连续错误同样会锁（防轮换用户名爆破）；
  * 登录成功后失败计数清零；
  * 验证码错误不计入密码错误次数（否则打验证码就能把别人锁死）。

运行：
    cd backend && python -m pytest tests/test_auth_captcha.py -q
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from fastapi.testclient import TestClient

import db
import main
from captcha_store import CaptchaStore, captcha_store
from login_guard import LoginGuard, login_guard


class CaptchaAuthTest(unittest.TestCase):
    """独立临时库，绝不读写 competitions.db。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.mkdtemp(prefix="kuao-captcha-test-")
        cls._orig_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(cls._tmpdir, "captcha.db")
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
        login_guard.clear()
        captcha_store.clear()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM users")

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _issue_captcha(self, override_code: str | None = None) -> tuple[str, str]:
        """领一张验证码，返回 (key, 正确答案)；override_code 可强制填错。"""
        resp = self.client.get("/api/auth/captcha")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        key = body["captcha_key"]
        real = captcha_store.peek(key)
        self.assertIsNotNone(real, "验证码应已登记进缓存")
        return key, (override_code if override_code is not None else real)

    def _register(self, username: str = "captcha_user", password: str = "pass1234"):
        key, code = self._issue_captcha()
        return self.client.post(
            "/api/auth/register",
            json={
                "username": username,
                "password": password,
                "captcha_key": key,
                "captcha_code": code,
            },
        )

    def _login(self, username: str, password: str, code: str | None = None):
        key, captcha = self._issue_captcha(code)
        return self.client.post(
            "/api/auth/login",
            json={
                "username": username,
                "password": password,
                "captcha_key": key,
                "captcha_code": captcha,
            },
        )

    # ------------------------------------------------------------------
    # 验证码接口
    # ------------------------------------------------------------------
    def test_captcha_returns_png_data_url_and_key(self) -> None:
        body = self.client.get("/api/auth/captcha").json()
        self.assertTrue(body["captcha_image"].startswith("data:image/png;base64,"))
        self.assertGreater(len(body["captcha_image"]), 1000)
        self.assertEqual(body["expires_in"], 300)
        self.assertEqual(len(body["captcha_key"]), 36)  # UUID4
        self.assertIsNotNone(captcha_store.peek(body["captcha_key"]))
        self.assertEqual(len(captcha_store.peek(body["captcha_key"])), 4)

    def test_captcha_keys_are_unique(self) -> None:
        keys = {self.client.get("/api/auth/captcha").json()["captcha_key"] for _ in range(5)}
        self.assertEqual(len(keys), 5)

    # ------------------------------------------------------------------
    # 校验逻辑
    # ------------------------------------------------------------------
    def test_login_without_captcha_is_422(self) -> None:
        resp = self.client.post(
            "/api/auth/login", json={"username": "someone", "password": "pass1234"}
        )
        self.assertEqual(resp.status_code, 422)

    def test_register_without_captcha_is_422(self) -> None:
        resp = self.client.post(
            "/api/auth/register", json={"username": "nocaptcha", "password": "pass1234"}
        )
        self.assertEqual(resp.status_code, 422)

    def test_wrong_captcha_code_value_is_rejected(self) -> None:
        key, real = self._issue_captcha()
        wrong = "0000" if real != "0000" else "1111"
        resp = self.client.post(
            "/api/auth/login",
            json={
                "username": "ghost",
                "password": "pass1234",
                "captcha_key": key,
                "captcha_code": wrong,
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "验证码错误或已过期")

    def test_captcha_is_single_use(self) -> None:
        """同一 key 第二次使用必须失败（一次性）。"""
        self._register("single_use", "pass1234")
        key, code = self._issue_captcha()
        first = self.client.post(
            "/api/auth/login",
            json={"username": "single_use", "password": "pass1234", "captcha_key": key, "captcha_code": code},
        )
        second = self.client.post(
            "/api/auth/login",
            json={"username": "single_use", "password": "pass1234", "captcha_key": key, "captcha_code": code},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)

    def test_expired_captcha_is_rejected(self) -> None:
        store = CaptchaStore(ttl_seconds=0)   # 立刻过期
        key = store.issue("1234")
        time.sleep(0.01)
        self.assertFalse(store.consume(key, "1234"))
        self.assertIsNone(store.peek(key))

    def test_wrong_captcha_does_not_lock_account(self) -> None:
        """验证码错误是「前端机器人」级别的问题，不能用来把真实用户锁死。"""
        self._register("victim", "pass1234")
        for _ in range(10):
            self._login("victim", "pass1234", code="wrong")
        # 计数不该被验证码失败推高
        self.assertEqual(login_guard.locked_remaining("victim", ""), 0)
        self.assertEqual(self._login("victim", "pass1234").status_code, 200)

    # ------------------------------------------------------------------
    # 防爆破
    # ------------------------------------------------------------------
    def test_lockout_after_five_wrong_passwords(self) -> None:
        self._register("lock_me", "pass1234")
        for i in range(5):
            resp = self._login("lock_me", "wrong-password")
            self.assertEqual(resp.status_code, 401, f"第 {i + 1} 次应为 401")

        blocked = self._login("lock_me", "pass1234")   # 密码正确也被锁
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["detail"], "请稍后再试")
        self.assertGreater(int(blocked.headers["Retry-After"]), 0)

    def test_lock_tracks_username_and_ip_separately(self) -> None:
        """两个维度各自独立：账号被锁则换 IP 也进不来，IP 被打爆则换用户名也进不来。"""
        # 用户名维度：同一账号换 IP 仍被锁
        g1 = LoginGuard(max_failures=5, lock_seconds=900)
        for _ in range(5):
            g1.record_failure("only_a", "10.0.0.1")
        self.assertGreater(g1.locked_remaining("only_a", "2.2.2.2"), 0)

        # IP 维度：同一 IP 轮换用户名仍被锁
        g2 = LoginGuard(max_failures=5, lock_seconds=900)
        for i in range(5):
            g2.record_failure(f"rotating_{i}", "3.3.3.3")
        self.assertGreater(g2.locked_remaining("brand_new_name", "3.3.3.3"), 0)

        # 无关组合不受影响
        self.assertEqual(g2.locked_remaining("brand_new_name", "4.4.4.4"), 0)

    def test_lock_expires_after_window(self) -> None:
        guard = LoginGuard(max_failures=2, lock_seconds=1)
        guard.record_failure("u", "1.1.1.1")
        guard.record_failure("u", "1.1.1.1")
        self.assertGreater(guard.locked_remaining("u", "1.1.1.1"), 0)
        time.sleep(1.1)
        self.assertEqual(guard.locked_remaining("u", "1.1.1.1"), 0)

    def test_success_resets_failure_counter(self) -> None:
        self._register("reset_me", "pass1234")
        for _ in range(4):
            self.assertEqual(self._login("reset_me", "bad").status_code, 401)
        self.assertEqual(self._login("reset_me", "pass1234").status_code, 200)
        # 清零后再错 4 次仍不应锁定
        for _ in range(4):
            self.assertEqual(self._login("reset_me", "bad").status_code, 401)
        self.assertEqual(self._login("reset_me", "pass1234").status_code, 200)

    def test_ip_lockout_blocks_rotating_usernames(self) -> None:
        """同一 IP 换着用户名撞库，第 6 次也必须被挡。"""
        self._register("real_user", "pass1234")
        for i in range(5):
            self.assertEqual(self._login(f"random_name_{i}", "whatever").status_code, 401)
        blocked = self._login("real_user", "pass1234")
        self.assertEqual(blocked.status_code, 429)

    def test_register_requires_valid_captcha(self) -> None:
        resp = self._register("bad_code_user", "pass1234")
        self.assertEqual(resp.status_code, 200)

        key, _ = self._issue_captcha()
        dup = self.client.post(
            "/api/auth/register",
            json={
                "username": "bad_code_user",
                "password": "pass1234",
                "captcha_key": key,
                "captcha_code": "zzzz",
            },
        )
        self.assertEqual(dup.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)