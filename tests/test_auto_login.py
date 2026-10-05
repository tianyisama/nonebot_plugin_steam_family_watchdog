import asyncio
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from pydantic import ValidationError

from steam_family_watchdog_core.authentication import save_auth
from steam_family_watchdog_core.login_web import LoginWeb
from nonebot_plugin_steam_family_watchdog.access import management_token
from nonebot_plugin_steam_family_watchdog.config import Config
from nonebot_plugin_steam_family_watchdog.login import LoginManager
from nonebot_plugin_steam_family_watchdog.service import Service
from .helpers import FakeAuth, FakeApi, FakeBot, token
from .core.test_login_web import FakeSession
from steam_family_watchdog_core.config import Config as CoreConfig


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class AutomaticLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bot, self.api = FakeBot(), FakeApi()
        self.bots = {}
        self.logs = []
        self.port = free_port()
        self.config = Config(steam_family_data_dir=self.root, steam_family_push_groups=["100"],
            steam_family_web_enabled=False, steam_family_login_port=self.port, steam_family_auto_public_ip=False,
            steam_family_web_token="test-management-token-" + "x" * 40)
        def login_manager(service):
            def controller(config, port, **kwargs):
                return LoginWeb(config, port, session_factory=FakeSession, **kwargs)
            manager = LoginManager(service, controller)
            manager.success_delay = 0
            return manager
        self.service = Service(self.config, lambda: self.bots, lambda: {"999"}, self.logs.append,
            auth_factory=FakeAuth, api_factory=self.api.factory, login_manager_factory=login_manager)

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    async def test_first_boot_missing_credentials_opens_page_and_logs_local_address(self):
        await self.service.startup()
        self.assertTrue(self.service.login_manager.active)
        self.assertFalse(self.service.state.activated)
        self.assertIsNone(self.service.monitor)
        self.assertIn(f"http://127.0.0.1:{self.port}/", "\n".join(self.logs))
        self.assertNotIn(self.config.steam_family_web_token, "\n".join(self.logs))
        async with ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{self.port}/") as response:
                page = await response.text()
                self.assertEqual(response.status, 200)
                self.assertIn("管理密钥", page)
                self.assertNotIn('id="password"', page)
                self.assertNotIn(self.config.steam_family_web_token, page)
            async with client.get(f"http://127.0.0.1:{self.port}/page") as response:
                self.assertEqual(response.status, 403)

    async def test_deferred_notification_when_bot_connects(self):
        await self.service.startup()
        self.assertFalse(self.bot.sent)
        self.bots[self.bot.self_id] = self.bot
        await self.service.pulse(self.bot)
        self.assertEqual(self.bot.sent[0][:2], ("private", "999"))
        self.assertIn(f"127.0.0.1:{self.port}", self.bot.sent[0][2])
        self.assertNotIn(self.config.steam_family_web_token, self.bot.sent[0][2])
        await self.service.pulse(self.bot)
        self.assertEqual(len(self.bot.sent), 1)

    async def login_success(self):
        controller = self.service.login_manager.controller
        headers = {"Authorization": f"Bearer {self.config.steam_family_web_token}", "X-Login-CSRF": controller.csrf}
        async with ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{self.port}/page", headers=headers) as response:
                self.assertEqual(response.status, 200)
                self.assertIn('id="password"', await response.text())
            async with client.post(f"http://127.0.0.1:{self.port}/login", headers=headers,
                json={"accountName": "account", "password": "secret-password", "code": "ABCDE"}) as response:
                self.assertEqual(response.status, 200)
        async with asyncio.timeout(3):
            while self.service.login_manager.active or self.service.credential_state != "authenticated":
                await asyncio.sleep(0.01)

    async def test_first_activation_waits_for_login_then_resumes_automatically(self):
        self.bots[self.bot.self_id] = self.bot
        await self.service.startup()
        result = await self.service.enable(self.bot, from_command=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["login_pending"])
        self.assertTrue(self.service.state.pending_activation)
        await self.login_success()
        self.assertTrue(self.service.state.activated)
        self.assertTrue(self.service.state.enabled)
        self.assertFalse(self.service.state.pending_activation)
        self.assertIsNotNone(self.service.monitor)
        await self.service.pulse()
        self.assertTrue(self.service.store.ready())
        self.assertNotIn("secret-password", (self.root / "auth.json").read_text())

    async def test_success_without_activation_does_not_silently_enable_monitor(self):
        await self.service.startup()
        await self.login_success()
        self.assertFalse(self.service.state.activated)
        self.assertFalse(self.service.state.enabled)
        self.assertIsNone(self.service.monitor)

    async def test_success_without_bot_resumes_pending_activation_after_connection(self):
        self.bots[self.bot.self_id] = self.bot
        await self.service.startup()
        await self.service.enable(self.bot, from_command=True)
        self.bots.clear()
        await self.login_success()
        self.assertTrue(self.service.state.pending_activation)
        self.bots[self.bot.self_id] = self.bot
        await self.service.pulse(self.bot)
        self.assertTrue(self.service.state.enabled)

    async def test_expired_login_page_reopens_and_duplicate_commands_reuse_server(self):
        await self.service.startup()
        controller = self.service.login_manager.controller
        await self.service.request_login(self.bot)
        self.assertIs(self.service.login_manager.controller, controller)
        controller.stop_event.set()
        async with asyncio.timeout(2):
            while self.service.login_manager.active:
                await asyncio.sleep(0.01)
        self.assertFalse((self.root / "monitor.lock").exists())
        await self.service.request_login(self.bot)
        self.assertTrue(self.service.login_manager.active)

    async def test_stop_closes_login_page_and_cancels_pending_activation(self):
        self.bots[self.bot.self_id] = self.bot
        await self.service.startup()
        await self.service.enable(self.bot, from_command=True)
        await self.service.disable()
        self.assertFalse(self.service.login_manager.active)
        self.assertFalse(self.service.state.pending_activation)
        self.assertFalse((self.root / "monitor.lock").exists())

    async def test_valid_but_revoked_token_is_checked_online_at_startup(self):
        save_auth(self.root / "auth.json", token())
        from steam_family_watchdog_core.errors import SteamError
        class RevokedAuth(FakeAuth):
            async def access(self, force=False):
                self.load()
                raise SteamError("AUTH_REQUIRED", "revoked")
        self.service.auth_factory = RevokedAuth
        await self.service.startup()
        self.assertTrue(self.service.login_manager.active)

    async def test_network_failure_does_not_discard_credentials_or_open_login(self):
        save_auth(self.root / "auth.json", token())
        from steam_family_watchdog_core.errors import SteamError
        class OfflineAuth(FakeAuth):
            async def access(self, force=False):
                raise SteamError("RATE_LIMIT", "limited", 1800)
        self.service.auth_factory = OfflineAuth
        await self.service.startup()
        self.assertFalse(self.service.login_manager.active)
        self.assertGreater(self.service.state.credential_retry_at - time.time(), 1700)


