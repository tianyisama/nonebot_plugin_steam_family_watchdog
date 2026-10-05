import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from pydantic import ValidationError

from steam_family_watchdog_core.authentication import save_auth
from steam_family_watchdog_core.config import acquire_lock
from steam_family_watchdog_core.errors import SteamError

from nonebot_plugin_steam_family_watchdog.config import Config
from nonebot_plugin_steam_family_watchdog.service import Service
from .helpers import A, T, FakeApi, FakeAuth, FakeBot, game, token


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        save_auth(self.root / "auth.json", token())
        self.bot = FakeBot()
        self.bots = {self.bot.self_id: self.bot}
        self.superusers = {"999"}
        self.logs = []
        self.api = FakeApi()
        self.config = Config(steam_family_data_dir=self.root, steam_family_push_groups=["100", "200"],
            steam_family_push_users=["300"], steam_family_jitter_seconds=0, steam_family_send_images=False,
            steam_family_web_enabled=False)
        self.service = self.make_service()
        await self.service.startup()

    def make_service(self):
        return Service(self.config, lambda: self.bots, lambda: self.superusers, self.logs.append,
                       auth_factory=FakeAuth, api_factory=self.api.factory)

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temp.cleanup()

    async def enable(self):
        result = await self.service.enable(self.bot, from_command=True)
        self.assertTrue(result["ok"], result)
        await self.service.pulse()

    async def test_first_command_gate_and_silent_baseline(self):
        self.assertFalse(self.service.state.activated)
        self.assertIsNone(self.service.monitor)
        self.assertFalse((await self.service.enable())["ok"])
        await self.service.pulse()
        self.assertEqual(self.api.calls, 0)
        await self.enable()
        self.assertTrue(self.service.state.enabled)
        self.assertEqual(self.api.calls, 1)
        self.assertTrue(self.service.store.ready())
        self.assertEqual(self.bot.sent, [])
        state = json.loads(self.service.state_file.read_text())
        self.assertTrue(state["activated"])
        self.assertTrue(state["enabled"])
        self.assertIsNone(self.service.monitor.task)  # APScheduler owns scheduling.

    async def test_all_targets_receive_then_ack(self):
        await self.enable()
        self.api.apps.append(game(20))
        self.service.next_scan = 0
        await self.service.pulse()
        self.assertEqual([(kind, id_) for kind, id_, _ in self.bot.sent], [("group", "100"), ("group", "200"), ("private", "300")])
        self.assertTrue(all("测试游戏20" in text for _, _, text in self.bot.sent))
        for kind, id_ in self.service.targets():
            self.assertEqual(self.service.store.changes(self.service.client_id(kind, id_))["count"], 0)
        await self.service.pulse()
        self.assertEqual(len(self.bot.sent), 3)
        self.assertEqual(self.api.calls, 2)

    async def test_failed_target_does_not_consume_or_block_others(self):
        await self.enable()
        self.bot.fail_groups.add("100")
        self.api.apps.append(game(20))
        self.service.next_scan = 0
        await self.service.pulse()
        clientid = self.service.client_id("group", "100")
        pending = self.service.store.changes(clientid)
        self.assertEqual(pending["count"], 1)
        self.assertEqual([(kind, id_) for kind, id_, _ in self.bot.sent], [("group", "200"), ("private", "300")])
        await self.service.pulse()
        self.assertEqual(self.service.store.changes(clientid)["delivery_id"], pending["delivery_id"])
        self.bot.fail_groups.clear()
        self.service.target_retries[clientid] = 0
        await self.service.pulse()
        self.assertEqual(self.bot.sent[-1][:2], ("group", "100"))
        self.assertEqual(self.service.store.changes(clientid)["count"], 0)

    async def test_restart_restores_enabled_and_pending_delivery(self):
        await self.enable()
        self.bot.fail_groups.add("100")
        self.api.apps.append(game(20))
        self.service.next_scan = 0
        await self.service.pulse()
        pending = self.service.store.changes("nonebot:group:100")
        await self.service.shutdown()
        self.service = self.make_service()
        await self.service.startup()
        self.assertTrue(self.service.state.enabled)
        self.assertIsNotNone(self.service.monitor)
        self.assertEqual(self.service.store.changes("nonebot:group:100")["delivery_id"], pending["delivery_id"])
        self.bot.fail_groups.clear()
        self.bot.sent.clear()
        self.service.next_scan = time.time() + 300
        await self.service.pulse()
        self.assertEqual([value[:2] for value in self.bot.sent], [("group", "100")])

    async def test_disable_persists_and_releases_login_lock(self):
        await self.enable()
        await self.service.disable()
        self.assertIsNone(self.service.monitor)
        self.assertFalse((self.root / "monitor.lock").exists())
        release = acquire_lock(self.root)
        release()
        await self.service.shutdown()
        self.service = self.make_service()
        await self.service.startup()
        self.assertTrue(self.service.state.activated)
        self.assertFalse(self.service.state.enabled)
        self.assertIsNone(self.service.monitor)
        self.assertTrue((await self.service.enable())["ok"])

    async def test_long_credential_failure_notifies_once_across_restart(self):
        await self.enable()
        self.api.failure = SteamError("AUTH_REQUIRED", "refresh rejected")
        self.service.next_scan = 0
        await self.service.pulse()
        self.assertEqual([value[:2] for value in self.bot.sent], [("private", "999")])
        self.assertFalse(self.service.status()["auth_notice_pending"])
        self.service.next_scan = 0
        await self.service.pulse()
        self.assertEqual(len(self.bot.sent), 1)
        await self.service.shutdown()
        self.service = self.make_service()
        await self.service.startup()
        await self.service.pulse()
        self.assertEqual(len(self.bot.sent), 1)
        self.assertNotIn("test-signature", "\n".join(self.logs))

    async def test_alert_deferred_until_bot_connects_and_retried_on_failure(self):
        await self.enable()
        self.bots.clear()
        self.api.failure = SteamError("AUTH_REQUIRED", "expired")
        self.service.next_scan = 0
        await self.service.pulse()
        self.assertEqual(self.bot.sent, [])
        self.assertTrue(self.service.status()["auth_notice_pending"])
        self.bots[self.bot.self_id] = self.bot
        self.bot.fail_users.add("999")
        await self.service.pulse(self.bot)
        self.assertTrue(self.service.status()["auth_notice_pending"])
        self.bot.fail_users.clear()
        self.service.target_retries["superuser:999"] = 0
        await self.service.pulse(self.bot)
        self.assertFalse(self.service.status()["auth_notice_pending"])

    async def test_recovery_clears_alert_and_new_revocation_can_notify(self):
        await self.enable()
        self.api.failure = SteamError("AUTH_REQUIRED", "expired")
        self.service.next_scan = 0
        await self.service.pulse()
        self.api.failure = None
        self.service.next_scan = 0
        await self.service.pulse()
        self.assertIsNone(self.service.state.auth_notice)
        self.api.failure = SteamError("AUTH_REQUIRED", "expired again")
        self.service.next_scan = 0
        await self.service.pulse()
        self.assertEqual(len(self.bot.sent), 2)

    async def test_failed_configuration_or_expired_token_prevents_activation(self):
        await self.service.update_config({"steam_family_push_groups": [], "steam_family_push_users": []})
        result = await self.service.enable(self.bot, from_command=True)
        self.assertFalse(result["ok"])
        self.assertFalse(self.service.state.activated)
        await self.service.update_config({"steam_family_push_groups": ["100"]})
        self.superusers.clear()
        self.assertFalse((await self.service.enable(self.bot, from_command=True))["ok"])
        self.superusers.add("999")
        (self.root / "auth.json").write_text(json.dumps({"platform": "MobileApp", "refresh_token": token(expiry=time.time() - 1)}))
        result = await self.service.enable(self.bot, from_command=True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["auth_valid"])
        self.assertEqual(self.bot.sent[0][:2], ("private", "999"))

    async def test_config_changes_persist_and_preserve_limit_pause(self):
        await self.enable()
        self.api.failure = SteamError("RATE_LIMIT", "limited", 1800)
        self.service.next_scan = 0
        await self.service.pulse()
        pause = self.service.next_scan
        await self.service.update_config({"steam_family_poll_seconds": 60})
        self.assertEqual(self.service.next_scan, pause)
        await self.service.pulse()
        self.assertEqual(self.api.calls, 2)
        await self.service.shutdown()
        self.service = self.make_service()
        await self.service.startup()
        self.assertEqual(self.service.config.steam_family_poll_seconds, 60)
        self.assertGreater(self.service.next_scan - time.time(), 1700)

    async def test_invalid_update_is_atomic(self):
        before = self.service.config
        for patch in ({"steam_family_poll_seconds": 1}, {"steam_family_push_groups": "100"}, {"steam_family_web_token": "x"}):
            with self.assertRaises((ValueError, ValidationError)):
                await self.service.update_config(patch)
        self.assertEqual(self.service.config, before)
        self.assertFalse(self.service.config_file.exists())

    async def test_new_targets_start_latest_or_replay_as_configured(self):
        await self.enable()
        self.api.apps.append(game(20))
        self.service.next_scan = 0
        await self.service.pulse()
        await self.service.update_config({"steam_family_push_groups": ["100", "200", "400"]})
        self.assertEqual(self.service.store.changes("nonebot:group:400")["count"], 0)
        await self.service.update_config({"steam_family_replay_history": True, "steam_family_push_groups": ["100", "200", "400", "500"]})
        self.assertEqual(self.service.store.changes("nonebot:group:500")["count"], 1)

    async def test_shutdown_or_disable_waits_for_inflight_scan(self):
        result = await self.service.enable(self.bot, from_command=True)
        self.assertTrue(result["ok"])
        self.api.entered, self.api.resume = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.service.pulse())
        await asyncio.wait_for(self.api.entered.wait(), 2)
        stopped = asyncio.create_task(self.service.disable())
        await asyncio.sleep(0.01)
        self.assertFalse(stopped.done())
        self.api.resume.set()
        await asyncio.gather(task, stopped)
        self.assertTrue(self.api.closed)
        self.assertFalse((self.root / "monitor.lock").exists())

    async def test_duplicate_instance_cannot_modify_state(self):
        other = self.make_service()
        await other.startup()
        try:
            self.assertFalse(other.started)
            with self.assertRaises(ValueError):
                await other.disable()
            with self.assertRaises(ValueError):
                await other.update_config({"steam_family_poll_seconds": 60})
        finally:
            await other.shutdown()

    async def test_bot_id_selection_and_wrong_bot_check(self):
        await self.service.update_config({"steam_family_bot_id": "54321"})
        self.assertFalse((await self.service.enable(self.bot, from_command=True))["ok"])
        self.assertIsNone(self.service.select_bot())

    async def test_invalid_saved_config_can_be_fixed_without_resetting_activation(self):
        await self.enable()
        await self.service.shutdown()
        self.service.config_file.write_text('{"steam_family_poll_seconds":1}')
        self.service = self.make_service()
        await self.service.startup()
        self.assertIsNotNone(self.service.startup_error)
        await self.service.update_config({"steam_family_poll_seconds": 60})
        result = await self.service.enable()
        self.assertTrue(result["ok"], result)
        self.assertIsNone(self.service.startup_error)
        self.assertTrue(self.service.state.activated)

    async def test_expired_refresh_is_detected_during_periodic_scan(self):
        await self.enable()
        (self.root / "auth.json").write_text(json.dumps({"platform": "MobileApp", "refresh_token": token(expiry=time.time() - 1)}))
        self.service.next_scan = 0
        await self.service.pulse()
        self.assertEqual(self.service.monitor.status["last_error"]["code"], "AUTH_REQUIRED")
        self.assertEqual(self.service.auth.state, "needs_login")
        self.assertEqual(self.bot.sent[0][:2], ("private", "999"))
