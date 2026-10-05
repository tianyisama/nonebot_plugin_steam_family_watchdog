import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import nonebot
import nonebot_plugin_steam_family_watchdog as plugin
from nonebot.adapters.onebot.v11 import Adapter, Bot, PrivateMessageEvent

from steam_family_watchdog_core.authentication import save_auth
from nonebot_plugin_steam_family_watchdog.config import Config
from nonebot_plugin_steam_family_watchdog.service import Service
from nonebot_plugin_steam_family_watchdog.web import WebManager
from .helpers import FakeApi, FakeAuth, FakeBot, game, token


class NoneBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_permissions_use_real_onebot_events(self):
        driver = nonebot.get_driver()
        adapter = Adapter(driver)
        bot = Bot(adapter, "12345")
        def event(user_id):
            return PrivateMessageEvent.model_validate({"time": 0, "self_id": 12345, "post_type": "message", "message_type": "private",
                "sub_type": "friend", "user_id": user_id, "message_id": 1, "message": "/steam启动", "raw_message": "/steam启动",
                "font": 0, "sender": {"user_id": user_id, "nickname": "test", "sex": "unknown", "age": 0}})
        for matcher in (plugin.activate, plugin.deactivate, plugin.check_config, plugin.show_status):
            self.assertTrue(await matcher.permission(bot, event(999)))
            self.assertFalse(await matcher.permission(bot, event(888)))

    async def test_real_lifecycle_and_apscheduler_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            save_auth(root / "auth.json", token())
            bot, api = FakeBot(), FakeApi()
            config = Config(steam_family_data_dir=root, steam_family_push_groups=["100"],
                steam_family_web_enabled=False, steam_family_jitter_seconds=0)
            service = Service(config, lambda: {bot.self_id: bot}, lambda: {"999"}, lambda _: None,
                              auth_factory=FakeAuth, api_factory=api.factory)
            web = WebManager(service, lambda _: None)
            with patch.object(plugin, "service", service), patch.object(plugin, "web_manager", web):
                async with nonebot.get_driver()._lifespan:
                    self.assertTrue(service.started)
                    self.assertIsNone(service.monitor)
                    self.assertIsNotNone(plugin.scheduler.get_job(plugin.JOB_ID))
                    self.assertTrue((await service.enable(bot, from_command=True))["ok"])
                    plugin.scheduler.start()
                    async with asyncio.timeout(3):
                        while not service.store.ready():
                            await asyncio.sleep(0.01)
                    api.apps.append(game(20))
                    service.next_scan = 0
                    plugin.scheduler.modify_job(plugin.JOB_ID, next_run_time=datetime.now(timezone.utc))
                    async with asyncio.timeout(3):
                        while not bot.sent:
                            await asyncio.sleep(0.01)
                    self.assertEqual(bot.sent[0][:2], ("group", "100"))
                await asyncio.sleep(0)
                self.assertFalse(service.started)
                self.assertFalse((root / "monitor.lock").exists())
                self.assertFalse(plugin.background)

    async def test_cancelled_aps_job_keeps_scan_until_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            save_auth(root / "auth.json", token())
            bot, api = FakeBot(), FakeApi()
            service = Service(Config(steam_family_data_dir=root, steam_family_push_groups=["100"], steam_family_web_enabled=False),
                lambda: {bot.self_id: bot}, lambda: {"999"}, lambda _: None, auth_factory=FakeAuth, api_factory=api.factory)
            await service.startup()
            await service.enable(bot, from_command=True)
            api.entered, api.resume = asyncio.Event(), asyncio.Event()
            with patch.object(plugin, "service", service):
                job = asyncio.create_task(plugin.scheduled_pulse())
                await asyncio.wait_for(api.entered.wait(), 2)
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                self.assertTrue(plugin.background)
                closing = asyncio.create_task(service.shutdown())
                await asyncio.sleep(0.01)
                self.assertFalse(closing.done())
                api.resume.set()
                await closing
                await asyncio.gather(*list(plugin.background))
                self.assertFalse((root / "monitor.lock").exists())

    async def test_dotenv_configuration_is_parsed_by_nonebot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            file = root / ".env"
            file.write_text('SUPERUSERS=["999"]\nSTEAM_FAMILY_PUSH_GROUPS=["100","200"]\nSTEAM_FAMILY_PUSH_USERS=[300]\nSTEAM_FAMILY_POLL_SECONDS=60\nSTEAM_FAMILY_WEB_ENABLED=false\n', encoding="utf-8")
            script = '''
import sys
import nonebot
nonebot.init(driver="~none", _env_file=sys.argv[1])
plugin = nonebot.load_plugin("nonebot_plugin_steam_family_watchdog")
assert plugin is not None
c = plugin.module.plugin_config
assert c.steam_family_push_groups == ["100", "200"]
assert c.steam_family_push_users == ["300"]
assert c.steam_family_poll_seconds == 60
assert not c.steam_family_web_enabled
print("dotenv verified")
'''
            result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(file)],
                text=True, encoding="utf-8", capture_output=True, timeout=10, env={**os.environ, "PYTHONUTF8": "1"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("dotenv verified", result.stdout)

    async def test_sqlite_status_failure_pauses_instead_of_polling_every_tick(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            save_auth(root / "auth.json", token())
            bot, api = FakeBot(), FakeApi()
            service = Service(Config(steam_family_data_dir=root, steam_family_push_groups=["100"], steam_family_web_enabled=False),
                lambda: {bot.self_id: bot}, lambda: {"999"}, lambda _: None, auth_factory=FakeAuth, api_factory=api.factory)
            await service.startup()
            await service.enable(bot, from_command=True)
            service.store.db.execute("CREATE TRIGGER deny_status BEFORE INSERT ON meta WHEN NEW.key='last_error' BEGIN SELECT RAISE(ABORT,'test'); END")
            try:
                with patch.object(plugin, "service", service):
                    await plugin.safe_pulse()
                    self.assertIsNotNone(service.startup_error)
                    self.assertEqual(api.calls, 1)
                    await plugin.safe_pulse()
                    self.assertEqual(api.calls, 1)
            finally:
                await service.shutdown()
