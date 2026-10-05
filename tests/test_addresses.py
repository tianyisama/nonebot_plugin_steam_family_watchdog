import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nonebot_plugin_steam_family_watchdog as plugin
from nonebot.exception import FinishedException

from steam_family_watchdog_core.authentication import save_auth
from nonebot_plugin_steam_family_watchdog.config import Config
from nonebot_plugin_steam_family_watchdog.service import Service
from nonebot_plugin_steam_family_watchdog.web import WebManager
from .helpers import FakeAuth, FakeApi, FakeBot, token
from .test_auto_login import free_port


class AddressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        save_auth(self.root / "auth.json", token())
        self.bot, self.api, self.logs = FakeBot(), FakeApi(), []
        self.resolver = AsyncMock(return_value="8.8.8.8")
        self.config = Config(steam_family_data_dir=self.root, steam_family_push_groups=["100"], steam_family_web_port=free_port())
        self.service = Service(self.config, lambda: {self.bot.self_id: self.bot}, lambda: {"999"}, self.logs.append,
            auth_factory=FakeAuth, api_factory=self.api.factory, public_ip_provider=self.resolver)
        self.web = WebManager(self.service, self.logs.append)

    async def asyncTearDown(self):
        await self.web.close()
        await self.service.shutdown()
        self.temp.cleanup()

    async def test_auto_public_ip_is_cached_and_only_generates_http_management_url(self):
        await self.service.discover_addresses()
        await self.service.discover_addresses()
        self.resolver.assert_awaited_once()
        self.assertEqual(self.service.urls("web")["public"], f"http://8.8.8.8:{self.config.steam_family_web_port}/")
        self.assertEqual(self.service.urls("login")["public"], "")

    async def test_public_ip_failure_leaves_local_address_available(self):
        self.resolver.side_effect = TimeoutError
        await self.service.discover_addresses()
        self.assertTrue(self.service.urls("web")["local"].startswith("http://127.0.0.1:"))
        self.assertEqual(self.service.urls("web")["public"], "")

    async def test_manual_ip_and_https_urls_override_discovery(self):
        self.service.config.steam_family_public_ip = "1.1.1.1"
        self.service.public_ip = "1.1.1.1"
        self.service.config.steam_family_login_public_url = "https://login.example.com/steam/"
        self.service.config.steam_family_web_public_url = "https://bot.example.com/settings/"
        await self.service.discover_addresses()
        self.resolver.assert_not_awaited()
        self.assertEqual(self.service.urls("login")["public"], "https://login.example.com/steam/")

    async def test_management_start_logs_urls_and_command_returns_them_without_key(self):
        await self.service.discover_addresses()
        await self.service.startup()
        await self.web.start()
        self.assertTrue(self.service.web_running)
        joined = "\n".join(self.logs)
        self.assertIn(self.service.urls("web")["local"], joined)
        self.assertIn(self.service.urls("web")["public"], joined)
        self.assertNotIn(self.web.token, joined)
        finish = AsyncMock(side_effect=FinishedException)
        with patch.object(plugin, "service", self.service), patch.object(plugin.show_config_page, "finish", finish):
            with self.assertRaises(FinishedException):
                await plugin.config_page_handler()
        text = finish.await_args.args[0]
        self.assertIn(self.service.urls("web")["local"], text)
        self.assertIn(self.service.urls("web")["public"], text)
        self.assertNotIn(self.web.token, text)

    async def test_configuration_command_reports_when_page_is_disabled(self):
        finish = AsyncMock(side_effect=FinishedException)
        with patch.object(plugin, "service", self.service), patch.object(plugin.show_config_page, "finish", finish):
            with self.assertRaises(FinishedException):
                await plugin.config_page_handler()
        self.assertIn("未启动", finish.await_args.args[0])