class LoginTransportTests(unittest.IsolatedAsyncioTestCase):
    def controller(self):
        return LoginWeb(CoreConfig(), session_factory=FakeSession,
            access_token="x" * 40, allowed_origins={"http://127.0.0.1:11453", "https://login.example.com"},
            trusted_proxies=["127.0.0.1"])

    async def test_external_plain_http_and_spoofed_forward_headers_are_denied(self):
        controller = self.controller()
        transport = Mock()
        transport.get_extra_info.side_effect = lambda key, default=None: ("8.8.8.8", 1234) if key == "peername" else None
        for headers in ({"Host": "127.0.0.1:11453"}, {"Host": "login.example.com", "X-Forwarded-Proto": "https"}):
            request = make_mocked_request("GET", "/", headers=headers, transport=transport)
            self.assertEqual((await controller.handle(request)).status, 403)

    async def test_only_literal_127_host_is_accepted_over_local_http(self):
        controller = self.controller()
        async with TestClient(TestServer(controller.create_app())) as client:
            self.assertEqual((await client.get("/", headers={"Host": "localhost:11453"})).status, 403)
            self.assertEqual((await client.get("/", headers={"Host": "login.example.com"})).status, 403)
            self.assertEqual((await client.get("/", headers={"Host": "127.0.0.1:11453"})).status, 200)
            self.assertEqual((await client.get("/", headers={"Host": "127.0.0.1:11453", "X-Forwarded-Proto": "http", "X-Forwarded-For": "8.8.8.8"})).status, 403)
        controller.trusted_proxies = []
        request = make_mocked_request("GET", "/", headers={"Host": "127.0.0.1:11453", "X-Forwarded-Proto": "https"},
            transport=type("Transport", (), {"get_extra_info": lambda self, key, default=None: ("127.0.0.1", 1234) if key == "peername" else None})())
        self.assertEqual((await controller.handle(request)).status, 403)

    async def test_https_from_trusted_proxy_still_requires_key_and_csrf(self):
        controller = self.controller()
        headers = {"Host": "login.example.com", "X-Forwarded-Proto": "https", "Origin": "https://login.example.com"}
        async with TestClient(TestServer(controller.create_app())) as client:
            self.assertEqual((await client.get("/", headers=headers)).status, 200)
            self.assertEqual((await client.get("/page", headers=headers)).status, 403)
            headers["Authorization"] = "Bearer " + "x" * 40
            response = await client.get("/page", headers=headers)
            self.assertEqual(response.status, 200)
            self.assertIn('id="password"', await response.text())
            self.assertEqual((await client.get("/state", headers=headers)).status, 403)
            headers["X-Login-CSRF"] = controller.csrf
            self.assertEqual((await client.get("/state", headers=headers)).status, 200)
            response = await client.post("/login", headers=headers, json={"accountName": "account", "password": "fake-password"})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["stage"], "awaiting_code")
            http_headers = {**headers, "X-Forwarded-Proto": "http"}
            self.assertEqual((await client.post("/login", headers=http_headers, json={"accountName": "account", "password": "fake-password"})).status, 403)

    async def test_direct_tls_is_detected_without_trusting_headers(self):
        controller = self.controller()
        transport = Mock()
        transport.get_extra_info.side_effect = lambda key, default=None: ("8.8.8.8", 1234) if key == "peername" else object() if key == "sslcontext" else None
        request = make_mocked_request("GET", "/", headers={"Host": "login.example.com"}, transport=transport, sslcontext=object())
        self.assertTrue(request.secure)
        self.assertEqual((await controller.handle(request)).status, 200)

    def test_configuration_allows_http_management_but_requires_https_login(self):
        self.assertEqual(Config(steam_family_web_public_url="http://8.8.8.8:11454").steam_family_web_public_url, "http://8.8.8.8:11454/")
        with self.assertRaises(ValidationError):
            Config(steam_family_login_public_url="http://8.8.8.8:11453")
