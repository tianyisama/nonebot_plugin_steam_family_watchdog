import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from steam_family_watchdog_core.authentication import save_auth

from nonebot_plugin_steam_family_watchdog.config import Config
from nonebot_plugin_steam_family_watchdog.service import Service
from nonebot_plugin_steam_family_watchdog.web import WebManager
from .helpers import FakeApi, FakeAuth, FakeBot, token


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        save_auth(self.root / "auth.json", token())
        self.bot, self.api = FakeBot(), FakeApi()
        config = Config(steam_family_data_dir=self.root, steam_family_push_groups=["100"], steam_family_web_token="x" * 40)
        self.service = Service(config, lambda: {self.bot.self_id: self.bot}, lambda: {"999"}, lambda _: None,
            auth_factory=FakeAuth, api_factory=self.api.factory)
        await self.service.startup()
        self.web = WebManager(self.service, lambda _: None)
        self.web.load_token()
        self.headers = {"Authorization": "Bearer " + self.web.token}
        self.client = TestClient(TestServer(self.web.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.shutdown()
        self.temp.cleanup()

    async def test_public_page_private_api_and_secret_redaction(self):
        response = await self.client.get("/")
        page = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("Steam 家庭库管理", page)
        self.assertNotIn(self.web.token, page)
        for route in ("/api/config", "/api/status"):
            self.assertEqual((await self.client.get(route)).status, 401)
            response = await self.client.get(route, headers=self.headers)
            self.assertEqual(response.status, 200)
            self.assertNotIn(self.web.token, await response.text())

    async def test_edit_config_live_persisted_and_no_rate_scan(self):
        response = await self.client.patch("/api/config", headers=self.headers, json={"steam_family_poll_seconds": 60, "steam_family_push_users": ["300", "300", 400]})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["config"]["steam_family_push_users"], ["300", "400"])
        self.assertEqual(self.service.config.steam_family_poll_seconds, 60)
        self.assertEqual(json.loads(self.service.config_file.read_text())["steam_family_poll_seconds"], 60)
        self.assertEqual(self.api.calls, 0)
        response = await self.client.post("/api/check", headers=self.headers, json={})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["ok"])
        self.assertEqual(self.api.calls, 0)

    async def test_invalid_changes_rejected_without_overwrite(self):
        for patch, code in (({"steam_family_poll_seconds": 1}, 422), ({"steam_family_push_groups": "100"}, 422),
                            ({"steam_family_web_token": "secret"}, 400), ({"steam_family_data_dir": "other"}, 400)):
            response = await self.client.patch("/api/config", headers=self.headers, json=patch)
            self.assertEqual(response.status, code)
        self.assertEqual(self.service.config.steam_family_poll_seconds, 300)
        self.assertFalse(self.service.config_file.exists())

    async def test_web_cannot_bypass_first_bot_activation_then_can_resume(self):
        response = await self.client.post("/api/resume", headers=self.headers, json={})
        self.assertEqual(response.status, 409)
        self.assertFalse(self.service.state.activated)
        self.assertTrue((await self.service.enable(self.bot, from_command=True))["ok"])
        self.assertEqual((await self.client.post("/api/stop", headers=self.headers, json={})).status, 200)
        self.assertFalse(self.service.state.enabled)
        self.assertEqual((await self.client.post("/api/resume", headers=self.headers, json={})).status, 200)
        self.assertTrue(self.service.state.enabled)

    async def test_cross_origin_and_body_validation(self):
        self.assertEqual((await self.client.post("/api/stop", headers={**self.headers, "Origin": "https://evil.invalid"}, json={})).status, 403)
        self.assertEqual((await self.client.patch("/api/config", headers=self.headers, data="{}")).status, 415)
        headers = {**self.headers, "Content-Type": "application/json"}
        for body, code in (("[]", 400), ("invalid", 400), ('{"x":"' + "a" * 32768 + '"}', 413)):
            self.assertEqual((await self.client.patch("/api/config", headers=headers, data=body)).status, code)
        self.assertEqual((await self.client.get("/not-found", headers=self.headers)).status, 404)

    async def test_generated_management_token_persists(self):
        self.service.config.steam_family_web_token = ""
        first = WebManager(self.service, lambda _: None)
        first.load_token()
        second = WebManager(self.service, lambda _: None)
        second.load_token()
        self.assertEqual(first.token, second.token)
        self.assertEqual(len(first.token), 64)

    async def test_bad_config_check_returned_to_browser(self):
        await self.service.update_config({"steam_family_push_groups": []})
        response = await self.client.post("/api/check", headers=self.headers, json={})
        self.assertEqual(response.status, 409)
        self.assertIn("列表均为空", (await response.json())["errors"][0])
