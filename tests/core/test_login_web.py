import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from steam_family_watchdog_core.config import Config
from steam_family_watchdog_core.errors import SteamError
from steam_family_watchdog_core.login_web import LoginWeb
from .helpers import jwt


class FakeSession:
    confirmations = [3]
    interval = 0.01

    def __init__(self):
        self.ready = False
        self.refresh_token = jwt()
        self.closed = False

    async def start_credentials(self, account, password, code=""):
        if password == "wrong":
            raise SteamError("AUTH_REQUIRED", "safe", eresult=5)
        self.ready = bool(code)

    async def submit_code(self, code):
        if code != "ABCDE":
            raise SteamError("AUTH_REQUIRED", "safe", eresult=65)
        self.ready = True

    async def poll(self):
        return self.ready

    async def close(self):
        self.closed = True


class LoginWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.web = LoginWeb(Config(data_dir=Path(self.temp.name)), session_factory=FakeSession, log=lambda _: None)
        self.client = TestClient(TestServer(self.web.create_app()))
        await self.client.start_server()
        self.headers = {"Host": "127.0.0.1:11453", "X-Login-CSRF": self.web.csrf}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_host_origin_and_csrf_protection(self):
        self.assertEqual((await self.client.get("/", headers={"Host": "evil.invalid"})).status, 403)
        result = await self.client.get("/", headers=self.headers)
        page = await result.text()
        self.assertEqual(result.status, 200)
        self.assertIn(self.web.csrf, page)
        self.assertNotIn("__CSRF_JSON__", page)
        self.assertEqual((await self.client.get("/state", headers={"Host": "127.0.0.1:11453"})).status, 403)
        self.assertEqual((await self.client.get("/state", headers={**self.headers, "Origin": "http://evil.invalid"})).status, 403)
        self.assertEqual((await self.client.get("/state", headers=self.headers)).status, 200)

    async def test_password_code_login_saves_only_refresh_credentials(self):
        result = await self.client.post("/login", headers=self.headers, json={"accountName": "account", "password": "secret-password"})
        self.assertEqual((await result.json())["stage"], "awaiting_code")
        result = await self.client.post("/code", headers=self.headers, json={"code": "WRONG"})
        self.assertEqual(result.status, 400)
        self.assertEqual((await result.json())["stage"], "awaiting_code")
        result = await self.client.post("/code", headers=self.headers, json={"code": "ABCDE"})
        self.assertEqual(result.status, 200)
        async with asyncio.timeout(2):
            while self.web.state["stage"] != "authenticated":
                await asyncio.sleep(0.01)
        contents = (Path(self.temp.name) / "auth.json").read_text()
        self.assertEqual(set(json.loads(contents)), {"platform", "steamid", "refresh_token", "saved_at"})
        self.assertNotIn("secret-password", contents)
        self.assertNotIn("ABCDE", contents)
        self.assertNotIn("secret-password", json.dumps(self.web.state))
        self.assertEqual((await self.client.post("/login", headers=self.headers, json={})).status, 409)

    async def test_validation_throttling_and_cleanup(self):
        result = await self.client.post("/login", headers=self.headers, json={"accountName": "", "password": "p"})
        self.assertEqual(result.status, 400)
        result = await self.client.post("/login", headers=self.headers, json={"accountName": "a", "password": "p"})
        self.assertEqual(result.status, 200)
        current = self.web.session
        self.assertEqual((await self.client.post("/login", headers=self.headers, json={"accountName": "a", "password": "p"})).status, 429)
        for _ in range(3):
            self.assertEqual((await self.client.post("/code", headers=self.headers, json={"code": "WRONG"})).status, 400)
        self.assertEqual((await self.client.post("/code", headers=self.headers, json={"code": "WRONG"})).status, 429)
        await self.client.close()
        self.assertTrue(current.closed)
        self.assertIsNone(self.web.poll_task)

    async def test_bad_password_and_oversized_body(self):
        result = await self.client.post("/login", headers=self.headers, json={"accountName": "a", "password": "wrong"})
        self.assertEqual(result.status, 400)
        self.assertIn("账号或密码不正确", (await result.json())["message"])
        self.web.login_cooldown = 0
        result = await self.client.post("/login", headers=self.headers, data='{"password":"' + "a" * 8192 + '"}')
        self.assertEqual(result.status, 413)
